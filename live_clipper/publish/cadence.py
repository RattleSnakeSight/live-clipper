"""The streamer rules of live_clipper.moments, applied where a clip is actually sent: the upload claim.

- Different moments of one streamer are at least two hours apart across every real account. The same moment may
  go to its other accounts at once. An upload that is not confirmed yet holds the streamer's other moments.
- An account gets a moment once: a delivery of the same moment that reached the provider blocks another one.

Legacy: an installation patched by hand before these rules were wired has a table `local_english_claims`, a
trigger that refuses an upload without a claim row in it, and possibly an outside publisher writing to it. When
the table exists its rows count as posts and it is kept in step. This module never creates it.
"""
from __future__ import annotations

import hashlib
import time

from .. import moments
from ..store import Store as ClipStore

LEGACY = "local_english_claims"
RECHECK_S = 60.0          # how soon to look again while another moment waits for its provider's answer


class Cadence:
    def __init__(self, db):
        self.db, self.clips = db, ClipStore(db)

    def _legacy(self) -> bool:
        return bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (LEGACY,)).fetchone())

    def check(self, d: dict, dest: dict, now: float, media=None) -> tuple[str, float, str] | None:
        """None: this upload may start now. Otherwise ("duplicate" | "wait", when to look again, why).
        Call inside the claim's transaction, right before the delivery becomes `uploading`. media: the file."""
        cid, did = d["clip_id"], d["id"]
        clip = moments.clip_meta(self.clips, cid)
        if not clip:                                   # a clip the clip store never saw has no streamer or moment
            return None
        moment, creator = moments.moment_id(self.clips, cid), moments.creator_key(self.clips, clip["channel_id"])
        same = set(moments.moment_clips(self.clips, cid))
        account = self.clips.q1("SELECT * FROM destinations WHERE id=?", dest["id"])
        held, _ = moments._occupancy(self.clips, moments.keys_for(account), dest["id"])
        legacy = self._legacy()
        outside = self.db.execute(f"SELECT owner, network, moment, clip_id, post_time, pending FROM {LEGACY} WHERE "
                                  "creator=? AND occupies=1 AND owner NOT LIKE 'native:%'", (creator,)).fetchall() \
            if legacy else []
        # the old table also knows files by their hash: the same file never goes to one network twice
        digest = ""
        if legacy and media:
            with open(media, "rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
        same_file = legacy and digest and self.db.execute(
            f"SELECT 1 FROM {LEGACY} WHERE network=? AND digest=? AND occupies=1 AND owner<>?",
            (dest["provider"], digest, f"native:{did}")).fetchone()
        if same_file or any(r["clip_id"] in same and r["id"] != did and moments.sent_evidence(r) for r in held) or any(
                r["network"] == dest["provider"] and (r["moment"] == moment or r["clip_id"] in same) for r in outside):
            return "duplicate", 0.0, "this account already has this moment of the streamer; one post per moment per account"
        events = moments.creator_events(self.clips, cid, exclude=(("d", did),)) + [
            {"t": float(r["post_time"]), "moment": r["moment"], "provisional": bool(r["pending"])} for r in outside]
        if any(e["moment"] != moment and e.get("provisional") for e in events):
            return "wait", now + RECHECK_S, "another moment of this streamer is not confirmed by its platform yet"
        free = moments._creator_earliest(events, moment, now)
        if free > now:
            at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(free))
            return "wait", free, f"two hours between different moments of the same streamer; this one can go at {at}"
        if legacy:                                     # the old trigger and an outside publisher read this row
            self.db.execute(f"INSERT INTO {LEGACY}(owner, network, creator, moment, clip_id, digest, post_time, occupies, "
                            "pending) VALUES(?,?,?,?,?,?,?,1,1) ON CONFLICT(owner) DO UPDATE SET occupies=1, pending=1, "
                            "digest=excluded.digest, post_time=max(post_time, excluded.post_time)",
                            (f"native:{did}", dest["provider"], creator, moment, cid, digest, now))
        return None

    def settle(self, d: dict) -> None:
        """A delivery reached an end state: confirmed posts stop being pending, failed and cancelled ones free
        the streamer. Anything still open (uncertain, waiting for confirmation) keeps its claim."""
        if not self._legacy():
            return
        if d["status"] in ("failed", "cancelled"):
            self.db.execute(f"UPDATE {LEGACY} SET pending=0, occupies=0 WHERE owner=?", (f"native:{d['id']}",))
        else:
            at = float(d.get("public_at") or d.get("claimed_at") or d.get("created_at") or 0)
            self.db.execute(f"UPDATE {LEGACY} SET pending=0, post_time=max(post_time, ?) WHERE owner=?",
                            (at, f"native:{d['id']}"))
