"""Agent-assisted credential setup: the customer asks, a paired local helper finds keys, the app saves them.

Flow
- The AI agent that installed Live Clipper pairs a helper with this data folder: `live-clipper agent pair`
  writes `state/agent-helper.json` (agent name and a random helper secret). Only one helper is paired.
- In the app the customer picks a platform and where its keys are (named environment variables, a file they
  choose, or a credential manager entry) and presses "Ask my agent". That creates a request here.
- `live-clipper agent serve` (the helper, started by the agent) claims the request, reads only the named keys
  from that one source, and sends the values here over loopback. This service saves them through the same
  code as manual setup (sources are checked with the platform, destination keys go to the vault).
- The app shows the result with field names only. Values are never returned, logged or put in a URL.

Every route needs the app token (server Guard). The helper routes also need the helper secret, so the
browser, which holds only the token cookie, can create and cancel requests but never claim or answer one.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .db import register_migrations
from .logging_setup import register_secret
from .secrets import write_private

log = logging.getLogger(__name__)

AGENTS = {"claude": "Claude Code", "codex": "Codex"}
PAIR_FILE = "agent-helper.json"
TTL = 600.0  # a request not answered in 10 minutes expires; the customer can ask again
CLAIM_WAIT = 25.0  # the helper's long poll
HELPER_STALE = 60.0  # no poll for this long: the helper is shown as offline

# What can be set up, per target: (canonical field, environment variable name, required).
# The field names are the ones manual setup uses; the variable names are the only ones the helper reads.
TARGETS: dict[str, dict] = {
    "source:twitch": {
        "label": "Twitch",
        "fields": [("client_id", "TWITCH_CLIENT_ID", True), ("client_secret", "TWITCH_CLIENT_SECRET", True)],
    },
    "source:kick": {
        "label": "Kick",
        "fields": [("client_id", "KICK_CLIENT_ID", True), ("client_secret", "KICK_CLIENT_SECRET", True)],
    },
    "source:youtube": {"label": "YouTube", "fields": [("api_key", "YOUTUBE_API_KEY", True)]},
    "destination:youtube": {
        "label": "YouTube Shorts",
        "fields": [
            ("client_id", "YOUTUBE_CLIENT_ID", True),
            ("client_secret", "YOUTUBE_CLIENT_SECRET", False),
        ],
    },
    "destination:tiktok": {
        "label": "TikTok",
        "fields": [
            ("client_key", "TIKTOK_CLIENT_KEY", True),
            ("client_secret", "TIKTOK_CLIENT_SECRET", True),
        ],
    },
    "destination:instagram": {
        "label": "Instagram Reels",
        "fields": [
            ("client_id", "INSTAGRAM_CLIENT_ID", True),
            ("client_secret", "INSTAGRAM_CLIENT_SECRET", True),
        ],
    },
}
SOURCE_KINDS = ("env", "file", "keyring")
OPEN = ("pending", "claimed", "saving")

register_migrations(
    "agent_setup",
    [
        """
