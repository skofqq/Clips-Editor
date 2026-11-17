"""Secure storage for OAuth secrets.

Refresh tokens and the OAuth client secret go to the operating system's
credential store through ``keyring`` (Windows Credential Manager, which is
DPAPI-protected). They are never written to config.json or project files.
If no secure backend exists, secrets are kept in memory for the session
only and :attr:`TokenStore.persistent` is False.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

SERVICE = "TwitchShortsStudio YouTube"
CLIENT_KEY = "oauth-client"


class TokenStore:
    persistent = False

    def get(self, key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def set(self, key: str, value: dict[str, Any]) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class MemoryStore(TokenStore):
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        raw = self._data.get(key)
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = json.dumps(value)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class KeyringStore(TokenStore):
    """Windows Credential Manager (or the platform keychain) via keyring."""

    persistent = True

    def __init__(self, service: str = SERVICE) -> None:
        import keyring

        self._keyring = keyring
        self.service = service

    def get(self, key: str) -> dict[str, Any] | None:
        raw = self._keyring.get_password(self.service, key)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._keyring.set_password(self.service, key, json.dumps(value, separators=(",", ":")))

    def delete(self, key: str) -> None:
        try:
            self._keyring.delete_password(self.service, key)
        except Exception:  # noqa: BLE001 - missing entry is fine
            pass


def default_store() -> TokenStore:
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) <= 0:
            raise RuntimeError(f"no secure backend ({backend})")
        return KeyringStore()
    except Exception as exc:  # noqa: BLE001 - fall back to memory, never to plaintext
        log.warning("Secure credential storage unavailable (%s); YouTube sign-in lasts for this session only.", exc)
        return MemoryStore()
