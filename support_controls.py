import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import discord

_get_guild_config: Optional[Callable[[int], dict]] = None
_save_guild_configs: Optional[Callable[[], None]] = None

CLOSED_TICKETS_CATEGORY_NAME = "closed-tickets"
TRANSCRIPTS_CHANNEL_NAME = "ticket-transcripts"
TICKET_METADATA_DB_FILE = Path("ticket_metadata.db")

__all__ = [
    "CLOSED_TICKETS_CATEGORY_NAME",
    "TRANSCRIPTS_CHANNEL_NAME",
    "configure_storage",
    "SupportControlsView",
    "ClosedTicketControlsView",
    "CloseConfirmView",
    "DeleteConfirmView",
    "build_open_controls_embed",
    "build_closed_controls_embed",
    "send_ticket_controls_message",
    "refresh_ticket_controls_message",
    "ensure_support_infrastructure",
    "hydrate_ticket_controls",
    "set_ticket_metadata",
    "get_ticket_metadata",
]


def _init_ticket_metadata_database() -> None:
    with sqlite3.connect(TICKET_METADATA_DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_metadata (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


_init_ticket_metadata_database()


def configure_storage(getter: Callable[[int], dict], saver: Callable[[], None]) -> None:
    global _get_guild_config, _save_guild_configs
    _get_guild_config = getter
    _save_guild_configs = saver


def _require_storage() -> None:
    if _get_guild_config is None or _save_guild_configs is None:
        raise RuntimeError("support_controls storage has not been configured yet.")


def _safe_json_loads(value: Optional[str]) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_json_dumps(data: dict) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _load_metadata_from_db(channel_id: int) -> dict:
    with sqlite3.connect(TICKET_METADATA_DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.execute(
            "SELECT metadata_json FROM ticket_metadata WHERE channel_id = ?",
            (channel_id,),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return _safe_json_loads(row[0])


def _save_metadata_to_db(channel: discord.TextChannel, metadata: dict) -> None:
    payload = _safe_json_dumps(metadata)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(TICKET_METADATA_DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            INSERT INTO ticket_metadata (channel_id, guild_id, metadata_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id)
            DO UPDATE SET
                guild_id = excluded.guild_id,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (channel.id, channel.guild.id, payload, now),
        )
        conn.commit()


def _delete_metadata_from_db(channel_id: int) -> None:
    with sqlite3.connect(TICKET_METADATA_DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("DELETE FROM ticket_metadata WHERE channel_id = ?", (channel_id,))
        conn.commit()


def get_ticket_metadata(channel: discord.TextChannel) -> dict:
    metadata = _load_metadata_from_db(channel.id)
    if metadata:
        return metadata

    # Legacy fallback: if an older ticket stored JSON in the channel topic, read it once.
    # This does not write back to the topic.
    legacy = _safe_json_loads(channel.topic)
    if legacy:
        _save_metadata_to_db(channel, legacy)
    return legacy


async def set_ticket_metadata(channel: discord.TextChannel, metadata: dict) -> None:
    _save_metadata_to_db(channel, metadata)


def delete_ticket_metadata(channel: discord.TextChannel | int) -> None:
    channel_id = channel if isinstance(channel, int) else channel.id
    _delete_metadata_from_db(channel_id)


def is_support_member(member: discord.Member, support_role_id: Optional[int]) -> bool:
    """Check if member has the support role. Only support role members can use ticket controls."""
    if not support_role_id:
        return False

    role = member.guild.get_role(int(support_role_id))
    return role in member.roles if role else False


def sanitize_filename(name: str) -> str:
    allowed = []
    for ch in name.lower():
        if ch.isalnum() or ch in {"-", "_"}:
            allowed.append(ch)
        elif ch.isspace():
            allowed.append("_")
    value = "".join(allowed).strip("_")
    return value or "ticket"


def build_open_controls_embed(channel: discord.TextChannel, metadata: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Support Team Controls",
        description="Use the buttons below to manage this ticket.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Status", value="Open", inline=True)

    claimed_by_id = metadata.get("claimed_by_id")
    if claimed_by_id:
        embed.add_field(name="Claimed by", value=f"<@{claimed_by_id}>", inline=True)
    else:
        embed.add_field(name="Claimed by", value="Unclaimed", inline=True)

    creator_id = metadata.get("creator_id")
    if creator_id:
        embed.add_field(name="Created by", value=f"<@{creator_id}>", inline=True)

    ticket_type = metadata.get("ticket_type")
    if ticket_type:
        embed.add_field(name="Type", value=str(ticket_type).capitalize(), inline=True)

    embed.set_footer(text=f"Channel: #{channel.name}")
    return embed


def build_closed_controls_embed(channel: discord.TextChannel, metadata: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Ticket Closed",
        description="This ticket has been archived. Support can reopen or delete it.",
        color=discord.Color.dark_grey(),
    )
    embed.add_field(name="Status", value="Closed", inline=True)

    claimed_by_id = metadata.get("claimed_by_id")
    if claimed_by_id:
        embed.add_field(name="Claimed by", value=f"<@{claimed_by_id}>", inline=True)
    else:
        embed.add_field(name="Claimed by", value="Unclaimed", inline=True)

    creator_id = metadata.get("creator_id")
    if creator_id:
        embed.add_field(name="Created by", value=f"<@{creator_id}>", inline=True)

    ticket_type = metadata.get("ticket_type")
    if ticket_type:
        embed.add_field(name="Type", value=str(ticket_type).capitalize(), inline=True)

    embed.set_footer(text=f"Channel: #{channel.name}")
    return embed


def build_support_controls_view(*, closed: bool = False, claimed: bool = False) -> discord.ui.View:
    if closed:
        return ClosedTicketControlsView()
    return SupportControlsView(claimed=claimed)


async def _get_support_role(guild: discord.Guild) -> Optional[discord.Role]:
    _require_storage()
    config = _get_guild_config(guild.id)
    role_id = config.get("support_role")
    return guild.get_role(role_id) if role_id else None


async def _get_closed_category(guild: discord.Guild, support_role: discord.Role) -> discord.CategoryChannel:
    category = discord.utils.get(guild.categories, name=CLOSED_TICKETS_CATEGORY_NAME)
    if category:
        return category

    category = await guild.create_category(
        name=CLOSED_TICKETS_CATEGORY_NAME,
        reason="Create closed tickets category",
        overwrites={
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            support_role: discord.PermissionOverwrite(view_channel=True),
        },
    )
    return category


async def _get_transcripts_channel(guild: discord.Guild, support_role: discord.Role) -> discord.TextChannel:
    channel = discord.utils.get(guild.text_channels, name=TRANSCRIPTS_CHANNEL_NAME)
    if channel:
        return channel

    channel = await guild.create_text_channel(
        name=TRANSCRIPTS_CHANNEL_NAME,
        reason="Create ticket transcripts channel",
        overwrites={
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            support_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        },
    )
    return channel


async def ensure_support_infrastructure(guild: discord.Guild, support_role: discord.Role) -> None:
    await _get_closed_category(guild, support_role)
    await _get_transcripts_channel(guild, support_role)


async def generate_transcript_file(channel: discord.TextChannel) -> discord.File:
    lines: list[str] = []
    async for message in channel.history(limit=None, oldest_first=True):
        timestamp = message.created_at.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        author = f"{message.author} ({message.author.id})"
        content = message.content or ""
        if message.attachments:
            attachments = "\n".join(
                f"    attachment: {attachment.filename} -> {attachment.url}"
                for attachment in message.attachments
            )
            content = f"{content}\n{attachments}".strip()

        if message.embeds:
            content = f"{content}\n    [contains {len(message.embeds)} embed(s)]".strip()

        if not content:
            content = "[no content]"

        lines.append(f"[{timestamp}] {author}: {content}")

    transcript_text = "\n\n".join(lines) if lines else "No messages found in this ticket."
    filename = f"{sanitize_filename(channel.name)}-transcript.txt"
    return discord.File(fp=io.BytesIO(transcript_text.encode("utf-8")), filename=filename)


async def _persist_metadata(channel: discord.TextChannel, metadata: dict) -> None:
    await set_ticket_metadata(channel, metadata)


async def send_ticket_controls_message(channel: discord.TextChannel, metadata: dict) -> discord.Message:
    closed = metadata.get("state") == "closed"
    claimed = bool(metadata.get("claimed_by_id"))
    embed = build_closed_controls_embed(channel, metadata) if closed else build_open_controls_embed(channel, metadata)
    view = build_support_controls_view(closed=closed, claimed=claimed)
    return await channel.send(embed=embed, view=view)


async def refresh_ticket_controls_message(channel: discord.TextChannel) -> None:
    metadata = get_ticket_metadata(channel)
    controls_message_id = metadata.get("controls_message_id")
    if not controls_message_id:
        message = await send_ticket_controls_message(channel, metadata)
        metadata["controls_message_id"] = message.id
        await _persist_metadata(channel, metadata)
        return

    try:
        message = await channel.fetch_message(int(controls_message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        message = await send_ticket_controls_message(channel, metadata)
        metadata["controls_message_id"] = message.id
        await _persist_metadata(channel, metadata)
        return

    closed = metadata.get("state") == "closed"
    claimed = bool(metadata.get("claimed_by_id"))
    embed = build_closed_controls_embed(channel, metadata) if closed else build_open_controls_embed(channel, metadata)
    view = build_support_controls_view(closed=closed, claimed=claimed)
    await message.edit(embed=embed, view=view)


async def _archive_transcript(channel: discord.TextChannel, metadata: dict, support_role: discord.Role) -> None:
    transcript_channel = await _get_transcripts_channel(channel.guild, support_role)
    file = await generate_transcript_file(channel)
    await transcript_channel.send(
        content=f"Created transcript for {channel.mention}",
        file=file,
    )


async def _close_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("This can only be used in a ticket channel.", ephemeral=True)
        return

    metadata = get_ticket_metadata(channel)
    
    # Check if ticket is already closed
    if metadata.get("state") == "closed":
        await interaction.followup.send("This ticket is already closed.", ephemeral=True)
        return

    _require_storage()
    support_role = await _get_support_role(channel.guild)
    if not support_role:
        await interaction.followup.send("Support role not configured.", ephemeral=True)
        return

    creator_id = metadata.get("creator_id")

    metadata["state"] = "closed"
    metadata["closed_at"] = datetime.now(timezone.utc).isoformat()
    metadata["closed_by_id"] = interaction.user.id

    await _archive_transcript(channel, metadata, support_role)

    closed_category = await _get_closed_category(channel.guild, support_role)

    overwrites = {
        channel.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        support_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }

    if creator_id:
        creator = channel.guild.get_member(int(creator_id))
        if creator:
            overwrites[creator] = discord.PermissionOverwrite(
                view_channel=False,
                send_messages=False,
                read_message_history=False,
            )

    await channel.edit(
        category=closed_category,
        overwrites=overwrites,
        reason="Move closed ticket to archive category",
    )

    await _persist_metadata(channel, metadata)
    await refresh_ticket_controls_message(channel)

    await interaction.followup.send("Ticket closed and archived.", ephemeral=True)


async def _reopen_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("This can only be used in a ticket channel.", ephemeral=True)
        return

    metadata = get_ticket_metadata(channel)
    support_role = await _get_support_role(channel.guild)
    if not support_role:
        await interaction.followup.send("Support role not configured.", ephemeral=True)
        return

    creator_id = metadata.get("creator_id")
    original_category_id = metadata.get("original_category_id")

    metadata["state"] = "open"
    metadata.pop("closed_at", None)
    metadata.pop("closed_by_id", None)

    original_category = None
    if original_category_id:
        maybe_category = channel.guild.get_channel(int(original_category_id))
        if isinstance(maybe_category, discord.CategoryChannel):
            original_category = maybe_category

    overwrites = {
        channel.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        support_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }

    if creator_id:
        creator = channel.guild.get_member(int(creator_id))
        if creator:
            overwrites[creator] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

    await channel.edit(
        category=original_category or channel.category,
        overwrites=overwrites,
        reason="Reopen ticket",
    )

    await _persist_metadata(channel, metadata)
    await refresh_ticket_controls_message(channel)

    await interaction.followup.send("Ticket reopened.", ephemeral=True)


async def _delete_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("This can only be used in a ticket channel.", ephemeral=True)
        return

    delete_ticket_metadata(channel)
    await interaction.followup.send("Deleting ticket...", ephemeral=True)
    await channel.delete(reason=f"Ticket deleted by {interaction.user}")


async def _claim_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("This can only be used in a ticket channel.", ephemeral=True)
        return

    metadata = get_ticket_metadata(channel)
    support_role = await _get_support_role(channel.guild)
    if not support_role:
        await interaction.response.send_message("Support role not configured.", ephemeral=True)
        return

    claimed_by_id = metadata.get("claimed_by_id")
    if claimed_by_id and int(claimed_by_id) != interaction.user.id:
        await interaction.response.send_message(
            f"This ticket is already claimed by <@{claimed_by_id}>.",
            ephemeral=True,
        )
        return

    metadata["claimed_by_id"] = interaction.user.id
    metadata["claimed_by_name"] = interaction.user.display_name
    await _persist_metadata(channel, metadata)
    await refresh_ticket_controls_message(channel)

    await interaction.response.send_message("Ticket claimed.", ephemeral=True)


class CloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Close cancelled.", view=None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await _close_ticket(interaction)


class DeleteConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Delete cancelled.", view=None)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await _delete_ticket(interaction)


class SupportControlsView(discord.ui.View):
    def __init__(self, *, claimed: bool = False):
        super().__init__(timeout=None)
        self.claimed = claimed

        claim_button = None
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "support_controls:claim":
                claim_button = item
                break

        if claim_button and claimed:
            claim_button.disabled = True
            claim_button.label = "Claimed"

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="support_controls:close",
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This can only be used in a ticket channel.", ephemeral=True)
            return

        support_role = await _get_support_role(channel.guild)
        if not support_role or not is_support_member(interaction.user, support_role.id):
            await interaction.response.send_message("Only the support team can use this.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Are you sure you want to close this ticket?",
            view=CloseConfirmView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Claim Ticket",
        style=discord.ButtonStyle.secondary,
        custom_id="support_controls:claim",
    )
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This can only be used in a ticket channel.", ephemeral=True)
            return

        support_role = await _get_support_role(channel.guild)
        if not support_role or not is_support_member(interaction.user, support_role.id):
            await interaction.response.send_message("Only the support team can use this.", ephemeral=True)
            return

        await _claim_ticket(interaction)


class ClosedTicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Reopen Ticket",
        style=discord.ButtonStyle.success,
        custom_id="support_controls:reopen",
    )
    async def reopen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This can only be used in a ticket channel.", ephemeral=True)
            return

        support_role = await _get_support_role(channel.guild)
        if not support_role or not is_support_member(interaction.user, support_role.id):
            await interaction.response.send_message("Only the support team can use this.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await _reopen_ticket(interaction)

    @discord.ui.button(
        label="Delete Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="support_controls:delete",
    )
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This can only be used in a ticket channel.", ephemeral=True)
            return

        support_role = await _get_support_role(channel.guild)
        if not support_role or not is_support_member(interaction.user, support_role.id):
            await interaction.response.send_message("Only the support team can use this.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Are you sure you want to permanently delete this ticket?",
            view=DeleteConfirmView(),
            ephemeral=True,
        )


async def hydrate_ticket_controls(bot: discord.Client) -> None:
    _require_storage()
    for guild in bot.guilds:
        for channel in guild.text_channels:
            metadata = get_ticket_metadata(channel)
            if metadata.get("controls_message_id"):
                try:
                    await refresh_ticket_controls_message(channel)
                except Exception:
                    continue