"""Posting policy as the selection core computes it (live_clipper.moments): 2 h minimum between DIFFERENT
moments of
the SAME canonical streamer across the whole rotation (streamer_alpha's Twitch + Kick are one streamer);
different
streamers may post close together, even on the same account and in overlapping slots; no per-account ceiling;
definitive failures release, unknown outcomes hold; atomic across store connections.

The publisher is another subsystem: World.queue / World.claim run the same moments checks its enqueue and
upload
claim run (conflict, next_slot, account_problem, creator_problem, primary_key) inside one store transaction.
Nothing is sent anywhere."""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from selstore import SelStore

from live_clipper import candidates, formats, moments

H = 3600.0
NOW = 1_900_000_000.0
_n = itertools.count(1)


def settings(tmp_path):
    data = tmp_path / "data"
    s = SimpleNamespace(
        data_dir=data,
        clips_dir=data / "clips",
        freshness_hours=24.0,
        freshness_margin_minutes=15,
        db_path=data / "select.db",
    )
    s.clips_dir.mkdir(parents=True, exist_ok=True)
    return s


def make_clip(store, s, channel_id, langs=("en",), score=5.0):
    """A clean test clip: gameplay approved by an operator, a final render on disk, coherent speech."""
    cid = store.create_clip(
        channel_id,
        f"{channel_id}:chat_spike:{next(_n)}",
        "chat_spike",
        {"score": score, "review": {"gameplay": {"state": "approved"}}},
        title="Huge heist escape",
        action_time=time.time() - (next(_n) % 120) * 300,
    )
    d = s.clips_dir / str(cid)
    d.mkdir(parents=True, exist_ok=True)
    files = {"final": {}}
    for lang in langs:
        files["final"][lang] = f"final_{lang}.mp4"
        (d / f"final_{lang}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(64))
    caps = {
        lang: {"srt": f"{lang}.srt", "title": f"Heist ({lang})", "status": "ready", "final_status": "ready"}
        for lang in langs
    }
    caps["asr"] = {
        "speech_status": "speech",
        "language": langs[0],
        "language_probability": 0.9,
        "original_check": {"status": "pass", "reason": "clean test clip"},
    }
    store.update("clips", cid, files=files, captions=caps)
    return cid


class FakePub:
    """Duck-typed publisher: what candidates.editorial / candidates.candidates / moments.plan call. Every
    native
    destination carries an implied auto-post rule at the selection format's min_score."""

    def __init__(self, store, s, clock):
        self.store, self.settings, self._clock = store, s, clock
        self.min_score = formats.active().scoring.min_score

    def clock(self):
        return self._clock()

    def video_path(self, clip, lang):
        f = (((clip or {}).get("files") or {}).get("final") or {}).get(lang)
        p = self.settings.clips_dir / str(clip["id"]) / f if f else None
        return p if p is not None and p.is_file() else None

    def media_missing_reason(self, clip, lang):
        return None

    def auto_reason(self, clip, lang, dest=None):
        return None

    def platform_reason(self, clip, dest):
        ch = self.store.channel((clip or {}).get("channel_id")) or {}
        allowed = (ch.get("settings") or {}).get("publish_platforms")
        p = moments.dest_platform(dest)
        if allowed is not None and p not in allowed:
            return f"channel routing: {p} is not one of this channel's platforms"
        return None

    def rule_reason(self, clip, dest, language):
        return candidates.below_threshold(clip, self.min_score)

    def block_reason(self, dest):
        return (
            "native publishing is off" if (dest["settings"] or {}).get("native_publishing") is False else None
        )

    def identity_reason(self, dest):
        return None if (dest["settings"] or {}).get("expected_account_id") else "no pinned account"


class World:
    def __init__(self, tmp_path):
        self.clock = [NOW]
        self.s = settings(tmp_path)
        self.store = SelStore(self.s.db_path)
        self.pub = FakePub(self.store, self.s, lambda: self.clock[0])
        st = self.store
        self.tw = st.add_channel("twitch", "streamer_alpha", "", {"simulcast_group": "streamer_alpha"})
        self.kk = st.add_channel("kick", "streamer_alpha", "", {"simulcast_group": "streamer_alpha"})
        self.others = [
            st.add_channel("twitch", n) for n in ("streamer_beta", "streamer_gamma", "a", "b", "c")
        ]
        self.yt = st.add_destination(
            "youtube", "YT EN", {"account_language": "en", "expected_account_id": "UC-A"}, "connected"
        )
        self.tt = st.add_destination(
            "tiktok", "TT EN", {"account_language": "en", "expected_account_id": "tt-b"}, "connected"
        )
        self.ext = st.add_destination(
            "external",
            "YT EN (external)",
            {"platform": "youtube", "expected_account_id": "UC" + "c" * 22, "account_language": "en"},
            "external",
        )

    def dest(self, did, store=None):
        return (store or self.store).q1("SELECT * FROM destinations WHERE id=?", did)

    def clip(self, ch, at, lang="en", score=5.0):
        cid = make_clip(self.store, self.s, ch, langs=(lang,), score=score)
        self.store.update(
            "clips", cid, status="ready", action_time=at, window_start=at - 18, window_end=at + 12
        )
        self.store.merge_json("clips", cid, "captions", {"policy": {"render_default": lang}})
        return cid

    def queue(self, cid, dest, mode="auto", lang="en", store=None):
        """The enqueue check: refused while the creator moment is held on that physical account."""
        st = store or self.store
        with st.tx():
            if why := moments.conflict(st, cid, self.dest(dest, st)):
                raise PermissionError(why)
            now = time.time()
            return st.x(
                "INSERT INTO deliveries(clip_id,destination_id,language,idempotency_key,status,mode,"
                "created_at,updated_at) VALUES(?,?,?,?,'queued',?,?,?)",
                cid,
                dest,
                lang,
                f"{cid}:{dest}:{lang}:{next(_n)}",
                mode,
                now,
                now,
            )

    def _set(self, st, did, status, detail, **fields):
        row = st.q1("SELECT status, history FROM deliveries WHERE id=?", did)
        hist = row["history"] + [{"t": self.clock[0], "from": row["status"], "to": status, "detail": detail}]
        st.update("deliveries", did, status=status, history=hist, **fields)

    def claim(self, did, store=None):
        """The upload claim's posting-policy part: moment conflict, then the streamer's 2 h spacing."""
        st = store or self.store
        now = self.clock[0]
        with st.tx():
            d = st.q1("SELECT * FROM deliveries WHERE id=?", did)
            if d["status"] not in ("queued",) or float(d["next_attempt_at"] or 0) > now:
                return None
            dest = self.dest(d["destination_id"], st)
            if why := moments.conflict(st, d["clip_id"], dest, delivery_id=did):
                self._set(st, did, "held", f"held: {why}", next_attempt_at=0)
                return None
            ex = (("d", did),)
            at = moments.next_slot(st, dest, now, exclude=ex, clip_id=d["clip_id"])
            if at > now:
                why = (
                    moments.account_problem(st, dest, now, ex)
                    or moments.creator_problem(st, d["clip_id"], now, ex)
                    or "posting policy"
                )
                self._set(
                    st,
                    did,
                    "queued",
                    f"deferred by posting policy until "
                    f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(at))} UTC: {why}",
                    next_attempt_at=at,
                )
                return None
            self._set(
                st,
                did,
                "uploading",
                "upload attempt 1",
                claimed_at=now,
                account_key=moments.primary_key(dest),
            )
            return {**d, "status": "uploading"}

    def drain(self):
        """Claim every due queued delivery in the native dispatch order (moments.rotation_key per account)."""
        sent = []
        while True:
            rows = self.store.q(
                "SELECT id, clip_id, destination_id FROM deliveries WHERE status='queued' AND "
                "next_attempt_at<=?",
                self.clock[0],
            )
            cache: dict = {}

            def key(r, cache=cache):
                keys = frozenset(moments.keys_for(self.dest(r["destination_id"])))
                if keys not in cache:
                    cache[keys] = moments.recent_by_creator(self.store, set(keys), self.clock[0])
                return moments.rotation_key(self.store, r["clip_id"], cache[keys], self.clock[0])

            progressed = False
            for r in sorted(rows, key=key):
                if self.claim(r["id"]):
                    sent.append(r["id"])
                    progressed = True
                    break
            if not progressed:
                return sent

    def row(self, did):
        return self.store.q1("SELECT * FROM deliveries WHERE id=?", did)

    def detail(self, did):
        return self.row(did)["history"][-1]["detail"]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_different_streamers_may_post_simultaneously_on_the_same_account(w):
    ids = [w.queue(w.clip(ch, NOW - 600), w.yt) for ch in (w.tw, w.others[0], w.others[1])]
    assert [w.claim(d)["status"] for d in ids] == ["uploading"] * 3
    assert {w.row(d)["claimed_at"] for d in ids} == {NOW}  # the same instant, one account


