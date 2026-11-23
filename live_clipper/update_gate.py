"""Required update: find out whether a newer release exists, and hold the app's normal work until it is
current.

Source of truth: this project's version line (`__version__ = "X.Y.Z"` in `live_clipper/__init__.py`) on the
main branch of the canonical repository, read as plain text. It is the line that sets every installed copy's
version, so a release is exactly a commit on main that raises it. Documentation-only commits, other branches
and tags change nothing. A pre-release or development version on main is never offered; a copy newer than
main (a development build) is current.

The request is one anonymous GET of that fixed HTTPS address: no identifiers, paths, cookies, credentials or
token, a fixed User-Agent, the system HTTPS proxy if one is set, no redirects, a strict timeout and at most
MAX_BYTES read. A repeat sends the ETag of the last answer (If-None-Match), the same for every copy, so an
unchanged file costs an empty 304. Nothing is executed: the text is matched against one pattern and parsed
as a version.

When it checks: at every start of the worker and at every open (`live-clipper start`, a browser loading the
app, Check again), every RECHECK while running, and on a backoff after a failure. Overlapping checks share one
request, and the CLI's check right after it started the worker reuses the worker's start check (one launch).
Every other open asks again; with the ETag an unchanged answer is a 304.

States: checking (at start, before the first answer), current, pending (a newer release is confirmed),
unknown (no answer could be verified). The app's services start only once the state is current. A confirmed
pending update is saved in the data folder, so it holds across restarts and outages until this copy is at
least that version.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from packaging.version import InvalidVersion, Version

from . import __version__

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/RattleSnakeSight/live-clipper"
VERSION_FILE = "live_clipper/__init__.py"
BRANCH = "main"
MAX_BYTES = 4096
TIMEOUT = httpx.Timeout(8.0, connect=5.0)
DEADLINE = 10.0  # seconds for the whole request, however slowly the bytes arrive
VERSION_LINE = re.compile(r'^__version__ = "([0-9A-Za-z.!+-]{1,32})"\s*$', re.MULTILINE)

SAME_OPEN = 30.0  # the CLI asks within this many seconds of starting the worker
RETRY_SPACING = 2.0  # "Check again" pressed faster than this reuses the last check
RECHECK = 6 * 3600.0  # a running app checks again this often
RETRY = (15.0, 30.0, 60.0, 120.0, 300.0)  # waits after failed checks; the last one repeats
RESTART_EXIT = 75  # worker exit code: start a fresh worker now (not a crash)
SAVED = "update.json"


def manifest_url() -> str:
    """The raw-text address of the version file, built from the project URL."""
    path = urlsplit(REPO_URL).path.strip("/")
    return f"https://raw.githubusercontent.com/{path}/{BRANCH}/{VERSION_FILE}"


class CheckFailed(Exception):
    """The check gave no verified answer. `code` is one of: offline, timeout, rate_limited, unavailable,
    bad_response, proxy."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_version(text: str) -> str:
    m = VERSION_LINE.search(text)
    if not m:
        raise CheckFailed("bad_response")
    try:
        Version(m.group(1))
    except InvalidVersion:
        raise CheckFailed("bad_response") from None
    return m.group(1)


PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
PROXY_HOST = re.compile(r"[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\]")


def _proxy(url: str) -> str | None:
    """The HTTPS proxy this computer is set to use, if any. Raises CheckFailed("proxy") for one that cannot
    be used, without repeating it (it may hold a password)."""
    if urlsplit(url).hostname in ("127.0.0.1", "localhost"):
        return None
    proxy = urllib.request.getproxies().get("https")
    if not proxy:
        return None
    try:
        parts = urlsplit(proxy)
        host, _port = parts.hostname, parts.port  # .port raises ValueError for a bad port
    except ValueError:
        raise CheckFailed("proxy") from None
    if parts.scheme not in PROXY_SCHEMES or not host or not PROXY_HOST.fullmatch(host):
        raise CheckFailed("proxy")
    return proxy


