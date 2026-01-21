"""
Configuration management for Signal TUI
"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Config:
    """Application configuration."""

    # signal-cli paths
    signal_cli_path: str = "signal-cli"  # Assumes it's in PATH
    signal_cli_config_dir: str = ""  # Default: ~/.local/share/signal-cli

    # Account
    phone_number: str = ""  # E.164 format: +1234567890

    # UI preferences
    theme: str = "dark"
    show_timestamps: bool = True
    notification_sound: bool = False

    # Daemon settings
    use_daemon: bool = True
    daemon_port: int = 7583

    # Message storage
    messages_db_path: str = ""  # Default: ~/.config/signal-tui/messages.db

    # Auto-import from Signal Desktop on startup
    auto_import_enabled: bool = True

    def __post_init__(self):
        if not self.signal_cli_config_dir:
            self.signal_cli_config_dir = str(
                Path.home() / ".local" / "share" / "signal-cli"
            )
        if not self.messages_db_path:
            self.messages_db_path = str(
                Path.home() / ".config" / "signal-tui" / "messages.db"
            )


class ConfigManager:
    """Manages loading and saving configuration."""

    CONFIG_DIR = Path.home() / ".config" / "signal-tui"
    CONFIG_FILE = CONFIG_DIR / "config.json"

    def __init__(self):
        self.config = Config()
        self._ensure_config_dir()
        self.load()

    def _ensure_config_dir(self) -> None:
        """Create config directory if it doesn't exist."""
        self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def load(self) -> Config:
        """Load configuration from file."""
        if self.CONFIG_FILE.exists():
            try:
                with open(self.CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    self.config = Config(**data)
            except (json.JSONDecodeError, TypeError) as e:
                # Invalid config, use defaults
                pass
        return self.config

    def save(self) -> None:
        """Save configuration to file."""
        with open(self.CONFIG_FILE, "w") as f:
            json.dump(asdict(self.config), f, indent=2)

    def is_configured(self) -> bool:
        """Check if the app has been configured with an account."""
        return bool(self.config.phone_number)

    def set_account(self, phone_number: str) -> None:
        """Set the linked account phone number."""
        self.config.phone_number = phone_number
        self.save()


# Global instance
config_manager = ConfigManager()