def test_same_streamer_waits_two_hours_across_accounts_but_a_cross_post_counts_once(w):
    a = w.clip(w.tw, NOW - 600)
    assert w.claim(w.queue(a, w.yt))
    # another moment (Kick), other account
    b = w.queue(w.clip(w.kk, NOW - 3000), w.tt)
    assert w.claim(b) is None and "2 h between different moments" in w.detail(b)
    assert w.row(b)["next_attempt_at"] == NOW + 2 * H
    copy = w.queue(w.clip(w.kk, NOW - 597), w.tt)  # Kick copy of the SAME moment
    # intentional cross-post: counted once
    assert w.claim(copy)["status"] == "uploading"
    w.clock[0] = NOW + 2 * H - 1
    w.store.update("deliveries", b, next_attempt_at=0)
    assert w.claim(b) is None  # 1 s short
    w.clock[0] = NOW + 2 * H  # exactly two hours
    w.store.update("deliveries", b, next_attempt_at=0)
    assert w.claim(b)["status"] == "uploading"


def test_edits_and_re_cuts_of_one_moment_never_post_twice_on_an_account(w):
    a = w.clip(w.tw, NOW - 600)
    d = w.queue(a, w.yt, mode="manual")
    assert w.claim(d)
    w.store.update("deliveries", d, status="published", media_version=1)
    w.store.merge_json("clips", a, "files", {"final_version": {"en": 2}})  # edited / re-cropped
    assert "moment already held" in moments.conflict(w.store, a, w.dest(w.yt))
    with pytest.raises(PermissionError, match="moment already held"):
        w.queue(w.clip(w.tw, NOW - 595), w.yt)  # alternate cut of the same moment


