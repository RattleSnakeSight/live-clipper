"""Replay adapter: plays a JSONL chat log in real time (or faster) relative to a start time.

Each line: {"t": seconds_from_start, "user": "...", "text": "...", "id": optional,
            "sent_offset": optional extra seconds the platform timestamp lags/leads,
            "deliver_delay": optional seconds the delivery is delayed (sent_at stays at t)}
Used for fixtures, detector tuning and the end-to-end acceptance test.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ..events import ChatEvent
from .base import ChatAdapter


def load(path: str | Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


class ReplayChat(ChatAdapter):
    platform = "replay"

    async def run(self) -> None:
        rows = self.options.get("rows") or load(self.options["path"])
        t0 = float(self.options.get("start_at") or time.time())
        speed = float(self.options.get("speed", 1.0))
        sched = sorted(((r["t"] + r.get("deliver_delay", 0.0), i, r) for i, r in enumerate(rows)), key=lambda x: (x[0], x[1]))
        self.health.state = "healthy"
        for due, i, r in sched:
            wait = t0 + due / speed - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            now = time.time()
            self.emit(ChatEvent(platform="replay", channel=self.channel, msg_id=str(r.get("id", i)),
                                user_id=str(r["user"]), user_name=str(r["user"]), text=r["text"],
                                received_at=now, sent_at=t0 + r["t"] / speed + r.get("sent_offset", 0.0),
                                ts_source="fixture"))
            self.health.last_message_at = now
        self.health.state, self.health.detail = "stopped", "replay finished"
