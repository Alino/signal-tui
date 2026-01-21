"""Tests for Signal Desktop database import."""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

from desktop_import import SignalDesktopImporter, DesktopImportError


def encrypt_for_safe_storage(plaintext: str, password: bytes, version: bytes = b"v10") -> str:
    """Encrypt a string using Electron safeStorage format (for test fixtures)."""
    iterations = 1003 if version == b"v10" else 1

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=iterations,
        backend=default_backend()
    )
    derived_key = kdf.derive(password)

    iv = b" " * 16
    cipher = Cipher(algorithms.AES(derived_key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()

    # PKCS#7 padding
    plaintext_bytes = plaintext.encode("utf-8")
    padding_len = 16 - (len(plaintext_bytes) % 16)
    padded = plaintext_bytes + bytes([padding_len] * padding_len)

    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return (version + ciphertext).hex()


class TestSafeStorageDecryption:
    """Tests for Electron safeStorage decryption."""

    def test_decrypt_v10_macos(self):
        """Test decryption of v10 (macOS) encrypted key."""
        password = b"test-keychain-password"
        original_key = "6a354a76f7f51505ba3a36c64faec812abcd1234"

        encrypted = encrypt_for_safe_storage(original_key, password, b"v10")

        importer = SignalDesktopImporter(MagicMock(), "")
        decrypted = importer._decrypt_safe_storage(encrypted, password)

        assert decrypted == original_key

    def test_decrypt_v11_linux(self):
        """Test decryption of v11 (Linux) encrypted key."""
        password = b"linux-password"
        original_key = "abcdef1234567890abcdef1234567890"

        encrypted = encrypt_for_safe_storage(original_key, password, b"v11")

        importer = SignalDesktopImporter(MagicMock(), "")
        decrypted = importer._decrypt_safe_storage(encrypted, password)

        assert decrypted == original_key

    def test_decrypt_unknown_version_raises(self):
        """Test that unknown version header raises error."""
        importer = SignalDesktopImporter(MagicMock(), "")

        # Create data with invalid header
        bad_data = b"v99" + b"\x00" * 32

        with pytest.raises(DesktopImportError, match="Unknown encryption version"):
            importer._decrypt_safe_storage(bad_data.hex(), b"password")

    def test_decrypt_with_various_key_lengths(self):
        """Test decryption works with different key lengths."""
        password = b"my-password"

        importer = SignalDesktopImporter(MagicMock(), "")

        for key_len in [32, 64, 128]:
            original_key = "a" * key_len
            encrypted = encrypt_for_safe_storage(original_key, password)
            decrypted = importer._decrypt_safe_storage(encrypted, password)
            assert decrypted == original_key


class TestGetKeyFromConfig:
    """Tests for config.json key retrieval."""

    def test_plain_key_old_format(self, tmp_path):
        """Test reading plain key from old Signal Desktop versions."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"key": "abcdef1234567890"}')

        importer = SignalDesktopImporter(MagicMock(), "")
        importer.CONFIG_PATH = config_path

        key = importer.get_key_from_config()
        assert key == "abcdef1234567890"

    def test_encrypted_key_new_format(self, tmp_path):
        """Test reading and decrypting encryptedKey from Signal 7.17+."""
        password = b"keychain-password"
        original_key = "decryptedkey1234567890abcdef"
        encrypted = encrypt_for_safe_storage(original_key, password)

        config_path = tmp_path / "config.json"
        config_path.write_text(f'{{"encryptedKey": "{encrypted}"}}')

        importer = SignalDesktopImporter(MagicMock(), "")
        importer.CONFIG_PATH = config_path

        with patch.object(importer, '_get_keychain_password', return_value=password):
            key = importer.get_key_from_config()

        assert key == original_key

    def test_no_config_returns_none(self, tmp_path):
        """Test that missing config.json returns None."""
        importer = SignalDesktopImporter(MagicMock(), "")
        importer.CONFIG_PATH = tmp_path / "nonexistent.json"

        assert importer.get_key_from_config() is None

    def test_invalid_plain_key_ignored(self, tmp_path):
        """Test that non-hex plain key is ignored."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"key": "not-valid-hex-XYZ!"}')

        importer = SignalDesktopImporter(MagicMock(), "")
        importer.CONFIG_PATH = config_path

        # No encryptedKey and invalid plain key should return None
        key = importer.get_key_from_config()
        assert key is None


class TestGetKeychainPassword:
    """Tests for macOS Keychain access."""

    def test_keychain_password_encoded_as_utf8(self):
        """Test that keychain password is encoded as UTF-8 bytes."""
        importer = SignalDesktopImporter(MagicMock(), "")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "quT6ckDFhrSFn3M2kIxj\n"

        with patch('subprocess.run', return_value=mock_result):
            password = importer._get_keychain_password()

        # Should be raw UTF-8 encoded, not base64 decoded
        assert password == b"quT6ckDFhrSFn3M2kIxj"

    def test_keychain_failure_raises(self):
        """Test that keychain access failure raises DesktopImportError."""
        importer = SignalDesktopImporter(MagicMock(), "")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "security: SecKeychainSearchCopyNext: The specified item could not be found"

        with patch('subprocess.run', return_value=mock_result):
            with pytest.raises(DesktopImportError, match="Failed to retrieve key"):
                importer._get_keychain_password()


class TestGetEncryptionKey:
    """Tests for the key retrieval orchestration."""

    def test_prefers_config_over_keychain(self, tmp_path):
        """Test that config.json key is preferred over direct keychain."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"key": "abcdef123456789000"}')

        importer = SignalDesktopImporter(MagicMock(), "")
        importer.CONFIG_PATH = config_path

        with patch.object(importer, 'get_key_from_keychain', return_value="fedcba987654"):
            key = importer.get_encryption_key()

        assert key == "abcdef123456789000"

    def test_falls_back_to_keychain(self, tmp_path):
        """Test fallback to keychain when config.json has no key."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{}')

        importer = SignalDesktopImporter(MagicMock(), "")
        importer.CONFIG_PATH = config_path

        with patch.object(importer, 'get_key_from_keychain', return_value="keychain-key-abc"):
            key = importer.get_encryption_key()

        assert key == "keychain-key-abc"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
