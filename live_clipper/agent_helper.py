"""The paired helper: `live-clipper agent serve`. Started by the AI agent that installed Live Clipper.

It is a plain program, not an AI. It waits for a request the customer made in the app, reads only the
named keys from the one source the customer chose, and sends them to the app over loopback:

- env: the platform's own variable names (TWITCH_CLIENT_ID, ...) in the environment the helper runs in
- file: the file the customer named, as KEY=VALUE lines or a JSON object; only those names are read
- keyring: the credential manager entry the customer named (service), one item per variable name;
  needs the optional `keyring` package

Values never appear in its output, its arguments, a URL or a log line: it prints field names and outcomes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from .agent_setup import TARGETS, read_pairing
from .instance_lock import InstanceLockError
from .paths import get_paths
from .supervisor import read_lock

HEADER = "X-Live-Clipper-Helper"


class SourceError(ValueError):
    """The chosen source could not be read. `code` is what the app is told; no text from the source."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def read_file(path: Path, names: list[str]) -> dict[str, str]:
    text = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            data[k.strip()] = v
    if not isinstance(data, dict):
        raise SourceError("bad_format", "the file holds no KEY=VALUE lines or JSON object")
    return {n: str(data[n]) for n in names if data.get(n) not in (None, "")}


def read_keyring(service: str, names: list[str]) -> dict[str, str]:
    try:
        import keyring
    except ImportError:
        raise SourceError(
            "no_keyring", "no credential manager support is installed (the optional `keyring` package)"
        ) from None
    out = {}
    for n in names:
        v = keyring.get_password(service, n)
        if v:
            out[n] = v
    return out


def checked_fields(req: dict) -> list[tuple[str, str, bool]]:
    """The fields to look up, from this program's own list. The request only names the target; a request that
    asks for other fields or variable names than that list is refused before any source is opened."""
    spec = TARGETS.get(req.get("target", ""))
    if spec is None:
        raise SourceError("refused", "the app asked for a platform this helper does not know")
    asked = [(f.get("name"), f.get("env"), f.get("required")) for f in req.get("fields", [])]
    if asked != list(spec["fields"]):
        raise SourceError("refused", "the app asked for other keys than this platform uses; nothing was read")
    return spec["fields"]


def resolve(req: dict, environ=os.environ) -> tuple[dict[str, str], list[str]]:
    """{canonical field: value} found in the chosen source, and the required fields that were not found."""
    fields = checked_fields(req)
    by_env = {env: name for name, env, _ in fields}
    kind, ref = req["source"]["kind"], req["source"]["ref"]
    if kind == "env":
        found = {n: environ[n] for n in by_env if environ.get(n)}
    elif kind == "file":
        found = read_file(Path(ref).expanduser(), list(by_env))
    elif kind == "keyring":
        found = read_keyring(ref, list(by_env))
    else:
        raise SourceError("refused", f"unknown source {kind!r}")
    values = {by_env[n]: v for n, v in found.items()}
    missing = [name for name, _, required in fields if required and name not in values]
    return values, missing


def _where(req: dict) -> str:
    s = req["source"]
    return {
        "env": "the environment",
        "file": f"the file {s['ref']}",
        "keyring": f"the credential entry {s['ref']}",
    }[s["kind"]]


def handle(http: httpx.Client, req: dict, say) -> dict:
    try:
        values, missing = resolve(req)
        body = {"values": values, "missing": missing}
    except SourceError as e:
        body, why = {"error": e.code}, str(e)
    except FileNotFoundError:
        body, why = {"error": "not_found"}, "file not found"
    except (OSError, UnicodeDecodeError):
        body, why = {"error": "unreadable"}, "file could not be read"
    else:
        why = ""
    r = http.post(f"/api/agent/helper/requests/{req['id']}/result", json=body)
    out = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"detail": r.text}
    status = out.get("status") or f"refused ({r.status_code}): {out.get('detail')}"
    names = ", ".join(sorted(body.get("values", {}))) or "no keys"
    say(
        f"Request {req['id']} ({req['label']}, from {_where(req)}): found {names}"
        f"{f' ({why})' if why else ''}; result: {status}"
    )
    return out


def serve(once: bool = False, max_wait: float = 0, say=print) -> int:
    """Claim and answer requests until stopped. `once`: answer one request, then exit.
    `max_wait`: give up after this many seconds without a request (0 = wait for ever)."""
    paths = get_paths(create=False)
    pairing = read_pairing(paths.state)
    if not pairing:
        say(
            "No helper is paired with this Live Clipper. Run `live-clipper agent pair --agent claude` "
            "(or codex) first, after the customer agrees."
        )
        return 2
    say(
        f"Live Clipper agent helper ({pairing['agent']}) is waiting for requests from the app. "
        "Ctrl+C stops it."
    )
    started, warned = time.monotonic(), False
    while True:
        if max_wait and time.monotonic() - started > max_wait:
            say("No request arrived; stopping. Start the helper again when the customer is ready.")
            return 1
        try:
            lock = read_lock(paths)
        except InstanceLockError:
            if not warned:
                say("Live Clipper's instance is not ready or cannot be verified; "
                    "waiting without sending keys.")
                warned = True
            time.sleep(2)
            continue
        if not lock:
            if not warned:
                say("Live Clipper is not running; waiting for it to start.")
                warned = True
            time.sleep(2)
            continue
        warned = False
        token = (paths.state / "token").read_text().strip()
        headers = {"Authorization": f"Bearer {token}", HEADER: pairing["secret"]}
        try:
            # loopback only: no proxy from the environment, no redirects (the keys go to this app or nowhere)
            with httpx.Client(
                base_url=f"http://127.0.0.1:{lock['port']}",
                headers=headers,
                timeout=40,
                trust_env=False,
                follow_redirects=False,
            ) as http:
                r = http.post("/api/agent/helper/claim")
                if r.status_code in (403, 409):
                    say(f"The app refused this helper: {r.json().get('detail')}")
                    return 3
                r.raise_for_status()
                req = r.json().get("request")
                if req:
                    handle(http, req, say)
                    if once:
                        return 0
        except httpx.HTTPError:
            time.sleep(2)  # the app restarted or is starting; try again