def test_future_reservations_shape_the_streamers_next_eligible_time(w):
    planned = w.clip(w.tw, NOW - 9000)  # a reserved slot for one moment
    # a different moment of that streamer
    d = w.clip(w.kk, NOW - 600)
    extra = [(NOW + 1 * H, moments.moment_id(w.store, planned))]
    assert moments.next_slot(w.store, w.dest(w.yt), NOW, clip_id=d, creator_extra=extra) == NOW + 3 * H
    assert "has another moment" in moments.creator_problem(w.store, d, NOW, extra=extra)
    other = w.clip(w.others[0], NOW - 600)  # another streamer: not affected
    assert moments.next_slot(w.store, w.dest(w.yt), NOW, clip_id=other) == NOW


def test_native_send_in_flight_blocks_a_schedule_of_another_moment(w):
    assert w.claim(w.queue(w.clip(w.tw, NOW - 600), w.yt))
    other = w.clip(w.kk, NOW - 5000)
    assert "2 h between different moments" in moments.creator_problem(w.store, other, NOW + 1 * H)
    assert moments.creator_problem(w.store, other, NOW + 2 * H) is None


@pytest.mark.parametrize(
    "outcome,frees",
    [
        ("failed", True),
        ("cancelled", True),
        ("needs_confirmation", False),
        ("uncertain", False),
        ("published", False),
    ],
)
def test_definitive_failures_release_unknown_outcomes_hold(w, outcome, frees):
    a = w.queue(w.clip(w.tw, NOW - 600), w.yt)
    assert w.claim(a)
    w.store.update("deliveries", a, status=outcome)
    b = w.queue(w.clip(w.kk, NOW - 3000), w.tt)
    assert (w.claim(b) is not None) == frees


def test_no_per_account_ceiling_many_streamers_and_the_next_legitimate_same_streamer_clip(w):
    chans = [w.tw] + w.others  # six different streamers
    old = w.queue(w.clip(chans[0], NOW - 23.5 * H), w.yt)
    w.store.update("deliveries", old, status="published", claimed_at=NOW - 23 * H)
    for ch in chans[1:]:
        # 6 in 24 h on one account
        assert w.claim(w.queue(w.clip(ch, NOW - 600), w.yt))["status"] == "uploading"
    nxt = w.queue(w.clip(chans[1], NOW - 9000), w.yt)  # same streamer, a different moment
    assert w.claim(nxt) is None and w.row(nxt)["next_attempt_at"] == NOW + 2 * H
    w.clock[0] = NOW + 2 * H
    w.store.update("deliveries", nxt, next_attempt_at=0)
    assert w.claim(nxt)["status"] == "uploading"  # exactly 2 h later: released


