"""
Signal Desktop database importer for Signal TUI.

Reads message history from Signal Desktop's encrypted SQLite database
and imports it into the local message store.
"""

import base64
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

from signal_client import Message
from message_store import MessageStore


class DesktopImportError(Exception):
    """Exception raised for desktop import errors."""
    pass


class SignalDesktopImporter:
    """
    Imports message history from Signal Desktop's database.

    Signal Desktop stores messages in a SQLCipher-encrypted SQLite database.
    The encryption key is stored in the macOS Keychain.
    """

    # Default paths
    SIGNAL_DESKTOP_PATH = Path.home() / "Library" / "Application Support" / "Signal"
    DB_PATH = SIGNAL_DESKTOP_PATH / "sql" / "db.sqlite"
    CONFIG_PATH = SIGNAL_DESKTOP_PATH / "config.json"

    # Keychain identifiers
    KEYCHAIN_SERVICE = "Signal Safe Storage"
    KEYCHAIN_ACCOUNT = "Signal Key"

    def __init__(self, message_store: MessageStore, our_phone_number: str = ""):
        """
        Initialize the importer.

        Args:
            message_store: The MessageStore to import into
            our_phone_number: Our phone number (for determining outgoing messages)
        """
        self.message_store = message_store
        self.our_phone_number = our_phone_number
        self._conn = None
        self._key: Optional[str] = None

    def is_desktop_installed(self) -> bool:
        """Check if Signal Desktop is installed."""
        return self.DB_PATH.exists()

    def _decrypt_safe_storage(self, encrypted_hex: str, password: bytes) -> str:
        """
        Decrypt a value encrypted with Electron's safeStorage API.

        Args:
            encrypted_hex: Hex-encoded encrypted data (with v10/v11 prefix)
            password: The password from keychain

        Returns:
            Decrypted string
        """
        encrypted_data = bytes.fromhex(encrypted_hex)

        # Check version header
        if encrypted_data[:3] == b"v10":
            iterations = 1003  # macOS
        elif encrypted_data[:3] == b"v11":
            iterations = 1  # Linux
        else:
            raise DesktopImportError(f"Unknown encryption version: {encrypted_data[:3]}")

        # Derive key using PBKDF2
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA1(),
            length=16,
            salt=b"saltysalt",
            iterations=iterations,
            backend=default_backend()
        )
        derived_key = kdf.derive(password)

        # Decrypt using AES-128-CBC with IV of 16 spaces
        iv = b" " * 16
        cipher = Cipher(algorithms.AES(derived_key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()

        # Skip the 3-byte version header
        ciphertext = encrypted_data[3:]
        decrypted = decryptor.update(ciphertext) + decryptor.finalize()

        # Remove PKCS#7 padding
        padding_len = decrypted[-1]
        decrypted = decrypted[:-padding_len]

        return decrypted.decode("utf-8")

    def get_key_from_config(self) -> Optional[str]:
        """
        Retrieve the database encryption key from config.json.

        Returns:
            The hex-encoded encryption key, or None if not found

        Raises:
            DesktopImportError: If config exists but key retrieval fails
        """
        if not self.CONFIG_PATH.exists():
            return None

        try:
            with open(self.CONFIG_PATH, "r") as f:
                config = json.load(f)

            # Try plain key first (old Signal Desktop versions)
            key = config.get("key")
            if key and all(c in "0123456789abcdef" for c in key.lower()):
                return key

            # Try encrypted key (Signal Desktop 7.17+)
            encrypted_key = config.get("encryptedKey")
            if encrypted_key:
                # Get password from keychain to decrypt
                keychain_password = self._get_keychain_password()
                return self._decrypt_safe_storage(encrypted_key, keychain_password)

            return None

        except (json.JSONDecodeError, IOError) as e:
            raise DesktopImportError(f"Failed to read config.json: {e}")

    def _get_keychain_password(self) -> bytes:
        """Get the raw password from macOS Keychain for safeStorage decryption."""
        try:
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-s", self.KEYCHAIN_SERVICE,
                    "-a", self.KEYCHAIN_ACCOUNT,
                    "-w"
                ],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                raise DesktopImportError(
                    f"Failed to retrieve key from Keychain: {result.stderr.strip()}"
                )

            # Password should be used as raw UTF-8 bytes (not base64 decoded)
            password = result.stdout.strip()
            return password.encode("utf-8")

        except subprocess.TimeoutExpired:
            raise DesktopImportError("Keychain access timed out")
        except Exception as e:
            raise DesktopImportError(f"Error accessing Keychain: {e}")

    def get_key_from_keychain(self) -> str:
        """
        Retrieve the database encryption key from macOS Keychain.

        Returns:
            The hex-encoded encryption key

        Raises:
            DesktopImportError: If key retrieval fails
        """
        try:
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-s", self.KEYCHAIN_SERVICE,
                    "-a", self.KEYCHAIN_ACCOUNT,
                    "-w"
                ],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                raise DesktopImportError(
                    f"Failed to retrieve key from Keychain: {result.stderr.strip()}"
                )

            # Key is base64 encoded
            key_b64 = result.stdout.strip()
            # Decode to hex for SQLCipher
            key_bytes = base64.b64decode(key_b64)
            key_hex = key_bytes.hex()
            return key_hex

        except subprocess.TimeoutExpired:
            raise DesktopImportError("Keychain access timed out")
        except Exception as e:
            raise DesktopImportError(f"Error accessing Keychain: {e}")

    def get_encryption_key(self) -> str:
        """
        Get the encryption key, trying config.json first, then Keychain.

        Returns:
            The hex-encoded encryption key

        Raises:
            DesktopImportError: If key retrieval fails from all sources
        """
        # Try config.json first (newer Signal Desktop versions)
        key = self.get_key_from_config()
        if key:
            return key

        # Fall back to Keychain (older versions or macOS-specific)
        return self.get_key_from_keychain()

    def connect(self) -> None:
        """
        Connect to the Signal Desktop database.

        Raises:
            DesktopImportError: If connection fails
        """
        if not self.is_desktop_installed():
            raise DesktopImportError(
                "Signal Desktop not found. Please install Signal Desktop first."
            )

        try:
            # Import pysqlcipher3
            try:
                from pysqlcipher3 import dbapi2 as sqlcipher
            except ImportError:
                raise DesktopImportError(
                    "pysqlcipher3 not installed. Run: pip install -e ."
                )

            # Get encryption key
            if self._key is None:
                self._key = self.get_encryption_key()

            # Connect to database
            self._conn = sqlcipher.connect(str(self.DB_PATH))
            cursor = self._conn.cursor()

            # Set the encryption key (hex format for raw key)
            cursor.execute(f"PRAGMA key = \"x'{self._key}'\"")

            # Verify connection by querying
            cursor.execute("SELECT count(*) FROM sqlite_master")

        except Exception as e:
            if "file is not a database" in str(e).lower():
                raise DesktopImportError(
                    "Failed to decrypt database. The encryption key may be incorrect."
                )
            raise DesktopImportError(f"Failed to connect to database: {e}")

    def disconnect(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_conversations(self) -> list[dict]:
        """
        Get all conversations from Signal Desktop.

        Returns:
            List of conversation dicts with id, name, type, etc.
        """
        if not self._conn:
            self.connect()

        cursor = self._conn.cursor()

        # Query conversations table
        cursor.execute("""
            SELECT
                id,
                e164,
                groupId,
                type,
                name,
                profileName,
                profileFamilyName
            FROM conversations
            WHERE (e164 IS NOT NULL OR groupId IS NOT NULL)
        """)

        conversations = []
        for row in cursor.fetchall():
            conv_id = row[1] if row[1] else row[2]  # e164 or groupId
            if not conv_id:
                continue

            is_group = row[3] == "group"
            name = row[4] or ""
            if not name and row[5]:
                name = row[5]
                if row[6]:
                    name = f"{name} {row[6]}"

            conversations.append({
                "id": conv_id,
                "internal_id": row[0],
                "name": name or conv_id,
                "is_group": is_group
            })

        return conversations

    def get_messages_for_conversation(
        self,
        conversation_internal_id: str,
        limit: Optional[int] = None
    ) -> list[dict]:
        """
        Get messages for a specific conversation.

        Args:
            conversation_internal_id: The internal conversation ID
            limit: Maximum number of messages to retrieve (None for all)

        Returns:
            List of message dicts
        """
        if not self._conn:
            self.connect()

        cursor = self._conn.cursor()

        # Query messages table
        if limit is not None:
            cursor.execute("""
                SELECT
                    id,
                    conversationId,
                    source,
                    type,
                    body,
                    sent_at,
                    received_at,
                    hasAttachments,
                    json
                FROM messages
                WHERE conversationId = ?
                ORDER BY sent_at ASC
                LIMIT ?
            """, (conversation_internal_id, limit))
        else:
            cursor.execute("""
                SELECT
                    id,
                    conversationId,
                    source,
                    type,
                    body,
                    sent_at,
                    received_at,
                    hasAttachments,
                    json
                FROM messages
                WHERE conversationId = ?
                ORDER BY sent_at ASC
            """, (conversation_internal_id,))

        messages = []
        for row in cursor.fetchall():
            # Parse attachments from JSON if present
            attachments = []
            if row[7] and row[8]:  # hasAttachments and json
                try:
                    msg_json = json.loads(row[8])
                    for att in msg_json.get("attachments", []):
                        attachments.append({
                            "contentType": att.get("contentType", ""),
                            "filename": att.get("fileName", ""),
                            "size": att.get("size", 0),
                        })
                except (json.JSONDecodeError, TypeError):
                    pass

            messages.append({
                "id": row[0],
                "conversation_id": row[1],
                "source": row[2],
                "type": row[3],  # "incoming" or "outgoing"
                "body": row[4] or "",
                "sent_at": row[5],
                "received_at": row[6],
                "attachments": attachments
            })

        return messages

    def import_all(
        self,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> tuple[int, int]:
        """
        Import all messages from Signal Desktop.

        Args:
            progress_callback: Optional callback for progress updates
                               Called with (conversation_name, current, total)

        Returns:
            Tuple of (conversations_imported, messages_imported)
        """
        if not self._conn:
            self.connect()

        conversations = self.get_conversations()
        total_convs = len(conversations)
        total_messages = 0
        conv_count = 0

        for i, conv in enumerate(conversations):
            if progress_callback:
                progress_callback(conv["name"], i + 1, total_convs)

            # Get all messages for this conversation
            messages = self.get_messages_for_conversation(conv["internal_id"])

            # Convert to Message objects and prepare for bulk insert
            messages_to_insert = []
            for msg_data in messages:
                # Determine if outgoing
                is_outgoing = msg_data["type"] == "outgoing"

                # Create timestamp
                timestamp_ms = msg_data["sent_at"] or msg_data["received_at"] or 0
                if timestamp_ms:
                    timestamp = datetime.fromtimestamp(timestamp_ms / 1000)
                else:
                    timestamp = datetime.now()

                msg = Message(
                    sender=msg_data["source"] or (self.our_phone_number if is_outgoing else ""),
                    sender_name="You" if is_outgoing else "",
                    body=msg_data["body"],
                    timestamp=timestamp,
                    is_outgoing=is_outgoing,
                    group_id=conv["id"] if conv["is_group"] else "",
                    attachments=msg_data["attachments"],
                    is_read=True  # Mark imported messages as read
                )

                messages_to_insert.append((conv["id"], msg))

            # Bulk insert messages
            if messages_to_insert:
                inserted = self.message_store.bulk_insert_messages(messages_to_insert)
                total_messages += inserted

                # Ensure conversation exists
                self.message_store.ensure_conversation(
                    conv["id"],
                    conv["name"],
                    conv["is_group"]
                )

                # Update conversation metadata from imported messages
                self.message_store.update_conversation_from_messages(conv["id"])

            conv_count += 1

        return conv_count, total_messages

    def get_stats(self) -> dict:
        """
        Get statistics about the Signal Desktop database.

        Returns:
            Dict with conversation_count, message_count
        """
        if not self._conn:
            self.connect()

        cursor = self._conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM conversations WHERE e164 IS NOT NULL OR groupId IS NOT NULL")
        conv_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM messages")
        msg_count = cursor.fetchone()[0]

        return {
            "conversation_count": conv_count,
            "message_count": msg_count
        }


def import_from_desktop(
    message_store: MessageStore,
    our_phone_number: str,
    progress_callback: Optional[Callable[[str, int, int], None]] = None
) -> tuple[int, int]:
    """
    Convenience function to import from Signal Desktop.

    Args:
        message_store: The MessageStore to import into
        our_phone_number: Our phone number
        progress_callback: Optional progress callback

    Returns:
        Tuple of (conversations_imported, messages_imported)

    Raises:
        DesktopImportError: If import fails
    """
    importer = SignalDesktopImporter(message_store, our_phone_number)
    try:
        return importer.import_all(progress_callback)
    finally:
        importer.disconnect()
