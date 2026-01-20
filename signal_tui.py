#!/usr/bin/env python3
"""
Signal TUI - A terminal user interface for signal-cli
"""

import asyncio
from datetime import datetime
from typing import Optional
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer, Center, Middle
from textual.widgets import (
    Header, Footer, Static, Input, Label, Button, LoadingIndicator, Rule
)
from textual.binding import Binding
from textual.reactive import reactive
from textual.screen import Screen
from textual.worker import Worker, get_current_worker
from rich.text import Text
from rich.panel import Panel
from rich.console import RenderableType
from rich.align import Align

from config import config_manager, Config, contact_cache
from signal_client import SignalClient, AsyncSignalClient, Message, Contact, SignalCliNotFoundError

# QR Code generation
try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False


def generate_qr_code(data: str) -> str:
    """Generate an ASCII QR code using Unicode block characters."""
    if not HAS_QRCODE:
        return "[QR code library not installed]"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(data)
    qr.make(fit=True)

    # Get the QR matrix
    matrix = qr.get_matrix()

    # Use Unicode block characters for compact display
    # Each character represents 2 vertical pixels
    lines = []

    # Process two rows at a time
    for y in range(0, len(matrix), 2):
        line = ""
        for x in range(len(matrix[0])):
            top = matrix[y][x] if y < len(matrix) else False
            bottom = matrix[y + 1][x] if y + 1 < len(matrix) else False

            if top and bottom:
                line += "█"  # Full block
            elif top and not bottom:
                line += "▀"  # Upper half
            elif not top and bottom:
                line += "▄"  # Lower half
            else:
                line += " "  # Empty
        lines.append(line)

    return "\n".join(lines)


# ============================================================================
# Message Components
# ============================================================================

class MessageBubble(Static):
    """A single message bubble."""

    def __init__(self, sender: str, message: str, time: str, is_me: bool = False) -> None:
        super().__init__()
        self.sender = sender
        self.message = message
        self.time = time
        self.is_me = is_me

    def compose(self) -> ComposeResult:
        yield Static(self._render_message())

    def _render_message(self) -> RenderableType:
        align = "right" if self.is_me else "left"
        # Softer, Signal-inspired colors
        name_style = "bold #93c5fd" if self.is_me else "bold #9ca3af"
        # Sent: Signal blue tint, Received: subtle gray
        bubble_style = "on #2563b4" if self.is_me else "on #272730"

        header = Text()
        header.append(f"{self.sender}", style=name_style)
        header.append(f"  {self.time}", style="dim italic #9ca3af")

        content = Text()
        content.append(self.message, style="#f0f0f5")

        return Panel(
            content,
            title=header,
            title_align=align,
            border_style="#3b82f6" if self.is_me else "#373741",
            style=bubble_style,
            padding=(0, 1),
        )


class ContactItem(Static):
    """A contact in the sidebar."""

    can_focus = True  # Allow focus for keyboard navigation

    def __init__(
        self,
        contact_id: str,
        name: str,
        last_message: str,
        time: str,
        unread: int = 0,
        is_group: bool = False
    ) -> None:
        super().__init__()
        self.contact_id = contact_id
        self.contact_name = name
        self.last_message = last_message
        self.time = time
        self.unread = unread
        self.is_group = is_group

    def on_click(self) -> None:
        """Handle click on contact."""
        self.app.open_conversation(self.contact_id, self.contact_name, self.is_group)

    def render(self) -> RenderableType:
        text = Text()

        # Icon and name with unread indicator
        icon = "[G] " if self.is_group else ""
        if self.unread > 0:
            text.append("● ", style="bold #3b82f6")
        text.append(f"{icon}{self.contact_name}\n", style="bold #f0f0f5")

        # Last message preview (truncated)
        preview = self.last_message[:30] + "..." if len(self.last_message) > 30 else self.last_message
        text.append(f"{preview}\n", style="#71717a")

        # Time
        text.append(f"{self.time}", style="italic #52525b")

        return text


