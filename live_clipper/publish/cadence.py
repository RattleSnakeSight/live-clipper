"""The streamer rules of live_clipper.moments, applied where a clip is actually sent: the upload claim.

- Different moments of one streamer are at least two hours apart across every real account. The same moment may
  go to its other accounts at once. An upload that is not confirmed yet holds the streamer's other moments.
- An account gets a moment once: a delivery of the same moment that reached the provider blocks another one.

Legacy: an installation patched by hand before these rules were wired has a table `local_english_claims`, a
trigger that refuses an upload without a claim row in it, and possibly an outside publisher writing to it. When
the table exists it is honoured and kept in step, and this module never creates it:
- every row of another owner counts: a pending one holds the streamer, a confirmed one starts its two hours, and
  a row for the same moment or the same file (SHA-256) on the network is a duplicate whether or not it still
  holds the interval (`occupies`);
- a native upload writes the row the trigger asks for, with the file's SHA-256, and the row follows its delivery
  from then on (settle);
- a row that version left pending on a delivery that ended without a post keeps holding the streamer until that
  delivery is retried: only its owner knows what happened to it.
Without the table none of this applies: the moment rule covers re-cuts and re-renders.
"""
from __future__ import annotations

import time

from .. import moments
from ..store import Store as ClipStore

LEGACY = "local_english_claims"
NATIVE = "native:"        # the row of delivery N is owned by "native:N"; any other owner is an outside publisher
RECHECK_S = 60.0          # how soon to look again while another moment waits for its provider's answer
REREAD_S = 2.0            # how soon to come back with the hash of a file that changed after it was read


def _delivery(owner: str) -> int | None:
    n = owner[len(NATIVE):]
    return int(n) if owner.startswith(NATIVE) and n.isdigit() else None


class Cadence:
    def __init__(self, db):
        self.db, self.clips = db, ClipStore(db)

    def _legacy(self) -> bool:
        return bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (LEGACY,)).fetchone())

    def _may_be_posted(self, owner: str) -> bool:
        """Whether the delivery behind a native row reached, or may have reached, its provider."""
        n = _delivery(owner)
        row = self.clips.q1("SELECT id, status, reconcile, provider_ref FROM deliveries WHERE id=?", n) if n else None
        return bool(row) and moments.sent_evidence(row)

    def check(self, d: dict, dest: dict, now: float, digest: str = "") -> tuple[str, float, str] | None:
        """None: this upload may start now. Otherwise ("duplicate" | "wait", when to look again, why).
        Call inside the claim's transaction, right before the delivery becomes `uploading`. digest: the SHA-256 of
        the clip file, read by the caller before the transaction; a file is never read here, under the write lock."""
        cid, did = d["clip_id"], d["id"]
        clip = moments.clip_meta(self.clips, cid)
        if not clip:                                   # a clip the clip store never saw has no streamer or moment
            return None
        moment, creator = moments.moment_id(self.clips, cid), moments.creator_key(self.clips, clip["channel_id"])
        same = set(moments.moment_clips(self.clips, cid))
        account = self.clips.q1("SELECT * FROM destinations WHERE id=?", dest["id"])
        held, _ = moments._occupancy(self.clips, moments.keys_for(account), dest["id"])
        sent = {r["id"]: r for r in held if r["id"] != did and moments.sent_evidence(r)}  # this account's other posts
        legacy = self._legacy()
        rows = []
        if legacy:
            if not digest:                             # the file changed after it was read: read it, then come back
                return "wait", now + REREAD_S, "the clip file changed just now; it is read again before the upload"
            ids = ",".join("?" * len(same))
            rows = self.db.execute(
                f"SELECT owner, network, creator, moment, clip_id, digest, post_time, pending, occupies FROM {LEGACY} "
                f"WHERE owner<>? AND (creator=? OR moment=? OR clip_id IN ({ids}) OR (network=? AND lower(digest)=?))",
                (f"{NATIVE}{did}", creator, moment, *same, dest["provider"], digest)).fetchall()
        native = [r for r in rows if r["owner"].startswith(NATIVE)]        # their deliveries say where they went
        outside = [r for r in rows if not r["owner"].startswith(NATIVE)]
        here = [r for r in outside if r["network"] == dest["provider"]]    # an outside row names a network only
        # the old table knows files by their hash: one file goes to a network once, whatever clip carries it
        if any(r["digest"].lower() == digest for r in here) or any(
                r["digest"].lower() == digest and _delivery(r["owner"]) in sent for r in native):
            return "duplicate", 0.0, ("this network already has the same file (the same SHA-256) from another clip; "
                                      "a file is posted once")
        if any(r["clip_id"] in same for r in sent.values()) or any(
                r["clip_id"] in same or (r["creator"] == creator and r["moment"] == moment) for r in here):
            return "duplicate", 0.0, "this account already has this moment of the streamer; one post per moment per account"
        left = next((r for r in native if r["creator"] == creator and r["moment"] != moment and r["occupies"]
                     and r["pending"] and _delivery(r["owner"]) and not self._may_be_posted(r["owner"])), None)
        if left:
            return "wait", now + RECHECK_S, (f"delivery {_delivery(left['owner'])} of this streamer ended while the "
                                             "previous version still held the streamer for it; press Retry on that "
                                             "delivery to settle it")
        events = moments.creator_events(self.clips, cid, exclude=(("d", did),)) + [
            {"t": float(r["post_time"]), "moment": r["moment"], "provisional": bool(r["pending"])}
            for r in outside if r["creator"] == creator and r["occupies"]]
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
                            (f"{NATIVE}{did}", dest["provider"], creator, moment, cid, digest, now))
        return None

    def settle(self, d: dict) -> None:
        """The old table's row of a delivery says what the delivery says. Called after every change of a delivery,
        in the same transaction: a confirmed post stops being pending and carries the time it was confirmed
        public; a delivery that did not reach its provider (failed, cancelled, or the provider shows nothing of
        it) frees the streamer; anything still open (in flight, uncertain, waiting for confirmation) keeps its
        claim."""
        if not self._legacy():
            return
        if d["status"] in moments.FINAL_DELIVERY:
            self.db.execute(f"UPDATE {LEGACY} SET pending=0, post_time=max(post_time, ?) WHERE owner=?",
                            (moments.delivery_time(d)[0], f"{NATIVE}{d['id']}"))
        elif not moments.sent_evidence(d):
            self.db.execute(f"UPDATE {LEGACY} SET pending=0, occupies=0 WHERE owner=?", (f"{NATIVE}{d['id']}",))
