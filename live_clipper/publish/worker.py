"""Delivery worker: queue, state machine, retries, reconciliation.

Delivery status
  queued -> uploading -> processing -> published | private | draft
  held (manual mode: waits for the user's Post click) -> queued
  queued/processing  --transport error / ambiguous--> uncertain --reconcile--> (adopt | queued | needs_confirmation)
  any active --auth expired, refresh failed--> paused (destination needs_reconnect) --reconnect--> previous
  any non-terminal state of a destination the user disabled --> blocked (no provider call of any kind) --enable--> previous
  failed / cancelled / disallowed are terminal for the worker (manual retry re-queues; a re-queue always reconciles first).
Invariant: once an upload attempt has started (reconcile=1), no new upload happens until the provider has been asked
whether the earlier attempt landed (reconcile() is None) or the user confirmed it did not.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from . import quota
from .base import FINAL_STATES, DestinationAdapter, PublishJob, PublishResult, idempotency_key

log = logging.getLogger(__name__)

ACTIVE = ("waiting_language", "queued", "uploading", "processing", "uncertain")
HOLD = ("paused", "needs_confirmation", "blocked", "held", "disallowed")
REAL_PLATFORMS = ("youtube", "tiktok", "instagram")
TEST = "test"
SUCCESS = ("published", "private", "draft")
PROVIDER_BUSY = ("uploading", "processing", "uncertain", "needs_confirmation")
TERMINAL = ("published", "private", "draft", "failed", "cancelled")
HISTORY_MAX = 40
SENSITIVE_REFS = {"session", "upload_url"}
TEST_LABEL = "Test destination — nothing is posted"


def default_factories() -> dict[str, Callable]:
    from .instagram import InstagramAdapter
    from .sandbox import SandboxAdapter
    from .tiktok import TikTokAdapter
    from .youtube import YouTubeAdapter
    return {c.provider: (lambda s, app, c=c: c(s, app_settings=app))
            for c in (SandboxAdapter, YouTubeAdapter, TikTokAdapter, InstagramAdapter)}


class _Paused(Exception):
    pass


class _Unverified(Exception):
    """Credentials changed or were never verified: nothing is sent until connect() verifies them."""


class _Superseded(Exception):
    """The destination's credentials changed while a refresh or provider call was awaited."""


class _Blocked(Exception):
    """Raised before any provider effect when the destination is disabled / not acknowledged."""


class PublishConflict(Exception):
    pass


def refresh_hint(dest: dict, why: str) -> str:
    if not why:
        return ""
    hint = ""
    if dest.get("provider") == "youtube" and "invalid_grant" in why:
        hint = (" - Google revokes refresh tokens after 7 days while the OAuth consent screen is in 'Testing', and "
                "when the user removes access or changes the password; publish the consent screen or reconnect")
    elif dest.get("provider") == "instagram":
        hint = " - long-lived Instagram tokens can only be refreshed while still valid; reconnect if it lapsed"
    return f" (refresh failed: {why}{hint})"