class StatusBar(Static):
    """Connection status bar."""

    connected = reactive(False)
    phone_number = reactive("")
    version = reactive("")

    def render(self) -> RenderableType:
        text = Text()
        if self.connected:
            text.append("● ", style="bold #22c55e")
            text.append("Connected", style="#22c55e")
        else:
            text.append("○ ", style="bold #ef4444")
            text.append("Disconnected", style="#ef4444")
        text.append(" │ ", style="#3f3f46")
        text.append(f"{self.version}", style="#71717a")
        if self.phone_number:
            text.append(" │ ", style="#3f3f46")
            text.append(f"Linked to: {self.phone_number}", style="#3b82f6")
        return text


# ============================================================================
# Setup Screen
# ============================================================================

class SetupScreen(Screen):
    """Initial setup screen for linking signal-cli."""

    CSS = """
    /* Setup Screen - Signal-Inspired Theme */
    SetupScreen {
        align: center middle;
        background: rgb(17, 17, 20);
    }

    #setup-container {
        width: 70;
        height: auto;
        background: rgb(24, 24, 30);
        border: round rgb(45, 45, 55);
        padding: 2 4;
    }

    #setup-title {
        text-align: center;
        text-style: bold;
        color: rgb(59, 130, 246);
        margin-bottom: 1;
    }

    #setup-subtitle {
        text-align: center;
        color: rgb(113, 113, 122);
        margin-bottom: 2;
    }

    .setup-section {
        margin: 1 0;
        color: rgb(156, 163, 175);
    }

    .section-header {
        text-style: bold;
        color: rgb(59, 130, 246);
        margin-bottom: 1;
    }

    #qr-container {
        width: 100%;
        height: auto;
        background: rgb(255, 255, 255);
        color: rgb(0, 0, 0);
        padding: 1 2;
        margin: 1 0;
        text-align: center;
        display: none;
    }

    #qr-container.visible {
        display: block;
    }

    #qr-code {
        text-align: center;
        background: rgb(255, 255, 255);
        color: rgb(0, 0, 0);
    }

    #link-uri {
        background: rgb(32, 32, 40);
        padding: 1;
        margin: 1 0;
        max-height: 4;
        overflow: hidden;
    }

    #status-text {
        text-align: center;
        margin: 1 0;
    }

    .action-button {
        width: 100%;
        margin: 1 0;
    }

    #error-text {
        color: rgb(239, 68, 68);
        text-align: center;
        margin: 1 0;
    }

    #success-text {
        color: rgb(34, 197, 94);
        text-align: center;
        margin: 1 0;
    }

    #existing-accounts {
        margin: 1 0;
        padding: 1;
        background: rgb(32, 32, 40);
        border: round rgb(45, 45, 55);
    }

    .account-button {
        margin: 0 1;
    }

    Rule {
        color: rgb(45, 45, 55);
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, signal_client: SignalClient):
        super().__init__()
        self.signal_client = signal_client
        self.link_uri = ""
        self.linking_in_progress = False

    def compose(self) -> ComposeResult:
        with Container(id="setup-container"):
            yield Static("SIGNAL TUI SETUP", id="setup-title")
            yield Static("Link your Signal account to use this client", id="setup-subtitle")
            yield Rule()

            # Check for signal-cli
            if not self.signal_client.is_installed():
                yield Static("signal-cli not found!", id="error-text")
                yield Static(
                    "Please install signal-cli first:\n"
                    "  brew install signal-cli  (macOS)\n"
                    "  or visit: github.com/AsamK/signal-cli",
                    classes="setup-section"
                )
                yield Button("Quit", variant="error", id="quit-btn")
                return

            # Check for existing accounts
            existing = self.signal_client.get_linked_accounts()
            if existing:
                yield Static("Existing Linked Accounts", classes="section-header")
                with Container(id="existing-accounts"):
                    for account in existing:
                        yield Button(
                            f"Use {account}",
                            variant="success",
                            id=f"account-{account}",
                            classes="account-button"
                        )
                yield Rule()

            # Link new account section
            yield Static("Link New Device", classes="section-header")
            yield Static(
                "1. Open Signal on your phone\n"
                "2. Go to Settings > Linked Devices\n"
                "3. Tap '+' to add a new device\n"
                "4. Scan the QR code or enter the link below",
                classes="setup-section"
            )

            yield Button("Generate Link Code", variant="primary", id="generate-link-btn", classes="action-button")

            # QR Code display area (hidden initially)
            with Container(id="qr-container"):
                yield Static("", id="qr-code")

            yield Static("", id="link-uri")
            yield Static("", id="status-text")
            yield LoadingIndicator(id="loading")

    def on_mount(self) -> None:
        try:
            self.query_one("#loading", LoadingIndicator).display = False
        except Exception:
            pass  # Loading indicator may not exist if signal-cli not installed

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id

        if button_id == "quit-btn":
            self.app.exit()

        elif button_id == "generate-link-btn":
            self.start_linking()

        elif button_id and button_id.startswith("account-"):
            phone = button_id.replace("account-", "")
            self.use_existing_account(phone)

    def start_linking(self) -> None:
        """Start the device linking process."""
        if self.linking_in_progress:
            return

        self.linking_in_progress = True
        self.query_one("#loading", LoadingIndicator).display = True
        self.query_one("#status-text", Static).update("Generating link code...")
        self.query_one("#generate-link-btn", Button).disabled = True

        # Run linking in background thread
        import threading
        thread = threading.Thread(target=self._do_linking_thread, daemon=True)
        thread.start()

    def _do_linking_thread(self) -> None:
        """Background thread for device linking."""
        try:
            # Generate link URI (this blocks and waits for scan)
            uri = self.signal_client.generate_link_uri("Signal TUI")

            if uri:
                self.link_uri = uri
                # Use app.call_from_thread for thread-safe UI updates
                self.app.call_from_thread(self.update_link_uri, uri)

                # Wait for user to complete linking
                phone = self.signal_client.wait_for_link(120)

                if phone:
                    self.app.call_from_thread(self.linking_success, phone)
                else:
                    self.app.call_from_thread(self.linking_failed, "Linking timed out or was cancelled")
            else:
                self.app.call_from_thread(self.linking_failed, "Failed to generate link URI")

        except Exception as e:
            self.app.call_from_thread(self.linking_failed, str(e))

    def update_link_uri(self, uri: str) -> None:
        """Update the UI with the link URI and QR code."""
        # Generate and display QR code
        qr_text = generate_qr_code(uri)
        qr_container = self.query_one("#qr-container", Container)
        qr_container.add_class("visible")
        self.query_one("#qr-code", Static).update(qr_text)

        # Show truncated URI as backup
        if len(uri) > 60:
            display_uri = uri[:60] + "..."
        else:
            display_uri = uri
        self.query_one("#link-uri", Static).update(
            f"[dim]Link URI:[/dim] [cyan]{display_uri}[/cyan]"
        )
        self.query_one("#status-text", Static).update(
            "[yellow]Scan the QR code above with Signal on your phone...[/yellow]"
        )

    def linking_success(self, phone: str) -> None:
        """Handle successful linking."""
        self.linking_in_progress = False
        self.query_one("#loading", LoadingIndicator).display = False
        self.query_one("#status-text", Static).update(
            f"[green]Successfully linked to {phone}![/green]"
        )

        # Save config and switch to main screen
        config_manager.set_account(phone)
        self.signal_client.phone_number = phone

        # Dismiss setup screen and reinitialize app after a short delay
        def finish_setup():
            self.app.pop_screen()
            # Reinitialize the main app with new account
            if hasattr(self.app, 'reinitialize'):
                self.app.reinitialize()

        self.set_timer(1.5, finish_setup)

    def linking_failed(self, error: str) -> None:
        """Handle linking failure."""
        self.linking_in_progress = False
        self.query_one("#loading", LoadingIndicator).display = False
        self.query_one("#generate-link-btn", Button).disabled = False
        self.query_one("#status-text", Static).update(f"[red]Error: {error}[/red]")

    def use_existing_account(self, phone: str) -> None:
        """Use an existing linked account."""
        config_manager.set_account(phone)
        self.signal_client.phone_number = phone
        self.app.pop_screen()
        # Reinitialize the main app with new account
        if hasattr(self.app, 'reinitialize'):
            self.app.reinitialize()


# ============================================================================
# Main Chat Screen Components
# ============================================================================

class ContactsList(ScrollableContainer):
    """Scrollable list of contacts with search filtering."""

    selected_index = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.all_contacts: list[tuple] = []  # All contacts (id, name, last_msg, time, unread, is_group)
        self.filtered_contacts: list[tuple] = []  # Currently displayed (filtered) contacts
        self._search_term: str = ""

    def compose(self) -> ComposeResult:
        # Initially empty, populated by load_contacts
        yield Static("Loading contacts...", id="contacts-loading")

    def set_contacts(self, contacts: list[tuple]) -> None:
        """Update the contacts list."""
        self.all_contacts = contacts
        self.filtered_contacts = contacts
        self._search_term = ""
        self._render_contacts()

    def filter_contacts(self, search_term: str) -> None:
        """Filter contacts by search term."""
        self._search_term = search_term.lower().strip()

        if not self._search_term:
            self.filtered_contacts = self.all_contacts
        else:
            self.filtered_contacts = [
                c for c in self.all_contacts
                if (c[1] and self._search_term in c[1].lower())  # c[1] is the name
                or (c[0] and self._search_term in c[0].lower())  # c[0] is the id (phone number)
            ]

        self.selected_index = 0
        self._render_contacts()

    def _render_contacts(self) -> None:
        """Render the current filtered contacts list."""
        # Remove loading message if present
        try:
            loading = self.query_one("#contacts-loading", Static)
            loading.remove()
        except Exception:
            pass

        # Clear existing contacts
        for item in self.query(".contact-item"):
            item.remove()

        # Show message if no contacts match
        if not self.filtered_contacts:
            if self._search_term:
                self.mount(Static(f"No contacts match '{self._search_term}'", classes="no-results"))
            else:
                self.mount(Static("No contacts", classes="no-results"))
            return

        # Remove no-results message if present
        for item in self.query(".no-results"):
            item.remove()

        # Add filtered contacts
        for i, (cid, name, msg, time, unread, is_group) in enumerate(self.filtered_contacts):
            contact = ContactItem(cid, name, msg, time, unread, is_group)
            contact.add_class("contact-item")
            if i == 0:
                contact.add_class("selected")
            self.mount(contact)

    def select_contact(self, index: int) -> None:
        """Select a contact by index."""
        contacts = list(self.query(".contact-item"))
        if 0 <= index < len(contacts):
            for i, contact in enumerate(contacts):
                contact.remove_class("selected")
                if i == index:
                    contact.add_class("selected")
            self.selected_index = index
            self.scroll_to_widget(contacts[index])

    def get_selected_contact(self) -> Optional[tuple]:
        """Get the currently selected contact."""
        if 0 <= self.selected_index < len(self.filtered_contacts):
            return self.filtered_contacts[self.selected_index]
        return None


class ChatMessages(ScrollableContainer):
    """Container for chat messages."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.messages: list[Message] = []

    def compose(self) -> ComposeResult:
        yield Static("Select a conversation to start chatting", classes="centered-text empty-chat-message")

    def set_messages(self, messages: list[Message]) -> None:
        """Update displayed messages."""
        self.messages = messages

        # Clear existing
        self.query("*").remove()

        if not messages:
            self.mount(Static("No messages yet. Send one!", classes="centered-text empty-chat-message"))
            return

        # Group by date
        current_date = None
        for msg in messages:
            msg_date = msg.timestamp.date()
            if msg_date != current_date:
                current_date = msg_date
                if msg_date == datetime.now().date():
                    date_str = "Today"
                else:
                    date_str = msg_date.strftime("%B %d, %Y")
                self.mount(Static(f"─── {date_str} ───", classes="date-divider"))

            bubble = MessageBubble(
                msg.display_sender,
                msg.body,
                msg.timestamp.strftime("%I:%M %p"),
                msg.is_outgoing
            )
            self.mount(bubble)

        # Scroll to bottom
        self.scroll_end(animate=False)

    def add_message(self, msg: Message) -> None:
        """Add a single new message."""
        # Remove "no messages" placeholder if present
        for placeholder in self.query(".empty-chat-message"):
            placeholder.remove()

        bubble = MessageBubble(
            msg.display_sender,
            msg.body,
            msg.timestamp.strftime("%I:%M %p"),
            msg.is_outgoing
        )
        self.mount(bubble)
        self.scroll_end(animate=False)


