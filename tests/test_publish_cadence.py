"""Streamer cadence at the upload claim: two hours between different moments of a streamer, one post per moment
per account, open uploads hold the streamer, and a database from an installation patched by hand keeps working.

Real worker and real clip store; the provider is the local sandbox. Nothing leaves this computer."""
import time

from publish_util import Clock, World, make_clip, no_keyring, sb, videos  # noqa: F401

from live_clipper.publish.cadence import Cadence
from live_clipper.store import Store as ClipStore

GAP = 7200.0
# the table, trigger and rows an installation repaired by hand on 0.3.0 carries (structure only, synthetic rows)
LEGACY_SCHEMA = [
    """CREATE TABLE local_english_claims(
        owner TEXT PRIMARY KEY, network TEXT NOT NULL, creator TEXT NOT NULL,
        moment TEXT NOT NULL, clip_id INTEGER NOT NULL, digest TEXT NOT NULL,
        post_time REAL NOT NULL, occupies INTEGER NOT NULL DEFAULT 1, pending INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX local_english_creator ON local_english_claims(creator,post_time)",
    """CREATE TRIGGER local_require_english_claim
        BEFORE UPDATE OF status,attempts ON publish_deliveries
        WHEN NEW.language='en' AND NEW.status='uploading' AND NEW.attempts>OLD.attempts
        AND EXISTS(SELECT 1 FROM publish_destinations WHERE id=NEW.destination_id
                   AND provider IN ('youtube','instagram','tiktok'))
        AND NOT EXISTS(SELECT 1 FROM local_english_claims WHERE owner='native:'||NEW.id
                       AND clip_id=NEW.clip_id AND occupies=1 AND pending=1)
        BEGIN SELECT RAISE(ABORT,'English publishing requires an active duplicate and cadence check'); END""",
]


class Studio:
    """A worker with the cadence rules on, a controllable clock, and streamers whose clips are real clip rows."""

    def __init__(self, sb):
        self.clock = Clock()
        self.w = World(sb, clock=self.clock)
        self.w.pub.cadence = Cadence(self.w.ctx.db)
        self.clips = ClipStore(self.w.ctx.db)
        self.n = 0

    def streamer(self, name: str) -> int:
        return self.clips.add_channel("kick", name, f"https://kick.com/{name}")

    def clip(self, channel: int, at: float | None = None) -> dict:
        """A finished clip of a new moment (moments further apart than a few minutes are different moments)."""
        self.n += 1
        at = at if at is not None else self.clock.t - 600 + 300 * self.n
        cid = self.clips.create_clip(channel, f"k{self.n}", "chat_spike", {}, action_time=at, window_start=at - 20,
                                     window_end=at + 10, status="ready")
        return {**make_clip(self.w.ctx, channel_id=str(channel)), "clip_id": cid}

    def send(self, clip: dict) -> list[dict]:
        ids = self.w.pub.enqueue_clip(clip)
        self.w.drain(horizon=0)
        return [self.w.row(i) for i in ids]


def test_a_different_moment_waits_two_hours_and_the_same_moment_finishes_its_accounts(sb):
    s = Studio(sb)
    s.w.pub.factories["tiktok"] = s.w.pub.factories["test"]
    yt, tt = s.w.dest(provider="youtube"), s.w.dest(provider="tiktok")
    alpha = s.streamer("streamer_alpha")
    first = s.send(s.clip(alpha))
    said = [(d["status"], d["history"][-1]["detail"]) for d in first]
    assert [d["status"] for d in first] == ["published", "published"], said   # one moment, both accounts, at once
    assert {d["destination_id"] for d in first} == {yt, tt} and len(videos(sb)) == 2

    second = s.send(s.clip(alpha))                                         # another moment of the same streamer
    assert all(d["status"] == "queued" and d["attempts"] == 0 for d in second)
    posted = max(s.w.row(d["id"])["claimed_at"] for d in first)
    assert all(posted + GAP - 5 <= d["next_attempt_at"] <= s.clock.t + GAP + 5 for d in second)
    assert "two hours between different moments" in second[0]["history"][-1]["detail"] and len(videos(sb)) == 2

    s.clock.t += GAP - 60                                                  # just before the boundary: still waiting
    s.w.drain(horizon=0)
    assert all(s.w.row(d["id"])["status"] == "queued" for d in second) and len(videos(sb)) == 2
    s.clock.t += 120                                                       # just after it
    s.w.drain(horizon=0)
    assert all(s.w.row(d["id"])["status"] == "published" for d in second) and len(videos(sb)) == 4


def test_streamers_have_independent_intervals_and_the_fresh_moment_goes_first(sb):
    s = Studio(sb)
    s.w.dest(provider="youtube")
    alpha, beta = s.streamer("streamer_alpha"), s.streamer("streamer_beta")
    assert s.send(s.clip(alpha))[0]["status"] == "published"
    assert s.send(s.clip(beta))[0]["status"] == "published"                # another streamer is not held back
    old = s.send(s.clip(alpha))[0]
    s.clock.t += 600
    fresh = s.send(s.clip(alpha, at=s.clock.t - 30))[0]
    assert old["status"] == fresh["status"] == "queued"
    s.clock.t += GAP + 60                                                  # the gap opens: one moment may go
    s.w.drain(horizon=0)
    assert s.w.row(fresh["id"])["status"] == "published"                   # the fresh one, not the oldest
    assert s.w.row(old["id"])["status"] == "queued" and s.w.row(old["id"])["attempts"] == 0


def test_an_unconfirmed_upload_holds_the_streamer_and_a_restart_or_retry_sends_nothing_twice(sb):
    s = Studio(sb)
    s.w.dest(provider="youtube")
    alpha = s.streamer("streamer_alpha")
    first = s.send(s.clip(alpha))[0]
    assert first["status"] == "published" and len(videos(sb)) == 1
    # the provider's answer to an upload is lost: the worker may not know whether the post exists
    s.w.store.update("deliveries", first["id"], status="needs_confirmation", reconcile=1)
    s.clock.t += 3 * GAP                                                   # however long it stays open
    waiting = s.send(s.clip(alpha))[0]
    assert waiting["status"] == "queued" and "not confirmed" in waiting["history"][-1]["detail"]
    # a restart changes nothing: the rule reads the database, not the worker's memory
    s.w.pub.cadence = Cadence(s.w.ctx.db)
    s.w.drain(horizon=0)
    assert s.w.row(waiting["id"])["status"] == "queued" and len(videos(sb)) == 1
    # the owner confirms the post exists: that is when it counts as posted, and nothing is uploaded for it
    s.w.pub.confirm_posted(first["id"], "")
    s.clock.t += 61                                                        # the next look, a minute later
    s.w.drain(horizon=0)
    # resolved, and that post is more than two hours old: the next moment goes, once
    assert s.w.row(waiting["id"])["status"] == "published" and len(videos(sb)) == 2


def test_an_account_gets_a_moment_once(sb):
    s = Studio(sb)
    s.w.dest(provider="youtube")
    alpha = s.streamer("streamer_alpha")
    at = s.clock.t - 300
    one = s.send(s.clip(alpha, at=at))[0]
    recut = s.send(s.clip(alpha, at=at + 4))[0]                            # an overlapping cut of the same moment
    assert one["status"] == "published" and recut["status"] == "disallowed" and len(videos(sb)) == 1
    assert "one post per moment per account" in recut["history"][-1]["detail"]


def test_a_database_patched_by_hand_keeps_uploading_and_honours_its_outside_claims(sb):
    s = Studio(sb)
    yt = s.w.dest(provider="youtube")
    alpha = s.streamer("streamer_alpha")
    db = s.w.ctx.db
    for stmt in LEGACY_SCHEMA:
        db.execute(stmt)
    history = db.execute("SELECT COUNT(*) FROM publish_deliveries").fetchone()[0]
    # without a claim row the old trigger refuses the upload; the worker now writes the row it asks for
    first = s.send(s.clip(alpha))[0]
    assert first["status"] == "published" and len(videos(sb)) == 1
    row = db.execute("SELECT * FROM local_english_claims WHERE owner=?", (f"native:{first['id']}",)).fetchone()
    assert (row["network"], row["creator"], row["pending"], row["occupies"]) == ("youtube", f"channel:{alpha}", 0, 1)
    assert len(row["digest"]) == 64                                        # the file's hash, as the old table keeps it
    # the same file under another clip and moment is refused on this network by that hash
    twin = s.clip(alpha, at=s.clock.t + 9000)
    twin["path"] = s.w.store.clip(first["clip_id"])["path"]
    s.clock.t += GAP + 120
    assert s.send(twin)[0]["status"] == "disallowed" and len(videos(sb)) == 1
    # an outside publisher's pending claim for another moment of the streamer holds this account's next moment
    db.execute("INSERT INTO local_english_claims VALUES('outside:7','tiktok',?,'m-other',9001,'',?,1,1)",
               (f"channel:{alpha}", s.clock.t))
    s.clock.t += 2 * GAP
    held = s.send(s.clip(alpha))[0]
    assert held["status"] == "queued" and "not confirmed" in held["history"][-1]["detail"]
    db.execute("UPDATE local_english_claims SET pending=0, post_time=? WHERE owner='outside:7'", (s.clock.t,))
    s.clock.t += GAP + 120                                                 # two hours after the outside post
    s.w.drain(horizon=0)
    assert s.w.row(held["id"])["status"] == "published" and len(videos(sb)) == 2
    # an outside claim for the same moment on this network is a duplicate here
    dup = s.clip(alpha)
    moment = s.clips.clip(dup["clip_id"])["trigger"].get("moment_id") or ""
    s.clock.t += GAP + 120
    if not moment:
        from live_clipper import moments
        moment = moments.moment_id(s.clips, dup["clip_id"])
    db.execute("INSERT INTO local_english_claims VALUES('outside:8','youtube',?,?,?,'',?,1,0)",
               (f"channel:{alpha}", moment, dup["clip_id"], s.clock.t - 3 * GAP))
    assert s.send(dup)[0]["status"] == "disallowed" and len(videos(sb)) == 2
    # a failed delivery frees its claim; nothing of the earlier history was touched
    lost = s.w.pub.enqueue_clip(s.clip(alpha))[0]
    db.execute("INSERT INTO local_english_claims VALUES(?, 'youtube', ?, 'm-lost', 1, '', ?, 1, 1)",
               (f"native:{lost}", f"channel:{alpha}", s.clock.t))
    s.w.pub._set(lost, "failed", "the platform refused it")
    freed = db.execute("SELECT pending, occupies FROM local_english_claims WHERE owner=?", (f"native:{lost}",)).fetchone()
    assert tuple(freed) == (0, 0)
    assert db.execute("SELECT COUNT(*) FROM publish_deliveries").fetchone()[0] >= history + 4
    assert s.w.store.q1("SELECT enabled FROM publish_destinations WHERE id=?", yt)["enabled"] == 1


def test_going_back_to_the_patched_version_finds_its_claims_and_history(sb):
    """Rollback rehearsal: after the new worker ran, the old trigger still accepts what the old package would do,
    every native post has its claim row, and no delivery row was rewritten or removed."""
    s = Studio(sb)
    s.w.dest(provider="youtube")
    alpha = s.streamer("streamer_alpha")
    db = s.w.ctx.db
    for stmt in LEGACY_SCHEMA:
        db.execute(stmt)
    done = s.send(s.clip(alpha))[0]
    before = dict(s.w.row(done["id"]))
    s.w.pub.cadence = None                                                 # the old package: no new rules
    assert dict(s.w.row(done["id"])) == before                             # history as it was
    claims = db.execute("SELECT owner, pending, occupies FROM local_english_claims").fetchall()
    assert [tuple(c) for c in claims] == [(f"native:{done['id']}", 0, 1)]
    # the old package's own next upload: it writes its claim, then the trigger lets it through
    nxt = s.w.pub.enqueue_clip(s.clip(alpha))[0]
    db.execute("INSERT INTO local_english_claims VALUES(?, 'youtube', ?, 'm-next', ?, '', ?, 1, 1)",
               (f"native:{nxt}", f"channel:{alpha}", s.w.row(nxt)["clip_id"], s.clock.t))
    s.w.drain(horizon=0)
    assert s.w.row(nxt)["status"] == "published"


def test_a_clip_the_clip_store_never_saw_is_not_held(sb):
    s = Studio(sb)
    s.w.dest(provider="youtube")
    a, b = (s.w.pub.enqueue_clip(make_clip(s.w.ctx))[0] for _ in range(2))  # no streamer, no moment
    s.w.drain(horizon=0)
    assert s.w.row(a)["status"] == s.w.row(b)["status"] == "published"


def test_the_test_destination_is_never_held(sb):
    s = Studio(sb)
    s.w.dest()                                                             # provider "test"
    alpha = s.streamer("streamer_alpha")
    assert [s.send(s.clip(alpha))[0]["status"] for _ in range(2)] == ["published", "published"]
    assert time.time() - s.clock.t < 5
