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

    def __post_init__(self):
        if not self.signal_cli_config_dir:
            self.signal_cli_config_dir = str(
                Path.home() / ".local" / "share" / "signal-cli"
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


class ContactCache:
    """Cache for contacts and groups to speed up app startup."""

    CACHE_FILE = ConfigManager.CONFIG_DIR / "contacts_cache.json"

    def __init__(self):
        self._contacts: list[dict] = []
        self._groups: list[dict] = []
        self._last_updated: str = ""

    def load(self) -> tuple[list[dict], list[dict]]:
        """Load contacts and groups from cache."""
        if self.CACHE_FILE.exists():
            try:
                with open(self.CACHE_FILE, "r") as f:
                    data = json.load(f)
                    self._contacts = data.get("contacts", [])
                    self._groups = data.get("groups", [])
                    self._last_updated = data.get("last_updated", "")
                    return self._contacts, self._groups
            except (json.JSONDecodeError, TypeError):
                pass
        return [], []

    def save(self, contacts: list[dict], groups: list[dict]) -> None:
        """Save contacts and groups to cache."""
        from datetime import datetime
        self._contacts = contacts
        self._groups = groups
        self._last_updated = datetime.now().isoformat()

        # Ensure directory exists
        self.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

        with open(self.CACHE_FILE, "w") as f:
            json.dump({
                "contacts": contacts,
                "groups": groups,
                "last_updated": self._last_updated
            }, f)

    def has_cache(self) -> bool:
        """Check if cache exists."""
        return self.CACHE_FILE.exists()

    @property
    def contacts(self) -> list[dict]:
        return self._contacts

    @property
    def groups(self) -> list[dict]:
        return self._groups


# Global instances
config_manager = ConfigManager()
contact_cache = ContactCache()
