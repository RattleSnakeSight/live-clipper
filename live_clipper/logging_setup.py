"""Logging to a rotating file plus console, with secrets masked before anything is written."""

import logging
import logging.handlers
import re
import threading
from pathlib import Path

_KEYS = r"access_token|refresh_token|client_secret|api_key|code|password"
_PATTERNS = [
    # Authorization header in "Authorization: x", "authorization=x" or dict/JSON form.
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)[^\"'\r\n,}]+"), r"\1***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*"), r"\1 ***"),
    (re.compile(rf"(?i)([\"'](?:{_KEYS})[\"']\s*:\s*[\"'])[^\"']*"), r"\1***"),
    (re.compile(rf"(?i)\b({_KEYS})=[^&\s\"',]+"), r"\1=***"),
]
_secrets: set[str] = set()
_lock = threading.Lock()
_handlers: list[logging.Handler] = []


def register_secret(value: str) -> None:
    """Mask this exact value in all future log output. Values shorter than 4 chars are ignored."""
    if isinstance(value, str) and len(value) >= 4:
        with _lock:
            _secrets.add(value)


def redact(text: str) -> str:
    with _lock:
        known = sorted(_secrets, key=len, reverse=True)
    for s in known:
        text = text.replace(s, "***")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def setup_logging(logs_dir: Path, filename: str = "live-clipper.log", level: int = logging.INFO,
                  console: bool = True) -> None:
    """Configure the root logger. Each process should use its own `filename` (rotation is per-process)."""
    root = logging.getLogger()
    for h in _handlers:
        root.removeHandler(h)
        h.close()
    _handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(process)d %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        Path(logs_dir) / filename, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    _handlers.append(file_handler)
    if console:
        _handlers.append(logging.StreamHandler())
    for h in _handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        root.addHandler(h)
    root.setLevel(level)
