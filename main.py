from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask

from config import BotConfig, ChannelConfig, TicketPromptConfig

load_dotenv()

# Ensure "from main import ..." works even when this file is executed as a script.
sys.modules.setdefault("main", sys.modules[__name__])

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

# Use empty string as prefix since we're using slash commands
bot = commands.Bot(command_prefix="", intents=intents)


# =========================
# Render + Gunicorn Web App
# =========================

app = Flask(__name__)


@app.route("/")
def home():
    """Health check endpoint for Render/UptimeRobot"""
    return "Bot is alive!", 200

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
    config.setdefault("transcript_channel_id", None)
    config.setdefault("ticket_prompt", create_default_ticket_prompt())
    config.setdefault("ticket_categories", {})  # Maps ticket type value to category ID

    channel_config = config.setdefault("channel_config", {})
    if not isinstance(channel_config, dict):
        channel_config = {}
        config["channel_config"] = channel_config
    for key, value in ChannelConfig.DEFAULT_GUILD_CHANNEL_CONFIG.items():
        channel_config.setdefault(key, value)
    channel_config.setdefault(
        "prompt_channel_name",
        channel_config.get("support_channel_name", ChannelConfig.SUPPORT_CHANNEL_NAME),
    )

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

    if not isinstance(config["ticket_categories"], dict):
        config["ticket_categories"] = {}

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
        if overwrites is not None:
            try:
                await existing.edit(overwrites=overwrites, reason="Ticket system setup")
            except Exception:
                pass
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
        try:
            await existing.edit(category=category, overwrites=overwrites, reason="Ticket system setup")
        except Exception:
            pass
        return existing
    return await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        reason="Ticket system setup",
    )


async def create_ticket_channel(interaction: discord.Interaction, ticket_info: dict) -> None:
    """Create a new ticket channel for a user."""
    guild = ticket_info.get("guild") or interaction.guild
    creator = ticket_info.get("creator") or interaction.user
    ticket_type = ticket_info.get("type", "general")
    reason = ticket_info.get("reason", "")
    reported_member = ticket_info.get("reported_member", "")

    config = get_guild_config(guild.id)
    support_role_id = config.get("support_role")
    if not support_role_id:
        await interaction.followup.send("Support role not configured.", ephemeral=True)
        return

    support_role = guild.get_role(support_role_id)
    if not support_role:
        await interaction.followup.send("Support role not found.", ephemeral=True)
        return

    ticket_number = await get_next_ticket_number(guild.id)
    channel_name = build_ticket_channel_name(creator.name, ticket_number)

    ticket_categories = config.get("ticket_categories", {})
    category_id = ticket_categories.get(ticket_type)

    category = None
    if category_id:
        maybe_category = guild.get_channel(int(category_id))
        if isinstance(maybe_category, discord.CategoryChannel):
            category = maybe_category

    if not category:
        default_category_name = f"{ticket_type.title()} Tickets"
        overwrites = build_category_overwrites(guild, support_role, False)
        category = await find_or_create_category(guild, default_category_name, overwrites)

    overwrites = build_ticket_channel_overwrites(
        guild, support_role, creator, creator_visible=True, default_visible=False
    )

    try:
        ticket_channel = await find_or_create_text_channel(guild, channel_name, category, overwrites)
    except Exception as e:
        await interaction.followup.send(f"Error creating ticket channel: {str(e)}", ephemeral=True)
        return

    metadata = {
        "state": "open",
        "creator_id": creator.id,
        "creator_name": creator.name,
        "ticket_type": ticket_type,
        "reason": reason,
        "claimed_by_id": None,
        "claimed_by_name": None,
        "closed_by_id": None,
        "original_category_id": category.id,
    }

    if reported_member:
        metadata["reported_member"] = reported_member

    from support_controls import set_ticket_metadata, send_ticket_controls_message

    await set_ticket_metadata(ticket_channel, metadata)

    info_embed = discord.Embed(
        title="Ticket Information",
        description=f"Ticket created by {creator.mention}",
        color=discord.Color.blurple(),
    )
    info_embed.add_field(name="Type", value=ticket_type.capitalize(), inline=True)
    info_embed.add_field(name="Creator ID", value=f"`{creator.id}`", inline=True)

    if reason:
        info_embed.add_field(name="Reason", value=str(reason)[:1024], inline=False)

    if reported_member:
        info_embed.add_field(name="Reported Member", value=str(reported_member)[:1024], inline=False)

    await ticket_channel.send(embed=info_embed)

    controls_message = await send_ticket_controls_message(ticket_channel, metadata)
    metadata["controls_message_id"] = controls_message.id
    await set_ticket_metadata(ticket_channel, metadata)

    msg_content = f"Your ticket has been created: {ticket_channel.mention}"
    if reason:
        msg_content += f"\nReason: `{reason}`"

    await interaction.followup.send(content=msg_content, ephemeral=True)


