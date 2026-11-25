"""The preferred social route: Instagram and TikTok through an outside scheduling service (Metricool) that a local
sender program operates, with the direct routes (publish/instagram.py, tiktok.py) as the alternative.

Nothing here talks to that service and nothing is inferred from a preference. The state is what the sender itself
says and what it has confirmed:
- preference: settings key `social.route`, "metricool" (default) or "direct". "direct" is the skip.
- connected: the sender wrote a fresh, verified heartbeat to `<state folder>/social-sender.json`
  ({"name": "metricool", "verified": true, "networks": ["instagram", "tiktok"], "at": <epoch seconds>}). A stale,
  unverified, unreadable or missing file means the sender is not running now; it hands nothing back.
- confirmed publications: the counts in the heartbeat's `published`, which the sender computes only from receipts whose
  planner entry was read back as PUBLISHED with a public URL on the right host. A claim row in the shared claims
  table, a scheduled post, a native or test receipt is never counted. Unconfirmed: the sender's claims still pending.
Ownership is kept apart from liveness. The networks the sender's file names (`owns`, else `networks`; a list), verified
or not, are recorded in app_state `social.owned` the first time they are seen, and from then on the app does not send
them (Publisher.block_reason) whatever the file says later: stale, broken, wrong types or gone. Verification and
freshness decide only "connected". Only the user's explicit skip (route "direct") clears the record. With no sender
file, the default preference claims nothing. One sender per network. A clean install brings the sender, a Metricool account with the brand
and networks connected, and a way for that service to get the video (a public https address or the service's own
uploader); see `needs`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

NETWORKS = ("instagram", "tiktok")
ROUTES = ("metricool", "direct")
SENDER = "metricool"
FRESH_S = 600.0
SKEW_S = 60.0
HEARTBEAT = "social-sender.json"
OWNED = "social.owned"                                 # app_state: networks a verified sender registered
NEEDS = [
    "A Metricool account with your brand and your Instagram and TikTok accounts connected to it.",
    "The local sender program for Metricool, running on this computer. It tells this app it is connected; the app "
    "never sends to Metricool itself.",
    "A way for Metricool to get each video: a public https address, or the sender's own signed-in upload. Metricool's "
    "program interface is on paid plans; its terms do not allow scripted reading of its web pages without written "
    "permission.",
    "None of this is needed to use the app: skip it and connect Instagram and TikTok directly, or post to YouTube only.",
]


class Social:
    def __init__(self, ctx, lock=None):
        self.ctx = ctx
        self._lock = lock if lock is not None else threading.RLock()
        self._write_failed = False
        self._seen: set[str] = set()

    def preference(self) -> str:
        v = self.ctx.settings.get("social.route", "metricool")
        return v if v in ROUTES else "metricool"

    def read(self) -> tuple[str, dict | None]:
        """One snapshot of the sender's file: ("ok", body) for this sender, else ("missing" | "unreadable" | "other",
        None). Every decision works from one snapshot."""
        try:
            d = json.loads((self.ctx.paths.state / HEARTBEAT).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "missing", None
        except (OSError, ValueError):
            return "unreadable", None
        if not isinstance(d, dict):
            return "unreadable", None
        return ("ok", d) if d.get("name") == SENDER else ("other", None)

    @staticmethod
    def _alive(hb: dict | None) -> bool:
        """Verified, and written within the last FRESH_S seconds; a timestamp in the future never passes."""
        try:
            age = time.time() - float(hb.get("at"))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False
        return bool(hb and hb.get("verified") is True and -SKEW_S <= age <= FRESH_S)

    @staticmethod
    def _named(hb: dict | None) -> set[str]:
        v = (hb.get("owns") or hb.get("networks")) if hb else None
        return {n for n in v if isinstance(n, str) and n in NETWORKS} if isinstance(v, list) else set()

    def _registered(self) -> set[str]:
        """The recorded networks. A record that cannot be read holds both networks (fail closed)."""
        try:
            v = self.ctx.app_state.get(OWNED)
        except sqlite3.Error:
            return set(NETWORKS)
        return {n for n in v if isinstance(n, str) and n in NETWORKS} if isinstance(v, list) else set()

    def owned(self, snap: tuple[str, dict | None] | None = None) -> set[str]:
        """Networks the sender posts: the app leaves them alone, alive or not (no fall-through to the direct route on
        a stale, unverified or broken heartbeat or an outage). The networks the sender's file names are recorded for
        good; only the user's skip (release) frees them. A bad heartbeat or a failed write of the record never raises;
        a failed write is retried on the next call."""
        with self._lock:
            if self.preference() != "metricool":
                return set()
            hb = (snap or self.read())[1]
            kept = self._registered()
            self._seen |= self._named(hb)                  # held in memory too, until the record is written
            want = kept | self._seen
            if not want <= kept:
                try:
                    self.ctx.app_state.set(OWNED, sorted(want))
                    log.info("social sender registered for %s; only skipping the route hands them back",
                             ", ".join(sorted(want)))
                except sqlite3.Error as e:
                    if not self._write_failed:
                        log.warning("could not record the social sender's networks (%s); holding them in memory", e)
                    self._write_failed = True
                else:
                    self._write_failed = False
            return want

    def set_route(self, route: str) -> None:
        """Change preference and release ownership together; a failed transaction releases nothing.

        The service shares the publisher's lock so this cannot invert its lock/DB transaction order.
        """
        if route not in ROUTES:
            raise ValueError("invalid social route")
        with self._lock:
            with self.ctx.db.tx():
                if route == "direct":
                    self.ctx.app_state.set(OWNED, [])
                self.ctx.settings.set("social.route", route)
            if route == "direct":
                self._seen.clear()
                self._write_failed = False
                log.info("social sender networks handed back by the user's skip")

    def receipts(self, hb: dict | None = None) -> tuple[dict, dict]:
        """(confirmed, unconfirmed) per network. Confirmed is what the sender reports as read back PUBLISHED."""
        hb = hb if hb is not None else self.read()[1]
        pub = (hb or {}).get("published")
        confirmed = {n: c for n, c in (pub if isinstance(pub, dict) else {}).items()
                     if n in NETWORKS and isinstance(c, int) and not isinstance(c, bool) and c > 0}
        open_: dict = {}
        try:
            rows = self.ctx.db.execute("SELECT network, pending FROM local_english_claims WHERE owner LIKE ? "
                                       "AND occupies=1", (f"{SENDER}:%",)).fetchall()
        except sqlite3.OperationalError:               # no shared claims table: nothing is waiting
            return confirmed, {}
        for net, pending in rows:
            if pending:
                open_[net] = open_.get(net, 0) + 1
        return confirmed, open_

    def state(self) -> dict:
        with self._lock:
            return self._state()

    def _state(self) -> dict:
        pref, snap = self.preference(), self.read()
        hb, owned = snap[1], sorted(self.owned(snap))
        alive = self._alive(hb) and bool(self._named(hb))      # verified, fresh, and saying which networks it posts
        if pref != "metricool":
            state = "skipped"
        elif alive:
            state = "connected"
        elif owned:
            state = "held"                             # its networks stay off until it reports again or the user skips
        elif hb:
            state = "waiting"                          # a sender has been seen, but it is not verified or not alive
        else:
            state = "not_connected"
        confirmed, unconfirmed = self.receipts(hb or {})
        return {"preference": pref, "state": state, "networks": owned if alive else [], "owned": owned,
                "sender": snap[0],
                "label": "Metricool (preferred for Instagram and TikTok)", "needs": NEEDS,
                "confirmed": confirmed, "unconfirmed": unconfirmed}
