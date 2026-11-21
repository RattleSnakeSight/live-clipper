"""Namespaced view of the app vault (ctx.secrets) for publishing credentials.

Names: publish/app/<provider> (client id/secret the user typed in), publish/dest/<destination id> (tokens),
publish/ref/<delivery id> (sensitive provider refs such as upload session URLs), publish/oauth/<state>.
"""
from __future__ import annotations

import threading
import time

from ..logging_setup import redact

OAUTH_TTL = 900.0
CRED_FIELDS = {"client_id", "client_secret", "client_key"}


class Secrets:
    def __init__(self, vault):
        self.vault, self._lock = vault, threading.RLock()

    @staticmethod
    def _n(ns: str, key) -> str:
        return f"publish/{ns}/{key}"

    def get(self, ns: str, key) -> dict | None:
        return self.vault.get(self._n(ns, key))

    def update(self, ns: str, key, patch: dict) -> None:
        """Merge `patch` into the entry; a None or "" value deletes that field; an empty entry is removed."""
        with self._lock:
            cur = dict(self.get(ns, key) or {})
            for k, v in patch.items():
                if v is None or v == "":
                    cur.pop(k, None)
                else:
                    cur[k] = v
            if cur:
                self.vault.set(self._n(ns, key), cur)
            else:
                self.vault.delete(self._n(ns, key))

    def delete(self, ns: str, key) -> None:
        self.vault.delete(self._n(ns, key))

    def fields(self, ns: str, key) -> list[str]:
        """Names of the stored fields only, never values (what the API may show)."""
        return sorted((self.get(ns, key) or {}).keys())

    def purge_oauth(self, now: float | None = None) -> None:
        now = now or time.time()
        for name in self.vault.names():
            if name.startswith("publish/oauth/"):
                e = self.vault.get(name) or {}
                if now - float(e.get("t", 0)) > OAUTH_TTL:
                    self.vault.delete(name)

    redact = staticmethod(redact)