# ============================================================================
# Main Application
# ============================================================================

class SignalTUI(App):
    """A terminal user interface for signal-cli."""

    CSS = """
    /* Signal-Inspired Modern Dark Theme */
    /* Base: rgb(17, 17, 20) - Darkest */
    /* Surface: rgb(24, 24, 30) - Sidebar */
    /* Elevated: rgb(32, 32, 40) - Cards, inputs */
    /* Hover: rgb(40, 40, 50) - Hover states */
    /* Primary: rgb(59, 130, 246) - Signal blue */

    Screen {
        background: rgb(17, 17, 20);
    }

    #main-container {
        height: 100%;
    }

    /* Sidebar - Clean, minimal */
    #sidebar {
        width: 36;
        background: rgb(24, 24, 30);
        border-right: solid rgb(38, 38, 46);
        padding: 0;
    }

    #sidebar-header {
        height: 3;
        background: rgb(24, 24, 30);
        padding: 1;
        text-align: center;
        border-bottom: solid rgb(38, 38, 46);
    }

    #search-box {
        margin: 1;
        background: rgb(32, 32, 40);
        border: tall rgb(45, 45, 55);
        padding: 0 1;
    }

    #search-box:focus {
        border: tall rgb(59, 130, 246);
    }

    #contacts-list {
        height: 1fr;
        padding: 0;
    }

    /* Contact Items - Borderless cards with accent selection */
    .contact-item {
        height: auto;
        padding: 1 2;
        margin: 0;
        background: transparent;
        border: none;
        border-left: wide transparent;
    }

    .contact-item.selected {
        background: rgba(59, 130, 246, 0.15);
        border-left: wide rgb(59, 130, 246);
    }

    .contact-item:hover {
        background: rgba(255, 255, 255, 0.04);
    }

    /* Chat Area */
    #chat-area {
        height: 100%;
        background: rgb(17, 17, 20);
    }

    #chat-header {
        height: 3;
        background: rgb(24, 24, 30);
        padding: 1 2;
        border-bottom: solid rgb(38, 38, 46);
    }

    #chat-header-name {
        text-style: bold;
        color: rgb(240, 240, 245);
    }

    #chat-header-status {
        color: rgb(59, 130, 246);
        text-style: italic;
        margin-left: 2;
    }

    #messages-container {
        height: 1fr;
        padding: 1 2;
        background: rgb(17, 17, 20);
    }

    .date-divider {
        text-align: center;
        color: rgb(113, 113, 122);
        margin: 1 0;
    }

    .centered-text {
        text-align: center;
        color: rgb(113, 113, 122);
        margin: 2;
    }

    MessageBubble {
        margin: 0 0 1 0;
        width: 100%;
    }

    /* Input Area - Clean and minimal */
    #input-area {
        height: auto;
        min-height: 3;
        background: rgb(24, 24, 30);
        padding: 1;
        border-top: solid rgb(38, 38, 46);
    }

    #message-input {
        background: rgb(32, 32, 40);
        border: tall rgb(45, 45, 55);
        padding: 0 1;
    }

    #message-input:focus {
        border: tall rgb(59, 130, 246);
    }

    /* Status Bar - Compact, subtle */
    #status-bar {
        height: 1;
        background: rgb(20, 20, 26);
        padding: 0 1;
        border-top: solid rgb(32, 32, 40);
    }

    #app-title {
        color: rgb(59, 130, 246);
        text-style: bold;
    }

    /* Footer - Match theme */
    Footer {
        background: rgb(20, 20, 26);
        color: rgb(113, 113, 122);
    }

    LoadingIndicator {
        background: transparent;
    }

    .no-results {
        text-align: center;
        color: rgb(113, 113, 122);
        padding: 2;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+n", "new_chat", "New Chat"),
        Binding("ctrl+s", "search", "Search"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("ctrl+l", "setup", "Link Account"),
        Binding("up", "prev_contact", "Prev", show=False),
        Binding("down", "next_contact", "Next", show=False),
        Binding("escape", "focus_input", "Focus Input", show=False),
        Binding("enter", "select_contact", "Select", show=False),
        Binding("f1", "show_help", "Help"),
    ]

    TITLE = "Signal TUI"
    SUB_TITLE = "Terminal Client for Signal Messenger"

    def __init__(self):
        super().__init__()
        self.config = config_manager.config
        self.signal_client = SignalClient(
            phone_number=self.config.phone_number,
            config_dir=self.config.signal_cli_config_dir,
            cli_path=self.config.signal_cli_path
        )
        self.async_client = AsyncSignalClient(self.signal_client)
        self.current_contact: Optional[str] = None
        self.current_contact_name: str = ""
        self.conversations: dict[str, list[Message]] = {}
        self._receive_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal(id="main-container"):
            # Sidebar
            with Vertical(id="sidebar"):
                yield Static(
                    Text("SIGNAL", style="bold rgb(59, 130, 246)"),
                    id="sidebar-header"
                )
                yield Input(placeholder="Search...", id="search-box")
                yield ContactsList(id="contacts-list")

            # Chat area
            with Vertical(id="chat-area"):
                with Horizontal(id="chat-header"):
                    yield Static("Select a conversation", id="chat-header-name")
                    yield Static("", id="chat-header-status")

                yield ChatMessages(id="messages-container")

                with Container(id="input-area"):
                    yield Input(
                        placeholder="Type a message... (Enter to send)",
                        id="message-input"
                    )

                yield StatusBar(id="status-bar")

        yield Footer()

    def on_mount(self) -> None:
        """Initialize the app on mount."""
        # Update status bar
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.version = self.signal_client.get_version()
        status_bar.phone_number = self.config.phone_number

        # Check if setup is needed
        if not config_manager.is_configured():
            self.push_screen(SetupScreen(self.signal_client))
        else:
            self.reinitialize()

    def reinitialize(self) -> None:
        """Reinitialize the app with current config (called after setup or on refresh)."""
        # Reload config
        config_manager.load()
        self.config = config_manager.config

        # Update signal client with new phone number
        self.signal_client.phone_number = self.config.phone_number

        # Update status bar
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.version = self.signal_client.get_version()
        status_bar.phone_number = self.config.phone_number

        # Verify account and load data
        status_bar.connected = self.signal_client.verify_account()
        if status_bar.connected:
            self.load_contacts()
            self.start_message_receiver()
            self.notify("Connected to Signal!", severity="information")
        else:
            self.notify("Could not connect to Signal. Check signal-cli configuration.", severity="error")

    def load_contacts(self) -> None:
        """Load contacts - from cache first for instant display, then refresh in background."""
        # First, try to load from cache for instant display
        if contact_cache.has_cache():
            cached_contacts, cached_groups = contact_cache.load()
            if cached_contacts or cached_groups:
                contact_list = self._build_contact_list(cached_contacts, cached_groups, from_cache=True)
                self._update_contacts_ui(contact_list)

        # Then refresh from signal-cli in background
        import threading
        thread = threading.Thread(target=self._load_contacts_thread, daemon=True)
        thread.start()

    def _build_contact_list(self, contacts, groups, from_cache: bool = False) -> list:
        """Build contact list for UI from contacts and groups data."""
        contact_list = []

        if from_cache:
            # Contacts from cache are raw dicts
            for item in contacts:
                number = item.get("number") or ""
                name = item.get("name") or item.get("nickName") or ""
                profile_name = item.get("givenName") or ""
                if not profile_name:
                    profile = item.get("profile") or {}
                    if profile:
                        profile_name = profile.get("givenName") or ""
                display_name = name or profile_name or number or "Unknown"

                contact_list.append((
                    number,
                    display_name,
                    "",  # Last message
                    "",  # Time
                    0,   # Unread count
                    False  # Not a group
                ))
        else:
            # Contacts from signal_client are Contact objects
            for contact in contacts:
                contact_list.append((
                    contact.number or "",
                    contact.display_name or "Unknown",
                    "",  # Last message
                    "",  # Time
                    0,   # Unread count
                    False  # Not a group
                ))

        for group in groups:
            group_id = group.get("id", "")
            group_name = group.get("name", "Unknown Group")
            contact_list.append((
                group_id,
                group_name,
                "",
                "",
                0,
                True  # Is a group
            ))

        return contact_list

    def _load_contacts_thread(self) -> None:
        """Background thread to load contacts from signal-cli and update cache."""
        try:
            contacts = self.signal_client.list_contacts()
            groups = self.signal_client.list_groups()

            # Convert Contact objects to dicts for caching
            contacts_for_cache = []
            for contact in contacts:
                contacts_for_cache.append({
                    "number": contact.number,
                    "name": contact.name,
                    "givenName": contact.profile_name,
                    "uuid": contact.uuid,
                    "isBlocked": contact.is_blocked,
                })

            # Save to cache
            contact_cache.save(contacts_for_cache, groups)

            # Build contact list for UI
            contact_list = self._build_contact_list(contacts, groups, from_cache=False)

            # Update UI from thread using call_from_thread
            self.call_from_thread(self._update_contacts_ui, contact_list)

        except Exception as e:
            self.call_from_thread(self.notify, f"Failed to load contacts: {e}", severity="error")

    def _update_contacts_ui(self, contacts: list) -> None:
        """Update contacts list UI."""
        contacts_list = self.query_one("#contacts-list", ContactsList)
        contacts_list.set_contacts(contacts)

        # Update status
        status = self.query_one("#status-bar", StatusBar)
        status.connected = True

    def start_message_receiver(self) -> None:
        """Start background message receiver thread."""
        if self._receive_task is None:
            import threading
            self._running = True
            self._receive_task = threading.Thread(target=self._receive_loop_thread, daemon=True)
            self._receive_task.start()

    def _receive_loop_thread(self) -> None:
        """Background thread to receive messages."""
        import time
        while getattr(self, '_running', True):
            try:
                messages = self.signal_client.receive_messages(timeout=10)
                for msg in messages:
                    self.call_from_thread(self._handle_incoming_message, msg)
            except Exception as e:
                # Don't spam notifications for receive errors
                pass
            time.sleep(1)

    def _handle_incoming_message(self, msg: Message) -> None:
        """Handle an incoming message (called from main thread via call_from_thread)."""
        # Store in conversations
        contact_id = msg.group_id if msg.group_id else msg.sender
        if contact_id not in self.conversations:
            self.conversations[contact_id] = []
        self.conversations[contact_id].append(msg)

        # If this is the current conversation, update UI
        if contact_id == self.current_contact:
            messages_container = self.query_one("#messages-container", ChatMessages)
            messages_container.add_message(msg)

        # Show notification
        self.notify(f"New message from {msg.display_sender}")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle input changes - used for search filtering."""
        if event.input.id == "search-box":
            contacts_list = self.query_one("#contacts-list", ContactsList)
            contacts_list.filter_contacts(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle message submission."""
        if event.input.id == "message-input" and event.value.strip():
            if not self.current_contact:
                self.notify("Select a conversation first", severity="warning")
                return

            message_text = event.value.strip()
            event.input.value = ""

            # Send message in background thread
            import threading
            thread = threading.Thread(
                target=self._send_message_thread,
                args=(message_text,),
                daemon=True
            )
            thread.start()

    def _send_message_thread(self, text: str) -> None:
        """Send a message in a background thread."""
        if not self.current_contact:
            return

        # Determine if it's a group (signal-cli uses base64 encoded group IDs)
        is_group = not self.current_contact.startswith("+")

        success = self.signal_client.send_message(
            self.current_contact,
            text,
            group=is_group
        )

        if success:
            # Add to local conversation
            msg = Message(
                sender=self.config.phone_number,
                sender_name="You",
                body=text,
                timestamp=datetime.now(),
                is_outgoing=True
            )

            if self.current_contact not in self.conversations:
                self.conversations[self.current_contact] = []
            self.conversations[self.current_contact].append(msg)

            # Update UI
            self.call_from_thread(self._add_sent_message, msg)
        else:
            self.call_from_thread(
                self.notify,
                "Failed to send message",
                severity="error"
            )

    def _add_sent_message(self, msg: Message) -> None:
        """Add a sent message to the UI."""
        messages_container = self.query_one("#messages-container", ChatMessages)
        messages_container.add_message(msg)

    def action_quit(self) -> None:
        """Quit the application."""
        self._running = False  # Signal receive thread to stop
        self.exit()

    def action_new_chat(self) -> None:
        """Start a new chat."""
        self.notify("New chat: Enter phone number in search box", title="New Chat")
        self.query_one("#search-box", Input).focus()

    def action_search(self) -> None:
        """Focus search box."""
        self.query_one("#search-box", Input).focus()

    def action_focus_input(self) -> None:
        """Focus message input."""
        self.query_one("#message-input", Input).focus()

    def action_prev_contact(self) -> None:
        """Select previous contact."""
        contacts_list = self.query_one("#contacts-list", ContactsList)
        new_index = max(0, contacts_list.selected_index - 1)
        contacts_list.select_contact(new_index)

    def action_next_contact(self) -> None:
        """Select next contact."""
        contacts_list = self.query_one("#contacts-list", ContactsList)
        new_index = min(len(contacts_list.filtered_contacts) - 1, contacts_list.selected_index + 1)
        contacts_list.select_contact(new_index)

    def action_select_contact(self) -> None:
        """Select the highlighted contact and open conversation."""
        contacts_list = self.query_one("#contacts-list", ContactsList)
        contact = contacts_list.get_selected_contact()
        if contact:
            self.open_conversation(contact[0], contact[1], contact[5])

    def open_conversation(self, contact_id: str, name: str, is_group: bool = False) -> None:
        """Open a conversation."""
        self.current_contact = contact_id
        self.current_contact_name = name

        # Update header
        self.query_one("#chat-header-name", Static).update(name)
        status = "group" if is_group else "direct message"
        self.query_one("#chat-header-status", Static).update(f"● {status}")

        # Load messages for this conversation
        messages = self.conversations.get(contact_id, [])
        messages_container = self.query_one("#messages-container", ChatMessages)
        messages_container.set_messages(messages)

        # Focus input
        self.query_one("#message-input", Input).focus()

    def action_refresh(self) -> None:
        """Refresh contacts and messages."""
        self.load_contacts()
        self.notify("Refreshing...")

    def action_setup(self) -> None:
        """Open setup screen."""
        self.push_screen(SetupScreen(self.signal_client))

    def action_show_help(self) -> None:
        """Show help."""
        self.notify(
            "Ctrl+N: New Chat\n"
            "Ctrl+S: Search\n"
            "Ctrl+R: Refresh\n"
            "Ctrl+L: Link Account\n"
            "Up/Down: Navigate contacts\n"
            "Enter: Select/Send\n"
            "Ctrl+Q: Quit",
            title="Keyboard Shortcuts",
            timeout=10
        )


def main():
    app = SignalTUI()
    app.run()


if __name__ == "__main__":
    main()
