"""The preferred social route: Instagram and TikTok through an outside scheduling service (Metricool) that a local
sender program operates, with the direct routes (publish/instagram.py, tiktok.py) as the alternative.

Nothing here talks to that service and nothing is inferred from a preference. The state is what the sender itself
says and what it has confirmed:
- preference: settings key `social.route`, "metricool" (default) or "direct". "direct" is the skip.
- connected: the sender wrote a fresh, verified heartbeat to `<state folder>/social-sender.json`
  ({"name": "metricool", "verified": true, "networks": ["instagram", "tiktok"], "at": <epoch seconds>}). Deleting the
  file (or a stale or unverified one) is revoking it.
- confirmed publications: the counts in the heartbeat's `published`, which the sender computes only from receipts whose
  planner entry was read back as PUBLISHED with a public URL on the right host. A claim row in the shared claims
  table, a scheduled post, a native or test receipt is never counted. Unconfirmed: the sender's claims still pending.
While the sender is configured (its heartbeat names the networks it `owns`) the app does not send those networks
(Publisher.block_reason), also when the heartbeat goes stale or the sender is offline: only an explicit skip, or the
file being deleted, frees them. One sender per network. A clean install brings the sender, a Metricool account with the brand and networks connected, and a
way for that service to get the video (a public https address or the service's own uploader); see `needs`.
"""
from __future__ import annotations

import json
import sqlite3
import time

NETWORKS = ("instagram", "tiktok")
ROUTES = ("metricool", "direct")
SENDER = "metricool"
FRESH_S = 600.0
SKEW_S = 60.0
HEARTBEAT = "social-sender.json"
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
    def __init__(self, ctx):
        self.ctx = ctx

    def preference(self) -> str:
        v = self.ctx.settings.get("social.route", "metricool")
        return v if v in ROUTES else "metricool"

    def heartbeat(self) -> dict | None:
        try:
            d = json.loads((self.ctx.paths.state / HEARTBEAT).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) and d.get("name") == SENDER else None

    @staticmethod
    def _alive(hb: dict | None) -> bool:
        """Verified, and written within the last FRESH_S seconds; a timestamp in the future never passes."""
        try:
            age = time.time() - float(hb.get("at"))
        except (AttributeError, TypeError, ValueError):
            return False
        return bool(hb and hb.get("verified") is True and -SKEW_S <= age <= FRESH_S)

    def owned(self) -> set[str]:
        """Networks the configured sender posts: the app leaves them alone, alive or not (no fall-through to the
        direct route on a stale heartbeat or an outage). An explicit skip frees them."""
        hb = self.heartbeat()
        if hb is None or self.preference() != "metricool":
            return set()
        return {n for n in (hb.get("owns") or hb.get("networks") or []) if n in NETWORKS}

    def receipts(self) -> tuple[dict, dict]:
        """(confirmed, unconfirmed) per network. Confirmed is what the sender reports as read back PUBLISHED."""
        hb = self.heartbeat() or {}
        confirmed = {n: c for n, c in (hb.get("published") or {}).items()
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
        pref, hb = self.preference(), self.heartbeat()
        owned = sorted(self.owned()) if self._alive(hb) else []
        if pref != "metricool":
            state = "skipped"
        elif owned:
            state = "connected"
        elif hb:
            state = "waiting"                          # a sender has been seen, but it is not verified or not alive
        else:
            state = "not_connected"
        confirmed, unconfirmed = self.receipts()
        return {"preference": pref, "state": state, "networks": owned,
                "label": "Metricool (preferred for Instagram and TikTok)", "needs": NEEDS,
                "confirmed": confirmed, "unconfirmed": unconfirmed}
