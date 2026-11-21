"""Posting candidates: unsent finished clips ranked by reaction x freshness (live_clipper.candidates),
editorially
eligible first, separate from deliveries in progress; the rotation plan and native dispatch use the same
priority
while creator rotation, the 2 h creator cooldown, dedup and existing reservations still win. Nothing is
sent."""

from __future__ import annotations

import math

import pytest
from test_streamer_policy import NOW, H, World, make_clip

from live_clipper import candidates, formats, moments


@pytest.fixture
def w(tmp_path):
    w = World(tmp_path)
    for i in range(4):
        w.others.append(w.store.add_channel("twitch", f"c{i}"))
    return w


def spike(w, ch, age_h, ratio, users=40, approved=True):
    cid = w.clip(ch, NOW - age_h * H)
    trig = {k: v for k, v in w.store.clip(cid)["trigger"].items() if k != "review"}
    trig.update(kind="chat_spike", score=ratio, unique_users=users)
    if approved:
        trig["review"] = {"gameplay": {"state": "approved"}}
    w.store.update("clips", cid, trigger=trig, kind="chat_spike")
    return cid


def test_priority_is_reaction_times_freshness_and_manual_is_neutral(w):
    a = w.store.clip(spike(w, w.others[0], 1, 8.0))
    assert candidates.reaction(a)[0] == pytest.approx(math.log2(9))
    assert candidates.freshness(a, NOW)[0] == pytest.approx(0.5 ** (1 / 12))
    m = w.store.clip(w.clip(w.others[1], NOW - H))
    w.store.update("clips", m["id"], kind="manual", trigger={**m["trigger"], "kind": "manual", "score": 0})
    assert candidates.reaction(w.store.clip(m["id"]))[0] == candidates.NEUTRAL_REACTION
    assert candidates.priority(a, NOW) == pytest.approx(math.log2(9) * 0.5 ** (1 / 12))


def test_scoring_constants_come_from_the_selection_format(w):
    fmt = formats.active()
    assert (fmt.scoring.fresh_half_life_h, fmt.scoring.neutral_reaction) == (
        candidates.FRESH_HALF_LIFE_H,
        candidates.NEUTRAL_REACTION,
    )
    other = fmt.model_copy(
        update={"scoring": fmt.scoring.model_copy(update={"fresh_half_life_h": 6.0, "neutral_reaction": 0.5})}
    )
    a = w.store.clip(spike(w, w.others[0], 6, 3.0))
    assert candidates.freshness(a, NOW, other)[0] == pytest.approx(0.5)
    m = {"kind": "manual", "trigger": {"kind": "manual"}}
    assert candidates.reaction(m, other) == (0.5, "no chat measurement (manual clip): neutral")


def test_below_threshold_matches_the_auto_post_rule(w):
    assert candidates.below_threshold({"trigger": {"score": 3.0}}, 3.0) is None
    assert (
        candidates.below_threshold({"trigger": {"score": 2.94}}, 3)
        == "chat spike 2.9x is below this auto-post rule's threshold (3x)"
    )
    assert (
        candidates.below_threshold({"trigger": {"kind": "manual"}}, 0.5)
        == "chat spike 0.0x is below this auto-post rule's threshold (0.5x)"
    )
    assert candidates.below_threshold({"trigger": {"input": "vod_import", "kind": "audio"}}, 9) is None
    assert candidates.below_threshold(
        {"trigger": {"input": "vod_import", "kind": "chat_spike", "score": 1}}, 3
    )
    assert candidates.below_threshold(None, 0) is None


def test_a_qualifying_newer_moment_overtakes_a_stale_high_score_one(w):
    # 8x spike, 20 h old, never sent (inside the cutoff)
    stale = spike(w, w.others[0], 20, 8.0)
    fresh = spike(w, w.others[1], 1, 3.0)  # 3x spike, 1 h old
    strong = spike(w, w.others[2], 1, 8.0)  # 8x spike, 1 h old
    pri = {c["clip_id"]: c["priority"] for c in candidates.candidates(w.pub, now=NOW)}
    assert pri[strong] > pri[fresh] > pri[stale]
    slots = moments.plan(w.pub, "en", now=NOW)["slots"]
    order = [s["clip_id"] for s in slots]
    assert order.index(strong) < order.index(fresh) < order.index(stale)  # the stale one stays a candidate
    assert "chat spike 8.0x" in next(s for s in slots if s["clip_id"] == strong)["rank_reason"]


def test_editorial_holds_and_delivery_queue_separation(w):
    ok = spike(w, w.others[0], 1, 3.0)
    unreviewed = spike(w, w.others[1], 1, 20.0, approved=False)  # a huge spike, gameplay never reviewed
    rejected = spike(w, w.others[2], 1, 20.0, approved=False)
    w.store.merge_json("clips", rejected, "trigger", {"review": {"gameplay": {"state": "rejected"}}})
    sending = spike(w, w.others[3], 1, 9.0)
    # already in the delivery queue of ONE account
    w.queue(sending, w.yt)
    done = spike(w, w.others[4], 1, 9.0)
    w.queue(done, w.yt), w.queue(done, w.tt), w.queue(done, w.ext)  # held on every EN account
    # below the rule threshold on every native account
    low = spike(w, w.others[5], 1, 2.0)
    rows = {c["clip_id"]: c for c in candidates.candidates(w.pub, now=NOW)}
    assert rows[ok]["editorial"] == "eligible" and rows[ok]["rank"] == 2 and rows[ok]["already_on"] == []
    assert rows[unreviewed]["editorial"] == "needs_review" and "needs review" in rows[unreviewed]["reason"]
    assert rows[rejected]["editorial"] == "not_eligible" and done not in rows
    assert rows[rejected]["reason"].startswith("not GTA gameplay: ")
    # a clip that one account already holds stays a candidate for the accounts that do not (never hidden
    # wholesale)
    assert rows[sending]["editorial"] == "eligible" and rows[sending]["already_on"] == ["YT EN"]
    assert rows[sending]["destinations"] == ["TT EN", "YT EN (external)"]
    # the external route sends without a native rule; native routes refuse a 2x spike
    assert rows[low]["editorial"] == "eligible" and rows[low]["auto_destinations"] == ["YT EN (external)"]
    planned = [s["clip_id"] for s in moments.plan(w.pub, "en", now=NOW)["slots"]]
    assert ok in planned and unreviewed not in planned and rejected not in planned and sending not in planned


