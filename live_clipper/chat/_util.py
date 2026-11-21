"""Small helpers shared by chat adapters."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from datetime import UTC, datetime

_TOKEN_RE = re.compile(r"(?i)(bearer|oauth:|token=|access_token=|api_key=|key=)[\w\-\.~+/=]+")


def redact(text: object, secrets: Iterable[str] = ()) -> str:
    """Strip known secrets and token-looking substrings from an error string."""
    s = str(text)
    for sec in secrets:
        if sec and len(sec) > 5:
            s = s.replace(sec, "***")
    return _TOKEN_RE.sub(lambda m: m.group(1) + "***", s)


def parse_rfc3339(s: str | None) -> float | None:
    """RFC3339 -> epoch seconds. Accepts nanosecond fractions and 'Z'."""
    if not s:
        return None
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", s.strip())
    if not m:
        return None
    base, frac, tz = m.groups()
    tz = "+00:00" if tz == "Z" else tz
    dt = datetime.fromisoformat(f"{base}{tz}")
    return dt.timestamp() + (float("0." + frac) if frac else 0.0)


class Backoff:
    def __init__(self, first: float = 1.0, cap: float = 60.0):
        self.first, self.cap, self.cur = first, cap, first

    def next(self) -> float:
        d = self.cur
        self.cur = min(self.cap, self.cur * 2)
        return d

    def reset(self) -> None:
        self.cur = self.first


class AuthError(Exception):
    """HTTP 401/403 style failure carrying the user-facing fix."""
    def __init__(self, msg: str, action: str):
        super().__init__(msg)
        self.action = action


def sleeper(options: dict):
    return options.get("sleep") or asyncio.sleep


def now() -> float:
    return datetime.now(UTC).timestamp()
