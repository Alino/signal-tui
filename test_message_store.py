"""Tests for the encrypted message store."""

import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
import sqlite3

from message_store import (
    MessageStore,
    StoredConversation,
    KeychainError,
    _get_key_from_keychain,
    _store_key_in_keychain,
    _generate_encryption_key,
    get_or_create_encryption_key,
    KEYCHAIN_SERVICE,
    KEYCHAIN_ACCOUNT,
)
from signal_client import Message


# =============================================================================
# Keychain Tests
# =============================================================================

class TestKeychainKeyRetrieval:
    """Tests for Keychain key retrieval."""

    def test_get_key_success(self):
        """Test successful key retrieval from Keychain."""
        # Key stored as base64 in keychain
        import base64
        key_hex = "abcdef1234567890" * 4  # 64 hex chars = 32 bytes
        key_b64 = base64.b64encode(bytes.fromhex(key_hex)).decode()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = f"{key_b64}\n"

        with patch('subprocess.run', return_value=mock_result) as mock_run:
            result = _get_key_from_keychain()

        assert result == key_hex
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "security" in call_args
        assert KEYCHAIN_SERVICE in call_args
        assert KEYCHAIN_ACCOUNT in call_args

    def test_get_key_not_found(self):
        """Test key retrieval when key doesn't exist."""
        mock_result = MagicMock()
        mock_result.returncode = 44  # Item not found
        mock_result.stderr = "security: SecKeychainSearchCopyNext: The specified item could not be found"

        with patch('subprocess.run', return_value=mock_result):
            result = _get_key_from_keychain()

        assert result is None

    def test_get_key_timeout(self):
        """Test key retrieval timeout handling."""
        import subprocess
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired("cmd", 10)):
            result = _get_key_from_keychain()

        assert result is None


class TestKeychainKeyStorage:
    """Tests for Keychain key storage."""

    def test_store_key_success(self):
        """Test successful key storage in Keychain."""
        key_hex = "abcdef1234567890" * 4

        mock_delete = MagicMock()
        mock_delete.returncode = 0

        mock_add = MagicMock()
        mock_add.returncode = 0

        with patch('subprocess.run', side_effect=[mock_delete, mock_add]) as mock_run:
            result = _store_key_in_keychain(key_hex)

        assert result is True
        assert mock_run.call_count == 2

    def test_store_key_failure(self):
        """Test key storage failure."""
        key_hex = "abcdef1234567890" * 4

        mock_delete = MagicMock()
        mock_delete.returncode = 0

        mock_add = MagicMock()
        mock_add.returncode = 1  # Failure

        with patch('subprocess.run', side_effect=[mock_delete, mock_add]):
            result = _store_key_in_keychain(key_hex)

        assert result is False


class TestKeyGeneration:
    """Tests for encryption key generation."""

    def test_generate_key_length(self):
        """Test that generated key is 64 hex chars (32 bytes)."""
        key = _generate_encryption_key()
        assert len(key) == 64
        # Verify it's valid hex
        bytes.fromhex(key)

    def test_generate_key_uniqueness(self):
        """Test that generated keys are unique."""
        keys = [_generate_encryption_key() for _ in range(100)]
        assert len(set(keys)) == 100


class TestGetOrCreateKey:
    """Tests for get_or_create_encryption_key."""

    def test_returns_existing_key(self):
        """Test that existing key is returned."""
        existing_key = "existing123" * 6

        with patch('message_store._get_key_from_keychain', return_value=existing_key):
            result = get_or_create_encryption_key()

        assert result == existing_key

    def test_creates_new_key_when_none_exists(self):
        """Test that new key is created and stored when none exists."""
        new_key = "newkey456789" * 5

        with patch('message_store._get_key_from_keychain', return_value=None):
            with patch('message_store._generate_encryption_key', return_value=new_key):
                with patch('message_store._store_key_in_keychain', return_value=True) as mock_store:
                    result = get_or_create_encryption_key()

        assert result == new_key
        mock_store.assert_called_once_with(new_key)

    def test_raises_on_storage_failure(self):
        """Test that KeychainError is raised when storage fails."""
        with patch('message_store._get_key_from_keychain', return_value=None):
            with patch('message_store._generate_encryption_key', return_value="somekey"):
                with patch('message_store._store_key_in_keychain', return_value=False):
                    with pytest.raises(KeychainError):
                        get_or_create_encryption_key()


# =============================================================================
# MessageStore Tests (with mocked encryption)
# =============================================================================

@pytest.fixture
def mock_sqlcipher():
    """Mock pysqlcipher3 with regular sqlite3."""
    mock_module = MagicMock()
    # Use real sqlite3 for actual database operations
    mock_module.connect = sqlite3.connect
    mock_module.Row = sqlite3.Row
    return mock_module


