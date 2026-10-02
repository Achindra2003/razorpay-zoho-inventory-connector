"""Where a merchant connection (tokens + org binding) is persisted.

Default: the OS credential vault via `keyring` (Windows Credential Manager, macOS Keychain,
Secret Service on Linux). Fallback: a JSON file with owner-only permissions, for headless
boxes without a vault. Tokens are never written to .env, logs or tool output.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

KEYRING_SERVICE = "zoho-inventory-connector"


@dataclass
class Connection:
    name: str
    dc: str
    accounts_url: str
    api_domain: str
    refresh_token: str
    access_token: str = ""
    access_expires_at: float = 0.0
    organization_id: str = ""
    organization_name: str = ""
    currency_code: str = "INR"
    scopes: str = ""
    connected_at: float = field(default_factory=time.time)

    def __repr__(self) -> str:  # never leak tokens through repr/logging
        return f"Connection(name={self.name!r}, dc={self.dc!r}, org={self.organization_id!r})"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "Connection":
        data = json.loads(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class TokenStore(Protocol):
    def load(self, name: str) -> Connection | None: ...
    def save(self, conn: Connection) -> None: ...
    def delete(self, name: str) -> None: ...
    @property
    def description(self) -> str: ...


class MemoryStore:
    """Used by tests and the zero-setup demo mode."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    @property
    def description(self) -> str:
        return "in-memory (demo)"

    def load(self, name: str) -> Connection | None:
        raw = self._data.get(name)
        return Connection.from_json(raw) if raw else None

    def save(self, conn: Connection) -> None:
        self._data[conn.name] = conn.to_json()

    def delete(self, name: str) -> None:
        self._data.pop(name, None)


class FileStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def description(self) -> str:
        return f"file ({self.directory})"

    def _path(self, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "-_") or "default"
        return self.directory / f"connection-{safe}.json"

    def load(self, name: str) -> Connection | None:
        path = self._path(name)
        return Connection.from_json(path.read_text(encoding="utf-8")) if path.is_file() else None

    def save(self, conn: Connection) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(conn.name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(conn.to_json(), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - best effort on Windows
            pass
        tmp.replace(path)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)


class KeyringStore:
    def __init__(self) -> None:
        import keyring  # imported lazily so the file store works without a vault

        self._keyring = keyring

    @property
    def description(self) -> str:
        return f"OS keyring ({type(self._keyring.get_keyring()).__name__})"

    def load(self, name: str) -> Connection | None:
        raw = self._keyring.get_password(KEYRING_SERVICE, name)
        return Connection.from_json(raw) if raw else None

    def save(self, conn: Connection) -> None:
        self._keyring.set_password(KEYRING_SERVICE, conn.name, conn.to_json())

    def delete(self, name: str) -> None:
        try:
            self._keyring.delete_password(KEYRING_SERVICE, name)
        except Exception:  # keyring raises PasswordDeleteError when absent
            pass


def default_store(state_dir: Path) -> TokenStore:
    choice = os.environ.get("ZOHO_TOKEN_STORE", "keyring").lower()
    if choice == "file":
        return FileStore(state_dir)
    try:
        store = KeyringStore()
        backend = type(store._keyring.get_keyring()).__name__
        if "fail" in backend.lower() or "null" in backend.lower():
            raise RuntimeError(f"no usable keyring backend ({backend})")
        return store
    except Exception as exc:
        log.warning("OS keyring unavailable (%s); falling back to file store in %s", exc, state_dir)
        return FileStore(state_dir)
