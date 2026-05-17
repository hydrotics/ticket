import asyncio
import json
import os
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from config import BotConfig, ChannelConfig, TicketPromptConfig
from support_controls import (
    CLOSED_TICKETS_CATEGORY_NAME,
    TRANSCRIPTS_CHANNEL_NAME,
    ClosedTicketControlsView,
    SupportControlsView,
    configure_storage,
    ensure_support_infrastructure,
    hydrate_ticket_controls,
    send_ticket_controls_message,
    set_ticket_metadata,
)

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

# Use empty string as prefix since we're using slash commands
bot = commands.Bot(command_prefix="", intents=intents)

guild_configs: dict = {}
ticket_counter_lock = asyncio.Lock()
TICKET_DB_FILE = Path("ticket_counters.db")
GUILD_CONFIG_DB_FILE = Path("guild_config.db")


def init_guild_config_database() -> None:
    """Initialize SQLite database for guild configurations."""
    with sqlite3.connect(GUILD_CONFIG_DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_configs (
                guild_id INTEGER PRIMARY KEY,
                config_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def create_default_ticket_prompt() -> dict:
    return {
        "title": TicketPromptConfig.DEFAULT_TITLE,
        "description": TicketPromptConfig.DEFAULT_DESCRIPTION,
        "button_text": TicketPromptConfig.DEFAULT_BUTTON_TEXT,
        "types": [t.copy() for t in TicketPromptConfig.DEFAULT_TICKET_TYPES],
        "message_id": None,
        "channel_id": None,
    }


def normalize_guild_config(config: dict) -> dict:
    if not isinstance(config, dict):
        config = {}

    config.setdefault("support_role", None)
    config.setdefault("setup_complete", False)
    config.setdefault("ticket_prompt", create_default_ticket_prompt())
    ticket_prompt = config["ticket_prompt"]
    if not isinstance(ticket_prompt, dict):
        config["ticket_prompt"] = create_default_ticket_prompt()
    else:
        ticket_prompt.setdefault("title", TicketPromptConfig.DEFAULT_TITLE)
        ticket_prompt.setdefault("description", TicketPromptConfig.DEFAULT_DESCRIPTION)
        ticket_prompt.setdefault("button_text", TicketPromptConfig.DEFAULT_BUTTON_TEXT)
        ticket_prompt.setdefault("types", [t.copy() for t in TicketPromptConfig.DEFAULT_TICKET_TYPES])
        ticket_prompt.setdefault("message_id", None)
        ticket_prompt.setdefault("channel_id", None)

        if not isinstance(ticket_prompt["types"], list):
            ticket_prompt["types"] = [t.copy() for t in TicketPromptConfig.DEFAULT_TICKET_TYPES]

    return config


def load_guild_configs() -> None:
    """Load guild configurations from database."""
    global guild_configs
    try:
        with sqlite3.connect(GUILD_CONFIG_DB_FILE) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            cur = conn.execute("SELECT guild_id, config_json FROM guild_configs")
            rows = cur.fetchall()
            guild_configs = {}
            for guild_id, config_json in rows:
                try:
                    config = json.loads(config_json)
                    guild_configs[str(guild_id)] = normalize_guild_config(config)
                except Exception:
                    guild_configs[str(guild_id)] = normalize_guild_config({})
    except Exception as e:
        print(f"Error loading guild configs: {e}")
        guild_configs = {}


def save_guild_configs() -> None:
    """Save guild configurations to database."""
    try:
        with sqlite3.connect(GUILD_CONFIG_DB_FILE) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("DELETE FROM guild_configs")
            for guild_id_str, config in guild_configs.items():
                try:
                    guild_id = int(guild_id_str)
                    config_json = json.dumps(config)
                    conn.execute(
                        "INSERT INTO guild_configs (guild_id, config_json) VALUES (?, ?)",
                        (guild_id, config_json),
                    )
                except Exception:
                    continue
            conn.commit()
    except Exception as e:
        print(f"Error saving guild configs: {e}")


def get_guild_config(guild_id: int) -> dict:
    """Get or create guild configuration."""
    guild_id_str = str(guild_id)
    if guild_id_str not in guild_configs:
        guild_configs[guild_id_str] = normalize_guild_config({})
        save_guild_configs()
    else:
        guild_configs[guild_id_str] = normalize_guild_config(guild_configs[guild_id_str])
    return guild_configs[guild_id_str]


def create_progress_bar(completed: int, total: int) -> str:
    if total <= 0:
        return "[████████████████████] 100%"
    completed = max(0, min(completed, total))
    percentage = (completed / total) * 100
    filled = int((completed / total) * 20)
    bar = "█" * filled + "░" * (20 - filled)
    return f"[{bar}] {int(percentage)}%"


def ordinal_suffix(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def sanitize_channel_segment(text: str) -> str:
    text = text.lower().strip()
    allowed = []
    for ch in text:
        if ch.isalnum() or ch == "-":
            allowed.append(ch)
        elif ch in {" ", "_", "."}:
            allowed.append("-")
    cleaned = "".join(allowed)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


def build_ticket_channel_name(user_name: str, ticket_number: int) -> str:
    base = sanitize_channel_segment(user_name) or "user"
    prefix = "ticket-"
    suffix = f"-{ticket_number}"

    max_base_length = 100 - len(prefix) - len(suffix)
    max_base_length = max(1, max_base_length)
    base = base[:max_base_length].strip("-") or "user"

    return f"{prefix}{base}{suffix}"


def get_total_setup_items() -> int:
    return len(ChannelConfig.TICKET_SYSTEM_STRUCTURE) + sum(
        len(info["channels"]) for info in ChannelConfig.TICKET_SYSTEM_STRUCTURE.values()
    ) + 2


def init_ticket_database() -> None:
    with sqlite3.connect(TICKET_DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_ticket_counters (
                guild_id INTEGER PRIMARY KEY,
                counter INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


async def get_next_ticket_number(guild_id: int) -> int:
    async with ticket_counter_lock:
        with sqlite3.connect(TICKET_DB_FILE) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            cur = conn.execute(
                "SELECT counter FROM guild_ticket_counters WHERE guild_id = ?",
                (guild_id,),
            )
            row = cur.fetchone()
            current = row[0] if row else 0
            next_value = current + 1
            conn.execute(
                """
                INSERT INTO guild_ticket_counters (guild_id, counter)
                VALUES (?, ?)
                ON CONFLICT(guild_id)
                DO UPDATE SET counter = excluded.counter
                """,
                (guild_id, next_value),
            )
            conn.commit()
            return next_value


def build_category_overwrites(guild: discord.Guild, support_role: discord.Role, visible_to_all: bool) -> dict:
    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(view_channel=visible_to_all),
        support_role: discord.PermissionOverwrite(view_channel=True),
    }
    return overwrites


def build_ticket_channel_overwrites(
    guild: discord.Guild,
    support_role: discord.Role,
    creator: discord.Member | discord.User | None = None,
    *,
    creator_visible: bool = True,
    default_visible: bool = False,
) -> dict:
    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=default_visible,
            send_messages=False,
            read_message_history=default_visible,
        ),
        support_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        ),
    }

    if creator is not None:
        overwrites[creator] = discord.PermissionOverwrite(
            view_channel=creator_visible,
            send_messages=creator_visible,
            read_message_history=creator_visible,
        )

    return overwrites


async def find_or_create_category(
    guild: discord.Guild,
    category_name: str,
    overwrites: dict | None = None,
) -> discord.CategoryChannel:
    existing = discord.utils.get(guild.categories, name=category_name)
    if existing:
        return existing
    return await guild.create_category(
        name=category_name,
        overwrites=overwrites,
        reason="Ticket system setup",
    )


async def find_or_create_text_channel(
    guild: discord.Guild,
    channel_name: str,
    category: discord.CategoryChannel | None = None,
    overwrites: dict | None = None,
) -> discord.TextChannel:
    existing = discord.utils.get(guild.text_channels, name=channel_name)
    if existing:
        return existing
    return await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        reason="Ticket system setup",
    )


