from __future__ import annotations

import discord
from discord import app_commands

from config import BotConfig, ChannelConfig, TicketPromptConfig
from main import bot, get_guild_config, save_guild_configs, create_ticket_channel


def _ensure_ticket_prompt(config: dict) -> dict:
    prompt = config.setdefault("ticket_prompt", {})
    prompt.setdefault("title", TicketPromptConfig.DEFAULT_TITLE)
    prompt.setdefault("description", TicketPromptConfig.DEFAULT_DESCRIPTION)
    prompt.setdefault("button_text", TicketPromptConfig.DEFAULT_BUTTON_TEXT)
    prompt.setdefault("types", [t.copy() for t in TicketPromptConfig.DEFAULT_TICKET_TYPES])
    prompt.setdefault("message_id", None)
    prompt.setdefault("channel_id", None)
    return prompt


def _ensure_channel_config(config: dict) -> dict:
    channel_config = config.setdefault("channel_config", {})
    for key, value in ChannelConfig.DEFAULT_GUILD_CHANNEL_CONFIG.items():
        channel_config.setdefault(key, value)
    channel_config.setdefault(
        "prompt_channel_name",
        channel_config.get("support_channel_name", ChannelConfig.SUPPORT_CHANNEL_NAME),
    )
    return channel_config


def _get_prompt_channel_name(channel_config: dict) -> str:
    return str(
        channel_config.get("prompt_channel_name")
        or channel_config.get("support_channel_name")
        or ChannelConfig.SUPPORT_CHANNEL_NAME
    )


def _format_category_ref(guild: discord.Guild, category_id: int | None) -> str:
    if not category_id:
        return "Not configured"
    channel = guild.get_channel(int(category_id))
    if isinstance(channel, discord.CategoryChannel):
        return f"#{channel.name}"
    return f"Missing ({category_id})"


def _truncate(text: str, limit: int = 1024) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


async def _resolve_or_create_category(guild: discord.Guild, raw: str) -> discord.CategoryChannel | None:
    raw = raw.strip()
    if not raw:
        return None

    cleaned = raw.strip("<#>")
    if cleaned.isdigit():
        channel = guild.get_channel(int(cleaned))
        if isinstance(channel, discord.CategoryChannel):
            return channel

    category = discord.utils.find(lambda c: c.name.lower() == raw.lower(), guild.categories)
    if category:
        return category

    try:
        return await guild.create_category(name=raw, reason="Ticket system configuration")
    except Exception:
        return None


def _ticket_types(config: dict) -> list[dict]:
    prompt = _ensure_ticket_prompt(config)
    types = prompt.get("types", [])
    return types if isinstance(types, list) else []


def _build_dashboard_embed(guild: discord.Guild, config: dict) -> discord.Embed:
    prompt = _ensure_ticket_prompt(config)
    channel_config = _ensure_channel_config(config)
    ticket_types = _ticket_types(config)
    mapping = config.get("ticket_categories", {}) or {}

    embed = discord.Embed(
        title="Ticket System Configuration",
        description="Use the buttons below to edit the ticket system in focused sections.",
        color=discord.Color.blurple(),
    )

    channel_lines = [
        f"Support category: **{channel_config['support_category_name']}**",
        f"Support channel: **{channel_config['support_channel_name']}**",
        f"Closed tickets category: **{channel_config['closed_tickets_category_name']}**",
        f"Transcript channel: **{channel_config['transcripts_channel_name']}**",
        f"Prompt channel: **{_get_prompt_channel_name(channel_config)}**",
    ]
    embed.add_field(name="1) Channel Configs", value=_truncate("\n".join(channel_lines)), inline=False)

    if ticket_types:
        category_lines = []
        for ticket_type in ticket_types:
            value = str(ticket_type.get("value", "unknown"))
            category_id = mapping.get(value)
            category_ref = _format_category_ref(guild, category_id)
            template_category = ticket_type.get("category", "Not set")
            category_lines.append(
                f"**{ticket_type.get('name', value)}** (`{value}`)\n"
                f"Template: `{template_category}`\n"
                f"Creates in: {category_ref}"
            )
        embed.add_field(
            name="2) Category Configs",
            value=_truncate("\n\n".join(category_lines)),
            inline=False,
        )
    else:
        embed.add_field(
            name="2) Category Configs",
            value="No ticket types are configured yet.",
            inline=False,
        )

    if ticket_types:
        types_lines = [
            f"**{t.get('name', 'Unnamed')}** (`{t.get('value', 'unknown')}`)"
            + (f" → `{t.get('category')}`" if t.get("category") else "")
            for t in ticket_types
        ]
        embed.add_field(
            name="3) Ticket Types",
            value=_truncate("\n".join(types_lines)),
            inline=False,
        )
    else:
        embed.add_field(name="3) Ticket Types", value="No ticket types configured.", inline=False)

    embed.add_field(
        name="Prompt",
        value=_truncate(
            f"Title: `{prompt['title']}`\n"
            f"Description: `{prompt['description']}`\n"
            f"Button text: `{prompt['button_text']}`"
        ),
        inline=False,
    )

    embed.set_footer(text="Configuration changes are saved per server.")
    return embed


