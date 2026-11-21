"""Local credential vault: a Fernet-encrypted JSON file. Never log values from here."""

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from .logging_setup import register_secret

log = logging.getLogger(__name__)
KEYRING_SERVICE = "live-clipper"


def write_private(path: Path, data: bytes) -> None:
    """Atomic write, readable by the owner only (0600 on POSIX)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _register_all(value: Any) -> None:
    if isinstance(value, dict):
        for v in value.values():
            _register_all(v)
    elif isinstance(value, list):
        for v in value:
            _register_all(v)
    elif isinstance(value, str):
        register_secret(value)


class Vault:
    def __init__(self, secrets_dir: Path):
        self.dir = Path(secrets_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "vault.json.enc"
        self._lock = threading.Lock()
        self._fernet: Fernet | None = None

    def _key_from_keyring(self) -> bytes | None:
        try:
            import keyring

            user = "vault-key-" + hashlib.sha256(str(self.dir.resolve()).encode()).hexdigest()[:16]
            key = keyring.get_password(KEYRING_SERVICE, user)
            if not key:
                keyring.set_password(KEYRING_SERVICE, user, Fernet.generate_key().decode())
                key = keyring.get_password(KEYRING_SERVICE, user)
            return key.encode() if key else None
        except Exception as e:  # missing package or no usable backend
            log.debug("keyring unavailable, using key file: %s", type(e).__name__)
            return None

    def _key_from_file(self) -> bytes:
        path = self.dir / "vault.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            return path.read_bytes().strip()
        key = Fernet.generate_key()
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        os.chmod(path, 0o600)
        return key

    def _f(self) -> Fernet:
        if self._fernet is None:
            key = None if (self.dir / "vault.key").exists() else self._key_from_keyring()
            self._fernet = Fernet(key or self._key_from_file())
        return self._fernet

    def _load(self) -> dict[str, dict]:
        if not self.file.exists():
            return {}
        data = json.loads(self._f().decrypt(self.file.read_bytes()))
        _register_all(data)
        return data

    def _save(self, data: dict[str, dict]) -> None:
        write_private(self.file, self._f().encrypt(json.dumps(data).encode()))

    def get(self, name: str) -> dict | None:
        with self._lock:
            return self._load().get(name)

    def set(self, name: str, value: dict) -> None:
        _register_all(value)
        with self._lock:
            data = self._load()
            data[name] = value
            self._save(data)

    def delete(self, name: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(name, None) is not None:
                self._save(data)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._load())