# Import support/config modules after core helpers are defined.
# This avoids circular import breakage when they import from main.
from support_controls import (  # noqa: E402
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

import config_command  # noqa: E402


class RoleSelectView(discord.ui.View):
    def __init__(self, roles: list):
        super().__init__(timeout=600)
        options = [discord.SelectOption(label=role.name, value=str(role.id)) for role in roles[:25]]
        select = discord.ui.Select(
            placeholder="Select a support role",
            options=options,
            min_values=1,
            max_values=1,
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
            description="Would you like to automatically set up all channels and categories?",
            color=discord.Color.blurple(),
        )
        proceed_embed.add_field(name="Support Role", value=f"Selected: {role.mention}", inline=False)

        proceed_view = AutoSetupPromptView(interaction.guild)
        await interaction.response.edit_message(embed=proceed_embed, view=proceed_view)


class AutoSetupPromptView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=600)
        self.guild = guild

    @discord.ui.button(label="Yes - Auto Setup", style=discord.ButtonStyle.success)
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await auto_setup_ticket_system(interaction, self.guild)

    @discord.ui.button(label="No - Manual Setup", style=discord.ButtonStyle.secondary)
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await manual_setup_ticket_system(interaction, self.guild)


async def auto_setup_ticket_system(interaction: discord.Interaction, guild: discord.Guild):
    """Automatic setup with progress bar - creates all channels and categories"""
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
        color=discord.Color.blurple(),
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

        transcript_channel = discord.utils.get(guild.text_channels, name=TRANSCRIPTS_CHANNEL_NAME)
        if transcript_channel:
            config["transcript_channel_id"] = transcript_channel.id

        for ticket_type in config["ticket_prompt"]["types"]:
            for cat_name in ChannelConfig.TICKET_SYSTEM_STRUCTURE.keys():
                if cat_name not in ["Support", "Logs"]:
                    category = discord.utils.get(guild.categories, name=cat_name)
                    if category and ticket_type["value"] not in config["ticket_categories"]:
                        config["ticket_categories"][ticket_type["value"]] = category.id

        config["setup_complete"] = True
        save_guild_configs()

        completion_embed = discord.Embed(
            title="Ticket Setup Complete",
            description="Your ticket system has been successfully created with auto setup.",
            color=discord.Color.green(),
        )
        completion_embed.add_field(name="Progress", value=create_progress_bar(total_items, total_items), inline=False)
        completion_embed.add_field(name="Support Role", value=f"{support_role.mention}", inline=False)
        completion_embed.add_field(
            name="Next Step",
            value="Use `/ticket-prompt` to configure and send the ticket creation prompt",
            inline=False,
        )
        completion_embed.set_footer(text=f"Setup completed by {interaction.user.name}")

        await progress_message.edit(embed=completion_embed)
    except Exception as e:
        error_embed = discord.Embed(
            title="Setup Failed",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red(),
        )
        error_embed.add_field(name="Progress", value=create_progress_bar(completed, total_items), inline=False)
        await progress_message.edit(embed=error_embed)


async def manual_setup_ticket_system(interaction: discord.Interaction, guild: discord.Guild):
    """Manual setup - user selects channels and categories"""
    config = get_guild_config(guild.id)
    ticket_types = config["ticket_prompt"]["types"]

    if not ticket_types:
        await interaction.followup.send(
            "Default ticket types are ready. Select your channels and categories below.",
            ephemeral=True,
        )

    embed = discord.Embed(
        title="Manual Setup",
        description="Select the transcript channel and category for each ticket type",
        color=discord.Color.blurple(),
    )

    view = config_command.ChannelSelectView(guild, ticket_types)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.event
async def on_ready():
    """Initialize database and hydrate ticket controls on bot startup."""
    print(f"{bot.user} has connected to Discord!")
    init_guild_config_database()
    init_ticket_database()
    load_guild_configs()
    configure_storage(get_guild_config, save_guild_configs)

    try:
        bot.add_view(SupportControlsView())
        bot.add_view(ClosedTicketControlsView())
        bot.add_view(config_command.TicketTypeSelectView([], register_only=True))
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
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You must be an administrator to use this command.", ephemeral=True)
        return

    config = get_guild_config(interaction.guild.id)
    if config.get("setup_complete"):
        await interaction.response.send_message(
            "Ticket system has already been set up for this server. Use `/config` to make changes.",
            ephemeral=True,
        )
        return

    role_embed = discord.Embed(
        title="Ticket Setup - Step 1",
        description="Select the support role for your ticket system",
        color=discord.Color.blurple(),
    )
    role_embed.add_field(
        name="Support Role",
        value="This role will have access to all support channels and ticket commands.",
        inline=False,
    )

    roles = [role for role in interaction.guild.roles if role != interaction.guild.default_role]
    if not roles:
        await interaction.response.send_message(
            "No roles available to select. Please create a role first.",
            ephemeral=True,
        )
        return

    view = RoleSelectView(roles)
    await interaction.response.send_message(embed=role_embed, view=view, ephemeral=True)


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN not found in .env file")


    # Run Discord bot
    bot.run(token)


if __name__ == "__main__":
    main()
