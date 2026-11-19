"""Unit tests for Fernet credential encryption service and key derivation."""


from app.core.crypto import CredentialEncryptionService, derive_fernet_key_from_secret


def test_encryption_and_decryption_roundtrip() -> None:
    service = CredentialEncryptionService()
    secret_payload = "ghp_mockPersonalAccessToken123456789"  # noqa: S105

    ciphertext = service.encrypt(secret_payload)
    assert ciphertext != secret_payload

    decrypted = service.decrypt(ciphertext)
    assert decrypted == secret_payload


def test_derived_key_works_with_encryption() -> None:
    derived_key = derive_fernet_key_from_secret("my-secure-passphrase-salt")
    import os

    os.environ["CREDENTIAL_ENCRYPTION_KEYS"] = derived_key
    from app.core.config import get_settings

    get_settings.cache_clear()

    service = CredentialEncryptionService()
    token = "github_pat_token_test"  # noqa: S105
    assert service.decrypt(service.encrypt(token)) == token