def test_held_rule_when_no_account_sends_it_unattended(w):
    w.store.x("DELETE FROM destinations WHERE id=?", w.ext)
    low = spike(w, w.others[0], 1, 2.0)
    ed = candidates.editorial(w.pub, w.store.clip(low))
    assert ed["state"] == "held_rule" and ed["destinations"] == ["YT EN", "TT EN"]
    assert ed["reason"] == (
        "no account will post it unattended: YT EN: chat spike 2.0x is below this auto-post "
        "rule's threshold (3x); TT EN: chat spike 2.0x is below this auto-post rule's threshold "
        "(3x)"
    )
    es = spike(w, w.others[1], 1, 9.0)
    w.store.merge_json("clips", es, "captions", {"policy": {"render_default": "es"}})
    ed = candidates.editorial(w.pub, w.store.clip(es))
    assert ed["state"] == "not_eligible" and ed["reason"] == "no final render yet"


def test_creator_rotation_and_cooldown_still_apply_over_priority(w):
    a, b = w.others[0], w.others[1]
    sent = w.queue(w.clip(a, NOW - 3 * H), w.yt)  # creator A posted 30 min ago
    assert w.claim(sent)
    w.store.update("deliveries", sent, status="published", claimed_at=NOW - 0.5 * H)
    hi = spike(w, a, 0.5, 15.0)  # A: very strong fresh moment
    lo = spike(w, b, 1, 2.0)  # B: modest moment
    slots = moments.plan(w.pub, "en", now=NOW)["slots"]
    sb = next(s for s in slots if s["clip_id"] == lo)
    sa = next(s for s in slots if s["clip_id"] == hi)
    assert sb["slot_at"] == NOW and sa["slot_at"] == NOW + 1.5 * H  # A waits for its 2 h cooldown


def test_existing_posts_are_never_moved_and_candidates_write_nothing_to_them(w):
    c = w.others[0]
    held = w.clip(c, NOW - 9 * H)
    did = w.queue(held, w.yt)
    assert w.claim(did)
    w.store.update("deliveries", did, status="published", claimed_at=NOW - 0.5 * H)
    before = w.store.q1("SELECT status, claimed_at, history, updated_at FROM deliveries WHERE id=?", did)
    new = spike(w, c, 0.2, 25.0)  # a fresh, much stronger moment of C
    slots = moments.plan(w.pub, "en", now=NOW)["slots"]
    s = next(x for x in slots if x["clip_id"] == new)
    assert abs(s["slot_at"] - (NOW - 0.5 * H)) >= 2 * H  # planned around the post, not over it
    candidates.candidates(w.pub, now=NOW)
    assert (
        w.store.q1("SELECT status, claimed_at, history, updated_at FROM deliveries WHERE id=?", did) == before
    )


def test_the_whole_backlog_is_ranked_before_truncation(w):
    w.store.add_destination(
        "youtube", "YT ES", {"account_language": "es", "expected_account_id": "UC-ES"}, "connected"
    )
    old_es = make_clip(w.store, w.s, w.others[0], langs=("es",))
    w.store.merge_json("clips", old_es, "captions", {"policy": {"render_default": "es"}})
    w.store.update(
        "clips",
        old_es,
        status="ready",
        action_time=NOW - 20 * H,
        window_start=NOW - 20 * H - 18,
        window_end=NOW - 20 * H + 12,
    )
    for i in range(505):  # 505 newer EN clips (different moments)
        w.clip(w.others[1], NOW - i * 60)
    assert old_es in [s["clip_id"] for s in moments.plan(w.pub, "es", now=NOW)["slots"]]
    one = w.others[2]
    many = [spike(w, one, 0.1 + k * 0.01, 5.0) for k in range(8)]
    for k, cid in enumerate(many):  # 8 distinct moments of one creator
        # half an hour apart: every one of them is still inside the 24 h cutoff when its slot comes
        w.store.update(
            "clips",
            cid,
            action_time=NOW - 0.1 * H - k * 0.5 * H,
            window_start=NOW - k * 0.5 * H - 400,
            window_end=NOW - k * 0.5 * H - 370,
        )
    slots = [s for s in moments.plan(w.pub, "en", now=NOW, horizon_h=24)["slots"] if s["clip_id"] in many]
    assert len(slots) == 8  # not capped at 5 per creator
    ts = sorted(s["slot_at"] for s in slots)
    assert all(b - a >= 2 * H for a, b in zip(ts, ts[1:], strict=False))


def test_native_dispatch_order_follows_priority_within_creator_rotation(w):
    stale = w.queue(spike(w, w.others[0], 20, 8.0), w.yt)
    fresh = w.queue(spike(w, w.others[1], 1, 3.0), w.yt)
    sent = w.drain()
    assert sent.index(fresh) < sent.index(stale)