def fetch_latest(etag: str | None = None) -> tuple[str | None, str | None]:
    """(version on the project's main branch, its ETag); version None means unchanged since `etag` (304).
    Raises CheckFailed. Blocking: run it in a thread."""
    url = manifest_url()
    end = time.monotonic() + DEADLINE
    headers = {"User-Agent": "live-clipper-update-check", "Accept": "text/plain"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        # trust_env=False: no .netrc credentials and no cookies from the environment; the proxy is passed
        # explicitly instead
        http = httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False, proxy=_proxy(url),
                            headers=headers)
    except (ImportError, ValueError, httpx.HTTPError):
        # the system proxy cannot be used: a SOCKS proxy without the optional socksio package, or a malformed
        # address. Never repeat the address: it may hold a password.
        raise CheckFailed("proxy") from None
    try:
        with http, http.stream("GET", url) as r:
                if r.status_code == 304 and etag:
                    return None, etag
                if r.status_code in (403, 429):
                    raise CheckFailed("rate_limited")
                if r.status_code != 200:
                    raise CheckFailed("unavailable")
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise CheckFailed("bad_response")
                    if time.monotonic() > end:
                        raise CheckFailed("timeout")
                new_etag = r.headers.get("etag")
    except httpx.TimeoutException:
        raise CheckFailed("timeout") from None
    except httpx.HTTPError:
        raise CheckFailed("offline") from None
    try:
        return parse_version(body.decode("utf-8")), new_etag if new_etag and len(new_etag) <= 200 else None
    except UnicodeDecodeError:
        raise CheckFailed("bad_response") from None


def is_newer(available: str, installed: str) -> bool:
    """True when `available` is a final release above `installed` (0.10.0 > 0.9.0; 0.3.0 > 0.3.0.dev1)."""
    try:
        a, i = Version(available), Version(installed)
    except InvalidVersion:
        return False
    return not (a.is_prerelease or a.is_devrelease) and a > i


def install_method() -> str:
    """How this copy was installed: pipx, pip, editable (a developer checkout) or unknown."""
    from .cli import _direct_url, install_source

    try:
        source = install_source(_direct_url())
    except (OSError, ValueError, KeyError):
        return "unknown"
    if source is None:
        return "editable"
    parts = [p.lower() for p in Path(sys.prefix).parts]
    if "pipx" in parts and "venvs" in parts:
        return "pipx"
    return "pip"


def installing_agent(state_dir: Path) -> str | None:
    """The agent paired with this data folder (claude or codex), if any: the one to ask for the update."""
    from .agent_setup import read_pairing

    p = read_pairing(state_dir)
    return p["agent"] if p else None