class RoleSelectView(discord.ui.View):
    def __init__(self, roles: list):
        super().__init__(timeout=600)
        options = [discord.SelectOption(label=role.name, value=str(role.id)) for role in roles]
        select = discord.ui.Select(
            placeholder="Select a support role",
            options=options,
            min_values=1,
            max_values=1
        )
        select.callback = self.role_select_callback
        self.add_item(select)

    async def role_select_callback(self, interaction: discord.Interaction):
        role_id = int(interaction.data["values"][0])
        role = interaction.guild.get_role(role_id)

        if not role:
            await interaction.response.send_message("Role not found.", ephemeral=True)
            return

        config = get_guild_config(interaction.guild.id)
        config["support_role"] = role_id
        save_guild_configs()

        proceed_embed = discord.Embed(
            title="Ticket Setup",
            description="Ready to create your ticket system.",
            color=discord.Color.blurple()
        )
        proceed_embed.add_field(name="Support Role", value=f"Selected: {role.mention}", inline=False)

        proceed_view = SetupProceedView(interaction.guild)
        await interaction.response.edit_message(embed=proceed_embed, view=proceed_view)


class SetupProceedView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=600)
        self.guild = guild

    @discord.ui.button(label="Proceed", style=discord.ButtonStyle.blurple)
    async def proceed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await setup_ticket_system(interaction, self.guild)


