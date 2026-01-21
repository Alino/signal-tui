"""
Message persistence layer for Signal TUI.

Stores messages and conversations in an encrypted SQLite database.
Uses SQLCipher for encryption with the key stored in macOS Keychain.
"""

import base64
import json
import os
import secrets
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from signal_client import Message


# Keychain configuration for encryption key storage
KEYCHAIN_SERVICE = "Signal TUI Safe Storage"
KEYCHAIN_ACCOUNT = "Signal TUI Key"


class KeychainError(Exception):
    """Exception raised for Keychain access errors."""
    pass


def _get_key_from_keychain() -> Optional[str]:
    """
    Retrieve the database encryption key from macOS Keychain.

    Returns:
        The hex-encoded encryption key, or None if not found.
    """
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", KEYCHAIN_ACCOUNT,
                "-w"
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return None

        # Key is stored as base64
        key_b64 = result.stdout.strip()
        key_bytes = base64.b64decode(key_b64)
        return key_bytes.hex()

    except (subprocess.TimeoutExpired, Exception):
        return None


def _store_key_in_keychain(key_hex: str) -> bool:
    """
    Store the database encryption key in macOS Keychain.

    Args:
        key_hex: The hex-encoded encryption key

    Returns:
        True if successful, False otherwise.
    """
    try:
        # Convert hex to base64 for storage
        key_bytes = bytes.fromhex(key_hex)
        key_b64 = base64.b64encode(key_bytes).decode('utf-8')

        # Delete existing key if present (ignore errors)
        subprocess.run(
            [
                "security", "delete-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", KEYCHAIN_ACCOUNT
            ],
            capture_output=True,
            timeout=10
        )

        # Add new key
        result = subprocess.run(
            [
                "security", "add-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", KEYCHAIN_ACCOUNT,
                "-w", key_b64,
                "-U"  # Update if exists
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        return result.returncode == 0

    except (subprocess.TimeoutExpired, Exception):
        return False


def _generate_encryption_key() -> str:
    """
    Generate a new random encryption key.

    Returns:
        A 32-byte hex-encoded key (256 bits).
    """
    return secrets.token_hex(32)


def get_or_create_encryption_key() -> str:
    """
    Get existing encryption key from Keychain, or create and store a new one.

    Returns:
        The hex-encoded encryption key.

    Raises:
        KeychainError: If key cannot be retrieved or stored.
    """
    # Try to get existing key
    key = _get_key_from_keychain()
    if key:
        return key

    # Generate new key
    key = _generate_encryption_key()

    # Store in keychain
    if not _store_key_in_keychain(key):
        raise KeychainError(
            "Failed to store encryption key in Keychain. "
            "Please ensure Keychain access is available."
        )

    return key


@dataclass
class StoredConversation:
    """Represents a stored conversation."""
    id: str  # phone number or group ID
    name: str
    type: str  # 'private' or 'group'
    last_message: str
    last_message_at: Optional[int]  # timestamp in ms
    unread_count: int


class MessageStore:
    """SQLCipher-encrypted message persistence layer."""

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Path):
        """
        Initialize the message store.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        self._ensure_db_dir()
        self._conn = None
        self._encryption_key: Optional[str] = None
        self._init_db()

    def _ensure_db_dir(self) -> None:
        """Create database directory if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_conn(self):
        """Get encrypted database connection (lazy initialization)."""
        if self._conn is None:
            # Import pysqlcipher3 for encrypted database
            try:
                from pysqlcipher3 import dbapi2 as sqlcipher
            except ImportError:
                raise ImportError(
                    "pysqlcipher3 is required for encrypted message storage. "
                    "Install it with: brew install sqlcipher && pip install pysqlcipher3"
                )

            # Get or create encryption key
            if self._encryption_key is None:
                self._encryption_key = get_or_create_encryption_key()

            # Connect to database
            self._conn = sqlcipher.connect(str(self.db_path), check_same_thread=False)

            # Set encryption key
            cursor = self._conn.cursor()
            cursor.execute(f"PRAGMA key = \"x'{self._encryption_key}'\"")

            # Verify the key works (will fail if wrong key or unencrypted db)
            try:
                cursor.execute("SELECT count(*) FROM sqlite_master")
            except Exception as e:
                self._conn.close()
                self._conn = None
                raise RuntimeError(
                    f"Failed to open encrypted database. "
                    f"If you have an old unencrypted database, delete it: {self.db_path}"
                ) from e

            # Set row factory for dict-like access
            self._conn.row_factory = sqlcipher.Row

        return self._conn

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Create messages table
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

        # Create conversations table
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

        # Create contacts table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                number TEXT PRIMARY KEY,
                name TEXT,
                profile_name TEXT,
                uuid TEXT,
                is_blocked INTEGER DEFAULT 0
            )
        """)

        # Create groups table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id TEXT PRIMARY KEY,
                name TEXT,
                data_json TEXT
            )
        """)

        # Create cache metadata table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Create indexes for faster queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conv
            ON messages(conversationId, sent_at DESC)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_source
            ON messages(source)
        """)

        # Create schema version table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)

        # Check/set schema version
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (self.SCHEMA_VERSION,))

        conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ========================================================================
    # Message Operations
    # ========================================================================

    def save_message(self, message: Message, conversation_id: str) -> int:
        """
        Save a message to the database.

        Args:
            message: The Message object to save
            conversation_id: The conversation ID (phone number or group ID)

        Returns:
            The row ID of the inserted message
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        sent_at = int(message.timestamp.timestamp() * 1000)
        received_at = int(datetime.now().timestamp() * 1000)
        msg_type = "outgoing" if message.is_outgoing else "incoming"

        attachments_json = json.dumps(message.attachments) if message.attachments else None

        try:
            cursor.execute("""
                INSERT OR IGNORE INTO messages
                (conversationId, source, body, sent_at, received_at, type, hasAttachments, attachments_json, isRead)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                conversation_id,
                message.sender,
                message.body,
                sent_at,
                received_at,
                msg_type,
                1 if message.attachments else 0,
                attachments_json,
                1 if message.is_read or message.is_outgoing else 0
            ))
            conn.commit()

            # Update conversation
            self._update_conversation_on_message(conversation_id, message)

            return cursor.lastrowid
        except sqlite3.Error:
            return -1

    def get_messages(
        self,
        conversation_id: str,
        limit: int = 100,
        before_timestamp: Optional[int] = None
    ) -> list[Message]:
        """
        Get messages for a conversation.

        Args:
            conversation_id: The conversation ID
            limit: Maximum number of messages to return
            before_timestamp: Only return messages before this timestamp (ms)

        Returns:
            List of Message objects, oldest first
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        if before_timestamp:
            cursor.execute("""
                SELECT * FROM messages
                WHERE conversationId = ? AND sent_at < ?
                ORDER BY sent_at DESC
                LIMIT ?
            """, (conversation_id, before_timestamp, limit))
        else:
            cursor.execute("""
                SELECT * FROM messages
                WHERE conversationId = ?
                ORDER BY sent_at DESC
                LIMIT ?
            """, (conversation_id, limit))

        rows = cursor.fetchall()

        messages = []
        for row in reversed(rows):  # Reverse to get chronological order
            attachments = json.loads(row["attachments_json"]) if row["attachments_json"] else []
            msg = Message(
                sender=row["source"] or "",
                sender_name="",  # Will be populated by caller
                body=row["body"] or "",
                timestamp=datetime.fromtimestamp(row["sent_at"] / 1000) if row["sent_at"] else datetime.now(),
                is_outgoing=(row["type"] == "outgoing"),
                group_id=conversation_id if not conversation_id.startswith("+") else "",
                attachments=attachments,
                is_read=bool(row["isRead"])
            )
            messages.append(msg)

        return messages

    def mark_messages_read(self, conversation_id: str) -> None:
        """Mark all messages in a conversation as read."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE messages SET isRead = 1
            WHERE conversationId = ? AND isRead = 0
        """, (conversation_id,))
        conn.commit()

        # Update unread count in conversation
        cursor.execute("""
            UPDATE conversations SET unreadCount = 0
            WHERE id = ?
        """, (conversation_id,))
        conn.commit()

    def get_unread_count(self, conversation_id: str) -> int:
        """Get the number of unread messages in a conversation."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM messages
            WHERE conversationId = ? AND isRead = 0 AND type = 'incoming'
        """, (conversation_id,))
        return cursor.fetchone()[0]

    def search_messages(self, query: str, limit: int = 50) -> list[tuple[str, Message]]:
        """
        Search messages across all conversations.

        Args:
            query: Search query string
            limit: Maximum number of results

        Returns:
            List of (conversation_id, Message) tuples
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        # Use LIKE for simple text search
        search_pattern = f"%{query}%"
        cursor.execute("""
            SELECT * FROM messages
            WHERE body LIKE ?
            ORDER BY sent_at DESC
            LIMIT ?
        """, (search_pattern, limit))

        results = []
        for row in cursor.fetchall():
            attachments = json.loads(row["attachments_json"]) if row["attachments_json"] else []
            msg = Message(
                sender=row["source"] or "",
                sender_name="",
                body=row["body"] or "",
                timestamp=datetime.fromtimestamp(row["sent_at"] / 1000) if row["sent_at"] else datetime.now(),
                is_outgoing=(row["type"] == "outgoing"),
                attachments=attachments,
                is_read=bool(row["isRead"])
            )
            results.append((row["conversationId"], msg))

        return results

    # ========================================================================
    # Conversation Operations
    # ========================================================================

    def _update_conversation_on_message(self, conversation_id: str, message: Message) -> None:
        """Update conversation metadata when a message is added."""
        conn = self._get_conn()
        cursor = conn.cursor()

        sent_at = int(message.timestamp.timestamp() * 1000)
        is_group = not conversation_id.startswith("+")
        conv_type = "group" if is_group else "private"

        # Get existing conversation
        cursor.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        existing = cursor.fetchone()

        if existing:
            # Update if this message is newer
            if existing["lastMessageAt"] is None or sent_at >= existing["lastMessageAt"]:
                new_unread = existing["unreadCount"]
                if not message.is_outgoing and not message.is_read:
                    new_unread += 1

                cursor.execute("""
                    UPDATE conversations
                    SET lastMessage = ?, lastMessageAt = ?, unreadCount = ?
                    WHERE id = ?
                """, (message.body[:100] if message.body else "[Attachment]", sent_at, new_unread, conversation_id))
        else:
            # Create new conversation
            unread = 0 if message.is_outgoing else 1
            cursor.execute("""
                INSERT INTO conversations (id, name, type, lastMessage, lastMessageAt, unreadCount)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                conversation_id,
                message.sender_name or conversation_id,
                conv_type,
                message.body[:100] if message.body else "[Attachment]",
                sent_at,
                unread
            ))

        conn.commit()

    def get_conversation(self, conversation_id: str) -> Optional[StoredConversation]:
        """Get a conversation by ID."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        row = cursor.fetchone()

        if row:
            return StoredConversation(
                id=row["id"],
                name=row["name"] or row["id"],
                type=row["type"] or "private",
                last_message=row["lastMessage"] or "",
                last_message_at=row["lastMessageAt"],
                unread_count=row["unreadCount"] or 0
            )
        return None

    def get_all_conversations(self) -> list[StoredConversation]:
        """Get all conversations, sorted by last message time."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM conversations
            ORDER BY lastMessageAt DESC NULLS LAST
        """)

        conversations = []
        for row in cursor.fetchall():
            conversations.append(StoredConversation(
                id=row["id"],
                name=row["name"] or row["id"],
                type=row["type"] or "private",
                last_message=row["lastMessage"] or "",
                last_message_at=row["lastMessageAt"],
                unread_count=row["unreadCount"] or 0
            ))

        return conversations

    def update_conversation_name(self, conversation_id: str, name: str) -> None:
        """Update the display name for a conversation."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE conversations SET name = ?
            WHERE id = ?
        """, (name, conversation_id))
        conn.commit()

    def ensure_conversation(self, conversation_id: str, name: str, is_group: bool) -> None:
        """Ensure a conversation exists in the database."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        if cursor.fetchone() is None:
            conv_type = "group" if is_group else "private"
            cursor.execute("""
                INSERT INTO conversations (id, name, type, lastMessage, lastMessageAt, unreadCount)
                VALUES (?, ?, ?, '', NULL, 0)
            """, (conversation_id, name, conv_type))
            conn.commit()

    def update_conversation_from_messages(self, conversation_id: str) -> None:
        """Update conversation metadata from stored messages."""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Get the most recent message for this conversation
        cursor.execute("""
            SELECT body, sent_at FROM messages
            WHERE conversationId = ?
            ORDER BY sent_at DESC
            LIMIT 1
        """, (conversation_id,))

        row = cursor.fetchone()
        if row:
            last_message = row["body"][:100] if row["body"] else "[Attachment]"
            last_message_at = row["sent_at"]

            cursor.execute("""
                UPDATE conversations
                SET lastMessage = ?, lastMessageAt = ?
                WHERE id = ? AND (lastMessageAt IS NULL OR lastMessageAt < ?)
            """, (last_message, last_message_at, conversation_id, last_message_at))
            conn.commit()

    # ========================================================================
    # Bulk Operations (for import)
    # ========================================================================

    def bulk_insert_messages(self, messages: list[tuple[str, Message]]) -> int:
        """
        Bulk insert messages from import.

        Args:
            messages: List of (conversation_id, Message) tuples

        Returns:
            Number of messages inserted
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        inserted = 0
        for conversation_id, message in messages:
            sent_at = int(message.timestamp.timestamp() * 1000)
            received_at = sent_at  # Use sent_at for imported messages
            msg_type = "outgoing" if message.is_outgoing else "incoming"
            attachments_json = json.dumps(message.attachments) if message.attachments else None

            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO messages
                    (conversationId, source, body, sent_at, received_at, type, hasAttachments, attachments_json, isRead)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    conversation_id,
                    message.sender,
                    message.body,
                    sent_at,
                    received_at,
                    msg_type,
                    1 if message.attachments else 0,
                    attachments_json,
                    1  # Mark imported messages as read
                ))
                if cursor.rowcount > 0:
                    inserted += 1
            except sqlite3.Error:
                continue

        conn.commit()
        return inserted

    def get_message_count(self) -> int:
        """Get total number of messages in the database."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages")
        return cursor.fetchone()[0]

    def get_conversation_count(self) -> int:
        """Get total number of conversations in the database."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM conversations")
        return cursor.fetchone()[0]

    # ========================================================================
    # Contact Cache Operations
    # ========================================================================

    def save_contacts(self, contacts: list[dict]) -> None:
        """
        Save contacts to the database cache.

        Args:
            contacts: List of contact dicts with number, name, profile_name, uuid, is_blocked
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        for contact in contacts:
            number = contact.get("number") or ""
            if not number:
                continue

            cursor.execute("""
                INSERT OR REPLACE INTO contacts (number, name, profile_name, uuid, is_blocked)
                VALUES (?, ?, ?, ?, ?)
            """, (
                number,
                contact.get("name") or contact.get("nickName") or "",
                contact.get("givenName") or contact.get("profile_name") or "",
                contact.get("uuid") or "",
                1 if contact.get("isBlocked") else 0
            ))

        # Update cache timestamp
        cursor.execute("""
            INSERT OR REPLACE INTO cache_meta (key, value)
            VALUES ('contacts_updated', ?)
        """, (datetime.now().isoformat(),))

        conn.commit()

    def get_contacts(self) -> list[dict]:
        """
        Get all cached contacts.

        Returns:
            List of contact dicts
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contacts")

        contacts = []
        for row in cursor.fetchall():
            contacts.append({
                "number": row["number"],
                "name": row["name"],
                "givenName": row["profile_name"],
                "uuid": row["uuid"],
                "isBlocked": bool(row["is_blocked"])
            })

        return contacts

    def save_groups(self, groups: list[dict]) -> None:
        """
        Save groups to the database cache.

        Args:
            groups: List of group dicts with id, name, and other data
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        for group in groups:
            group_id = group.get("id") or ""
            if not group_id:
                continue

            cursor.execute("""
                INSERT OR REPLACE INTO groups (id, name, data_json)
                VALUES (?, ?, ?)
            """, (
                group_id,
                group.get("name") or "Unknown Group",
                json.dumps(group)
            ))

        # Update cache timestamp
        cursor.execute("""
            INSERT OR REPLACE INTO cache_meta (key, value)
            VALUES ('groups_updated', ?)
        """, (datetime.now().isoformat(),))

        conn.commit()

    def get_groups(self) -> list[dict]:
        """
        Get all cached groups.

        Returns:
            List of group dicts
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM groups")

        groups = []
        for row in cursor.fetchall():
            if row["data_json"]:
                try:
                    group = json.loads(row["data_json"])
                    groups.append(group)
                except json.JSONDecodeError:
                    groups.append({"id": row["id"], "name": row["name"]})
            else:
                groups.append({"id": row["id"], "name": row["name"]})

        return groups

    def has_contact_cache(self) -> bool:
        """Check if contact cache exists."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM contacts")
        return cursor.fetchone()[0] > 0

    def get_cache_timestamp(self) -> Optional[str]:
        """Get the timestamp of the last cache update."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM cache_meta WHERE key = 'contacts_updated'")
        row = cursor.fetchone()
        return row["value"] if row else None