class UpdateGate:
    """Holds the update state for one worker, runs the checks and tells the worker when to start its services
    (on_current) or to restart into the gated state (on_restart)."""

    def __init__(self, state_dir: Path, version: str = __version__,
                 fetch: Callable[[str | None], tuple[str | None, str | None]] | None = None,
                 clock: Callable[[], float] = time.time):
        self.file = state_dir / SAVED
        self.version, self.clock = version, clock
        self.fetch = fetch or (lambda etag: fetch_latest(etag))  # looked up per call (tests swap it)
        self.etag: str | None = None
        self.start_check = 0.0  # when the worker's own start check began
        self.state = "checking"
        self.available: str | None = None
        self.checked_at: float | None = None  # last check that gave an answer
        self.error: str | None = None  # why the last check gave none
        self.last_try = 0.0
        self.next_check = 0.0
        self.failures = 0
        self.services_started = False
        self.on_current: Callable[[], Awaitable[None]] = self._noop
        self.on_restart: Callable[[], None] = lambda: None
        self._inflight: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self.how = {"method": install_method(), "agent": installing_agent(state_dir)}

    @staticmethod
    async def _noop() -> None:
        return None

    def view(self) -> dict:
        return {"state": self.state, "installed": self.version, "available": self.available,
                "checked_at": self.checked_at, "error": self.error,
                "next_check": self.next_check or None, **self.how}

    def _load(self) -> None:
        try:
            saved = json.loads(self.file.read_text())
            available, checked_at = str(saved["available"]), float(saved["checked_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if is_newer(available, self.version):
            self.state, self.available, self.checked_at = "pending", available, checked_at
            # saved moments ago by the worker this one replaces: that answer is this start's check
            if self.clock() - checked_at < SAME_OPEN:
                self.next_check = checked_at + RECHECK
                self.last_try = self.start_check = checked_at
            log.info("update pending (saved): %s available, %s installed", available, self.version)
        else:
            self.file.unlink(missing_ok=True)  # this copy is already at least that version

    def _save(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"available": self.available, "installed": self.version,
                                   "checked_at": self.checked_at}))
        os.replace(tmp, self.file)

    async def start(self) -> None:
        self._load()
        self._loop_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        for task in (self._loop_task, self._inflight):
            if task:
                task.cancel()

    def _reuse(self, kind: str) -> bool:
        """Whether a check of this kind may reuse the last one (see the module docstring)."""
        now = self.clock()
        if kind == "retry":
            return now - self.last_try < RETRY_SPACING
        if kind == "after_start":  # the CLI, right after it started this worker
            return 0 < self.start_check == self.last_try and now - self.last_try < SAME_OPEN
        return False

    async def check(self, kind: str = "open") -> dict:
        """Check now (kind: open, after_start, retry, timer), unless a check is running (then share it) or
        the last one may be reused."""
        if self._inflight is None or self._inflight.done():
            if self._reuse(kind):
                return self.view()
            self._inflight = asyncio.create_task(self._check(kind))
            self._inflight.add_done_callback(lambda _: setattr(self, "_inflight", None))
        await asyncio.shield(self._inflight)
        self._wake.set()  # the background loop picks up the new next_check
        return self.view()

    async def _run(self) -> None:
        while True:
            delay = max(0.0, self.next_check - self.clock())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            self._wake.clear()
            if self.clock() >= self.next_check:
                try:
                    await self.check("timer")
                except Exception:  # a bug here must not end the checks
                    log.exception("update check failed")
                    self.next_check = self.clock() + RETRY[-1]

    async def _check(self, kind: str) -> None:
        self.last_try = self.clock()
        if kind == "timer" and not self.start_check:
            self.start_check = self.last_try
        try:
            available, self.etag = await asyncio.to_thread(self.fetch, self.etag if self.available else None)
        except CheckFailed as e:
            self._failed(e.code)
            return
        except Exception as e:  # a bug or an odd environment: no answer, never stuck
            # the type only: an exception's text can carry an address with a password in it
            log.error("update check broke (%s)", type(e).__name__)
            self._failed("unavailable")
            return
        available = available or self.available  # 304: unchanged
        self.failures, self.error = 0, None
        self.available, self.checked_at = available, self.clock()
        self.next_check = self.checked_at + RECHECK
        if is_newer(available, self.version):
            first = self.state != "pending"
            self.state = "pending"
            try:
                self._save()
            except OSError:  # a full disk: the gate still closes, it just will not survive a restart
                log.exception("could not save the pending update")
            if first:
                log.warning("update pending: %s available, %s installed", available, self.version)
            if self.services_started:
                # this worker's services are running: answer the caller first, then restart into the gate
                asyncio.get_running_loop().call_later(0.5, self.on_restart)
            return
        self.file.unlink(missing_ok=True)
        if self.state != "current":
            log.info("up to date: %s installed, %s on main", self.version, available)
        self.state = "current"
        if not self.services_started:
            self.services_started = True
            await self.on_current()

    def _failed(self, code: str) -> None:
        self.error = code
        self.next_check = self.clock() + RETRY[min(self.failures, len(RETRY) - 1)]
        self.failures += 1
        # a confirmed pending update stays; a copy verified current in this run keeps running
        if self.state == "checking":
            self.state = "unknown"
        wait = self.next_check - self.clock()
        log.warning("update check gave no answer (%s); next try in %.0fs", code, wait)