def _build_prompt_embed(prompt_config: dict) -> discord.Embed:
    embed = discord.Embed(
        title=prompt_config["title"],
        description=prompt_config["description"],
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Select a ticket type from the dropdown below")
    return embed


class ChannelSelectView(discord.ui.View):
    """View for selecting channels and categories during manual setup."""

    def __init__(self, guild: discord.Guild, ticket_types: list):
        super().__init__(timeout=BotConfig.CONFIG_VIEW_TIMEOUT)
        self.guild = guild
        self.ticket_types = ticket_types

        options = [
            discord.SelectOption(
                label=str(t.get("name", t.get("value", "Unknown")))[:100],
                value=str(t.get("value", ""))[:100],
            )
            for t in ticket_types[:25]
            if str(t.get("value", "")).strip()
        ]

        if not options:
            self.add_item(
                discord.ui.Button(
                    label="No ticket types configured",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                )
            )
            return

        select = discord.ui.Select(
            placeholder="Select a ticket type to configure",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        selected_value = interaction.data["values"][0]

        selected_type = None
        for t in self.ticket_types:
            if str(t.get("value", "")).strip() == selected_value:
                selected_type = t
                break

        if not selected_type:
            await interaction.response.send_message("Ticket type not found.", ephemeral=True)
            return

        modal = CategorySelectionModal(interaction.guild, selected_type)
        await interaction.response.send_modal(modal)


class CategorySelectionModal(discord.ui.Modal):
    """Modal for selecting category for a ticket type."""

    def __init__(self, guild: discord.Guild, ticket_type: dict):
        safe_value = str(ticket_type.get("value", "Unknown"))[:20]
        super().__init__(title=f"Configure: {safe_value}")
        self.guild = guild
        self.ticket_type = ticket_type

        self.category_input = discord.ui.TextInput(
            label="Category Name or ID",
            placeholder="Type category name or paste category ID",
            default=str(ticket_type.get("category", "") or ""),
            max_length=100,
            required=False,
        )
        self.add_item(self.category_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        raw = self.category_input.value.strip()
        if not raw:
            await interaction.followup.send("Category cannot be blank.", ephemeral=True)
            return

        category = None

        cleaned = raw.strip("<#>")
        if cleaned.isdigit():
            maybe_cat = self.guild.get_channel(int(cleaned))
            if isinstance(maybe_cat, discord.CategoryChannel):
                category = maybe_cat

        if not category:
            category = discord.utils.find(lambda c: c.name.lower() == raw.lower(), self.guild.categories)

        if not category:
            try:
                category = await self.guild.create_category(
                    name=raw,
                    reason="Ticket system configuration",
                )
            except Exception as e:
                await interaction.followup.send(
                    f"Could not create category: {str(e)}",
                    ephemeral=True,
                )
                return

        config = get_guild_config(interaction.guild.id)
        config.setdefault("ticket_categories", {})[str(self.ticket_type.get("value"))] = category.id
        save_guild_configs()

        await interaction.followup.send(
            f"✅ Ticket type `{self.ticket_type.get('value')}` now creates in **#{category.name}**.",
            ephemeral=True,
        )


async def _resolve_or_create_category(guild: discord.Guild, raw: str) -> discord.CategoryChannel | None:
    raw = raw.strip()
    if not raw:
        return None

    cleaned = raw.strip("<#>")
    if cleaned.isdigit():
        channel = guild.get_channel(int(cleaned))
        if isinstance(channel, discord.CategoryChannel):
            return channel

    category = discord.utils.find(lambda c: c.name.lower() == raw.lower(), guild.categories)
    if category:
        return category

    try:
        return await guild.create_category(name=raw, reason="Ticket system configuration")
    except Exception:
        return None


def _ticket_types(config: dict) -> list[dict]:
    prompt = _ensure_ticket_prompt(config)
    types = prompt.get("types", [])
    return types if isinstance(types, list) else []


def _build_dashboard_embed(guild: discord.Guild, config: dict) -> discord.Embed:
    prompt = _ensure_ticket_prompt(config)
    channel_config = _ensure_channel_config(config)
    ticket_types = _ticket_types(config)
    mapping = config.get("ticket_categories", {}) or {}

    embed = discord.Embed(
        title="Ticket System Configuration",
        description="Use the buttons below to edit the ticket system in focused sections.",
        color=discord.Color.blurple(),
    )

    channel_lines = [
        f"Support category: **{channel_config['support_category_name']}**",
        f"Support channel: **{channel_config['support_channel_name']}**",
        f"Closed tickets category: **{channel_config['closed_tickets_category_name']}**",
        f"Transcript channel: **{channel_config['transcripts_channel_name']}**",
        f"Prompt channel: **{_get_prompt_channel_name(channel_config)}**",
    ]
    embed.add_field(name="1) Channel Configs", value=_truncate("\n".join(channel_lines)), inline=False)

    if ticket_types:
        category_lines = []
        for ticket_type in ticket_types:
            value = ticket_type.get("value", "unknown")
            category_id = mapping.get(value)
            category_ref = _format_category_ref(guild, category_id)
            template_category = ticket_type.get("category", "Not set")
            category_lines.append(
                f"**{ticket_type.get('name', value)}** (`{value}`)\n"
                f"Template: `{template_category}`\n"
                f"Creates in: {category_ref}"
            )
        embed.add_field(
            name="2) Category Configs",
            value=_truncate("\n\n".join(category_lines)),
            inline=False,
        )
    else:
        embed.add_field(
            name="2) Category Configs",
            value="No ticket types are configured yet.",
            inline=False,
        )

    if ticket_types:
        types_lines = [
            f"**{t.get('name', 'Unnamed')}** (`{t.get('value', 'unknown')}`)"
            + (f" → `{t.get('category')}`" if t.get("category") else "")
            for t in ticket_types
        ]
        embed.add_field(
            name="3) Ticket Types",
            value=_truncate("\n".join(types_lines)),
            inline=False,
        )
    else:
        embed.add_field(name="3) Ticket Types", value="No ticket types configured.", inline=False)

    embed.add_field(
        name="Prompt",
        value=_truncate(
            f"Title: `{prompt['title']}`\n"
            f"Description: `{prompt['description']}`\n"
            f"Button text: `{prompt['button_text']}`"
        ),
        inline=False,
    )

    embed.set_footer(text="Configuration changes are saved per server.")
    return embed


def _build_prompt_embed(prompt_config: dict) -> discord.Embed:
    embed = discord.Embed(
        title=prompt_config["title"],
        description=prompt_config["description"],
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Select a ticket type from the dropdown below")
    return embed


async def _show_dashboard(interaction: discord.Interaction) -> None:
    config = get_guild_config(interaction.guild.id)
    embed = _build_dashboard_embed(interaction.guild, config)
    view = ConfigDashboardView(interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)


async def _show_dashboard_followup(interaction: discord.Interaction) -> None:
    config = get_guild_config(interaction.guild.id)
    embed = _build_dashboard_embed(interaction.guild, config)
    view = ConfigDashboardView(interaction.user.id)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def show_config_menu(interaction: discord.Interaction):
    config = get_guild_config(interaction.guild.id)
    embed = _build_dashboard_embed(interaction.guild, config)
    view = ConfigDashboardView(interaction.user.id)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def show_ticket_prompt_editor(interaction: discord.Interaction):
    config = get_guild_config(interaction.guild.id)
    prompt = _ensure_ticket_prompt(config)
    ticket_types = prompt["types"]

    if not ticket_types:
        types_text = "No types added yet"
    else:
        types_text = "\n".join(
            f"{i}. {t['name']} (value: {t['value']})"
            + (f" | category: {t.get('category', 'not set')}" if t.get("category") else "")
            for i, t in enumerate(ticket_types, 1)
        )

    editor_embed = discord.Embed(
        title="Ticket Prompt Configuration",
        description="Configure the ticket creation prompt and ticket type list.",
        color=discord.Color.blurple(),
    )
    editor_embed.add_field(name="Title", value=f"```{prompt['title']}```", inline=False)
    editor_embed.add_field(name="Description", value=f"```{prompt['description']}```", inline=False)
    editor_embed.add_field(name="Button Text", value=f"```{prompt['button_text']}```", inline=False)
    editor_embed.add_field(name="Ticket Types", value=_truncate(types_text), inline=False)

    view = TicketPromptEditView(interaction.user.id)
    await interaction.followup.send(embed=editor_embed, view=view, ephemeral=True)


class ChannelConfigModal(discord.ui.Modal):
    def __init__(self, guild: discord.Guild):
        super().__init__(title="Edit Channel Configs")
        self.guild = guild

        config = get_guild_config(guild.id)
        channel_config = _ensure_channel_config(config)

        self.support_category_input = discord.ui.TextInput(
            label="Support Category Name",
            placeholder="Support",
            default=channel_config["support_category_name"],
            max_length=100,
        )
        self.support_channel_input = discord.ui.TextInput(
            label="Support Channel Name",
            placeholder="create-ticket",
            default=channel_config["support_channel_name"],
            max_length=100,
        )
        self.closed_category_input = discord.ui.TextInput(
            label="Closed Tickets Category Name",
            placeholder="closed-tickets",
            default=channel_config["closed_tickets_category_name"],
            max_length=100,
        )
        self.transcripts_channel_input = discord.ui.TextInput(
            label="Transcript Channel Name",
            placeholder="ticket-transcripts",
            default=channel_config["transcripts_channel_name"],
            max_length=100,
        )
        self.prompt_channel_input = discord.ui.TextInput(
            label="Prompt Channel Name",
            placeholder="create-ticket",
            default=_get_prompt_channel_name(channel_config),
            max_length=100,
        )

        self.add_item(self.support_category_input)
        self.add_item(self.support_channel_input)
        self.add_item(self.closed_category_input)
        self.add_item(self.transcripts_channel_input)
        self.add_item(self.prompt_channel_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(interaction.guild.id)
        channel_config = _ensure_channel_config(config)

        channel_config["support_category_name"] = self.support_category_input.value.strip() or ChannelConfig.SUPPORT_CATEGORY_NAME
        channel_config["support_channel_name"] = self.support_channel_input.value.strip() or ChannelConfig.SUPPORT_CHANNEL_NAME
        channel_config["closed_tickets_category_name"] = self.closed_category_input.value.strip() or ChannelConfig.CLOSED_TICKETS_CATEGORY_NAME
        channel_config["transcripts_channel_name"] = self.transcripts_channel_input.value.strip() or ChannelConfig.TRANSCRIPTS_CHANNEL_NAME
        channel_config["prompt_channel_name"] = self.prompt_channel_input.value.strip() or channel_config["support_channel_name"]

        save_guild_configs()

        await interaction.followup.send(
            "Channel configuration saved. Use the refresh button to review the updated dashboard.",
            ephemeral=True,
        )


class PromptSettingsModal(discord.ui.Modal):
    def __init__(self, guild: discord.Guild):
        super().__init__(title="Edit Ticket Prompt")
        self.guild = guild

        config = get_guild_config(guild.id)
        prompt = _ensure_ticket_prompt(config)

        self.title_input = discord.ui.TextInput(
            label="Prompt Title",
            placeholder="Create a Support Ticket",
            default=prompt["title"],
            max_length=100,
        )
        self.description_input = discord.ui.TextInput(
            label="Prompt Description",
            placeholder="Select a ticket type to get started",
            default=prompt["description"],
            max_length=500,
            style=discord.TextStyle.long,
        )
        self.button_text_input = discord.ui.TextInput(
            label="Button Text",
            placeholder="Create Ticket",
            default=prompt["button_text"],
            max_length=80,
        )

        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.button_text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(interaction.guild.id)
        prompt = _ensure_ticket_prompt(config)
        prompt["title"] = self.title_input.value.strip() or TicketPromptConfig.DEFAULT_TITLE
        prompt["description"] = self.description_input.value.strip() or TicketPromptConfig.DEFAULT_DESCRIPTION
        prompt["button_text"] = self.button_text_input.value.strip() or TicketPromptConfig.DEFAULT_BUTTON_TEXT

        save_guild_configs()

        await interaction.followup.send("Prompt settings saved.", ephemeral=True)


class TicketCategoryAssignmentModal(discord.ui.Modal):
    def __init__(self, ticket_type_value: str, current_category: str = ""):
        safe_value = str(ticket_type_value)[:20]
        super().__init__(title=f"Assign Category: {safe_value}")
        self.ticket_type_value = ticket_type_value

        self.category_input = discord.ui.TextInput(
            label="Category Name or ID",
            placeholder="Type a category name or paste a category ID",
            default=current_category,
            max_length=100,
        )
        self.add_item(self.category_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        raw = self.category_input.value.strip()
        if not raw:
            await interaction.followup.send("Category cannot be blank.", ephemeral=True)
            return

        category = await _resolve_or_create_category(interaction.guild, raw)
        if not category:
            await interaction.followup.send("Could not resolve or create that category.", ephemeral=True)
            return

        config = get_guild_config(interaction.guild.id)
        config.setdefault("ticket_categories", {})[self.ticket_type_value] = category.id
        save_guild_configs()

        await interaction.followup.send(
            f"Ticket type `{self.ticket_type_value}` now creates in **#{category.name}**.",
            ephemeral=True,
        )


class TicketTypeAddModal(discord.ui.Modal):
    def __init__(self, guild: discord.Guild):
        super().__init__(title="Add Ticket Type")
        self.guild = guild

        self.type_name_input = discord.ui.TextInput(
            label="Ticket Type Name",
            placeholder="General Support",
            max_length=100,
        )
        self.type_value_input = discord.ui.TextInput(
            label="Ticket Type Value",
            placeholder="general",
            max_length=50,
        )
        self.category_input = discord.ui.TextInput(
            label="Default Category Name or ID",
            placeholder="General Tickets",
            required=False,
            default=ChannelConfig.GENERAL_CATEGORY_NAME,
            max_length=100,
        )

        self.add_item(self.type_name_input)
        self.add_item(self.type_value_input)
        self.add_item(self.category_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(interaction.guild.id)
        prompt = _ensure_ticket_prompt(config)

        type_name = self.type_name_input.value.strip()
        type_value = self.type_value_input.value.lower().strip()
        category_value = self.category_input.value.strip()

        if not type_name or not type_value:
            await interaction.followup.send("Both ticket type name and value are required.", ephemeral=True)
            return

        existing_values = [t["value"].lower() for t in prompt["types"] if isinstance(t, dict) and "value" in t]
        if type_value in existing_values:
            await interaction.followup.send(
                f"A ticket type with value `{type_value}` already exists.",
                ephemeral=True,
            )
            return

        new_type = {
            "name": type_name,
            "value": type_value,
            "category": category_value or None,
        }
        prompt["types"].append(new_type)

        if category_value:
            category = await _resolve_or_create_category(interaction.guild, category_value)
            if category:
                config.setdefault("ticket_categories", {})[type_value] = category.id

        save_guild_configs()

        await interaction.followup.send(
            f"Ticket type `{type_value}` added successfully.",
            ephemeral=True,
        )


class TicketTypeEditModal(discord.ui.Modal):
    def __init__(self, original_type: dict, guild: discord.Guild):
        safe_value = str(original_type.get("value", "unknown"))[:20]
        super().__init__(title=f"Edit Ticket Type: {safe_value}")
        self.guild = guild
        self.original_value = str(original_type.get("value", "")).lower().strip()

        self.name_input = discord.ui.TextInput(
            label="Ticket Type Name",
            placeholder="General Support",
            default=str(original_type.get("name", "")),
            max_length=100,
        )
        self.value_input = discord.ui.TextInput(
            label="Ticket Type Value",
            placeholder="general",
            default=self.original_value,
            max_length=50,
        )
        self.category_input = discord.ui.TextInput(
            label="Default Category Name or ID",
            placeholder="General Tickets",
            required=False,
            default=str(original_type.get("category", "") or ""),
            max_length=100,
        )

        self.add_item(self.name_input)
        self.add_item(self.value_input)
        self.add_item(self.category_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(interaction.guild.id)
        prompt = _ensure_ticket_prompt(config)
        type_name = self.name_input.value.strip()
        type_value = self.value_input.value.lower().strip()
        category_value = self.category_input.value.strip()

        if not type_name or not type_value:
            await interaction.followup.send("Both ticket type name and value are required.", ephemeral=True)
            return

        ticket_types = prompt["types"]
        current_index = None
        for idx, ticket_type in enumerate(ticket_types):
            if str(ticket_type.get("value", "")).lower().strip() == self.original_value:
                current_index = idx
                break

        if current_index is None:
            await interaction.followup.send("The ticket type no longer exists.", ephemeral=True)
            return

        for ticket_type in ticket_types:
            existing_value = str(ticket_type.get("value", "")).lower().strip()
            if existing_value == type_value and existing_value != self.original_value:
                await interaction.followup.send(
                    f"A ticket type with value `{type_value}` already exists.",
                    ephemeral=True,
                )
                return

        old_value = self.original_value
        updated = {
            "name": type_name,
            "value": type_value,
            "category": category_value or None,
        }
        ticket_types[current_index] = updated

        mapping = config.setdefault("ticket_categories", {})
        if old_value in mapping and type_value != old_value:
            mapping[type_value] = mapping.pop(old_value)

        if category_value:
            category = await _resolve_or_create_category(interaction.guild, category_value)
            if category:
                mapping[type_value] = category.id
            else:
                mapping.pop(type_value, None)
        else:
            mapping.pop(type_value, None)

        save_guild_configs()

        await interaction.followup.send(
            f"Ticket type `{old_value}` updated successfully.",
            ephemeral=True,
        )


class TicketTypeDeleteModal(discord.ui.Modal):
    def __init__(
        self,
        original_type: dict,
        *,
        title: str = "Delete Ticket Type",
        refresh_after_submit: bool = False,
    ):
        safe_value = str(original_type.get("value", "unknown"))[:20]
        super().__init__(title=f"{title}: {safe_value}")
        self.refresh_after_submit = refresh_after_submit
        self.original_value = str(original_type.get("value", "")).lower().strip()

        self.confirm_input = discord.ui.TextInput(
            label="Type the ticket value to confirm",
            placeholder=self.original_value,
            default=self.original_value,
            max_length=50,
        )
        self.add_item(self.confirm_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        confirmation = self.confirm_input.value.lower().strip()
        if confirmation != self.original_value:
            await interaction.followup.send(
                "Confirmation text did not match. Ticket type not deleted.",
                ephemeral=True,
            )
            return

        config = get_guild_config(interaction.guild.id)
        prompt = _ensure_ticket_prompt(config)

        before = len(prompt["types"])
        prompt["types"] = [
            t for t in prompt["types"]
            if str(t.get("value", "")).lower().strip() != self.original_value
        ]

        if len(prompt["types"]) == before:
            await interaction.followup.send("Ticket type not found.", ephemeral=True)
            return

        config.setdefault("ticket_categories", {}).pop(self.original_value, None)
        save_guild_configs()

        await interaction.followup.send(f"Ticket type `{self.original_value}` deleted.", ephemeral=True)


class TicketTypeActionSelectView(discord.ui.View):
    def __init__(self, interaction_user_id: int, action: str, guild: discord.Guild):
        super().__init__(timeout=BotConfig.CONFIG_VIEW_TIMEOUT)
        self.interaction_user_id = interaction_user_id
        self.action = action
        self.guild = guild

        config = get_guild_config(guild.id)
        ticket_types = _ticket_types(config)

        options = []
        for ticket_type in ticket_types[:25]:
            label = str(ticket_type.get("name", "Unnamed"))[:100]
            value = str(ticket_type.get("value", ""))[:100]
            description = str(ticket_type.get("category", ""))[:100] or None
            options.append(discord.SelectOption(label=label, value=value, description=description))

        if not options:
            self.add_item(
                discord.ui.Button(
                    label="No ticket types configured",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                )
            )
            return

        select = discord.ui.Select(
            placeholder="Select a ticket type",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return

        selected_value = interaction.data["values"][0]
        config = get_guild_config(interaction.guild.id)
        ticket_types = _ticket_types(config)

        selected = None
        for ticket_type in ticket_types:
            if str(ticket_type.get("value", "")).lower().strip() == selected_value.lower().strip():
                selected = ticket_type
                break

        if not selected:
            await interaction.response.send_message("Selected ticket type was not found.", ephemeral=True)
            return

        if self.action == "edit":
            modal = TicketTypeEditModal(selected, interaction.guild)
            await interaction.response.send_modal(modal)
        elif self.action == "delete":
            modal = TicketTypeDeleteModal(selected)
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.send_message("Unknown action.", ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return
        await _render_dashboard(interaction)


class CategoryConfigView(discord.ui.View):
    def __init__(self, interaction_user_id: int, guild: discord.Guild):
        super().__init__(timeout=BotConfig.CONFIG_VIEW_TIMEOUT)
        self.interaction_user_id = interaction_user_id
        self.guild = guild

        config = get_guild_config(guild.id)
        ticket_types = _ticket_types(config)

        options = []
        for ticket_type in ticket_types[:25]:
            label = str(ticket_type.get("name", "Unnamed"))[:100]
            value = str(ticket_type.get("value", ""))[:100]
            current_mapping = config.get("ticket_categories", {}).get(ticket_type.get("value"))
            current_ref = _format_category_ref(guild, current_mapping)
            description = current_ref[:100]
            options.append(discord.SelectOption(label=label, value=value, description=description))

        if not options:
            self.add_item(
                discord.ui.Button(
                    label="No ticket types configured",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                )
            )
            return

        select = discord.ui.Select(
            placeholder="Select a ticket type to assign a category",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return

        selected_value = interaction.data["values"][0]
        config = get_guild_config(interaction.guild.id)

        current_category_id = config.get("ticket_categories", {}).get(selected_value)
        current_category = interaction.guild.get_channel(int(current_category_id)) if current_category_id else None
        current_name = current_category.name if isinstance(current_category, discord.CategoryChannel) else ""

        modal = TicketCategoryAssignmentModal(selected_value, current_name)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return
        await _render_dashboard(interaction)


class ConfigDashboardView(discord.ui.View):
    def __init__(self, interaction_user_id: int):
        super().__init__(timeout=BotConfig.CONFIG_VIEW_TIMEOUT)
        self.interaction_user_id = interaction_user_id

    @discord.ui.button(label="Channel Configs", style=discord.ButtonStyle.blurple)
    async def channel_configs_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return
        modal = ChannelConfigModal(interaction.guild)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Category Configs", style=discord.ButtonStyle.primary)
    async def category_configs_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return

        config = get_guild_config(interaction.guild.id)
        embed = _build_dashboard_embed(interaction.guild, config)
        view = CategoryConfigView(self.interaction_user_id, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Ticket Types", style=discord.ButtonStyle.success)
    async def ticket_types_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return

        config = get_guild_config(interaction.guild.id)
        embed = _build_dashboard_embed(interaction.guild, config)
        view = TicketTypeManagementView(self.interaction_user_id, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Prompt Settings", style=discord.ButtonStyle.secondary)
    async def prompt_settings_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return
        modal = PromptSettingsModal(interaction.guild)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return
        await _render_dashboard(interaction)


class TicketTypeManagementView(discord.ui.View):
    def __init__(self, interaction_user_id: int, guild: discord.Guild):
        super().__init__(timeout=BotConfig.CONFIG_VIEW_TIMEOUT)
        self.interaction_user_id = interaction_user_id
        self.guild = guild

    @discord.ui.button(label="Add Type", style=discord.ButtonStyle.success)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return
        modal = TicketTypeAddModal(interaction.guild)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Edit Type", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return

        config = get_guild_config(interaction.guild.id)
        ticket_types = _ticket_types(config)
        if not ticket_types:
            await interaction.response.send_message("There are no ticket types to edit.", ephemeral=True)
            return

        embed = _build_dashboard_embed(interaction.guild, config)
        view = TicketTypeActionSelectView(self.interaction_user_id, "edit", interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Delete Type", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return

        config = get_guild_config(interaction.guild.id)
        ticket_types = _ticket_types(config)
        if not ticket_types:
            await interaction.response.send_message("There are no ticket types to delete.", ephemeral=True)
            return

        embed = _build_dashboard_embed(interaction.guild, config)
        view = TicketTypeActionSelectView(self.interaction_user_id, "delete", interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You cannot use this menu.", ephemeral=True)
            return
        await _render_dashboard(interaction)


class TicketTypeSelectView(discord.ui.View):
    def __init__(self, ticket_types: list, register_only: bool = False):
        super().__init__(timeout=None)

        select_options = [
            discord.SelectOption(
                label=str(t.get("name", "Unnamed"))[:100],
                value=str(t.get("value", ""))[:100],
            )
            for t in ticket_types[:25]
            if str(t.get("value", "")).strip()
        ]

        disabled = False
        if not select_options:
            select_options = [discord.SelectOption(label="No ticket types", value="__none__")]
            disabled = True

        select = discord.ui.Select(
            placeholder="Select a ticket type",
            options=select_options,
            min_values=1,
            max_values=1,
            custom_id="ticket_type_select",
            disabled=disabled or register_only,
        )
        select.callback = self.ticket_type_callback
        self.add_item(select)

    async def ticket_type_callback(self, interaction: discord.Interaction):
        ticket_type = interaction.data["values"][0]
        if ticket_type == "__none__":
            await interaction.response.send_message("No ticket types are configured.", ephemeral=True)
            return
        modal = TicketCreationModal(ticket_type=ticket_type, title="Create Support Ticket")
        await interaction.response.send_modal(modal)


class TicketPromptConfigModal(discord.ui.Modal, title="Configure Ticket Prompt"):
    title_input = discord.ui.TextInput(
        label="Prompt Title",
        placeholder="e.g., Create a Support Ticket",
        default="Create a Support Ticket",
        max_length=100,
    )

    description_input = discord.ui.TextInput(
        label="Prompt Description",
        placeholder="e.g., Select a ticket type to get started",
        default="Select a ticket type to get started",
        max_length=500,
        style=discord.TextStyle.long,
    )

    button_text_input = discord.ui.TextInput(
        label="Button Text",
        placeholder="e.g., Create Ticket",
        default="Create Ticket",
        max_length=80,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        config = get_guild_config(interaction.guild.id)
        prompt = _ensure_ticket_prompt(config)
        prompt["title"] = self.title_input.value.strip() or TicketPromptConfig.DEFAULT_TITLE
        prompt["description"] = self.description_input.value.strip() or TicketPromptConfig.DEFAULT_DESCRIPTION
        prompt["button_text"] = self.button_text_input.value.strip() or TicketPromptConfig.DEFAULT_BUTTON_TEXT
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
            style=discord.TextStyle.long,
        )
        self.add_item(self.reason_input)

        if ticket_type == "report":
            self.reported_member = discord.ui.TextInput(
                label="Member ID or Username",
                placeholder="e.g., username or ID",
                max_length=100,
            )
            self.add_item(self.reported_member)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        ticket_info = {
            "type": self.ticket_type,
            "reason": self.reason_input.value.strip(),
            "creator": interaction.user,
            "guild": interaction.guild,
        }

        if self.ticket_type == "report" and hasattr(self, "reported_member"):
            ticket_info["reported_member"] = self.reported_member.value.strip()

        await create_ticket_channel(interaction, ticket_info)


class TicketTypeConfigModal(discord.ui.Modal, title="Add Ticket Type"):
    type_name = discord.ui.TextInput(
        label="Ticket Type Name",
        placeholder="e.g., General Support",
        max_length=100,
    )

    type_value = discord.ui.TextInput(
        label="Type Value (internal)",
        placeholder="e.g., general",
        max_length=50,
    )

    category_name = discord.ui.TextInput(
        label="Default Category Name",
        placeholder="e.g., General Tickets",
        required=False,
        default=ChannelConfig.GENERAL_CATEGORY_NAME,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(interaction.guild.id)
        prompt = _ensure_ticket_prompt(config)
        type_value = self.type_value.value.lower().strip()
        type_name = self.type_name.value.strip()
        category_name = self.category_name.value.strip()

        existing_values = [t["value"].lower() for t in prompt["types"] if isinstance(t, dict) and "value" in t]
        if type_value in existing_values:
            await interaction.followup.send(
                f"A ticket type with value '{type_value}' already exists!",
                ephemeral=True,
            )
            return

        new_type = {
            "name": type_name,
            "value": type_value,
            "category": category_name or None,
        }
        prompt["types"].append(new_type)

        if category_name:
            category = await _resolve_or_create_category(interaction.guild, category_name)
            if category:
                config.setdefault("ticket_categories", {})[type_value] = category.id

        save_guild_configs()

        await interaction.followup.send(
            f"Ticket type '{type_value}' added successfully.",
            ephemeral=True,
        )
        await show_config_menu(interaction)


class DeleteTicketTypeModal(discord.ui.Modal, title="Delete Ticket Type"):
    type_value = discord.ui.TextInput(
        label="Type Value to Delete",
        placeholder="e.g., general",
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(interaction.guild.id)
        prompt = _ensure_ticket_prompt(config)
        type_value = self.type_value.value.lower().strip()

        initial_count = len(prompt["types"])
        prompt["types"] = [
            t for t in prompt["types"]
            if str(t.get("value", "")).lower().strip() != type_value
        ]

        if len(prompt["types"]) == initial_count:
            await interaction.followup.send(
                f"Ticket type '{type_value}' not found!",
                ephemeral=True,
            )
            return

        config.setdefault("ticket_categories", {}).pop(type_value, None)
        save_guild_configs()

        await interaction.followup.send(
            f"Ticket type '{type_value}' deleted!",
            ephemeral=True,
        )
        await show_config_menu(interaction)


class ConfigMenuView(ConfigDashboardView):
    pass


class TicketPromptEditView(discord.ui.View):
    def __init__(self, interaction_user_id: int):
        super().__init__(timeout=BotConfig.CONFIG_VIEW_TIMEOUT)
        self.interaction_user_id = interaction_user_id

    @discord.ui.button(label="Edit Prompt", style=discord.ButtonStyle.secondary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You can't use this button.", ephemeral=True)
            return
        modal = PromptSettingsModal(interaction.guild)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Send Prompt", style=discord.ButtonStyle.success)
    async def send_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You can't use this button.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(interaction.guild.id)
        prompt = _ensure_ticket_prompt(config)
        ticket_types = prompt["types"]

        if not ticket_types:
            await interaction.followup.send(
                "You need at least one ticket type before sending the prompt.",
                ephemeral=True,
            )
            return

        channel_config = _ensure_channel_config(config)
        channel_name = _get_prompt_channel_name(channel_config)
        support_channel = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == channel_name.lower(),
            interaction.guild.text_channels,
        )

        if not support_channel:
            support_channel = discord.utils.find(
                lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == ChannelConfig.SUPPORT_CHANNEL_NAME.lower(),
                interaction.guild.text_channels,
            )

        if not support_channel:
            await interaction.followup.send("Could not find the prompt channel.", ephemeral=True)
            return

        try:
            prompt_embed = _build_prompt_embed(prompt)
            view = TicketTypeSelectView(ticket_types)
            message = await support_channel.send(embed=prompt_embed, view=view)
            prompt["message_id"] = message.id
            prompt["channel_id"] = support_channel.id
            save_guild_configs()

            await interaction.followup.send("Ticket prompt sent successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error sending prompt: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Full Config", style=discord.ButtonStyle.secondary)
    async def full_config_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("You can't use this button.", ephemeral=True)
            return
        await _render_dashboard(interaction)


async def _render_dashboard(interaction: discord.Interaction) -> None:
    config = get_guild_config(interaction.guild.id)
    embed = _build_dashboard_embed(interaction.guild, config)
    view = ConfigDashboardView(interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)


@bot.tree.command(name="ticket-prompt", description="Configure and send the ticket creation prompt")
@app_commands.default_permissions(administrator=True)
async def ticket_prompt_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You must be an administrator to use this command.", ephemeral=True)
        return

    config = get_guild_config(interaction.guild.id)

    if not config.get("setup_complete"):
        await interaction.response.send_message(
            "Please run `/setup` first to configure the ticket system.",
            ephemeral=True,
        )
        return

    if not config.get("support_role"):
        await interaction.response.send_message(
            "Support role not configured. Please run `/setup` first.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    await show_ticket_prompt_editor(interaction)


@bot.tree.command(name="config", description="Configure ticket system (channels, categories, types)")
@app_commands.default_permissions(administrator=True)
async def config_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You must be an administrator to use this command.", ephemeral=True)
        return

    config = get_guild_config(interaction.guild.id)

    if not config.get("setup_complete"):
        await interaction.response.send_message(
            "Please run `/setup` first before configuring.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    await show_config_menu(interaction)
