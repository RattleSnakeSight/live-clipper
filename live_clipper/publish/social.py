"""The preferred social route: Instagram and TikTok through an outside scheduling service (Metricool) that a local
sender program operates, with the direct routes (publish/instagram.py, tiktok.py) as the alternative.

Nothing here talks to that service and nothing is inferred from a preference. The state is what the sender itself
says and what it has confirmed:
- preference: settings key `social.route`, "metricool" (default) or "direct". "direct" is the skip.
- connected: the sender wrote a fresh, verified heartbeat to `<state folder>/social-sender.json`
  ({"name": "metricool", "verified": true, "networks": ["instagram", "tiktok"], "at": <epoch seconds>}). Deleting the
  file (or a stale or unverified one) is revoking it.
- confirmed publications: rows the sender itself wrote to the shared claims table `local_english_claims` (owner
  "<name>:..."), settled (not pending). Native and test destination receipts never count here.
While the route is connected the app does not send the networks the sender owns (Publisher.block_reason): one sender
per network. A clean install brings the sender, a Metricool account with the brand and networks connected, and a
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
        try:
            return bool(hb and hb.get("verified") is True and time.time() - float(hb.get("at") or 0) <= FRESH_S)
        except (TypeError, ValueError):
            return False

    def owned(self) -> set[str]:
        """Networks a connected sender posts: the app leaves them alone."""
        hb = self.heartbeat()
        if hb is None or self.preference() != "metricool" or not self._alive(hb):
            return set()
        return {n for n in (hb.get("networks") or []) if n in NETWORKS}

    def receipts(self) -> tuple[dict, dict]:
        confirmed: dict = {}
        open_: dict = {}
        try:
            rows = self.ctx.db.execute("SELECT network, pending FROM local_english_claims WHERE owner LIKE ? "
                                       "AND occupies=1", (f"{SENDER}:%",)).fetchall()
        except sqlite3.OperationalError:               # no shared claims table: no sender has written receipts
            return {}, {}
        for net, pending in rows:
            bucket = open_ if pending else confirmed
            bucket[net] = bucket.get(net, 0) + 1
        return confirmed, open_

    def state(self) -> dict:
        pref, hb = self.preference(), self.heartbeat()
        owned = sorted(self.owned())
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
