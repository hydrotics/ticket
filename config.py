"""
Bot configuration and settings.
Centralized configuration for the Discord ticket bot.
These values are intentionally editable so you can tailor the template without
touching the command logic.
"""

from __future__ import annotations

from typing import Any


def _build_ticket_system_structure(category_templates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    structure: dict[str, dict[str, Any]] = {}

    for template in category_templates:
        channels = template.get("channels", [])
        normalized_channels: list[str] = []
        for channel in channels:
            if isinstance(channel, dict):
                channel_name = str(channel.get("name", "")).strip()
                if channel_name:
                    normalized_channels.append(channel_name)
            elif isinstance(channel, str) and channel.strip():
                normalized_channels.append(channel.strip())

        structure[str(template["name"])] = {
            "channels": normalized_channels,
            "visible_to_all": bool(template.get("visible_to_all", False)),
            "description": str(template.get("description", "")),
        }

    return structure


class ChannelConfig:
    """
    Editable template for the ticket system infrastructure.

    Change these names here if you want the bot to create different categories
    or channels during `/setup`.
    """

    # Core infrastructure names
    SUPPORT_CATEGORY_NAME = "Support"
    SUPPORT_CHANNEL_NAME = "create-ticket"
    REPORT_CATEGORY_NAME = "Report Tickets"
    GENERAL_CATEGORY_NAME = "General Tickets"

    CLOSED_TICKETS_CATEGORY_NAME = "closed-tickets"
    TRANSCRIPTS_CHANNEL_NAME = "ticket-transcripts"

    # Default guild-configurable channel template used by the config dashboard
    DEFAULT_GUILD_CHANNEL_CONFIG = {
        "support_category_name": SUPPORT_CATEGORY_NAME,
        "support_channel_name": SUPPORT_CHANNEL_NAME,
        "closed_tickets_category_name": CLOSED_TICKETS_CATEGORY_NAME,
        "transcripts_channel_name": TRANSCRIPTS_CHANNEL_NAME,
    }

    # Ticket system structure: categories and their channels
    # Edit this template to change what `/setup` creates.
    CATEGORY_TEMPLATES = [
        {
            "name": SUPPORT_CATEGORY_NAME,
            "visible_to_all": True,
            "description": "Public entry point for users to create support tickets.",
            "channels": [
                {
                    "name": SUPPORT_CHANNEL_NAME,
                    "description": "Channel where the ticket prompt is posted.",
                    "visible_to_all": True,
                }
            ],
        },
        {
            "name": REPORT_CATEGORY_NAME,
            "visible_to_all": False,
            "description": "Private report-handling area.",
            "channels": [],
        },
        {
            "name": GENERAL_CATEGORY_NAME,
            "visible_to_all": False,
            "description": "Private general support area.",
            "channels": [],
        },
    ]

    TICKET_SYSTEM_STRUCTURE = _build_ticket_system_structure(CATEGORY_TEMPLATES)


class BotConfig:
    """Main bot configuration."""

    BOT_NAME = "Ticket Bot"
    BOT_VERSION = "2.1.0"

    # Timing settings
    PROGRESS_UPDATE_DELAY = 0.5  # Seconds between progress updates during setup

    # UI settings
    CONFIG_VIEW_TIMEOUT = 900
    MODAL_TIMEOUT = 900


class TicketPromptConfig:
    """Configuration for ticket prompt and types."""

    # Default prompt appearance
    DEFAULT_TITLE = "Create a Support Ticket"
    DEFAULT_DESCRIPTION = "Select a ticket type to get started"
    DEFAULT_BUTTON_TEXT = "Create Ticket"

    # Default ticket types
    # The category field is the template category name; the actual server
    # category mapping is managed separately by the config command.
    DEFAULT_TICKET_TYPES = [
        {"name": "General Support", "value": "general", "category": ChannelConfig.GENERAL_CATEGORY_NAME},
        {"name": "Report Member", "value": "report", "category": ChannelConfig.REPORT_CATEGORY_NAME},
    ]


__all__ = [
    "BotConfig",
    "ChannelConfig",
    "TicketPromptConfig",
]
