"""Creator moments: one delivery per creator moment per physical account; no per-account cap and 120 min
spacing
between a streamer's different moments; the rotation plan; the live trigger merge rule. Nothing is sent
anywhere."""

from __future__ import annotations

import threading
import time

import pytest
from selstore import SelStore
from test_streamer_policy import FakePub, World, make_clip, settings

from live_clipper import moments

YT = "UC" + "a" * 22
T0 = 1_800_000_000.0


def world(tmp_path):
    w = World(tmp_path)
    w.clock[0] = time.time()
    w.store.x("DELETE FROM destinations")  # each test sets up its own accounts
    return w, w.store, w.tw, w.kk, w.others[0]


def clip(w, ch, at, lang="en", score=5.0):
    return w.clip(ch, at, lang=lang, score=score)


def native(store, provider, label, acct, lang="en", **extra):
    return store.add_destination(
        provider, label, {"expected_account_id": acct, "account_language": lang, **extra}, "connected"
    )


def ext(store, platform, acct, lang="en", **extra):
    return store.add_destination(
        "external",
        f"{lang} {platform}",
        {"platform": platform, "expected_account_id": acct, "account_language": lang, **extra},
        "external",
    )


def held(w, cid, did, status="uploading", at=None):
    """A delivery of cid on destination did that holds the account (claimed at `at`)."""
    d = w.queue(cid, did)
    dest = w.dest(did)
    w.store.update(
        "deliveries", d, status=status, claimed_at=at or time.time(), account_key=moments.primary_key(dest)
    )
    return d


# ------------------------------------------------------------------ live trigger merge (engine rule)
def test_live_trigger_merge_rule():
    assert moments.window_for(100.0, 18.0, 12.0) == (82.0, 112.0)
    start, end = moments.window_for(118.0, 18.0, 12.0)
    first = {"id": 1, "action_time": 100.0, "window_end": 112.0}
    # onset 18 s later: separate
    assert moments.merge_target([first], "chat_spike", 118.0, start, end) is None
    # post_roll 30: inside window
    start, end = moments.window_for(118.0, 18.0, 30.0)
    first["window_end"] = 130.0
    assert moments.merge_target([first], "chat_spike", 118.0, start, end) is first
    assert moments.merge_target([first], "manual", 118.0, start, end) is None  # manual never merges
    start, end = moments.window_for(105.0, 18.0, 12.0)
    first["window_end"] = 112.0
    assert moments.merge_target([first], "audio", 105.0, start, end) is first  # |delta| 5 < merge 12
    near_end = {"id": 2, "action_time": 100.0, "window_end": None}
    # merge 0: window end + 2 s
    start, end = moments.window_for(101.5, 18.0, 0.0)
    assert moments.merge_target([near_end], "chat_spike", 101.5, start, end) is near_end
    old = {"id": 3, "action_time": 100.0, "window_end": 400.0}
    start, end = moments.window_for(220.0, 18.0, 12.0)
    # older than 120 s: never
    assert moments.merge_target([old], "chat_spike", 220.0, start, end) is None
    assert moments.dedupe_key(7, "chat_spike", 1234.9) == "7:chat_spike:1234"


