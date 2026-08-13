from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from .contracts import ContractError


def new_public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def new_secret() -> str:
    return secrets.token_urlsafe(48)


@dataclass(frozen=True)
class SecretHash:
    algorithm: str
    salt_b64: str
    digest_b64: str
    iterations: int

    def encode(self) -> str:
        return f"{self.algorithm}${self.iterations}${self.salt_b64}${self.digest_b64}"

    @classmethod
    def decode(cls, value: str) -> "SecretHash":
        parts = value.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            raise ContractError("unauthorised_installation", "Invalid credential")
        return cls(
            algorithm=parts[0],
            iterations=int(parts[1]),
            salt_b64=parts[2],
            digest_b64=parts[3],
        )


class InstallationSecretHasher:
    def __init__(self, *, iterations: int = 210_000) -> None:
        self._iterations = iterations

    def hash_secret(self, secret: str) -> SecretHash:
        salt = secrets.token_bytes(24)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            secret.encode("utf-8"),
            salt,
            self._iterations,
        )
        return SecretHash(
            algorithm="pbkdf2_sha256",
            iterations=self._iterations,
            salt_b64=base64.urlsafe_b64encode(salt).decode("ascii"),
            digest_b64=base64.urlsafe_b64encode(digest).decode("ascii"),
        )

    def verify(self, secret: str, encoded_hash: str) -> bool:
        try:
            stored = SecretHash.decode(encoded_hash)
            salt = base64.urlsafe_b64decode(stored.salt_b64.encode("ascii"))
            expected = base64.urlsafe_b64decode(stored.digest_b64.encode("ascii"))
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                secret.encode("utf-8"),
                salt,
                stored.iterations,
            )
        except Exception:
            return False
        return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class ProtectedToken:
    keyed_hash: str
    ciphertext: bytes
    key_version: str


class TokenProtector:
    def __init__(self, *, hash_key: str, encryption_keys: str) -> None:
        if not hash_key.strip() or not encryption_keys.strip():
            raise ContractError("service_unavailable", "Token protection is not configured")
        self._hash_key = hash_key.encode("utf-8")
        self._key_specs = [item.strip() for item in encryption_keys.split(",") if item.strip()]
        if not self._key_specs:
            raise ContractError("service_unavailable", "Token encryption key is missing")
        try:
            from cryptography.fernet import Fernet, MultiFernet
        except Exception as exc:
            raise ContractError("service_unavailable", "Token encryption dependency unavailable") from exc
        fernets = []
        for spec in self._key_specs:
            version, _, key = spec.partition(":")
            if not version or not key:
                raise ContractError("service_unavailable", "Token encryption key must include version")
            fernets.append(Fernet(key.encode("ascii")))
        self._key_version = self._key_specs[0].partition(":")[0]
        self._fernet = MultiFernet(fernets)

    def protect(self, token: str) -> ProtectedToken:
        clean = token.strip()
        if not clean:
            raise ContractError("malformed_request", "Token is required")
        keyed_hash = hmac.new(self._hash_key, clean.encode("utf-8"), hashlib.sha256).hexdigest()
        ciphertext = self._fernet.encrypt(clean.encode("utf-8"))
        return ProtectedToken(
            keyed_hash=keyed_hash,
            ciphertext=ciphertext,
            key_version=self._key_version,
        )

    def reveal(self, protected: ProtectedToken) -> str:
        return self._fernet.decrypt(protected.ciphertext).decode("utf-8")


class DeterministicTestTokenProtector:
    key_version = "test-v1"

    def protect(self, token: str) -> ProtectedToken:
        clean = token.strip()
        if not clean:
            raise ContractError("malformed_request", "Token is required")
        keyed_hash = hmac.new(b"test", clean.encode("utf-8"), hashlib.sha256).hexdigest()
        return ProtectedToken(
            keyed_hash=keyed_hash,
            ciphertext=b"test-ciphertext:" + base64.urlsafe_b64encode(clean.encode("utf-8")),
            key_version=self.key_version,
        )

    def reveal(self, protected: ProtectedToken) -> str:
        prefix = b"test-ciphertext:"
        if not protected.ciphertext.startswith(prefix):
            raise ContractError("service_unavailable", "Token ciphertext is invalid")
        return base64.urlsafe_b64decode(protected.ciphertext[len(prefix):]).decode("utf-8")


def token_protector_from_env() -> TokenProtector:
    return TokenProtector(
        hash_key=os.getenv("PRICE_ALERTS_TOKEN_HASH_KEY", ""),
        encryption_keys=os.getenv("PRICE_ALERTS_TOKEN_ENCRYPTION_KEYS", ""),
    )
