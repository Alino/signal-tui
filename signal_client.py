"""
Signal-cli wrapper for Signal TUI

Handles all communication with signal-cli, supporting both
command-line invocation and JSON-RPC daemon mode.
"""

import asyncio
import json
import subprocess
import shutil
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, AsyncIterator
from pathlib import Path
import threading
import queue


@dataclass
class Contact:
    """Represents a Signal contact."""
    number: str
    name: str = ""
    profile_name: str = ""
    uuid: str = ""
    is_blocked: bool = False

    @property
    def display_name(self) -> str:
        return self.name or self.profile_name or self.number


@dataclass
class Message:
    """Represents a Signal message."""
    sender: str
    sender_name: str
    body: str
    timestamp: datetime
    is_outgoing: bool = False
    group_id: str = ""
    group_name: str = ""
    attachments: list = field(default_factory=list)
    is_read: bool = False

    @property
    def display_sender(self) -> str:
        if self.is_outgoing:
            return "You"
        return self.sender_name or self.sender


@dataclass
class Conversation:
    """Represents a conversation (contact or group)."""
    id: str  # Phone number or group ID
    name: str
    is_group: bool = False
    last_message: str = ""
    last_message_time: Optional[datetime] = None
    unread_count: int = 0
    messages: list = field(default_factory=list)


class SignalCliError(Exception):
    """Exception raised for signal-cli errors."""
    pass


class SignalCliNotFoundError(SignalCliError):
    """Exception raised when signal-cli is not installed."""
    pass