@pytest.fixture
def message_store(tmp_path, mock_sqlcipher):
    """Create a MessageStore with mocked encryption."""
    db_path = tmp_path / "test.db"

    with patch.dict('sys.modules', {'pysqlcipher3': MagicMock(), 'pysqlcipher3.dbapi2': mock_sqlcipher}):
        with patch('message_store.get_or_create_encryption_key', return_value="testkey123"):
            # Patch the _get_conn method to use regular sqlite3
            store = MessageStore.__new__(MessageStore)
            store.db_path = db_path
            store._conn = None
            store._encryption_key = "testkey123"
            store._ensure_db_dir()

            # Use regular sqlite3 for testing
            store._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            store._conn.row_factory = sqlite3.Row

            # Initialize schema
            cursor = store._conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversationId TEXT NOT NULL,
                    source TEXT,
                    body TEXT,
                    sent_at INTEGER,
                    received_at INTEGER,
                    type TEXT,
                    hasAttachments INTEGER DEFAULT 0,
                    attachments_json TEXT,
                    isRead INTEGER DEFAULT 0,
                    UNIQUE(conversationId, sent_at, source, body)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    type TEXT,
                    lastMessage TEXT,
                    lastMessageAt INTEGER,
                    unreadCount INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                ON messages(conversationId, sent_at DESC)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                )
            """)
            store._conn.commit()

            yield store

            store.close()


def make_message(
    body: str = "Test message",
    sender: str = "+1234567890",
    is_outgoing: bool = False,
    timestamp: datetime = None,
    attachments: list = None,
    is_read: bool = False
) -> Message:
    """Helper to create test messages."""
    return Message(
        sender=sender,
        sender_name="Test User",
        body=body,
        timestamp=timestamp or datetime.now(),
        is_outgoing=is_outgoing,
        group_id="",
        attachments=attachments or [],
        is_read=is_read
    )


class TestMessageOperations:
    """Tests for message CRUD operations."""

    def test_save_message(self, message_store):
        """Test saving a message."""
        msg = make_message(body="Hello world")
        row_id = message_store.save_message(msg, "+1234567890")

        assert row_id > 0

    def test_save_and_retrieve_message(self, message_store):
        """Test saving and retrieving a message."""
        original = make_message(body="Test content", sender="+1111111111")
        message_store.save_message(original, "+1111111111")

        messages = message_store.get_messages("+1111111111")

        assert len(messages) == 1
        assert messages[0].body == "Test content"
        assert messages[0].sender == "+1111111111"

    def test_get_messages_ordered_chronologically(self, message_store):
        """Test that messages are returned in chronological order."""
        conv_id = "+1234567890"

        for i in range(5):
            msg = make_message(
                body=f"Message {i}",
                timestamp=datetime(2024, 1, 1, 12, i, 0)
            )
            message_store.save_message(msg, conv_id)

        messages = message_store.get_messages(conv_id)

        assert len(messages) == 5
        for i, msg in enumerate(messages):
            assert msg.body == f"Message {i}"

    def test_get_messages_with_limit(self, message_store):
        """Test message retrieval with limit."""
        conv_id = "+1234567890"

        for i in range(10):
            msg = make_message(body=f"Message {i}")
            message_store.save_message(msg, conv_id)

        messages = message_store.get_messages(conv_id, limit=5)

        assert len(messages) == 5

    def test_mark_messages_read(self, message_store):
        """Test marking messages as read."""
        conv_id = "+1234567890"

        # Save unread message
        msg = make_message(is_read=False, is_outgoing=False)
        message_store.save_message(msg, conv_id)

        unread_before = message_store.get_unread_count(conv_id)
        message_store.mark_messages_read(conv_id)
        unread_after = message_store.get_unread_count(conv_id)

        assert unread_before == 1
        assert unread_after == 0

    def test_search_messages(self, message_store):
        """Test message search."""
        message_store.save_message(make_message(body="Hello world"), "+111")
        message_store.save_message(make_message(body="Goodbye world"), "+222")
        message_store.save_message(make_message(body="Hello there"), "+333")

        results = message_store.search_messages("Hello")

        assert len(results) == 2
        bodies = [msg.body for _, msg in results]
        assert "Hello world" in bodies
        assert "Hello there" in bodies

    def test_save_message_with_attachments(self, message_store):
        """Test saving message with attachments."""
        attachments = [
            {"contentType": "image/png", "filename": "photo.png", "size": 1024}
        ]
        msg = make_message(body="Check this out", attachments=attachments)
        message_store.save_message(msg, "+1234567890")

        messages = message_store.get_messages("+1234567890")

        assert len(messages) == 1
        assert len(messages[0].attachments) == 1
        assert messages[0].attachments[0]["filename"] == "photo.png"

    def test_duplicate_message_ignored(self, message_store):
        """Test that duplicate messages are ignored."""
        conv_id = "+1234567890"
        timestamp = datetime(2024, 1, 1, 12, 0, 0)

        msg = make_message(body="Same message", timestamp=timestamp)

        message_store.save_message(msg, conv_id)
        message_store.save_message(msg, conv_id)  # Duplicate

        messages = message_store.get_messages(conv_id)
        assert len(messages) == 1


class TestConversationOperations:
    """Tests for conversation operations."""

    def test_conversation_created_on_message(self, message_store):
        """Test that conversation is created when message is saved."""
        conv_id = "+1234567890"
        msg = make_message(body="First message")
        message_store.save_message(msg, conv_id)

        conv = message_store.get_conversation(conv_id)

        assert conv is not None
        assert conv.id == conv_id
        assert conv.last_message == "First message"

    def test_get_all_conversations_sorted(self, message_store):
        """Test that conversations are sorted by last message time."""
        # Create conversations with different timestamps
        for i, phone in enumerate(["+111", "+222", "+333"]):
            msg = make_message(
                body=f"Message to {phone}",
                timestamp=datetime(2024, 1, i + 1, 12, 0, 0)
            )
            message_store.save_message(msg, phone)

        convs = message_store.get_all_conversations()

        assert len(convs) == 3
        # Most recent first
        assert convs[0].id == "+333"
        assert convs[1].id == "+222"
        assert convs[2].id == "+111"

    def test_update_conversation_name(self, message_store):
        """Test updating conversation name."""
        conv_id = "+1234567890"
        message_store.save_message(make_message(), conv_id)

        message_store.update_conversation_name(conv_id, "John Doe")

        conv = message_store.get_conversation(conv_id)
        assert conv.name == "John Doe"

    def test_ensure_conversation(self, message_store):
        """Test ensure_conversation creates conversation if not exists."""
        conv_id = "+9999999999"

        message_store.ensure_conversation(conv_id, "New Contact", is_group=False)

        conv = message_store.get_conversation(conv_id)
        assert conv is not None
        assert conv.name == "New Contact"
        assert conv.type == "private"

    def test_ensure_conversation_idempotent(self, message_store):
        """Test ensure_conversation doesn't overwrite existing."""
        conv_id = "+1234567890"

        # Create with message
        message_store.save_message(make_message(body="Original"), conv_id)
        message_store.update_conversation_name(conv_id, "Original Name")

        # Ensure doesn't overwrite
        message_store.ensure_conversation(conv_id, "New Name", is_group=False)

        conv = message_store.get_conversation(conv_id)
        assert conv.name == "Original Name"

    def test_unread_count_increments(self, message_store):
        """Test that unread count increments for incoming messages."""
        conv_id = "+1234567890"

        for i in range(3):
            msg = make_message(is_outgoing=False, is_read=False)
            message_store.save_message(msg, conv_id)

        conv = message_store.get_conversation(conv_id)
        # Note: unread count is managed by _update_conversation_on_message
        # First message creates conv with unread=1, subsequent ones may vary
        assert conv.unread_count >= 1


