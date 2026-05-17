"""
Bot configuration and settings
Centralized configuration for the Discord ticket bot
"""


class ChannelConfig:
    """Configuration for ticket channels and categories"""
    
    # Ticket system structure: categories and their channels
    TICKET_SYSTEM_STRUCTURE = {
        "Support": {
            "channels": ["create-ticket"],
            "visible_to_all": True
        },
        "Report Tickets": {
            "channels": [],
            "visible_to_all": False
        },
        "General Tickets": {
            "channels": [],
            "visible_to_all": False
        }
    }


class BotConfig:
    """Main bot configuration"""
    
    # Bot identification
    BOT_NAME = "Ticket Bot"
    BOT_VERSION = "2.0.0"
    
    # Timing settings
    PROGRESS_UPDATE_DELAY = 0.5  # Seconds between progress updates during setup


class TicketPromptConfig:
    """Configuration for ticket prompt and types"""
    
    # Default prompt appearance
    DEFAULT_TITLE = "Create a Support Ticket"
    DEFAULT_DESCRIPTION = "Select a ticket type to get started"
    DEFAULT_BUTTON_TEXT = "Create Ticket"
    
    # Default ticket types
    DEFAULT_TICKET_TYPES = [
        {"name": "General Support", "value": "general", "category": "General Tickets"},
        {"name": "Report Member", "value": "report", "category": "Report Tickets"},
    ]


__all__ = [
    "BotConfig",
    "ChannelConfig",
    "TicketPromptConfig",
]