class TicketPromptConfigModal(discord.ui.Modal, title="Configure Ticket Prompt"):
    title_input = discord.ui.TextInput(
        label="Prompt Title",
        placeholder="e.g., Create a Support Ticket",
        default="Create a Support Ticket",
        max_length=100
    )

    description_input = discord.ui.TextInput(
        label="Prompt Description",
        placeholder="e.g., Select a ticket type to get started",
        default="Select a ticket type to get started",
        max_length=500,
        style=discord.TextStyle.long
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        config = get_guild_config(interaction.guild.id)
        config["ticket_prompt"]["title"] = self.title_input.value
        config["ticket_prompt"]["description"] = self.description_input.value
        save_guild_configs()
        await show_ticket_prompt_editor(interaction)


class TicketTypeConfigModal(discord.ui.Modal, title="Add Ticket Type"):
    type_name = discord.ui.TextInput(
        label="Ticket Type Name",
        placeholder="e.g., General Support",
        max_length=100
    )

    type_value = discord.ui.TextInput(
        label="Type Value (internal)",
        placeholder="e.g., general",
        max_length=50
    )

    type_category = discord.ui.TextInput(
        label="Category Name (where tickets go)",
        placeholder="e.g., General Tickets",
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(interaction.guild.id)
        type_value = self.type_value.value.lower().strip()

        existing_values = [t["value"].lower() for t in config["ticket_prompt"]["types"]]
        if type_value in existing_values:
            await interaction.followup.send(
                f"A ticket type with value '{type_value}' already exists!",
                ephemeral=True
            )
            return

        new_type = {
            "name": self.type_name.value.strip(),
            "value": type_value,
            "category": self.type_category.value.strip()
        }
        config["ticket_prompt"]["types"].append(new_type)
        save_guild_configs()

        await show_ticket_prompt_editor(interaction)


class TicketCreationModal(discord.ui.Modal):
    def __init__(self, ticket_type: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ticket_type = ticket_type

        self.reason_input = discord.ui.TextInput(
            label="Reason for ticket",
            placeholder="Describe your issue or request",
            max_length=1000,
            style=discord.TextStyle.long
        )
        self.add_item(self.reason_input)

        if ticket_type == "report":
            self.reported_member = discord.ui.TextInput(
                label="Member ID or Username",
                placeholder="e.g., username or ID",
                max_length=100
            )
            self.add_item(self.reported_member)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        ticket_info = {
            "type": self.ticket_type,
            "reason": self.reason_input.value.strip(),
            "creator": interaction.user,
            "guild": interaction.guild
        }

        if self.ticket_type == "report" and hasattr(self, "reported_member"):
            ticket_info["reported_member"] = self.reported_member.value.strip()

        await create_ticket_channel(interaction, ticket_info)


class TicketTypeSelectView(discord.ui.View):
    def __init__(self, ticket_types: list):
        super().__init__(timeout=None)  # Persistent view

        select_options = [
            discord.SelectOption(
                label=t["name"],
                value=t["value"],
                description=f"Category: {t['category']}"
            )
            for t in ticket_types
        ]

        select = discord.ui.Select(
            placeholder="Select a ticket type",
            options=select_options,
            min_values=1,
            max_values=1,
            custom_id="ticket_type_select"  # Add custom_id for persistence
        )
        select.callback = self.ticket_type_callback
        self.add_item(select)

    async def ticket_type_callback(self, interaction: discord.Interaction):
        ticket_type = interaction.data["values"][0]
        modal = TicketCreationModal(ticket_type=ticket_type, title="Create Support Ticket")
        await interaction.response.send_modal(modal)


class TicketPromptEditView(discord.ui.View):
    def __init__(self, interaction_user: discord.User):
        super().__init__(timeout=900)
        self.interaction_user = interaction_user

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.grey)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user.id:
            await interaction.response.send_message("You can't use this button.", ephemeral=True)
            return
        modal = TicketPromptConfigModal()
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Add Ticket Type", style=discord.ButtonStyle.blurple)
    async def add_type_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user.id:
            await interaction.response.send_message("You can't use this button.", ephemeral=True)
            return
        modal = TicketTypeConfigModal()
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Send", style=discord.ButtonStyle.success)
    async def send_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user.id:
            await interaction.response.send_message("You can't use this button.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(interaction.guild.id)
        ticket_types = config["ticket_prompt"]["types"]

        if not ticket_types:
            await interaction.followup.send(
                "You need at least one ticket type before sending the prompt.",
                ephemeral=True
            )
            return

        create_ticket_channel = discord.utils.get(interaction.guild.text_channels, name="create-ticket")
        if not create_ticket_channel:
            await interaction.followup.send("Could not find create-ticket channel.", ephemeral=True)
            return

        try:
            prompt_embed = create_ticket_prompt_embed(config["ticket_prompt"])
            view = TicketTypeSelectView(ticket_types)
            message = await create_ticket_channel.send(embed=prompt_embed, view=view)
            config["ticket_prompt"]["message_id"] = message.id
            config["ticket_prompt"]["channel_id"] = create_ticket_channel.id
            save_guild_configs()

            await interaction.followup.send("Ticket prompt sent successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error sending prompt: {str(e)}", ephemeral=True)


def create_ticket_prompt_embed(prompt_config: dict) -> discord.Embed:
    embed = discord.Embed(
        title=prompt_config["title"],
        description=prompt_config["description"],
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Select a ticket type from the dropdown below")
    return embed


async def show_ticket_prompt_editor(interaction: discord.Interaction):
    config = get_guild_config(interaction.guild.id)
    types = config["ticket_prompt"]["types"]

    if not types:
        types_text = "No types added yet"
    else:
        types_text = "\n".join(f"{i}. {t['name']} (value: {t['value']})" for i, t in enumerate(types, 1))

    editor_embed = discord.Embed(
        title="Ticket Prompt Configuration",
        description="Configure and customize your ticket creation prompt",
        color=discord.Color.blurple()
    )
    editor_embed.add_field(name="Title", value=f"```{config['ticket_prompt']['title']}```", inline=False)
    editor_embed.add_field(name="Description", value=f"```{config['ticket_prompt']['description']}```", inline=False)
    editor_embed.add_field(name="Ticket Types", value=types_text, inline=False)

    view = TicketPromptEditView(interaction.user)
    await interaction.followup.send(embed=editor_embed, view=view, ephemeral=True)


async def setup_ticket_system(interaction: discord.Interaction, guild: discord.Guild):
    config = get_guild_config(guild.id)
    support_role_id = config["support_role"]

    if not support_role_id:
        await interaction.followup.send("No support role selected.", ephemeral=True)
        return

    support_role = guild.get_role(support_role_id)
    if not support_role:
        await interaction.followup.send("Support role not found.", ephemeral=True)
        return

    total_items = get_total_setup_items()
    progress_embed = discord.Embed(
        title="Ticket Setup",
        description="Creating your ticket system...",
        color=discord.Color.blurple()
    )
    progress_embed.add_field(name="Progress", value=create_progress_bar(0, total_items), inline=False)
    progress_embed.add_field(name="Status", value="Initializing setup...", inline=False)

    progress_message = await interaction.channel.send(embed=progress_embed)
    completed = 0

    try:
        for category_name, category_info in ChannelConfig.TICKET_SYSTEM_STRUCTURE.items():
            category_overwrites = build_category_overwrites(guild, support_role, category_info["visible_to_all"])
            category = await find_or_create_category(guild, category_name, overwrites=category_overwrites)

            completed += 1
            progress_embed.set_field_at(0, name="Progress", value=create_progress_bar(completed, total_items), inline=False)
            progress_embed.set_field_at(1, name="Status", value=f"Ready category: {category_name}", inline=False)
            await progress_message.edit(embed=progress_embed)
            await asyncio.sleep(BotConfig.PROGRESS_UPDATE_DELAY)

            for channel_name in category_info["channels"]:
                if channel_name == "create-ticket":
                    channel_overwrites = build_ticket_channel_overwrites(
                        guild,
                        support_role,
                        default_visible=True,
                    )
                else:
                    channel_overwrites = build_ticket_channel_overwrites(
                        guild,
                        support_role,
                        default_visible=False,
                    )

                await find_or_create_text_channel(guild, channel_name, category, overwrites=channel_overwrites)

                completed += 1
                progress_embed.set_field_at(0, name="Progress", value=create_progress_bar(completed, total_items), inline=False)
                progress_embed.set_field_at(1, name="Status", value=f"Ready channel: {channel_name}", inline=False)
                await progress_message.edit(embed=progress_embed)
                await asyncio.sleep(BotConfig.PROGRESS_UPDATE_DELAY)

        await ensure_support_infrastructure(guild, support_role)
        completed += 2
        progress_embed.set_field_at(0, name="Progress", value=create_progress_bar(completed, total_items), inline=False)
        progress_embed.set_field_at(1, name="Status", value=f"Created {CLOSED_TICKETS_CATEGORY_NAME} and {TRANSCRIPTS_CHANNEL_NAME}", inline=False)
        await progress_message.edit(embed=progress_embed)

        config["setup_complete"] = True
        save_guild_configs()

        completion_embed = discord.Embed(
            title="Ticket Setup",
            description="Your ticket system has been successfully created.",
            color=discord.Color.green()
        )
        completion_embed.add_field(name="Progress", value=create_progress_bar(total_items, total_items), inline=False)
        completion_embed.add_field(name="Support Role", value=f"{support_role.mention}", inline=False)
        completion_embed.set_footer(text=f"Setup completed by {interaction.user.name}")

        await progress_message.edit(embed=completion_embed)
    except Exception as e:
        error_embed = discord.Embed(
            title="Setup Failed",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        error_embed.add_field(name="Progress", value=create_progress_bar(completed, total_items), inline=False)
        await progress_message.edit(embed=error_embed)


async def create_ticket_channel(interaction: discord.Interaction, ticket_info: dict):
    guild = ticket_info["guild"]
    creator = ticket_info["creator"]
    ticket_type = ticket_info["type"]

    config = get_guild_config(guild.id)
    support_role_id = config["support_role"]
    support_role = guild.get_role(support_role_id) if support_role_id else None

    if not support_role:
        await interaction.followup.send("Support role not found. Please run setup again.", ephemeral=True)
        return

    category = None
    for t in config["ticket_prompt"]["types"]:
        if t["value"] == ticket_type:
            category_name = t["category"]
            category = discord.utils.get(guild.categories, name=category_name)
            break

    if not category:
        await interaction.followup.send("Could not find ticket category.", ephemeral=True)
        return

    try:
        ticket_number = await get_next_ticket_number(guild.id)
        channel_name = build_ticket_channel_name(creator.name, ticket_number)

        overwrites = build_ticket_channel_overwrites(
            guild,
            support_role,
            creator,
            creator_visible=True,
            default_visible=False,
        )

        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket created by {creator.name}"
        )

        ticket_embed = discord.Embed(
            title="Support Ticket",
            description=f"Ticket created by {creator.mention}",
            color=discord.Color.blurple()
        )
        ticket_embed.add_field(name="Reason", value=ticket_info["reason"], inline=False)

        if ticket_type == "report" and "reported_member" in ticket_info:
            ticket_embed.add_field(name="Reported Member", value=ticket_info["reported_member"], inline=False)

        ticket_embed.add_field(name="Type", value=ticket_type.capitalize(), inline=True)
        ticket_embed.set_footer(text=f"Ticket ID: {ticket_channel.id}")

        await ticket_channel.send(embed=ticket_embed)

        controls_metadata = {
            "state": "open",
            "creator_id": creator.id,
            "creator_name": creator.name,
            "ticket_type": ticket_type,
            "original_category_id": category.id,
            "claimed_by_id": None,
        }
        controls_message = await send_ticket_controls_message(ticket_channel, controls_metadata)
        controls_metadata["controls_message_id"] = controls_message.id
        await set_ticket_metadata(ticket_channel, controls_metadata)

        await interaction.followup.send(f"Ticket created: {ticket_channel.mention}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error creating ticket: {str(e)}", ephemeral=True)


@bot.event
async def on_ready():
    init_guild_config_database()
    load_guild_configs()
    init_ticket_database()
    configure_storage(get_guild_config, save_guild_configs)

    try:
        bot.add_view(SupportControlsView())
        bot.add_view(ClosedTicketControlsView())
        bot.add_view(TicketTypeSelectView([]))  # Add persistent ticket type select view
    except Exception:
        pass

    try:
        await hydrate_ticket_controls(bot)
    except Exception as e:
        print(f"Failed to hydrate ticket controls: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    print(f"Bot logged in as {bot.user}")


@bot.tree.command(name="setup", description="Setup the ticket system for your server")
@app_commands.default_permissions(administrator=True)
async def setup_command(interaction: discord.Interaction):
    # Check admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You must be an administrator to use this command.", ephemeral=True)
        return

    # Check if setup is already complete
    config = get_guild_config(interaction.guild.id)
    if config.get("setup_complete"):
        await interaction.response.send_message(
            "Ticket system has already been set up for this server. Use `/ticket-prompt` to configure prompts.",
            ephemeral=True
        )
        return

    role_embed = discord.Embed(
        title="Ticket Setup",
        description="Select the support role for your ticket system",
        color=discord.Color.blurple()
    )
    role_embed.add_field(
        name="Support Role",
        value="This role will have access to all support channels and ticket commands.",
        inline=False
    )

    roles = [role for role in interaction.guild.roles if role != interaction.guild.default_role]
    if not roles:
        await interaction.response.send_message(
            "No roles available to select. Please create a role first.",
            ephemeral=True
        )
        return

    view = RoleSelectView(roles)
    await interaction.response.send_message(embed=role_embed, view=view, ephemeral=True)


@bot.tree.command(name="ticket-prompt", description="Configure and send the ticket creation prompt")
@app_commands.default_permissions(administrator=True)
async def ticket_prompt_command(interaction: discord.Interaction):
    # Check admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You must be an administrator to use this command.", ephemeral=True)
        return

    config = get_guild_config(interaction.guild.id)
    
    # Check if setup is complete
    if not config.get("setup_complete"):
        await interaction.response.send_message(
            "Please run `/setup` first to configure the ticket system.",
            ephemeral=True
        )
        return

    # Check if support role exists
    if not config.get("support_role"):
        await interaction.response.send_message(
            "Support role not configured. Please run `/setup` first.",
            ephemeral=True
        )
        return

    modal = TicketPromptConfigModal()
    await interaction.response.send_modal(modal)


@bot.tree.command(name="ping", description="Check bot latency")
async def ping_command(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    ping_embed = discord.Embed(
        title="Pong!",
        description=f"Bot latency: {latency}ms",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=ping_embed, ephemeral=True)


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN not found in .env file")
    bot.run(token)


if __name__ == "__main__":
    main()