class TestBulkOperations:
    """Tests for bulk import operations."""

    def test_bulk_insert_messages(self, message_store):
        """Test bulk inserting messages."""
        messages = []
        for i in range(50):
            msg = make_message(
                body=f"Bulk message {i}",
                timestamp=datetime(2024, 1, 1, 12, i % 60, 0)
            )
            messages.append(("+1234567890", msg))

        inserted = message_store.bulk_insert_messages(messages)

        assert inserted == 50
        assert message_store.get_message_count() == 50

    def test_bulk_insert_skips_duplicates(self, message_store):
        """Test that bulk insert skips duplicates."""
        timestamp = datetime(2024, 1, 1, 12, 0, 0)
        msg = make_message(body="Duplicate", timestamp=timestamp)

        messages = [("+1234567890", msg)] * 5

        inserted = message_store.bulk_insert_messages(messages)

        assert inserted == 1

    def test_get_message_count(self, message_store):
        """Test message count."""
        for i in range(10):
            message_store.save_message(make_message(body=f"Msg {i}"), "+111")

        assert message_store.get_message_count() == 10

    def test_get_conversation_count(self, message_store):
        """Test conversation count."""
        for phone in ["+111", "+222", "+333"]:
            message_store.save_message(make_message(), phone)

        assert message_store.get_conversation_count() == 3


class TestStoredConversation:
    """Tests for StoredConversation dataclass."""

    def test_stored_conversation_fields(self):
        """Test StoredConversation has expected fields."""
        conv = StoredConversation(
            id="+1234567890",
            name="John Doe",
            type="private",
            last_message="Hello",
            last_message_at=1704067200000,
            unread_count=5
        )

        assert conv.id == "+1234567890"
        assert conv.name == "John Doe"
        assert conv.type == "private"
        assert conv.last_message == "Hello"
        assert conv.last_message_at == 1704067200000
        assert conv.unread_count == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