class Publisher:
    def __init__(self, store, settings, secrets, adapters: dict[str, Callable] | None = None,
                 clock: Callable[[], float] = time.time, max_attempts: int = 6, sandbox_dir=None,
                 notify: Callable[[str, str], None] | None = None,
                 emit: Callable[[dict], None] | None = None):
        self.store, self.settings, self.secrets, self.clock = store, settings, secrets, clock
        self.sandbox_dir = sandbox_dir
        self.factories = {**default_factories(), **(adapters or {})}
        self.max_attempts = max_attempts
        self.notify, self.emit = notify or (lambda t, b: None), emit or (lambda p: None)
        self.refresh_errors: dict = {}
        self._adapters: dict[int, tuple[str, DestinationAdapter]] = {}
        self._sems: dict[int, asyncio.Semaphore] = {}
        self._inflight: set[int] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._recovered = False

    # ---- plumbing --------------------------------------------------------
    def adapter(self, dest: dict) -> DestinationAdapter:
        key = json.dumps(dest["settings"], sort_keys=True)
        hit = self._adapters.get(dest["id"])
        if hit and hit[0] == key:
            return hit[1]
        if dest["provider"] not in self.factories:
            raise KeyError(f"unknown provider {dest['provider']!r}")
        a = self.factories[dest["provider"]](dest["settings"], self.sandbox_dir)
        self._adapters[dest["id"]] = (key, a)
        return a

    def creds(self, dest: dict) -> dict:
        return {**(self.secrets.get("app", dest["provider"]) or {}), **(self.secrets.get("dest", dest["id"]) or {})}

    def save_tokens(self, dest_id: int, tok: dict) -> None:
        keep = {k: v for k, v in tok.items() if k not in ("token_type",) and v is not None}
        if keep.get("expires_in"):
            keep["expires_at"] = self.clock() + float(keep.pop("expires_in"))
        self.secrets.update("dest", dest_id, keep)

    def replace_tokens(self, dest_id: int, tok: dict) -> None:
        """A new access token never inherits the previous account's refresh token or expiry."""
        if tok.get("access_token"):
            stale = {k: None for k in ("refresh_token", "expires_at")
                     if k not in tok and not (k == "expires_at" and tok.get("expires_in"))}
            if stale:
                self.secrets.update("dest", dest_id, stale)
        self.save_tokens(dest_id, tok)

    def log(self, msg: str, level: str = "info") -> None:
        msg = self.secrets.redact(msg)
        getattr(log, {"warn": "warning"}.get(level, level), log.info)("%s", msg)
        try:
            self.store.db.event({"warn": "warning"}.get(level, level), "publish", msg)
        except Exception:
            pass

    def _ensure_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop, self._sems, self._wake = loop, {}, asyncio.Event()

    def wake(self) -> None:
        if self._loop and self._wake and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)

    def _set(self, did: int, status: str, detail: str = "", **fields) -> None:
        with self.store._lock:
            d = self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did)
            h = d["history"] + [{"t": round(self.clock(), 3), "from": d["status"], "to": status,
                                 "detail": self.secrets.redact(detail)[:300]}]
            if "last_error" in fields:
                fields["last_error"] = self.secrets.redact(fields["last_error"])[:500]
            self.store.update("deliveries", did, status=status, history=h[-HISTORY_MAX:], **fields)
            d.update(fields, status=status)
        self._emit(d, detail)

    def _emit(self, d: dict, detail: str = "") -> None:
        dest = self.store.q1("SELECT provider, label FROM publish_destinations WHERE id=?", d["destination_id"]) or {}
        p = {"delivery_id": d["id"], "clip_id": d["clip_id"], "destination": dest.get("provider", ""),
             "destination_id": d["destination_id"], "state": d["status"], "test": dest.get("provider") == TEST}
        if d.get("provider_url"):
            p["url"] = d["provider_url"]
        err = d.get("last_error") or (self.secrets.redact(detail)[:300] if d["status"] in ("failed", "needs_confirmation")
                                      else "")
        if err:
            p["error"] = err
        try:
            self.emit(p)
        except Exception:
            log.exception("publish.delivery emit failed")

    def _backoff(self, dest: dict, attempts: int) -> float:
        base = float(dest["settings"].get("backoff_base", 10.0))
        return min(900.0, base * 2 ** max(0, attempts - 1)) * (0.5 + random.random() / 2)

    # ---- clip inputs -----------------------------------------------------
    @staticmethod
    def video_path(clip: dict | None) -> Path | None:
        p = Path(clip["path"]) if clip and clip.get("path") else None
        return p if p and p.is_file() and p.stat().st_size > 0 else None

    def media_fp(self, clip: dict | None) -> str:
        p = self.video_path(clip)
        if not p:
            return ""
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return f"v1:{p.stat().st_size}:{h.hexdigest()[:16]}"

    def holds_provider_bytes(self, clip_id) -> list[dict]:
        """Deliveries for which a provider may hold or later resume this clip's bytes: the clip file must not be
        deleted while any exists (used by the storage cleanup)."""
        out = []
        for r in self.store.q("SELECT id, destination_id, language, status, reconcile FROM publish_deliveries "
                              "WHERE clip_id=?", clip_id):
            if r["status"] in SUCCESS or r["status"] == "cancelled" and not r["reconcile"]:
                continue
            if r["status"] in PROVIDER_BUSY or r["reconcile"] or r["id"] in self._inflight:
                out.append(r)
        return out

    # ---- eligibility -----------------------------------------------------
    @property
    def mode(self) -> str:
        return "manual" if self.settings.get("publish.mode", "auto") == "manual" else "auto"

    def block_reason(self, dest: dict) -> str | None:
        """Why no provider effect may happen for this destination (None = allowed)."""
        if dest.get("provider") == TEST:
            return None
        if not dest.get("rights_ack_at"):
            return "the rights/permission acknowledgement for this destination is missing"
        if not dest.get("enabled"):
            return "destination is disabled"
        return None

    def social_blocked(self, dest: dict) -> bool:
        return self.block_reason(dest) is not None

    def platform_reason(self, clip: dict | None, dest: dict) -> str | None:
        """Per-channel allowed destinations (`publish.channel_platforms`: {channel_id: [provider, ...]}; absent = all).
        The test destination is never restricted."""
        if not clip or dest.get("provider") == TEST:
            return None
        allowed = (self.settings.get("publish.channel_platforms") or {}).get(str(clip.get("channel_id")))
        if allowed is not None and dest["provider"] not in allowed:
            return f"channel {clip.get('channel_id')} only allows publishing to: {', '.join(allowed) or 'none'}"
        return None

    def eligible(self, dest: dict) -> bool:
        return self.block_reason(dest) is None and dest["status"] in ("connected", "needs_reconnect")

    def _job(self, d: dict, clip: dict, dest: dict) -> PublishJob:
        lang = d["language"]
        title = (clip.get("title") or f"Clip {clip['clip_id']}")
        desc = clip.get("description") or title
        tags = [t.lstrip("#") for t in (clip.get("hashtags") or []) if t]
        if tags:
            desc += "\n\n" + " ".join(f"#{t}" for t in tags)
        try:
            ref = json.loads(d["provider_ref"] or "{}")
            ref = ref if isinstance(ref, dict) else {"id": str(ref)}
        except ValueError:
            ref = {"id": d["provider_ref"]}
        did = d["id"]
        ref.update(self.secrets.get("ref", did) or {})

        def checkpoint(patch: dict) -> None:
            secret = {k: v for k, v in patch.items() if k in SENSITIVE_REFS}
            if secret:
                self.secrets.update("ref", did, {k: v or None for k, v in secret.items()})
            patch = {k: v for k, v in patch.items() if k not in SENSITIVE_REFS}
            if not patch:
                return
            with self.store._lock:
                row = self.store.q1("SELECT provider_ref FROM publish_deliveries WHERE id=?", did)
                try:
                    cur = json.loads(row["provider_ref"] or "{}")
                except ValueError:
                    cur = {}
                cur.update(patch)
                self.store.update("deliveries", did, provider_ref=json.dumps(cur))

        def may_write() -> str | None:        # evaluated at the moment of each write
            row = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest["id"])
            return self.block_reason(row) if row else "destination deleted"
        return PublishJob(did, d["idempotency_key"], self.video_path(clip) or Path("/nonexistent"), title, desc,
                          lang, None, dict(dest["settings"]), ref, checkpoint, media_fp=self.media_fp(clip),
                          may_write=may_write)

    # ---- enqueue ---------------------------------------------------------
    def enqueue_clip(self, clip: dict) -> list[int]:
        """Called for every edit.clip_ready: one delivery per eligible destination. Manual mode holds them."""
        self.store.save_clip(clip)
        ids = []
        for dest in self.store.q("SELECT * FROM publish_destinations ORDER BY id"):
            if self.eligible(dest) and not self.platform_reason(clip, dest):
                try:
                    ids.append(self._insert(clip, dest, clip.get("language") or "en", self.mode, 1))
                except PublishConflict as e:
                    self.log(f"clip {clip.get('clip_id')} not queued for '{dest['label']}': {e}", "warn")
        return ids

    def enqueue(self, clip_id, destination_id: int, language: str = "en", mode: str = "manual") -> int:
        """Explicit enqueue (user chose this destination); raises PermissionError when it may not be used."""
        clip = self.store.clip(clip_id, language)
        dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", destination_id)
        if not clip or not dest:
            raise KeyError("clip or destination not found")
        if why := self.block_reason(dest) or self.platform_reason(clip, dest):
            raise PermissionError(why)
        return self._insert(clip, dest, language, mode, 1, held=False)

    def _insert(self, clip: dict, dest: dict, language: str, mode: str, version: int, held: bool | None = None) -> int:
        held = (mode == "manual") if held is None else held
        with self.store.tx():
            same = self.store.q1("SELECT id FROM publish_deliveries WHERE clip_id=? AND destination_id=? AND "
                                 "language=? AND version=?", clip["clip_id"], dest["id"], language, version)
            if same:
                return same["id"]
            if not self.video_path(clip):
                raise PublishConflict("the clip file does not exist (any more)")
            key = idempotency_key(f"clip:{clip['clip_id']}", dest["id"], language) + (f":v{version}" if version > 1 else "")
            now = self.clock()
            status = "held" if held else "queued"
            if dest["status"] == "needs_reconnect":
                status = "paused"
            note = "held for manual post" if status == "held" else f"enqueued ({mode})"
            hist = json.dumps([{"t": round(now, 3), "from": "", "to": status, "detail": note}])
            self.store.x("INSERT INTO publish_deliveries(clip_id,destination_id,language,idempotency_key,status,mode,"
                         "history,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                         clip["clip_id"], dest["id"], language, key, status, mode, hist, now, now, version)
            row = self.store.q1("SELECT * FROM publish_deliveries WHERE clip_id=? AND destination_id=? AND language=? "
                                "AND version=?", clip["clip_id"], dest["id"], language, version)
        self._emit(row)
        self.wake()
        return row["id"]

    # ---- user actions ----------------------------------------------------
    def _guard(self, did: int) -> dict:
        d = self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did)
        if not d:
            raise ValueError("delivery not found")
        dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", d["destination_id"])
        if dest and (why := self.block_reason(dest)):
            raise PermissionError(why)
        if did in self._inflight:
            raise ValueError("the provider is being contacted for this delivery right now; wait for the result")
        return d

    def post(self, did: int) -> None:
        """The user's Post click on a held delivery (manual mode)."""
        d = self._guard(did)
        if d["status"] != "held":
            raise ValueError("only held deliveries can be posted")
        dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", d["destination_id"])
        self._set(did, "paused" if dest and dest["status"] == "needs_reconnect" else "queued",
                  "released by the user (manual post)", mode="manual", next_attempt_at=0)
        self.wake()

    def post_clip(self, clip_id, destination_id: int, language: str = "en") -> int:
        """Post a clip to a destination that has no delivery yet (e.g. connected after the clip was finished)."""
        did = self.enqueue(clip_id, destination_id, language, mode="manual")
        self.wake()
        return did

    def retry(self, did: int) -> None:
        d = self._guard(did)
        if d["status"] not in ("failed", "cancelled", "needs_confirmation", "paused", "held", "disallowed"):
            raise ValueError("only failed, cancelled, paused, held, disallowed or needs_confirmation deliveries "
                             "can be retried")
        if d["status"] == "held":
            return self.post(did)
        if d["status"] == "needs_confirmation":
            # read-only re-check: may adopt the provider post, never uploads again
            with self.store._lock:
                try:
                    ref = json.loads(d["provider_ref"] or "{}")
                except ValueError:
                    ref = {}
                ref = ref if isinstance(ref, dict) else {"id": str(ref)}
                self.store.update("deliveries", did, provider_ref=json.dumps({**ref, "recheck_only": True}))
            self._set(did, "uncertain", "manual retry: re-checking the provider (read-only)", attempts=0,
                      next_attempt_at=0)
        else:
            self._set(did, "queued", "manual retry", attempts=0, next_attempt_at=0)
        self.wake()

    def confirm_not_posted(self, did: int) -> None:
        d = self._guard(did)
        if d["status"] not in ("needs_confirmation", "uncertain", "failed"):
            raise ValueError("delivery is not awaiting confirmation")
        self._set(did, "queued", "user confirmed the earlier attempt did not post", reconcile=0,
                  provider_ref="{}", next_attempt_at=0)
        self.secrets.delete("ref", did)
        self.wake()

    def confirm_posted(self, did: int, url: str = "") -> None:
        d = self._guard(did)
        if d["status"] not in ("needs_confirmation", "uncertain"):
            raise ValueError("delivery is not awaiting confirmation")
        dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", d["destination_id"])
        self._set(did, "published", "user confirmed the post exists", provider_url=url or d["provider_url"],
                  outcome="confirmed by user" if dest["provider"] != TEST else "test", next_attempt_at=0,
                  last_error="")
        self.secrets.delete("ref", did)
        self._after_posted(self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did), dest)

    def cancel(self, did: int) -> None:
        d = self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did)
        if not d:
            raise ValueError("delivery not found")
        if d["status"] in TERMINAL:
            raise ValueError("delivery is already finished")
        if did in self._inflight or d["status"] in ("uploading", "processing", "uncertain") or d["reconcile"]:
            raise ValueError("an upload may be in flight at the provider; wait, or use confirm-posted / "
                             "confirm-not-posted")
        self._set(did, "cancelled", "cancelled by the user", next_attempt_at=0)

    # ---- destination states ------------------------------------------------
    def _block(self, did: int, prev: str) -> None:
        prev = {"uploading": "uncertain"}.get(prev, prev)
        d = self.store.q1("SELECT destination_id FROM publish_deliveries WHERE id=?", did)
        dest = d and self.store.q1("SELECT * FROM publish_destinations WHERE id=?", d["destination_id"])
        why = (self.block_reason(dest) if dest else None) or "destination is disabled"
        self._set(did, "blocked", why, blocked_from=prev, next_attempt_at=0)

    def unblock_allowed(self) -> int:
        n = 0
        for d in self.store.q("SELECT d.id, d.blocked_from, d.reconcile, t.provider, t.enabled, t.rights_ack_at FROM publish_deliveries d JOIN "
                              "publish_destinations t ON t.id=d.destination_id WHERE d.status='blocked'"):
            if self.social_blocked(d):
                continue
            prev = d["blocked_from"] or "queued"
            if prev == "queued" and d["reconcile"]:
                prev = "uncertain"
            self._set(d["id"], prev if prev in ACTIVE + HOLD and prev != "blocked" else "queued",
                      "destination enabled again", blocked_from="", next_attempt_at=0)
            n += 1
        self.wake()
        return n

    def block_destination(self, dest_id: int) -> int:
        """The user disabled a destination: everything not finished stops (evidence kept)."""
        n = 0
        for d in self.store.q("SELECT id, status, reconcile FROM publish_deliveries WHERE destination_id=? AND status "
                              "IN ('queued','uploading','processing','uncertain','paused')", dest_id):
            if d["status"] in ("uploading", "processing"):
                self.store.update("deliveries", d["id"], reconcile=1)
            self._block(d["id"], d["status"])
            n += 1
        return n

    def resume_destination(self, dest_id: int) -> int:
        n = 0
        for d in self.store.q("SELECT id, history FROM publish_deliveries WHERE destination_id=? AND status='paused'",
                              dest_id):
            prev = next((h["from"] for h in reversed(d["history"]) if h["to"] == "paused"), "queued")
            prev = {"uploading": "uncertain", "processing": "uncertain"}.get(prev, prev)
            prev = prev if prev in ACTIVE else "queued"
            dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest_id)
            if dest and self.social_blocked(dest):
                self._block(d["id"], prev)
            else:
                self._set(d["id"], prev, "destination reconnected", next_attempt_at=0)
            n += 1
        self.wake()
        return n

    @staticmethod
    def identity_reason(dest: dict | None) -> str | None:
        """Connected, and verified under the current credential generation (bumped on credential replacement)."""
        if not dest:
            return "destination deleted"
        if dest["status"] != "connected":
            return f"destination is {dest['status']}: verify the account before anything is sent"
        s = dest.get("settings") or {}
        if int((s.get("account_info") or {}).get("verified_gen", 0) or 0) != int(s.get("cred_gen", 0) or 0):
            return "credentials changed since the account was verified"
        return None

    def begin_credential_change(self, dest_id: int, why: str) -> int:
        with self.store._lock:
            d = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest_id)
            if not d:
                raise KeyError("destination not found")
            st = dict(d["settings"] or {})
            st["cred_gen"] = int(st.get("cred_gen", 0) or 0) + 1
            self.store.update("destinations", dest_id, settings=st, status="unverified",
                              status_detail=f"{why}: verifying the account before anything is sent")
            return st["cred_gen"]

    def _pause_unverified(self, did: int, why: str) -> None:
        self._set(did, "paused", f"held until the destination account is verified: {why}", next_attempt_at=0)

    def mark_needs_reconnect(self, dest: dict, detail: str) -> None:
        rows = self.store.q(f"SELECT id FROM publish_deliveries WHERE destination_id=? AND status IN "
                            f"({','.join('?' * len(ACTIVE))})", dest["id"], *ACTIVE)
        msg = (f"{dest['provider']} access for '{dest['label']}' expired or was revoked and could not be refreshed"
               f"{refresh_hint(dest, self.refresh_errors.get(dest['id'], ''))}. Reconnect the account "
               f"(Destinations > {dest['label']} > Connect) to resume {len(rows)} held deliveries.")
        self.store.update("destinations", dest["id"], status="needs_reconnect", status_detail=self.secrets.redact(msg))
        for r in rows:
            self._set(r["id"], "paused", "destination needs reconnect: " + detail)
        self.log(msg, "warn")
        if dest["provider"] != TEST:
            self.notify(f"Reconnect {dest['label']}", f"Posting to {dest['provider']} is on hold: access expired or "
                                                      f"was revoked. Open Destinations and click Connect.")

    # ---- auth ------------------------------------------------------------
    def _gen(self, dest_id: int) -> int | None:
        d = self.store.q1("SELECT settings FROM publish_destinations WHERE id=?", dest_id)
        return int((d["settings"] or {}).get("cred_gen", 0) or 0) if d else None

    async def _refresh(self, dest: dict, adapter: DestinationAdapter, creds: dict, gen: int | None = None) -> dict | None:
        gen = self._gen(dest["id"]) if gen is None else gen
        adapter.last_refresh_error = ""
        try:
            new = await adapter.refresh(creds)
        except Exception as e:
            if self._gen(dest["id"]) != gen:
                raise _Superseded() from e
            self.refresh_errors[dest["id"]] = self.secrets.redact(f"{type(e).__name__}: {e}")[:240]
            self.log(f"token refresh for '{dest['label']}' failed: {self.refresh_errors[dest['id']]}", "warn")
            return None
        if not new or not new.get("access_token"):
            why = adapter.last_refresh_error or ("no refresh token stored" if not creds.get("refresh_token")
                                                 else "provider returned no new access token")
            self.refresh_errors[dest["id"]] = self.secrets.redact(why)[:240]
        with self.store._lock:
            if self._gen(dest["id"]) != gen:
                raise _Superseded()
            if not new or not new.get("access_token"):
                return None
            self.save_tokens(dest["id"], {k: v for k, v in new.items() if k not in creds or creds[k] != v})
        self.refresh_errors.pop(dest["id"], None)
        self.log(f"refreshed access token for '{dest['label']}'")
        return self.creds(dest)

    async def _call(self, dest: dict, adapter: DestinationAdapter, fn):
        """Single choke point for every provider effect (upload/poll/reconcile)."""
        fresh = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest["id"])
        if fresh and self.block_reason(fresh) is not None:
            raise _Blocked()
        if why := self.identity_reason(fresh):
            raise _Unverified(why)
        gen = int((fresh["settings"] or {}).get("cred_gen", 0) or 0)

        def same_identity():
            cur = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest["id"])
            if not cur or int((cur["settings"] or {}).get("cred_gen", 0) or 0) != gen:
                raise _Superseded()
            if why := self.identity_reason(cur):
                raise _Unverified(why)

        creds = self.creds(dest)
        if creds.get("expires_at") and creds["expires_at"] < self.clock() + 120 and creds.get("refresh_token"):
            creds = await self._refresh(dest, adapter, creds, gen) or creds
            same_identity()
        res = await fn(creds)
        if isinstance(res, PublishResult) and res.state == "auth_expired":
            same_identity()
            new = await self._refresh(dest, adapter, creds, gen)
            if new:
                same_identity()
                res = await fn(new)
            if not new or (isinstance(res, PublishResult) and res.state == "auth_expired"):
                with self.store._lock:
                    if self._gen(dest["id"]) != gen:
                        raise _Superseded()
                    self.mark_needs_reconnect(self.store.q1("SELECT * FROM publish_destinations WHERE id=?",
                                                            dest["id"]), res.detail)
                raise _Paused()
        return res

    def _superseded(self, did: int) -> None:
        row = self.store.q1("SELECT status FROM publish_deliveries WHERE id=?", did)
        if not row:
            return
        msg = "credentials changed during this step; retrying with the verified account"
        if row["status"] == "uploading":
            self._set(did, "queued", msg, reconcile=1, next_attempt_at=self.clock() + 1)
        else:
            self._set(did, row["status"], msg, next_attempt_at=self.clock() + 1)
        self.wake()

    # ---- state machine ---------------------------------------------------
    async def process(self, did: int) -> None:
        d = self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did)
        dest = d and self.store.q1("SELECT * FROM publish_destinations WHERE id=?", d["destination_id"])
        if not d or d["status"] not in ("queued", "uncertain", "processing"):
            return
        if not dest:
            if d["reconcile"] or d["provider_ref"] not in ("", "{}"):
                return self._set(did, "needs_confirmation", "destination deleted; claim kept until reconciled")
            return self._set(did, "cancelled", "destination deleted")
        if dest["status"] != "connected":
            return
        if self.social_blocked(dest):
            return self._block(did, d["status"])
        clip = self.store.clip(d["clip_id"], d["language"])
        if why := self.platform_reason(clip, dest):
            if d["status"] == "queued" and not d["reconcile"]:
                return self._set(did, "disallowed", why, next_attempt_at=0)
            adapter, job = self.adapter(dest), self._job(d, clip, dest)
            try:
                res = await self._call(dest, adapter, lambda c: adapter.reconcile(job, c))
            except _Blocked:
                return self._block(did, d["status"])
            except _Unverified as e:
                return self._pause_unverified(did, str(e))
            except _Superseded:
                return self._superseded(did)
            except _Paused:
                return
            except (httpx.TransportError, httpx.TimeoutException) as e:
                return self._set(did, d["status"], f"reconcile: network error ({type(e).__name__})",
                                 next_attempt_at=self.clock() + self._backoff(dest, max(1, d["attempts"])))
            if res is not None and res.state in FINAL_STATES:
                return self._apply(d, dest, adapter, job, res, "reconcile")
            return self._set(did, "disallowed", f"{why}; provider shows no completed earlier post",
                             next_attempt_at=0, reconcile=0)
        if d["status"] == "queued" and not self.video_path(clip):
            msg = "the clip file is missing"
            return self._set(did, "failed", msg, last_error=msg, next_attempt_at=0)
        adapter = self.adapter(dest)
        job = self._job(d, clip, dest)
        phase = "start"
        try:
            if d["status"] == "processing":
                phase = "poll"
                res = await self._call(dest, adapter, lambda c: adapter.poll(job, job.ref.get("id", ""), c))
                return self._apply(d, dest, adapter, job, res, phase)
            if d["status"] == "uncertain" or d["reconcile"]:
                phase = "reconcile"
                res = await self._call(dest, adapter, lambda c: adapter.reconcile(job, c))
                if res is not None:
                    return self._apply(d, dest, adapter, job, res, phase)
                if job.ref.get("recheck_only"):
                    return self._set(did, "needs_confirmation", "re-check: the provider shows no post for this delivery"
                                     " | Check the account; if the clip is not there, use 'Confirm not posted' to "
                                     "upload again.", next_attempt_at=0)
                self._abandon_stale_session(did, job)
                self._set(did, "queued", "reconcile: provider has no earlier post; safe to upload", reconcile=0)
                job = self._job(self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did), clip, dest)
            phase = "upload"
            if job.ref.get("session") and job.ref.get("session_fp") != job.media_fp:
                return self._set(did, "uncertain", "stored upload session is for different media; reconciling first",
                                 reconcile=1, next_attempt_at=0)
            d = self._claim_upload(did)
            if d is None:
                return
            job = self._job(d, clip, dest)
            res = await self._call(dest, adapter, lambda c: adapter.upload(job, c))
            self._apply(d, dest, adapter, job, res, phase)
        except _Paused:
            return
        except _Unverified as e:
            return self._pause_unverified(did, str(e))
        except _Superseded:
            return self._superseded(did)
        except _Blocked:
            row = self.store.q1("SELECT status FROM publish_deliveries WHERE id=?", did)
            return self._block(did, row["status"] if row else d["status"])
        except (httpx.TransportError, httpx.TimeoutException) as e:
            # ambiguous: the provider may have accepted the request; never re-upload without reconciling
            back = self._backoff(dest, max(1, d["attempts"]))
            st = "processing" if phase == "poll" else "uncertain"
            if phase == "start" and d["status"] == "queued":
                st = "queued"
            self._set(did, st, f"{phase}: network error ({type(e).__name__}); will reconcile before retrying",
                      next_attempt_at=self.clock() + back, last_error=f"{type(e).__name__}: {e}")

    def _abandon_stale_session(self, did: int, job: PublishJob) -> None:
        if job.ref.get("session") and job.ref.get("session_fp") != job.media_fp:
            self.secrets.update("ref", did, {"session": None})
            job.save(session_fp="", sent_all=False)
            job.ref.pop("session", None)
            self.log(f"delivery {did}: dropped an unfinished upload session that belonged to older media", "warn")

    def _claim_upload(self, did: int) -> dict | None:
        """Atomically move queued -> uploading, re-checking everything under the database transaction."""
        with self.store.tx():
            d = self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did)
            dest = d and self.store.q1("SELECT * FROM publish_destinations WHERE id=?", d["destination_id"])
            if not d or d["status"] != "queued" or not dest:
                return None
            clip = self.store.clip(d["clip_id"], d["language"])
            if self.social_blocked(dest):
                self._block(did, "queued")
                return None
            if why := self.identity_reason(dest):
                self._pause_unverified(did, why)
                return None
            if not clip or not self.video_path(clip):
                self._set(did, "failed", "the clip file is missing", last_error="the clip file is missing",
                          next_attempt_at=0)
                return None
            if why := self.platform_reason(clip, dest):
                self._set(did, "disallowed", why, next_attempt_at=0)
                return None
            now = self.clock()
            limit = quota.daily_limit(dest) if dest["provider"] != TEST else 0
            if limit and quota.used(self.store, dest, now) >= limit:
                at = quota.next_reset(dest, now)
                self._set(did, "queued", f"daily upload quota reached ({limit}); waiting for the provider day to reset",
                          next_attempt_at=at)
                return None
            attempts = d["attempts"] + 1
            if limit:
                quota.add(self.store, dest, now, 1)
            self._set(did, "uploading", f"upload attempt {attempts}", attempts=attempts, reconcile=1, last_error="",
                      media_version=1, claimed_at=now)
            return {**d, "attempts": attempts, "status": "uploading", "media_version": 1}

    def _after_posted(self, d: dict, dest: dict) -> None:
        if dest["provider"] == TEST:
            return
        if self.settings.get("publish.notifications", True):
            clip = self.store.clip(d["clip_id"], d["language"]) or {}
            self.notify(f"Posted to {dest['label']}", f"{clip.get('title') or 'Clip'} → {d['outcome'] or d['status']}"
                        + (f" {d['provider_url']}" if d.get("provider_url") else ""))

    def _apply(self, d: dict, dest: dict, adapter: DestinationAdapter, job: PublishJob, res: PublishResult,
               phase: str) -> None:
        did, now = d["id"], self.clock()
        if res.expired:                      # a write was refused (destination disabled meanwhile); nothing is public
            self.log(f"delivery {did}: {res.detail}", "warn")
            return self._set(did, "cancelled", res.detail[:300], next_attempt_at=0, last_error="")
        if res.provider_ref and not job.ref.get("id"):
            job.save(id=res.provider_ref)
        if res.state == "uncertain" and res.keep_reconciling:
            n = int(job.ref.get("reconcile_tries", 0)) + 1
            job.save(reconcile_tries=n)
            if n < 8:
                return self._set(did, "uncertain", res.detail, reconcile=1, last_error=res.detail,
                                 next_attempt_at=now + self._backoff(dest, n))
            return self._set(did, "needs_confirmation", res.detail + " | Check the account; if the clip is not there, "
                             "use 'Confirm not posted' to upload again.", last_error=res.detail)
        if res.state in ("accepted", "processing"):
            pi = float(dest["settings"].get("poll_interval", 5.0))
            self._set(did, "processing", res.detail or "accepted; provider is processing", reconcile=1,
                      next_attempt_at=now + pi)
        elif res.state in FINAL_STATES:
            outcome = adapter.outcome_label or {"published": "public"}.get(res.state, res.state)
            extra = {}
            if res.public_at and float(d.get("claimed_at") or 0) - 600 <= res.public_at <= now + 600:
                extra["public_at"] = float(res.public_at)
            self._set(did, res.state, res.detail or f"{res.state} on provider", provider_post_id=res.post_id,
                      provider_url=res.url, outcome=outcome, next_attempt_at=0, last_error="", **extra)
            self.log(f"delivery {did} -> {dest['label']}: {outcome} {res.url}".strip())
            if self.secrets.get("ref", did):
                self.secrets.delete("ref", did)
            self._after_posted(self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did), dest)
        elif res.state == "rate_limited":
            wait = res.retry_after if res.retry_after is not None else self._backoff(dest, d["attempts"])
            st = {"poll": "processing", "reconcile": d["status"]}.get(phase, "queued")
            if phase == "upload" and dest["provider"] != TEST and quota.daily_limit(dest):
                quota.add(self.store, dest, now, -1)      # nothing was accepted: not a used upload
            self._set(did, st, f"rate limited by provider; retrying in {wait:.0f}s", next_attempt_at=now + wait,
                      last_error=res.detail)
        elif res.state == "uncertain" or (phase == "reconcile" and res.state == "failed" and res.permanent
                                          and not res.rejected):
            if phase == "reconcile":
                self._set(did, "needs_confirmation", res.detail + " | Check the account; if the clip is not there, "
                          "use 'Confirm not posted' to upload again.", last_error=res.detail)
                self.log(f"delivery {did} needs confirmation: {res.detail}", "warn")
            else:
                self._set(did, "uncertain", res.detail, next_attempt_at=now + self._backoff(dest, d["attempts"]))
        else:  # failed (auth_expired is handled in _call)
            pid = job.ref.get("id", "")
            if res.rejected:
                if pid:
                    job.save(id="", rejected_id=pid)
                self.secrets.update("ref", did, {k: None for k in SENSITIVE_REFS})
                job.save(recheck_only=False, session_fp="", sent_all=False, publish_id="", container="", media_id="")
                self._set(did, "failed", res.detail, last_error=res.detail, next_attempt_at=0, reconcile=0)
                self.log(f"delivery {did} rejected by the provider: {res.detail}", "error")
                return
            if phase == "poll" or pid or phase == "reconcile":
                # a known provider id (or a possibly-accepted earlier attempt) never becomes `failed` on an HTTP error
                if not res.permanent:
                    key = "poll_errors" if phase == "poll" else "reconcile_errors"
                    n = int(job.ref.get(key, 0)) + 1
                    job.save(**{key: n})
                    if n < (20 if phase == "poll" else 8):
                        return self._set(did, "processing" if phase == "poll" else "uncertain", res.detail,
                                         last_error=res.detail, next_attempt_at=now + self._backoff(dest, min(n, 6)))
                what = f"provider id {pid}" if pid else "an earlier upload attempt"
                return self._set(did, "needs_confirmation", f"{res.detail} | the provider holds {what} but its status "
                                 f"could not be read; verify the post (Re-check) before anything else",
                                 last_error=res.detail, next_attempt_at=0)
            if phase == "upload" and dest["provider"] != TEST and quota.daily_limit(dest):
                quota.add(self.store, dest, now, -1)
            if res.permanent or d["attempts"] >= self.max_attempts:
                self._set(did, "failed", res.detail, last_error=res.detail, next_attempt_at=0)
                self.log(f"delivery {did} failed: {res.detail}", "error")
            else:
                self._set(did, "queued", res.detail, last_error=res.detail,
                          next_attempt_at=now + self._backoff(dest, d["attempts"]))

    # ---- scheduling ------------------------------------------------------
    def recover(self) -> None:
        """At startup: anything mid-flight may or may not have reached the provider."""
        self._recovered = True
        self.unblock_allowed()
        for d in self.store.q("SELECT d.id, d.status, t.provider, t.enabled, t.rights_ack_at FROM publish_deliveries d JOIN publish_destinations t ON "
                              "t.id=d.destination_id WHERE d.status IN ('uploading','processing','uncertain','queued',"
                              "'needs_confirmation')"):
            if self.social_blocked(d):
                if d["status"] in ("uploading", "processing"):
                    self.store.update("deliveries", d["id"], reconcile=1)
                self._block(d["id"], "uncertain" if d["status"] in ("uploading", "processing") else d["status"])
            elif d["status"] in ("uploading", "processing"):
                self._set(d["id"], "uncertain", f"worker restarted during '{d['status']}'; reconciling before any upload",
                          reconcile=1, next_attempt_at=0)

    def _due(self) -> list[dict]:
        rows = self.store.q(
            "SELECT d.id, d.clip_id, d.destination_id, d.status, t.provider, t.enabled, t.rights_ack_at FROM publish_deliveries d JOIN "
            "publish_destinations t ON t.id=d.destination_id "
            "WHERE d.status IN ('queued','uncertain','processing') AND d.next_attempt_at<=? AND t.status='connected' "
            "ORDER BY CASE d.status WHEN 'uncertain' THEN 0 WHEN 'processing' THEN 1 ELSE 2 END, d.id", self.clock())
        blocked = {r["destination_id"] for r in self.store.q(
            "SELECT DISTINCT destination_id FROM publish_deliveries WHERE status='uncertain'")}
        out = []
        for r in rows:
            if r["id"] in self._inflight:
                continue
            if self.social_blocked(r):
                self._block(r["id"], r["status"])
                continue
            if not (r["status"] == "queued" and r["destination_id"] in blocked):
                out.append(r)
        return out

    async def recheck(self, did: int) -> str:
        """Read-only provider check of an existing delivery; NEVER uploads or reposts."""
        d = self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did)
        if not d or d["status"] not in ("published", "private", "draft", "uncertain", "processing", "needs_confirmation"):
            raise ValueError("only an existing (sent or uncertain) delivery can be re-checked")
        dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", d["destination_id"])
        clip = self.store.clip(d["clip_id"], d["language"])
        if not dest or not clip:
            raise ValueError("destination or clip not found")
        if did in self._inflight:
            raise ValueError("the provider is being contacted for this delivery right now")
        adapter, job = self.adapter(dest), self._job(d, clip, dest)
        ref = job.ref.get("id") or ""
        self._inflight.add(did)
        try:
            res = await self._call(dest, adapter, (lambda c: adapter.status(job, ref, c)) if ref else
                                   (lambda c: adapter.reconcile(job, c)))
        except (_Blocked, _Unverified, _Superseded, _Paused) as e:
            raise ValueError(f"not checked: {e or type(e).__name__}") from e
        finally:
            self._inflight.discard(did)
        if res is not None and res.state in ("published", "private", "draft", "failed"):
            if res.state == "failed" and not res.permanent:
                return d["status"]
            self._apply(d, dest, adapter, job, res, "reconcile")
        return (self.store.q1("SELECT status FROM publish_deliveries WHERE id=?", did) or {}).get("status", d["status"])

    def spawn_due(self) -> list[asyncio.Task]:
        self._ensure_loop()
        if not self._recovered:
            self.recover()
        tasks = []
        for r in self._due():
            self._inflight.add(r["id"])
            tasks.append(asyncio.create_task(self._run_one(r["id"], r["destination_id"])))
        return tasks

    async def _run_one(self, did: int, dest_id: int) -> None:
        dest = self.store.q1("SELECT settings FROM publish_destinations WHERE id=?", dest_id)
        sem = self._sems.setdefault(dest_id, asyncio.Semaphore(int((dest or {}).get("settings", {}).get("concurrency", 1))))
        try:
            async with sem:
                await self.process(did)
        except asyncio.CancelledError:
            raise
        except Exception as e:      # adapter bug etc.: treat as ambiguous, keep the reconcile guard
            self.log(f"delivery {did}: internal error {type(e).__name__}: {e}", "error")
            row = self.store.q1("SELECT status FROM publish_deliveries WHERE id=?", did)
            if row and row["status"] in ("uploading", "queued", "uncertain"):
                self._set(did, "uncertain", f"internal error: {type(e).__name__}: {e}",
                          next_attempt_at=self.clock() + 60, last_error=str(e))
            elif row and row["status"] == "processing":
                self._set(did, "processing", f"internal error while polling: {type(e).__name__}: {e}",
                          next_attempt_at=self.clock() + 60, last_error=str(e))
        finally:
            self._inflight.discard(did)
            self.wake()

    async def start(self) -> None:
        self._ensure_loop()
        self.recover()
        self._task = asyncio.create_task(self._run(), name="publisher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                self.spawn_due()
            except Exception as e:
                self.log(f"publisher loop error: {e}", "error")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except TimeoutError:
                pass
            self._wake.clear()

    async def drain(self, timeout: float = 10.0, horizon: float = 2.0) -> None:
        """Process until nothing is due within `horizon` seconds (tests / CLI)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            tasks = self.spawn_due()
            if tasks:
                await asyncio.gather(*tasks)
                continue
            r = self.store.q1("SELECT MIN(d.next_attempt_at) AS n FROM publish_deliveries d JOIN publish_destinations t "
                              "ON t.id=d.destination_id WHERE d.status IN ('queued','uncertain','processing') "
                              "AND t.status='connected'")
            if r["n"] is None or r["n"] - self.clock() > horizon:
                return
            await asyncio.sleep(0.02)

    # ---- destinations ----------------------------------------------------
    def add_destination(self, provider: str, label: str, settings: dict | None = None) -> int:
        return self.store.x("INSERT INTO publish_destinations(provider,label,settings,created_at) VALUES(?,?,?,?)",
                            provider, label, json.dumps(settings or {}), self.clock())

    async def connect(self, dest_id: int) -> dict:
        """Verify stored credentials with the provider and mark the destination connected (or not)."""
        dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest_id)
        gen = int((dest["settings"] or {}).get("cred_gen", 0) or 0)
        adapter, creds = self.adapter(dest), self.creds(dest)
        try:
            info = await adapter.account(creds)
        except Exception as e:
            try:
                new = await self._refresh(dest, adapter, creds, gen) if creds.get("refresh_token") else None
            except _Superseded:
                return {"ok": False, "detail": "credentials changed during verification; verify again"}
            if not new:
                msg = self.secrets.redact(f"could not verify account: {type(e).__name__}: {e}"
                                          f"{refresh_hint(dest, self.refresh_errors.get(dest_id, ''))}")
                self.store.update("destinations", dest_id, status="error", status_detail=msg)
                return {"ok": False, "detail": msg}
            info = await adapter.account(new)
        with self.store._lock:
            cur = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest_id)
            if not cur or int((cur["settings"] or {}).get("cred_gen", 0) or 0) != gen:
                return {"ok": False, "detail": "credentials changed during verification; verify again"}
            ai = {k: v for k, v in info.items() if k not in ("account", "ok")}
            ai["verified_gen"] = gen
            self.store.update("destinations", dest_id, account=str(info.get("account", "")),
                              settings={**cur["settings"], "account_info": ai}, status="connected",
                              status_detail=info.get("notes", ""))
        n = self.resume_destination(dest_id)
        self.log(f"destination '{dest['label']}' connected as {info.get('account')}"
                 + (f"; resumed {n} deliveries" if n else ""))
        return {"ok": True, **info}

    def disconnect(self, dest_id: int) -> None:
        """Forget the tokens (app credentials stay), disable the destination, hold unfinished deliveries."""
        dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest_id)
        if not dest:
            raise KeyError("destination not found")
        self.secrets.update("dest", dest_id, {k: None for k in (self.secrets.get("dest", dest_id) or {})})
        self.begin_credential_change(dest_id, "disconnected")
        self.store.update("destinations", dest_id, status="disconnected", enabled=0, account="",
                          status_detail="disconnected by the user")
        self.block_destination(dest_id)

    async def ensure_test_destination(self) -> int:
        """The labelled local test destination, auto-connected to the in-process sandbox. Nothing is posted."""
        d = self.store.q1("SELECT id FROM publish_destinations WHERE provider=? ORDER BY id LIMIT 1", TEST)
        if d:
            return d["id"]
        did = self.add_destination(TEST, TEST_LABEL, {"poll_interval": 1.0})
        dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", did)
        self.save_tokens(did, await self.adapter(dest).issue_test_token())
        await self.connect(did)
        self.store.update("destinations", did, enabled=1)
        return did
