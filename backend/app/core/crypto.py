"""Application-level credential encryption using Fernet symmetric encryption with key rotation."""

import base64
import logging

from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class CredentialEncryptionService:
    """Provides secure encryption, decryption, and key rotation for third-party credentials."""

    def __init__(self) -> None:
        settings = get_settings()
        fernet_keys = settings.fernet_keys_list
        try:
            self._multi_fernet = MultiFernet([Fernet(key) for key in fernet_keys])
        except Exception as exc:
            logger.error("Failed to initialize MultiFernet encryption engine", exc_info=True)
            raise RuntimeError("Credential encryption initialization failed.") from exc

    def encrypt(self, plain_text: str) -> str:
        """Encrypt plain text credential string into a secure ciphertext token."""
        if not plain_text:
            return ""
        encrypted_bytes = self._multi_fernet.encrypt(plain_text.encode("utf-8"))
        return encrypted_bytes.decode("utf-8")

    def decrypt(self, cipher_text: str) -> str:
        """Decrypt ciphertext token back into plain text credential string."""
        if not cipher_text:
            return ""
        try:
            decrypted_bytes = self._multi_fernet.decrypt(cipher_text.encode("utf-8"))
            return decrypted_bytes.decode("utf-8")
        except Exception as exc:
            logger.error("Failed to decrypt ciphertext credential", exc_info=True)
            raise ValueError("Decryption failed or ciphertext is invalid/corrupted.") from exc

    def rotate(self, cipher_text: str) -> str:
        """Re-encrypt ciphertext using the primary (newest) key if rotated."""
        if not cipher_text:
            return ""
        plain_text = self.decrypt(cipher_text)
        return self.encrypt(plain_text)


def get_crypto_service() -> CredentialEncryptionService:
    """Return a singleton instance of CredentialEncryptionService."""
    return CredentialEncryptionService()


def derive_fernet_key_from_secret(secret: str, salt: bytes = b"ai-sdlc-salt-v1") -> str:
    """Derive a valid 32-url-safe-base64 Fernet key from a passphrase string."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))
    return key.decode("utf-8")