class SignalClient:
    """
    Client for interacting with signal-cli.

    Supports both direct command invocation and JSON-RPC daemon mode.
    """

    def __init__(self, phone_number: str = "", config_dir: str = "", cli_path: str = "signal-cli"):
        self.phone_number = phone_number
        self.config_dir = config_dir or str(Path.home() / ".local" / "share" / "signal-cli")
        self.cli_path = cli_path
        self._daemon_process: Optional[subprocess.Popen] = None
        self._message_callbacks: list[Callable] = []
        self._message_queue: queue.Queue = queue.Queue()
        self._receive_thread: Optional[threading.Thread] = None
        self._running = False

        # Cache
        self._contacts: dict[str, Contact] = {}
        self._conversations: dict[str, Conversation] = {}

    def is_installed(self) -> bool:
        """Check if signal-cli is installed and accessible."""
        return shutil.which(self.cli_path) is not None

    def get_version(self) -> str:
        """Get signal-cli version."""
        try:
            result = subprocess.run(
                [self.cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return "unknown"

    def _run_command(self, args: list[str], timeout: int = 30) -> dict:
        """Run a signal-cli command and return parsed JSON output."""
        cmd = [self.cli_path]

        if self.config_dir:
            cmd.extend(["--config", self.config_dir])

        if self.phone_number:
            cmd.extend(["-u", self.phone_number])

        cmd.extend(["--output=json"])
        cmd.extend(args)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                raise SignalCliError(f"Command failed: {error_msg}")

            if result.stdout.strip():
                # Handle multiple JSON objects (one per line)
                lines = result.stdout.strip().split('\n')
                if len(lines) == 1:
                    return json.loads(lines[0])
                return [json.loads(line) for line in lines if line.strip()]

            return {}

        except subprocess.TimeoutExpired:
            raise SignalCliError("Command timed out")
        except json.JSONDecodeError as e:
            raise SignalCliError(f"Invalid JSON response: {e}")
        except FileNotFoundError:
            raise SignalCliNotFoundError("signal-cli not found. Please install it first.")

    def get_linked_accounts(self) -> list[str]:
        """Get list of phone numbers linked to signal-cli."""
        data_dir = Path(self.config_dir) / "data"
        if not data_dir.exists():
            return []

        accounts = []
        for item in data_dir.iterdir():
            if item.is_file() and item.suffix == "":
                # Account files are named with the phone number
                name = item.name
                if name.startswith("+"):
                    accounts.append(name)
        return accounts

    def generate_link_uri(self, device_name: str = "Signal TUI") -> str:
        """
        Generate a linking URI for connecting to an existing account.

        Returns the URI that should be displayed as a QR code.
        """
        cmd = [self.cli_path]
        if self.config_dir:
            cmd.extend(["--config", self.config_dir])
        cmd.extend(["link", "-n", device_name])

        # This command outputs the URI to stdout and waits for linking
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Read the URI (first line of output)
        uri_line = process.stdout.readline().strip()

        # Store the process so we can wait for linking to complete
        self._link_process = process

        return uri_line

    def wait_for_link(self, timeout: int = 60) -> Optional[str]:
        """
        Wait for the linking process to complete.

        Returns the phone number if successful, None if failed/timeout.
        """
        if not hasattr(self, '_link_process'):
            return None

        try:
            stdout, stderr = self._link_process.communicate(timeout=timeout)

            if self._link_process.returncode == 0:
                # Parse the phone number from output
                # Output format varies, look for the phone number
                for line in (stdout + stderr).split('\n'):
                    if '+' in line:
                        # Extract phone number
                        import re
                        match = re.search(r'\+\d+', line)
                        if match:
                            return match.group()
                # If linking worked, check for new accounts
                accounts = self.get_linked_accounts()
                if accounts:
                    return accounts[0]
            return None
        except subprocess.TimeoutExpired:
            self._link_process.kill()
            return None
        finally:
            self._link_process = None

    def list_contacts(self) -> list[Contact]:
        """Get all contacts."""
        try:
            result = self._run_command(["listContacts"])

            contacts = []
            if isinstance(result, list):
                for item in result:
                    # Get profile name from givenName or profile.givenName
                    profile_name = item.get("givenName") or ""
                    if not profile_name:
                        profile = item.get("profile", {})
                        if profile:
                            profile_name = profile.get("givenName") or ""

                    contact = Contact(
                        number=item.get("number", ""),
                        name=item.get("name", "") or item.get("nickName", ""),
                        profile_name=profile_name,
                        uuid=item.get("uuid", ""),
                        is_blocked=item.get("isBlocked", False)
                    )
                    contacts.append(contact)
                    self._contacts[contact.number] = contact

            return contacts
        except SignalCliError as e:
            print(f"Error listing contacts: {e}", file=sys.stderr)
            return []

    def list_groups(self) -> list[dict]:
        """Get all groups."""
        try:
            result = self._run_command(["listGroups", "-d"])
            if isinstance(result, list):
                return result
            return []
        except SignalCliError:
            return []

    def send_message(self, recipient: str, message: str, group: bool = False) -> bool:
        """
        Send a message to a contact or group.

        Args:
            recipient: Phone number or group ID
            message: Message text
            group: If True, recipient is a group ID
        """
        try:
            args = ["send", "-m", message]
            if group:
                args.extend(["-g", recipient])
            else:
                args.append(recipient)

            self._run_command(args)
            return True
        except SignalCliError as e:
            print(f"Failed to send message: {e}", file=sys.stderr)
            return False

    def receive_messages(self, timeout: int = 5) -> list[Message]:
        """
        Receive pending messages.

        This is a blocking call that waits for messages.
        """
        try:
            result = self._run_command(["receive", "--timeout", str(timeout)], timeout=timeout + 10)

            messages = []
            items = result if isinstance(result, list) else [result] if result else []

            for item in items:
                envelope = item.get("envelope", {})

                # Skip non-data messages
                data_message = envelope.get("dataMessage")
                if not data_message:
                    continue

                sender = envelope.get("source", "")
                timestamp_ms = envelope.get("timestamp", 0)

                message = Message(
                    sender=sender,
                    sender_name=self._contacts.get(sender, Contact(sender)).display_name,
                    body=data_message.get("message", ""),
                    timestamp=datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else datetime.now(),
                    is_outgoing=False,
                    group_id=data_message.get("groupInfo", {}).get("groupId", ""),
                )
                messages.append(message)

            return messages
        except SignalCliError:
            return []

    def start_receive_daemon(self, callback: Callable[[Message], None]) -> None:
        """
        Start a background thread to continuously receive messages.

        Args:
            callback: Function to call when a message is received
        """
        if self._running:
            return

        self._running = True
        self._message_callbacks.append(callback)

        def receive_loop():
            while self._running:
                try:
                    messages = self.receive_messages(timeout=10)
                    for msg in messages:
                        for cb in self._message_callbacks:
                            cb(msg)
                except Exception as e:
                    if self._running:
                        print(f"Receive error: {e}", file=sys.stderr)
                        asyncio.get_event_loop().call_later(5, lambda: None)  # Wait before retry

        self._receive_thread = threading.Thread(target=receive_loop, daemon=True)
        self._receive_thread.start()

    def stop_receive_daemon(self) -> None:
        """Stop the receive daemon."""
        self._running = False
        if self._receive_thread:
            self._receive_thread.join(timeout=5)
            self._receive_thread = None

    def get_contact_name(self, number: str) -> str:
        """Get display name for a contact."""
        if number in self._contacts:
            return self._contacts[number].display_name
        return number

    def trust_identity(self, number: str) -> bool:
        """Trust a contact's identity (safety number)."""
        try:
            self._run_command(["trust", "-a", number])
            return True
        except SignalCliError:
            return False

    def verify_account(self) -> bool:
        """Verify that the account is properly configured."""
        if not self.phone_number:
            return False

        try:
            # Try to list contacts as a simple verification
            self._run_command(["listContacts"])
            return True
        except SignalCliError:
            return False

    def sync_contacts(self) -> bool:
        """Request contact sync from the primary device."""
        try:
            self._run_command(["sendSyncRequest"])
            return True
        except SignalCliError:
            return False


# Async wrapper for use with Textual
class AsyncSignalClient:
    """Async wrapper around SignalClient for use with Textual."""

    def __init__(self, client: SignalClient):
        self.client = client

    async def send_message(self, recipient: str, message: str, group: bool = False) -> bool:
        """Send a message asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.client.send_message, recipient, message, group)

    async def receive_messages(self, timeout: int = 5) -> list[Message]:
        """Receive messages asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.client.receive_messages, timeout)

    async def list_contacts(self) -> list[Contact]:
        """List contacts asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.client.list_contacts)

    async def list_groups(self) -> list[dict]:
        """List groups asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.client.list_groups)