CREATE TABLE IF NOT EXISTS agent_requests(
    id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    saved_json TEXT NOT NULL DEFAULT '[]', missing_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
    pair_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS agent_requests_status ON agent_requests(status);
"""
    ],
)


def spec_view(target: str) -> dict:
    s = TARGETS[target]
    return {
        "target": target,
        "label": s["label"],
        "fields": [{"name": f, "env": e, "required": r} for f, e, r in s["fields"]],
    }


# ---- pairing (also used by the CLI while the app is stopped) ---------------------------------------------


def read_pairing(state_dir: Path) -> dict | None:
    try:
        p = json.loads((state_dir / PAIR_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return p if p.get("agent") in AGENTS and p.get("secret") else None


def pair(state_dir: Path, agent: str) -> dict:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}; use one of {', '.join(AGENTS)}")
    p = {"agent": agent, "secret": secrets.token_urlsafe(32), "created_at": time.time()}
    state_dir.mkdir(parents=True, exist_ok=True)
    write_private(state_dir / PAIR_FILE, json.dumps(p).encode())
    return p


def unpair(state_dir: Path) -> bool:
    f = state_dir / PAIR_FILE
    existed = f.exists()
    f.unlink(missing_ok=True)
    return existed


# ---- request bodies -------------------------------------------------------------------------------------


class SourceIn(BaseModel):
    kind: str
    ref: str = ""  # file path or credential manager service name; not a secret


class RequestIn(BaseModel):
    target: str
    source: SourceIn


class ResultIn(BaseModel):
    values: dict[str, str] = {}
    missing: list[str] = []
    error: str = ""  # one of SOURCE_ERRORS: the helper could not read the source at all


# What the customer is told when the helper could not read the source. Only these fixed texts are stored:
# a helper's own error text could carry a path fragment or a value, so it is never kept.
SOURCE_ERRORS = {
    "not_found": "The agent could not find that file or entry.",
    "unreadable": "The agent could not open that file.",
    "bad_format": "That file has no KEY=VALUE lines or JSON object the agent could read.",
    "no_keyring": "Credential manager support is not installed for the agent's helper.",
    "refused": "The agent's helper refused the request because it asked for unexpected keys.",
}
GENERIC_SOURCE_ERROR = "The agent could not read that source."
NOT_ACCEPTED = "The keys were found, but the platform did not accept them. Check them in your developer app."
NOT_SAVED = "The keys were found, but the app could not save them right now. Ask again in a moment."


def pair_id(p: dict | None) -> str:
    """A stable name for one pairing, so a request made under one pairing is never answered under another."""
    return hashlib.sha256(p["secret"].encode()).hexdigest()[:16] if p else ""


class AgentSetupService:
    name = "agent_setup"

    def __init__(self) -> None:
        self.ctx = None
        self.last_seen = 0.0
        self._lock = threading.Lock()
        self._wake = asyncio.Event()
        self.router = self._routes()

    async def start(self, ctx) -> None:
        self.ctx = ctx
        self._wake = asyncio.Event()
        # a request that was being handled when the app stopped can be answered by nobody now
        self.db.execute(
            "UPDATE agent_requests SET status='failed', detail=?, updated_at=? "
            "WHERE status IN ('claimed', 'saving')",
            ("Live Clipper restarted before the keys were saved. Ask again.", time.time()),
        )

    async def stop(self) -> None:
        self._wake.set()

    def status(self) -> dict:
        p = self.pairing()
        return {
            "paired": p["agent"] if p else None,
            "helper_online": self.online(),
            "open_requests": self.db.execute(
                f"SELECT COUNT(*) FROM agent_requests WHERE status IN {OPEN}"
            ).fetchone()[0],
        }

    # ---- helpers ------------------------------------------------------------------------------------
    @property
    def db(self):
        return self.ctx.db

    def pairing(self) -> dict | None:
        return read_pairing(self.ctx.paths.state)

    def online(self) -> bool:
        return time.time() - self.last_seen < HELPER_STALE

    def expire(self) -> None:
        now = time.time()
        current = pair_id(self.pairing())
        self.db.execute(
            "UPDATE agent_requests SET status='cancelled', updated_at=?, detail=? "
            f"WHERE status IN {OPEN} AND status != 'saving' AND pair_id != ?",
            (now, "The agent pairing changed after this request. Ask again.", current),
        )
        self.db.execute(
            "UPDATE agent_requests SET status='expired', updated_at=?, detail=? "
            "WHERE status IN ('pending', 'claimed') AND expires_at < ?",
            (now, "No agent answered in time. Ask again or enter the keys by hand.", now),
        )

    def row(self, rid: int) -> dict:
        r = self.db.execute("SELECT * FROM agent_requests WHERE id=?", (rid,)).fetchone()
        if r is None:
            raise HTTPException(404, "unknown request")
        return self.view(r)

    @staticmethod
    def view(r) -> dict:
        kind, target = r["target"].split(":", 1)
        return {
            "id": r["id"],
            "target": r["target"],
            "kind": kind,
            "platform": target,
            "label": TARGETS[r["target"]]["label"],
            "source": {"kind": r["source_kind"], "ref": r["source_ref"]},
            "status": r["status"],
            "detail": r["detail"],
            "saved": json.loads(r["saved_json"]),
            "missing": json.loads(r["missing_json"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "expires_at": r["expires_at"],
        }

    def finish(self, rid: int, status: str, detail: str = "", saved=(), missing=()) -> dict:
        self.db.execute(
            "UPDATE agent_requests SET status=?, detail=?, saved_json=?, missing_json=?, updated_at=? "
            "WHERE id=?",
            (status, detail, json.dumps(list(saved)), json.dumps(list(missing)), time.time(), rid),
        )
        return self.row(rid)

    def check_helper(self, secret: str) -> dict:
        p = self.pairing()
        if not p:
            raise HTTPException(
                409, "no helper is paired with this Live Clipper; run `live-clipper agent pair`"
            )
        if not secret or not hmac.compare_digest(secret.encode(), p["secret"].encode()):
            raise HTTPException(403, "this helper is not the one paired with this Live Clipper")
        return p

    def service(self, name: str):
        return next((s for s in self.ctx.services if getattr(s, "name", "") == name), None)

    async def apply(self, target: str, values: dict) -> list[str]:
        """Save through the same code as manual setup. Raises HTTPException with the platform's reason."""
        kind, platform = target.split(":", 1)
        if kind == "source":
            from .monitor_service import ConnectIn

            mon = self.service("monitor")
            if mon is None:
                raise HTTPException(503, "monitoring is not running, so source keys cannot be checked now")
            from .sources import SourceAuthError

            try:
                await mon.connect(platform, ConnectIn(method="credentials", **values))
            except SourceAuthError:
                raise HTTPException(400, "not accepted") from None
            return sorted(values)
        pub = self.service("publish")
        if pub is None or pub.secrets is None:
            raise HTTPException(503, "posting is not running, so destination keys cannot be saved now")
        pub.secrets.update("app", platform, values)
        return sorted(values)

    # ---- routes -------------------------------------------------------------------------------------
    def _routes(self) -> APIRouter:
        r = APIRouter(prefix="/api/agent")

        @r.get("")
        def overview():
            self.expire()
            p = self.pairing()
            return {
                "paired": {"agent": p["agent"], "agent_label": AGENTS[p["agent"]], "since": p["created_at"]}
                if p
                else None,
                "helper": {"online": self.online(), "last_seen": self.last_seen or None},
                "targets": [spec_view(t) for t in TARGETS],
                "source_kinds": list(SOURCE_KINDS),
            }

        @r.delete("/pairing", status_code=204)
        def revoke():
            """The customer withdraws the agent's access. Open requests are cancelled."""
            unpair(self.ctx.paths.state)
            self.last_seen = 0.0
            self.db.execute(
                "UPDATE agent_requests SET status='cancelled', detail='Agent access was revoked.', "
                f"updated_at=? WHERE status IN {OPEN}",
                (time.time(),),
            )

        @r.get("/requests")
        def requests(target: str = ""):
            self.expire()
            q, args = "SELECT * FROM agent_requests", ()
            if target:
                q, args = q + " WHERE target=?", (target,)
            return {
                "requests": [self.view(x) for x in self.db.execute(q + " ORDER BY id DESC LIMIT 20", args)]
            }

        @r.post("/requests", status_code=201)
        def create(body: RequestIn):
            if body.target not in TARGETS:
                raise HTTPException(422, "unknown platform")
            if body.source.kind not in SOURCE_KINDS:
                raise HTTPException(422, f"source must be one of {', '.join(SOURCE_KINDS)}")
            ref = body.source.ref.strip()
            if body.source.kind != "env" and not ref:
                raise HTTPException(422, "choose the file or the credential manager entry to read")
            if len(ref) > 1000 or "\n" in ref:
                raise HTTPException(422, "that location is not valid")
            if not self.pairing():
                raise HTTPException(
                    409,
                    "no agent is paired with this Live Clipper; set the keys up by hand, "
                    "or ask the agent that installed it to run `live-clipper agent pair`",
                )
            self.expire()
            now = time.time()
            with self._lock, self.db.tx() as c:
                if c.execute(
                    f"SELECT 1 FROM agent_requests WHERE target=? AND status IN {OPEN}", (body.target,)
                ).fetchone():
                    raise HTTPException(409, "a request for this platform is already open; wait or cancel it")
                rid = c.execute(
                    "INSERT INTO agent_requests(target, source_kind, source_ref, status, created_at, "
                    "updated_at, expires_at, pair_id) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
                    (body.target, body.source.kind, ref, now, now, now + TTL, pair_id(self.pairing())),
                ).lastrowid
            self._wake.set()
            return self.row(rid)

        @r.post("/requests/{rid}/cancel")
        def cancel(rid: int):
            with self._lock:
                cur = self.row(rid)
                if cur["status"] == "saving":
                    raise HTTPException(409, "the keys are being saved right now")
                if cur["status"] not in OPEN:
                    return cur
                return self.finish(rid, "cancelled", "You cancelled this request.")

        # -- the paired helper only --------------------------------------------------------------------
        @r.post("/helper/claim")
        async def claim(x_live_clipper_helper: str = Header("")):
            end = time.monotonic() + CLAIM_WAIT
            while True:
                p = self.check_helper(
                    x_live_clipper_helper
                )  # again on every pass: revoke or re-pair stops it
                self.last_seen = time.time()
                self.expire()
                with self._lock:
                    row = self.db.execute(
                        "SELECT id, target FROM agent_requests WHERE status='pending' AND pair_id=? "
                        "ORDER BY id LIMIT 1",
                        (pair_id(p),),
                    ).fetchone()
                    if (
                        row
                        and self.db.execute(
                            "UPDATE agent_requests SET status='claimed', updated_at=? "
                            "WHERE id=? AND status='pending'",
                            (time.time(), row["id"]),
                        ).rowcount
                        == 1
                    ):
                        return {"request": {**self.row(row["id"]), **spec_view(row["target"])}}
                left = end - time.monotonic()
                if left <= 0:
                    return {"request": None}
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), min(left, 2.0))
                except TimeoutError:
                    pass

        @r.post("/helper/requests/{rid}/result")
        async def result(rid: int, body: ResultIn, x_live_clipper_helper: str = Header("")):
            p = self.check_helper(x_live_clipper_helper)
            self.last_seen = time.time()
            for v in body.values.values():
                register_secret(v)  # masked in every log line from here on
            self.expire()  # an answer after the deadline is refused even if nobody looked at the list
            spec = TARGETS[self.row(rid)["target"]]["fields"]
            with self._lock:
                cur = self.row(rid)
                mine = self.db.execute("SELECT pair_id FROM agent_requests WHERE id=?", (rid,)).fetchone()[0]
                if cur["status"] != "claimed" or mine != pair_id(p):
                    raise HTTPException(409, f"this request is {cur['status']}; nothing was saved")
                names = {f for f, _, _ in spec}
                values = {k: v.strip() for k, v in body.values.items() if k in names and v and v.strip()}
                if body.error:
                    return self.finish(rid, "failed", SOURCE_ERRORS.get(body.error, GENERIC_SOURCE_ERROR))
                missing = [f for f, _, req in spec if req and f not in values]
                if missing:  # save nothing: half a set of keys would only fail later
                    return self.finish(
                        rid, "missing", "Some keys were not found in that source.", missing=missing
                    )
                self.finish(rid, "saving")
            try:
                saved = await self.apply(cur["target"], values)
            except HTTPException as ex:  # fixed texts only: a platform's reply is not kept
                log.info("agent setup for %s not accepted (%s)", cur["target"], ex.status_code)
                return self.finish(
                    rid,
                    "invalid" if ex.status_code < 500 else "failed",
                    NOT_ACCEPTED if ex.status_code < 500 else NOT_SAVED,
                )
            except Exception:
                log.exception("agent setup for %s failed while saving", cur["target"])
                return self.finish(rid, "failed", NOT_SAVED)
            log.info("agent setup saved %s for %s", ", ".join(saved), cur["target"])
            return self.finish(rid, "done", "Saved.", saved=saved)

        return r