def test_simulcast_channels(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    assert moments.simulcast_channels(store, tw) == [tw, kk] == moments.simulcast_channels(store, kk)
    assert moments.simulcast_channels(store, other) == [other]


# ------------------------------------------------------------------ duplicates (creator + source moment)
def test_simulcast_and_recut_share_a_moment_but_other_creators_and_moments_do_not(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    a = clip(w, tw, T0)
    b = clip(w, kk, T0 + 4)  # same moment on Kick (linked identity)
    c = clip(w, tw, T0 + 6)  # alternate cut of the same moment
    # another streamer: identical gameplay/frames, own perspective
    d = clip(w, other, T0)
    e = clip(w, tw, T0 + 400)  # a different moment of the same creator
    m = {x: moments.moment_id(store, x) for x in (a, b, c, d, e)}
    assert m[a] == m[b] == m[c] and len({m[a], m[d], m[e]}) == 3
    yt = native(store, "youtube", "YT", YT)
    held(w, a, yt)
    for dup in (b, c):
        assert "moment already held" in moments.conflict(store, dup, w.dest(yt))
    assert moments.conflict(store, d, w.dest(yt)) is None
    assert moments.conflict(store, e, w.dest(yt)) is None


def test_same_moment_by_window_overlap_within_the_scan_range(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    a = clip(w, tw, T0)
    b = clip(w, tw, T0 + 16)
    store.update("clips", b, window_start=T0 - 2, window_end=T0 + 28)  # overlap 14 / 30 = 0.47
    assert moments.moment_id(store, b) != moments.moment_id(store, a)
    c = clip(w, tw, T0 + 22)
    store.update("clips", c, window_start=T0 - 5, window_end=T0 + 25)  # overlap 17 / 30 > 0.5
    assert moments.moment_id(store, c) == moments.moment_id(store, a)
    assert moments.moment_clips(store, a) == [a, c]


def test_one_delivery_per_moment_per_account_across_native_and_external_rows(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    a, b = clip(w, tw, T0), clip(w, kk, T0 + 3)
    yt = native(store, "youtube", "YT native", YT)
    w.queue(a, yt)
    with pytest.raises(PermissionError, match="moment already held"):
        w.queue(b, yt)
    # the same physical account through an external row: the native claim holds it there too
    with store.tx():
        x = ext(store, "youtube", YT, enabled=False)
        assert moments.conflict(store, b, w.dest(x))
        assert moments.conflict(store, a, w.dest(x))


def test_retry_failed_frees_and_uncertain_holds_and_claim_rechecks(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    a, b = clip(w, tw, T0), clip(w, kk, T0 + 3)
    yt = native(store, "youtube", "YT", YT)
    da = w.queue(a, yt)
    store.update("deliveries", da, status="uncertain")  # ambiguous outcome: held for verification
    with pytest.raises(PermissionError):
        w.queue(b, yt)
    store.update("deliveries", da, status="failed")  # a known failure frees the moment
    db = w.queue(b, yt)
    # a competing delivery comes back before b's upload: the claim re-check holds b instead of sending a
    # duplicate
    store.update("deliveries", da, status="published")
    assert w.claim(db) is None
    row = store.q1("SELECT status, history FROM deliveries WHERE id=?", db)
    assert row["status"] == "held" and "moment already held" in row["history"][-1]["detail"]


def test_old_deliveries_and_legacy_clips_count_even_after_the_destination_row_is_deleted(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    a, b = clip(w, tw, T0), clip(w, kk, T0 + 3)
    for cid in (a, b):  # legacy clips: no stored moment id yet
        t = store.clip(cid)["trigger"]
        t.pop("moment_id", None)
        store.update("clips", cid, trigger=t)
    yt = native(store, "youtube", "YT old", YT)
    held(w, a, yt, status="published")
    store.x("DELETE FROM destinations WHERE id=?", yt)  # destination deleted / recreated later
    yt2 = native(store, "youtube", "YT native", YT)
    assert yt2 != yt  # a deleted destination's id is never reused
    with pytest.raises(PermissionError, match="moment already held"):
        w.queue(b, yt2)  # the delivery's own account_key still holds
    assert moments.moment_id(store, b) == moments.moment_id(store, a)  # legacy clips reconciled
    assert store.clip(b)["trigger"]["moment_id"] == store.clip(a)["trigger"]["moment_id"]  # and stored


def test_retired_clips_keep_their_moment_through_the_ledger(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    a = clip(w, tw, T0)
    mid = moments.moment_id(store, a)
    c = store.clip(a)
    store.x(
        "INSERT INTO clip_ledger(clip_id, channel_id, creator_key, moment_id, moment_creator, action_time, "
        "window_start, window_end, dedupe_key, clip_created_at, confirmed_at, retired_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        a,
        tw,
        "group:streamer_alpha",
        mid,
        "group:streamer_alpha",
        c["action_time"],
        c["window_start"],
        c["window_end"],
        c["dedupe_key"],
        1,
        1,
        1,
    )
    store.x("DELETE FROM clips WHERE id=?", a)
    assert moments.clip_meta(store, a)["retired"] and moments.moment_id(store, a) == mid
    b = clip(w, kk, T0 + 5)
    assert b != a and moments.moment_id(store, b) == mid  # a removed moment still clusters
    assert store.create_clip(tw, c["dedupe_key"], "chat_spike", {}) is None  # and keeps its dedupe key


def test_concurrent_claims_for_one_moment_let_exactly_one_win(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    ids = [clip(w, tw, T0), clip(w, kk, T0 + 2), clip(w, tw, T0 + 5)]
    yt = native(store, "youtube", "YT", YT)
    barrier, results = threading.Barrier(len(ids)), []

    def go(cid):
        barrier.wait()
        try:
            w.queue(cid, yt)
            results.append("queued")
        except PermissionError:
            results.append("refused")

    threads = [threading.Thread(target=go, args=(c,)) for c in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["queued", "refused", "refused"]
    assert len(store.q("SELECT id FROM deliveries")) == 1


# ------------------------------------------------------------------ cadence and rotation
def test_streamer_spacing_without_an_account_limit(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    yt = native(store, "youtube", "YT", YT)
    base = time.time() + 3600
    ids = [clip(w, other, T0 + i * 1000) for i in range(8)]
    x = clip(w, tw, T0)  # a different streamer
    held(w, ids[0], yt, at=base)
    assert "2 h between different moments" in moments.creator_problem(store, ids[1], base + 60 * 60)
    assert moments.creator_problem(store, x, base + 60) is None  # other streamer: may overlap
    assert moments.slot_problem(store, w.dest(yt), base + 60, clip_id=x) is None
    for i in range(1, 8):  # one streamer every 2 h: 8 in 14 h, no cap
        assert moments.slot_problem(store, w.dest(yt), base + i * 2 * 3600, clip_id=ids[i]) is None
        held(w, ids[i], yt, at=base + i * 2 * 3600)
    assert len(moments.post_times(store, w.dest(yt))) == 8


def test_rotation_plan_distinct_moments_fair_creators_spacing_and_isolation(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    routed = store.add_channel(
        "rumble", "streamer_gamma", "", {"publish_platforms": ["instagram"], "publish_destinations": []}
    )
    now = time.time()
    xs = [clip(w, tw, now - 1000 - i * 500, score=9) for i in range(4)]
    xs.append(clip(w, kk, now - 1000 + 3, score=9))  # simulcast duplicate of xs[0]
    [clip(w, other, now - 2000 - i * 500, score=1) for i in range(3)]
    f = clip(w, routed, now - 500, score=99)
    es = clip(w, other, now - 3000, lang="es")
    yt, tt = ext(store, "youtube", YT), ext(store, "tiktok", "example_clips")
    ext(store, "instagram", "example_clips", enabled=False)  # disabled: never planned
    p = moments.plan(w.pub, "en", now=now)
    slots = p["slots"]
    assert slots and {x["destination_id"] for x in slots} <= {yt, tt}
    mids = [x["moment_id"] for x in slots]
    # distinct moments across accounts (rotation)
    assert len(mids) == len(set(mids))
    assert f not in [x["clip_id"] for x in slots] and es not in [x["clip_id"] for x in slots]
    for cr in {x["creator"] for x in slots}:  # 2 h between a streamer's moments
        ts = sorted(x["slot_at"] for x in slots if x["creator"] == cr)
        assert all(b - a >= 2 * 3600 for a, b in zip(ts, ts[1:], strict=False))
    assert len({x["slot_at"] for x in slots[:2]}) == 1 and slots[0]["creator"] != slots[1]["creator"]
    creators = [x["creator"] for x in slots[:4]]
    assert creators.count("group:streamer_alpha") <= 2 and f"channel:{other}" in creators  # fair rotation
    # a moment held on any EN account is not planned again elsewhere
    held(w, slots[0]["clip_id"], slots[0]["destination_id"], at=slots[0]["slot_at"])
    assert slots[0]["moment_id"] not in [x["moment_id"] for x in moments.plan(w.pub, "en", now=now)["slots"]]


def test_no_account_limit_but_next_slot_still_sees_the_streamers_future_reservations(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    yt = native(store, "youtube", "YT", YT)
    d = w.dest(yt)
    base = time.time() + 3600
    ids = [clip(w, other, T0 + i * 1000) for i in range(6)]
    for i, h in enumerate((0, 2, 4, 6, 8)):
        held(w, ids[i], yt, at=base + h * 3600)  # five in 8 h on one account: fine
    assert moments.next_slot(store, d, base) == base  # no account limit at all
    nxt = moments.next_slot(store, d, base + 3600, clip_id=ids[5])
    # the streamer's future reservations still count
    assert nxt == base + 10 * 3600
    own = store.q1("SELECT id FROM deliveries WHERE clip_id=?", ids[0])["id"]
    assert moments.creator_problem(store, ids[0], base - 0.5 * 3600, exclude=(("d", own),)) is None
    assert moments.creator_problem(store, ids[0], base - 0.5 * 3600, exclude=()) is None  # its own moment


def test_native_delivery_records_its_account_and_keeps_it_after_a_repin(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    a, b = clip(w, tw, T0), clip(w, kk, T0 + 2)
    n1 = native(store, "youtube", "YT", YT)
    did = w.queue(a, n1)
    w.clock[0] = T0 + 600
    assert w.claim(did)
    assert store.q1("SELECT account_key FROM deliveries WHERE id=?", did)["account_key"] == f"youtube:{YT}"
    store.update(
        "destinations",
        n1,
        settings={
            "expected_account_id": "UC" + "b" * 22,
            "account_language": "en",
            "account_info": {"account_id": "UC" + "b" * 22},
        },
    )
    n2 = native(store, "youtube", "YT again", YT)
    with pytest.raises(PermissionError, match="moment already held"):
        # the claim stayed with the account it was made for
        w.queue(b, n2)


def test_a_published_or_uncertain_clip_keeps_holding_its_moment(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    a = clip(w, other, T0)
    yt = native(store, "youtube", "YT", YT)
    did = w.queue(a, yt)
    for status in ("published", "uncertain", "needs_confirmation", "processing"):
        store.update("deliveries", did, status=status, media_version=1, provider_ref="P")
        assert "moment already held" in moments.conflict(store, a, w.dest(yt))  # an edited version too
        assert moments.conflict(store, a, w.dest(yt), delivery_id=did) is None  # the row itself is exempt


def test_claims_from_two_separate_database_connections_cannot_both_win(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    ids = [clip(w, tw, T0), clip(w, kk, T0 + 2)]
    yt = native(store, "youtube", "YT", YT)
    stores = [store, SelStore(w.s.db_path)]  # like two worker processes on one database
    barrier, results = threading.Barrier(2), []

    def go(st, cid):
        barrier.wait()
        try:
            w.queue(cid, yt, store=st)
            results.append("queued")
        except PermissionError:
            results.append("refused")

    ts = [threading.Thread(target=go, args=(st, c)) for st, c in zip(stores, ids, strict=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == ["queued", "refused"]


def test_linking_a_simulcast_after_both_halves_were_tagged_recomputes_the_moment(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    for ch in (tw, kk):
        store.update("channels", ch, settings={})  # not linked yet
    a, b = clip(w, tw, T0), clip(w, kk, T0 + 3)
    assert moments.moment_id(store, a) != moments.moment_id(store, b)  # both halves tagged separately
    for ch in (tw, kk):
        store.update("channels", ch, settings={"simulcast_group": "streamer_alpha"})
    assert moments.moment_id(store, a) == moments.moment_id(store, b)
    yt = ext(store, "youtube", YT)
    held(w, a, yt)
    assert "moment already held" in moments.conflict(store, b, w.dest(yt))


def test_native_automatic_sends_rotate_creators(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    yt = native(store, "youtube", "YT", YT)
    now = time.time()
    x1 = clip(w, tw, now - 5000)
    done = w.queue(x1, yt)
    store.update(
        "deliveries",
        done,
        status="published",
        claimed_at=now - 4 * 3600,
        account_key=moments.primary_key(w.dest(yt)),
    )  # streamer_alpha posted recently
    x2 = clip(w, tw, now - 100)  # fresher, but streamer_alpha again
    y1 = clip(w, other, now - 900)  # older, but a creator not posted recently
    recent = moments.recent_by_creator(store, moments.keys_for(w.dest(yt)), now)
    assert recent == {"group:streamer_alpha": 1}
    order = sorted((x2, y1), key=lambda c: moments.rotation_key(store, c, recent, now))
    assert order == [y1, x2]


def test_two_creators_identical_gameplay_stay_independent(tmp_path):
    w, store, tw, kk, other = world(tmp_path)
    a, d = clip(w, tw, T0), clip(w, other, T0)
    yt = native(store, "youtube", "YT", YT)
    w.queue(a, yt)
    assert w.queue(d, yt)


def test_accounts_are_physical_not_rows(tmp_path):
    s = settings(tmp_path)
    store = SelStore(s.db_path)
    FakePub(store, s, time.time)
    ig = {
        "provider": "instagram",
        "account": "@Example_Clips",
        "settings": {"expected_account_id": "1784000"},
    }
    assert moments.account_keys(ig) == {"instagram:1784000", "instagram:example_clips"}
    assert moments.primary_key(ig) == "instagram:1784000"
    x = {"provider": "external", "settings": {"platform": "instagram", "account_handle": "@example_clips"}}
    assert moments.account_keys(x) & moments.account_keys(ig)
    assert moments.norm("youtube", " UCabc ") == "UCabc" and moments.norm("tiktok", "@Ex") == "ex"
    assert moments.keys_for({"id": 9, "provider": "youtube", "settings": {}}) == {"dest:9"}
    ch = store.add_channel("twitch", "streamer_beta")
    cid = make_clip(store, s, ch)
    assert moments.creator_key(store, ch) == f"channel:{ch}" and moments.creator_channels(store, ch) == [ch]
    assert moments.moment_id(store, cid) == f"m{cid}" and moments.moment_id(store, 999) == "m999"


def test_delivery_time_basis():
    claim = {
        "status": "published",
        "claimed_at": 100.0,
        "history": [{"t": 150.0, "from": "processing", "to": "published"}],
    }
    assert moments.delivery_time(claim) == (150.0, "confirmed by the provider at this time", False)
    assert moments.delivery_time({**claim, "public_at": 140.0})[1] == "public time reported by the provider"
    # too far past the confirmation
    assert moments.delivery_time({**claim, "public_at": 900.0})[0] == 150.0
    assert moments.delivery_time(
        {"status": "processing", "claimed_at": 100.0, "history": [{"t": 130.0}]}
    ) == (130.0, "not confirmed public yet (provisional)", True)
    assert moments.delivery_time({"status": "held", "created_at": 90.0}) == (
        90.0,
        "upload claim (provisional, outcome open)",
        True,
    )
    assert moments.sent_evidence({"status": "held", "provider_ref": '{"id": "x"}'})
    assert not moments.sent_evidence({"status": "queued", "provider_ref": ""})
    assert moments.sent_evidence({"status": "paused", "reconcile": 1})