def test_backlog_is_dispatched_fairly_one_moment_per_streamer_no_duplicates(w):
    xs = [w.queue(w.clip(w.tw, NOW - 1000 - i * 400), w.yt) for i in range(3)]
    ys = [w.queue(w.clip(ch, NOW - 5000), w.yt) for ch in w.others[:2]]
    sent = w.drain()
    # freshest moment of A + each other streamer
    assert set(sent) == {xs[0]} | set(ys)
    assert len(sent) == len(set(sent))  # nothing posted twice
    for d in xs[1:]:
        r = w.row(d)
        assert r["status"] == "queued" and r["next_attempt_at"] >= NOW + 2 * H


def test_legacy_rows_are_evaluated_under_the_current_rules(w):
    st = w.store
    legacy = {**w.dest(w.yt)["settings"], "min_minutes_between_posts": 120}  # a legacy account spacing
    st.update("destinations", w.yt, settings=legacy)
    assert moments.account_cap(w.dest(w.yt)) is None
    prev = w.clip(w.others[0], NOW - 9000)
    st.x(
        "INSERT INTO deliveries(clip_id,destination_id,language,idempotency_key,status,media_version,"
        "claimed_at,"
        # no account_key
        "created_at,updated_at) VALUES(?,?,'en','legacy','published',1,?,1,1)",
        prev,
        w.yt,
        NOW - H,
    )
    assert moments.post_times(st, w.dest(w.yt)) == [NOW - H]  # counted by its destination's identity
    hist = json.dumps(
        [
            {
                "t": NOW - 60,
                "from": "queued",
                "to": "queued",
                "detail": "deferred by the account's cadence until 23:00 UTC",
            }
        ]
    )
    old_rows = []
    for ch, key in ((w.others[1], "k1"), (w.tw, "k2")):
        cid = w.clip(ch, NOW - 600)
        st.x(
            "INSERT INTO deliveries(clip_id,destination_id,language,idempotency_key,status,mode,"
            "media_version,"
            "history,next_attempt_at,created_at,updated_at) VALUES(?,?,'en',?,'queued','auto',0,?,0,1,1)",
            cid,
            w.yt,
            key,
            hist,
        )
        old_rows.append(st.q1("SELECT id FROM deliveries WHERE idempotency_key=?", key)["id"])
    # the restarted process: a new connection
    w2 = World.__new__(World)
    w2.__dict__.update(w.__dict__, store=SelStore(w.s.db_path))
    # 1 h after another streamer's post is fine under the policy
    assert set(w2.drain()) == set(old_rows)


def test_two_connections_on_one_database_cannot_both_claim_one_streamers_slot(w):
    a = w.queue(w.clip(w.tw, NOW - 600), w.yt)
    b = w.queue(w.clip(w.kk, NOW - 4000), w.tt)
    stores = [w.store, SelStore(w.s.db_path)]
    barrier, out = threading.Barrier(2), {}

    def go(st, did):
        barrier.wait()
        out[did] = w.claim(did, st)

    ts = [threading.Thread(target=go, args=x) for x in zip(stores, (a, b), strict=True)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(v is None for v in out.values()) == [False, True]  # exactly one claim wins
    w.store.update("deliveries", a, next_attempt_at=0)
    w.store.update("deliveries", b, next_attempt_at=0)
    loser = a if out[a] is None else b
    assert w.claim(loser, SelStore(w.s.db_path)) is None  # still held on a fresh connection


def test_off_topic_evidence_holds_unattended_dispatch(w):
    cid = w.clip(w.others[0], NOW - 600)
    trig = {k: v for k, v in w.store.clip(cid)["trigger"].items() if k != "review"}
    w.store.update(
        "clips", cid, trigger={**trig, "topic": {"state": "live_off_topic", "title": "Just Chatting"}}
    )
    ed = candidates.editorial(w.pub, w.store.clip(cid))
    assert ed["state"] == "not_eligible" and ed["reason"].startswith("not GTA gameplay: ")


def test_policy_constants(w):
    assert (
        moments.CREATOR_GAP_S == 2 * H and moments.account_cap({}) is None and moments.ACCOUNT_LIMIT is None
    )
