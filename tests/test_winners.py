"""Winning posts (views AND likes above comparable posts), content descriptors and the bounded ranking
preference.

Post identity, publication time and metric readings belong to the publishing side; here a store subclass
provides
them through post_anchors / post_readings / post_proof with the same shapes the reference computed. Nothing is
posted or fetched."""

from __future__ import annotations

import copy
import itertools
import time

import pytest
from selstore import SelStore
from test_streamer_policy import H, World, make_clip

from live_clipper import candidates, moments, winners


class PostStore(SelStore):
    """SelStore plus fake post data: confirmed posts (anchors) and time-stamped readings."""

    def __init__(self, path):
        super().__init__(path)
        self.posts: dict = {}
        self.obs: list = []

    def post_anchors(self):
        return copy.deepcopy(self.posts)

    def post_readings(self, anc):
        out = []
        for o in self.obs:
            a = anc.get((o["kind"], o["ref_id"]))
            if a:
                out.append(
                    {
                        "kind": o["kind"],
                        "ref_id": o["ref_id"],
                        "observed_at": o["observed_at"],
                        "age_h": (float(o["observed_at"]) - a["published_at"]) / 3600,
                        "views": o["views"],
                        "likes": None if o["source"] in winners.WEAK_LIKES else o["likes"],
                        "source": o["source"],
                        "label": "observation",
                        "provenance": "test reading",
                        "by": "operator",
                    }
                )
        return out

    def post_proof(self, kind, ref):
        return ("confirmed", "test post") if (kind, ref) in self.posts else (None, "not a post")


_world_n = itertools.count(1)


def new_world(tmp_path):
    winners._CACHE.clear()
    w = World(tmp_path / f"w{next(_world_n)}")
    w.store = PostStore(w.s.data_dir / "posts.db")
    w.pub.store = w.store
    st = w.store
    w.tw = st.add_channel("twitch", "streamer_alpha", "", {"simulcast_group": "streamer_alpha"})
    w.kk = st.add_channel("kick", "streamer_alpha", "", {"simulcast_group": "streamer_alpha"})
    w.others = [st.add_channel("twitch", n) for n in ("streamer_beta", "streamer_gamma", "a", "b", "c")]
    w.yt = st.add_destination(
        "youtube", "YT EN", {"account_language": "en", "expected_account_id": "UC-A"}, "connected"
    )
    w.tt = st.add_destination(
        "tiktok", "TT EN", {"account_language": "en", "expected_account_id": "tt-b"}, "connected"
    )
    w.clock[0] = time.time()
    return w


@pytest.fixture
def w(tmp_path):
    return new_world(tmp_path)


def srt(w, cid, lines):
    """lines: [(start, end, text)] written as the clip's original-language subtitles."""
    d = w.s.clips_dir / str(cid)
    d.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"{i}\n00:00:{int(a):02d},000 --> 00:00:{int(b):02d},000\n{t}\n\n"
        for i, (a, b, t) in enumerate(lines, 1)
    )
    (d / "en.srt").write_text(body, encoding="utf-8")
    w.store.merge_json(
        "clips", cid, "captions", {"en": {"srt": "en.srt"}, "policy": {"render_default": "en"}}
    )


def anchor(w, kind, did, cid, platform, account, lang, post_id, at, bounds=None):
    url = (
        f"https://www.youtube.com/shorts/{post_id}"
        if platform == "youtube"
        else f"https://www.tiktok.com/@example_clips/video/{post_id}"
    )
    w.store.posts[(kind, did)] = {
        "kind": kind,
        "ref_id": did,
        "clip_id": cid,
        "platform": platform,
        "account": account,
        "language": lang,
        "url": url,
        "post_id": post_id,
        "media_version": 1,
        "published_at": at,
        "public_bounds": (at, at) if bounds is None else bounds,
        "time_source": "provider",
    }


def posted(w, ch, age_h, dest=None, lines=None, spike=5.0, account="youtube:UC-A", lang="en"):
    """A published native delivery `age_h` ago; returns (delivery id, clip id)."""
    at = time.time() - age_h * H
    cid = w.clip(ch, at - 600)
    w.store.merge_json("clips", cid, "trigger", {"score": spike, "kind": "chat_spike"})
    w.store.merge_json("clips", cid, "files", {"duration": 24.0})
    if lines is not None:
        srt(w, cid, lines)
    vid = f"v{cid:010d}"[:11]
    did = w.store.x(
        "INSERT INTO deliveries(clip_id,destination_id,language,idempotency_key,status,mode,media_version,"
        "provider_post_id,provider_url,account_key,claimed_at,created_at,updated_at) VALUES(?,?,?,?,"
        "'published','auto',1,?,?,?,?,?,?)",
        cid,
        dest or w.yt,
        lang,
        f"k{cid}-{dest}",
        vid,
        f"https://www.youtube.com/shorts/{vid}",
        account,
        at,
        at,
        at,
    )
    anchor(w, "delivery", did, cid, account.split(":")[0], account, lang, vid, at)
    winners.describe(w.store, w.s, cid)
    return did, cid


def obs(w, did, age_h, views, likes, source="youtube_studio", kind="delivery"):
    a = w.store.posts[(kind, did)]
    w.store.obs.append(
        {
            "kind": kind,
            "ref_id": did,
            "observed_at": a["published_at"] + age_h * H,
            "views": views,
            "likes": likes,
            "source": source,
        }
    )


_CH = itertools.count(1)


def chans(w, n):
    return [w.store.add_channel("twitch", f"creator{next(_CH)}") for _ in range(n)]


def verdicts(w):
    return {(p["kind"], p["ref_id"]): p for g in winners.evaluate(w.store)["groups"] for p in g["posts"]}


def cohort(w, vals, dest=None, account="youtube:UC-A", age=6, lines=None):
    """One comparable cohort: posts by different creators read at the same age. vals: [(views, likes)]."""
    cs = chans(w, len(vals))
    ids = []
    for i, (v, lk) in enumerate(vals):
        d, cid = posted(w, cs[i], age + 24, dest=dest, account=account, lines=(lines[i] if lines else None))
        obs(w, d, age, v, lk)
        ids.append(d)
    return ids


# ---------------------------------------------------------------- verdicts: dominance, promising, ties,
# missing
def test_without_post_data_there_is_nothing_to_compare(w):
    plain = SelStore(w.s.data_dir / "plain.db")
    assert winners.anchors(plain) == {} and winners.readings(plain) == []
    assert winners.proof_state(plain, "delivery", 1) == (None, "no post data")
    ev = winners.evaluate(plain)
    assert ev == {"groups": [], "moments": [], "off_window": [], "coverage": [], "winning_posts": []}
    assert winners.patterns(plain, ev)["scopes"]["all"]["labels"] == []


def test_only_the_unique_top_on_both_metrics_is_a_verified_winner(w):
    ids = cohort(
        w, [(1000, 100), (1100, 90), (900, 110), (1000, 100), (5000, 400), (1300, 130), (1200, None)]
    )
    vd = verdicts(w)
    top = vd[("delivery", ids[4])]
    assert top["verdict"] == "winner" and top["peer_max_views"] == 1300 and top["peer_max_likes"] == 130
    # the post without likes is no peer
    assert top["peers"] == 5 and top["window"] == "4-8h"
    # above both medians, not the top
    assert vd[("delivery", ids[5])]["verdict"] == "promising"
    assert vd[("delivery", ids[1])]["verdict"] == "not_win"
    assert vd[("delivery", ids[6])]["verdict"] == "missing" and "no likes" in vd[("delivery", ids[6])]["why"]
    m = {x["clip_ids"][0]: x for x in winners.evaluate(w.store)["moments"]}
    assert sum(x["outcome"] == "winner" for x in m.values()) == 1


def test_split_leaders_or_a_tie_at_the_top_give_no_winner(w, tmp_path):
    split = cohort(w, [(5000, 200), (1200, 500), (900, 90), (920, 92), (940, 94), (950, 95)])
    vd = verdicts(w)
    assert [vd[("delivery", i)]["verdict"] for i in split[:2]] == ["promising", "promising"]
    assert not any(p["verdict"] == "winner" for p in vd.values())
    w2 = new_world(tmp_path / "tied")
    tied = cohort(w2, [(5000, 500), (5000, 400), (900, 90), (920, 92), (940, 94), (950, 95)])
    vd2 = verdicts(w2)
    assert not any(p["verdict"] == "winner" for p in vd2.values())
    assert "tied" in vd2[("delivery", tied[0])]["why"]
    few = new_world(tmp_path / "few")
    # 3 peers: no verdict at all
    small = cohort(few, [(9000, 900), (100, 10), (110, 11), (120, 12)])
    assert verdicts(few)[("delivery", small[0])]["verdict"] == "insufficient"


def test_only_the_same_cohort_and_window_is_compared_and_complete_readings_come_first(w):
    ids = cohort(w, [(1000, 100)] * 5, age=30)
    lone, _ = posted(w, chans(w, 1)[0], 40)
    obs(w, lone, 6, 50000, 5000)  # other window: never compared
    assert verdicts(w)[("delivery", lone)]["verdict"] == "insufficient"
    obs(w, lone, 25, 3000, None)  # sparse, nearest the centre
    obs(w, lone, 33, 3100, 310)  # complete: chosen first
    v = verdicts(w)[("delivery", lone)]
    assert (v["views"], v["likes"], v["verdict"]) == (3100, 310, "winner")
    tt = posted(w, chans(w, 1)[0], 30, dest=w.tt, account="tiktok:tt-b")[0]
    obs(w, tt, 30, 999999, 99999)
    # another account: nobody to beat
    assert verdicts(w)[("delivery", tt)]["verdict"] == "insufficient"
    assert verdicts(w)[("delivery", ids[0])]["peers"] == 5


def test_a_crosspost_is_one_moment_with_per_platform_results(w):
    cs = chans(w, 6)
    yt = [posted(w, c, 30) for c in cs]
    for i, (d, cid) in enumerate(yt):
        at = time.time() - 30 * H
        tid = w.store.x(
            "INSERT INTO deliveries(clip_id,destination_id,language,idempotency_key,status,mode,"
            "media_version,provider_post_id,account_key,claimed_at,created_at,updated_at) "
            "VALUES(?,?,'en',?,'published','auto',1,?,'tiktok:tt-b',?,?,?)",
            cid,
            w.tt,
            f"t{cid}",
            f"7{cid:018d}",
            at,
            at,
            at,
        )
        anchor(w, "delivery", tid, cid, "tiktok", "tiktok:tt-b", "en", f"7{cid:018d}", at)
        big = i == 0
        obs(w, d, 6, 9000 if big else 1000 - i, 900 if big else 100 + i)
        obs(w, tid, 6, 20000 if big else 2000 - i, 3000 if big else 200 + i)
    ev = winners.evaluate(w.store)
    m = [x for x in ev["moments"] if yt[0][1] in x["clip_ids"]]
    assert len(m) == 1 and m[0]["outcome"] == "winner" and (m[0]["wins"], m[0]["decisive_results"]) == (2, 2)
    assert sorted((p["platform"], p["views"], p["likes"]) for p in m[0]["platforms"]) == [
        ("tiktok", 20000, 3000),
        ("youtube", 9000, 900),
    ]
    pat = winners.patterns(w.store, ev)["scopes"]["all"]
    assert pat["decided_moments"] == 6 and pat["winning_moments"] == 1  # counted once, not twice


def test_rejected_posts_are_excluded_and_never_peers(w):
    ids = cohort(w, [(5000, 500), (100, 10), (110, 11), (120, 12), (130, 13)])
    cid = w.store.q1("SELECT clip_id FROM deliveries WHERE id=?", ids[0])["clip_id"]
    w.store.merge_json("clips", cid, "trigger", {"review": {"gameplay": {"state": "rejected"}}})
    vd = verdicts(w)
    assert vd[("delivery", ids[0])]["verdict"] == "excluded"
    assert vd[("delivery", ids[0])]["why"].startswith("editorially rejected (not a worthwhile GTA VI moment)")
    assert vd[("delivery", ids[4])]["verdict"] == "insufficient"  # 3 eligible peers left
    pat = winners.patterns(w.store)
    # undecided moments do not count
    assert pat["not_teaching"] == {"rejected": 0, "unverified": 0}


# ---------------------------------------------------------------- content descriptors
def test_descriptors_are_source_grounded_and_mark_what_is_missing(w):
    cid = w.clip(w.others[0], time.time() - 600)
    w.store.merge_json("clips", cid, "trigger", {"score": 9.5, "kind": "chat_spike", "unique_users": 41})
    w.store.merge_json("clips", cid, "files", {"duration": 18.0})
    w.store.update("clips", cid, window_start=time.time() - 610)
    srt(
        w,
        cid,
        [(0, 2, "Wait, what is he doing?"), (4, 7, "Oh my god no way!"), (8, 12, "he actually made it")],
    )
    d = winners.describe(w.store, w.s, cid)
    f = d["features"]
    assert f["opening_text"] == "Wait, what is he doing?" and f["opening_question"] is True
    assert f["spoken_reaction"] is True and f["chat_spike_ratio"] == 9.5 and f["chatters"] == 41
    assert f["payoff_at_s"] == 10.0 and f["words_per_s"] == round(14 / 18, 2)
    assert {
        "opening:question",
        "spoken:reaction",
        "spike:high",
        "duration:short",
        "payoff:middle",
        "density:slow",
    } <= set(d["labels"])
    assert winners.describe(w.store, w.s, cid)["updated_at"] == d["updated_at"]  # idempotent
    bare = w.clip(w.others[1], time.time() - 700)
    w.store.merge_json("clips", bare, "trigger", {"kind": "manual", "score": None})
    w.store.update("clips", bare, captions={"policy": {"render_default": "en"}})
    b = winners.describe(w.store, w.s, bare)
    assert b["features"]["opening_text"] is None and "spike:none" in b["labels"]
    assert any("subtitles" in m for m in b["missing"]) and any("duration" in m for m in b["missing"])
    (w.s.clips_dir / str(cid) / "en.srt").unlink()  # media gone
    w.store.merge_json("clips", cid, "files", {"duration": 18.0, "x": 1})
    assert winners.describe(w.store, w.s, cid)["features"]["opening_text"] == "Wait, what is he doing?"
    assert len(w.store.q1("SELECT features FROM clip_descriptors WHERE clip_id=?", cid)["features"]) < 600


def test_subtitle_parsing_and_text_rules():
    cues = winners._parse_srt(
        "1\r\n00:00:01,500 --> 00:00:02,25\r\n<i>Hola</i> ¿qué?\r\n\r\n"
        "2\n1:00:03.000 --> 1:00:04.000\nline\nsecond\n"
    )
    assert cues == [
        {"start": 1.5, "end": 2.25, "text": "Hola ¿qué?"},
        {"start": 3603.0, "end": 3604.0, "text": "line\nsecond"},
    ]
    assert winners._is_question("¿Dónde está?", "es") and winners._is_question("is that it", "en")
    assert winners._is_reaction("no manches güey", "es") and not winners._is_reaction("calm line", "en")
    assert winners._norm(" a <b>b</b>\n c ") == "a b c"
    assert [winners.window_of(x) for x in (0.5, 1.0, 3.9, 17.0, 20.0, 1000.0)] == [
        None,
        "1-2h",
        "2-4h",
        None,
        "24h",
        None,
    ]
    assert winners._center("1-2h") == pytest.approx(2**0.5)
    assert winners._render_layouts(
        {
            "final_layout": {
                "en": {
                    "1": {"layout_version": 1, "mode": "split", "requested": "auto"},
                    "2": {"layout_version": True, "mode": "fit"},
                    "3": {"layout_version": 1, "mode": "zoom"},
                }
            }
        }
    ) == {"en": {"1": {"mode": "split", "layout_version": 1, "requested": "auto"}}}


def test_a_retired_clip_keeps_its_descriptor(w):
    d, cid = posted(w, w.others[0], 30, lines=[(0, 2, "Is that a tank?")])
    assert winners.descriptor(w.store, cid)["features"]["opening_question"] is True
    w.store.x("DELETE FROM clips WHERE id=?", cid)  # the clip row is retired
    assert w.store.clip(cid) is None
    assert winners.describe(w.store, w.s, cid)["features"]["opening_question"] is True


# ---------------------------------------------------------------- the bounded preference: verified winners
# only
Q = [(0, 2, "What is he doing?"), (3, 6, "he did it")]
S = [(0, 2, "Here we go now."), (3, 6, "he did it")]


def build_patterns(w, dominant=True):
    """Two cohorts (YouTube, TikTok). In each, a question-opener from a different creator is the unique top
    (dominant=True) or only one of two split leaders (dominant=False)."""
    for dest, acct in ((w.yt, "youtube:UC-A"), (w.tt, "tiktok:tt-b")):
        top = (5000, 500) if dominant else (5000, 120)
        rows = [top, (900, 90)] + [(1000 - i, 100 + i) for i in range(4)]
        if not dominant:
            # a statement post leads on likes
            rows[2] = (1100, 600)
        cohort(w, rows, dest=dest, account=acct, lines=[Q, Q] + [S] * 4)
    winners._CACHE.clear()


def fresh(w, ch, lines, score=5.0):
    cid = make_clip(w.store, w.s, ch, score=score)
    at = time.time() - 600
    w.store.update(
        "clips",
        cid,
        status="ready",
        source_lang="en",
        action_time=at,
        window_start=at - 18,
        window_end=at + 12,
    )
    w.store.merge_json(
        "clips", cid, "trigger", {"topic": {"state": "live_gta_category", "category": "Grand Theft Auto VI"}}
    )
    srt(w, cid, lines)
    # housekeeping writes descriptors
    winners.describe_pending(w.store, w.s)
    return cid


def not_exploring():
    return (int(time.time() // 3600) // 4) * 4 * 3600 + 600


def test_verified_winners_teach_a_bounded_preference_and_exploration_stays(w):
    build_patterns(w)
    lab = {x["label"]: x for x in winners.patterns(w.store)["scopes"]["all"]["labels"]}
    assert lab["opening:question"]["evidenced"] and lab["opening:question"]["winning_creators"] == 2
    a = fresh(w, w.others[0], [(0, 2, "Why is he running?")])
    b = fresh(w, w.others[1], [(0, 2, "Okay here it comes.")])
    now = not_exploring()
    rows = {x["clip_id"]: x for x in candidates.candidates(w.pub, now)}
    assert rows[a]["editorial"] == "eligible" and rows[b]["editorial"] == "eligible"
    assert 1.0 < rows[a]["pattern_boost"] <= 1.0 + winners.MAX_BOOST and rows[b]["pattern_boost"] == 1.0
    assert rows[a]["rank"] < rows[b]["rank"] and "verified winners" in rows[a]["pattern_reasons"][0]
    rows2 = {x["clip_id"]: x for x in candidates.candidates(w.pub, now + 3 * 3600)}
    assert winners.exploring(now + 3 * 3600) and rows2[a]["pattern_boost"] == 1.0
    strong = fresh(w, w.others[2], [(0, 2, "Okay here it comes.")], score=60.0)
    rows3 = {x["clip_id"]: x for x in candidates.candidates(w.pub, now)}
    assert rows3[strong]["rank"] < rows3[a]["rank"]  # bounded nudge
    assert winners.factor(w.store, w.s, w.store.clip(a), now, prefer=False) == (
        1.0,
        ["pattern preference off"],
    )


def test_promising_posts_never_teach_or_boost(w):
    build_patterns(w, dominant=False)
    sc = winners.patterns(w.store)["scopes"]["all"]
    assert sc["winning_moments"] == 0 and not any(x["evidenced"] for x in sc["labels"])
    assert any(m["outcome"] == "promising" for m in winners.evaluate(w.store)["moments"])
    a = fresh(w, w.others[0], [(0, 2, "Why is he running?")])
    assert winners.factor(w.store, w.s, w.store.clip(a), not_exploring())[0] == 1.0


def test_no_preference_for_a_posted_moment_and_every_gate_still_applies(w):
    build_patterns(w)
    now = not_exploring()
    won = [m for m in winners.evaluate(w.store)["moments"] if m["outcome"] == "winner"][0]
    src = w.store.clip(won["clip_ids"][0])
    recut = fresh(w, src["channel_id"], Q)
    w.store.update(
        "clips",
        recut,
        action_time=src["action_time"],
        window_start=src["window_start"],
        window_end=src["window_end"],
    )
    w.store.merge_json("clips", recut, "trigger", {"moment_id": None, "moment_creator": None})
    assert moments.moment_id(w.store, recut) == won["moment_id"]
    w.store.x("DELETE FROM clip_descriptors WHERE clip_id=?", recut)
    winners.describe_pending(w.store, w.s)
    winners._CACHE.clear()
    f, why = winners.factor(w.store, w.s, w.store.clip(recut), now)
    assert f == 1.0 and "already posted" in why[0]
    old = fresh(w, w.others[3], [(0, 2, "Who did that?")])
    w.store.update("clips", old, action_time=time.time() - 25 * H)
    rows = {x["clip_id"]: x for x in candidates.candidates(w.pub, now)}
    assert rows[old]["editorial"] == "not_eligible" and "freshness" in rows[old]["reason"]
    assert rows[old]["pattern_boost"] == 1.0
    a = fresh(w, w.others[0], [(0, 2, "Why is he running?")])
    b = fresh(w, w.others[1], [(0, 2, "Okay here it comes.")])
    ka = moments.rotation_key(w.store, a, {moments.creator_key(w.store, w.others[0]): 3}, now)
    kb = moments.rotation_key(w.store, b, {}, now)
    assert kb < ka  # rotation stays the first key


def test_winner_without_retained_content_is_counted_but_cannot_teach(w):
    ids = cohort(w, [(500, 50), (90, 15), (60, 7), (43, 2), (17, 1)])
    cid = w.store.q1("SELECT clip_id FROM deliveries WHERE id=?", ids[0])["clip_id"]
    w.store.x("DELETE FROM clip_descriptors WHERE clip_id=?", cid)
    ev = winners.evaluate(w.store)
    assert [p["ref_id"] for p in ev["winning_posts"]] == [ids[0]]
    scope = winners.patterns(w.store, ev)["scopes"]["en"]
    assert (scope["winning_moments"], scope["described_winners"], scope["undescribed"]) == (1, 0, 1)


def test_an_evidenced_spanish_pattern_cannot_boost_an_english_clip(w):
    build_patterns(w)
    for a in w.store.posts.values():
        a["language"] = "es"
    clip_id = fresh(w, w.others[0], Q)
    scopes = winners.patterns(w.store)["scopes"]
    assert any(x["evidenced"] for x in scopes["all"]["labels"])
    assert any(x["evidenced"] for x in scopes["es"]["labels"])
    assert winners.factor(w.store, w.s, w.store.clip(clip_id), not_exploring())[0] == 1.0


def test_weak_feed_likes_and_posts_without_readings_are_reported_not_hidden(w):
    seen, _ = posted(w, chans(w, 1)[0], 30)
    unseen, _ = posted(w, chans(w, 1)[0], 30)
    # the feed's likes are not verified
    obs(w, seen, 6, 200, 10, source="youtube_feed")
    ev = winners.evaluate(w.store)
    assert ev["groups"][0]["posts"][0]["likes"] is None
    assert "(the public feed has no verified like count)" in ev["groups"][0]["posts"][0]["why"]
    assert ev["coverage"] == [
        {
            "language": "en",
            "platform": "youtube",
            "account": "youtube:UC-A",
            "posts": 2,
            "no_window_reading": 1,
            "missing": 1,
            "insufficient": 0,
            "timing_unverified": 0,
            "excluded": 0,
            "decided": 0,
            "winners": 0,
        }
    ]
    assert unseen not in [p["ref_id"] for g in ev["groups"] for p in g["posts"]]


def test_readings_outside_every_window_are_kept_but_never_compared(w):
    did, _ = posted(w, chans(w, 1)[0], 30)
    obs(w, did, 17, 100, 5)
    ev = winners.evaluate(w.store)
    assert ev["groups"] == [] and ev["off_window"][0]["ref_id"] == did
    assert ev["off_window"][0]["why"] == "age outside every comparable window: kept, never compared"


def test_historical_duplicate_placement_and_creator_moment_cannot_supply_peers(w):
    ids = cohort(w, [(100, 10), (50, 4), (30, 1), (25, 0), (10, 0), (5, 0)])
    # One duplicate physical video, then a second edit of the same creator moment with a different public URL.
    orig = w.store.posts[("delivery", ids[0])]
    w.store.posts[("delivery", ids[1])]["post_id"] = orig["post_id"]
    w.store.posts[("delivery", ids[2])]["clip_id"] = orig["clip_id"]
    ev = winners.evaluate(w.store)
    assert ev["groups"][0]["posts_with_both_metrics"] == 4
    assert not ev["winning_posts"] and ev["coverage"][0]["insufficient"] == 4
    assert ev["coverage"][0]["posts"] == 4


def test_platform_winner_survives_a_loss_on_another_platform(w):
    ids = cohort(w, [(900, 100), (400, 60), (300, 50), (200, 40), (100, 30)])
    tts = cohort(w, [(5, 0), (500, 30), (40, 2), (30, 1), (20, 0)], dest=w.tt, account="tiktok:tt-b")
    cid = w.store.q1("SELECT clip_id FROM deliveries WHERE id=?", ids[0])["clip_id"]
    w.store.posts[("delivery", tts[0])]["clip_id"] = cid
    ev = winners.evaluate(w.store)
    assert any(p["ref_id"] == ids[0] and p["platform"] == "youtube" for p in ev["winning_posts"])
    m = next(m for m in ev["moments"] if cid in m["clip_ids"])
    assert m["outcome"] == "promising" and m["wins"] == 1 and m["decisive_results"] == 2


def test_an_unknown_publication_time_cannot_supply_winner_or_peer_until_it_is_known(w):
    posts = []
    for i in range(5):
        did, _ = posted(w, chans(w, 1)[0], 30)
        w.store.posts[("delivery", did)]["public_bounds"] = None  # planner time only
        obs(w, did, 6, 100 + i, 10 + i)
        posts.append(did)
    before = winners.evaluate(w.store)
    assert before["coverage"][0]["timing_unverified"] == 5 and not before["winning_posts"]
    assert not any(g["posts_with_both_metrics"] for g in before["groups"])
    # the provider reports the time
    for did in posts:
        a = w.store.posts[("delivery", did)]
        a["public_bounds"] = (a["published_at"], a["published_at"])
    result = winners.evaluate(w.store)
    assert result["winning_posts"][0]["ref_id"] == posts[-1]
    assert (
        result["winning_posts"][0]["window"] == "4-8h"
        and result["winning_posts"][0]["time_source"] == "provider"
    )


def test_bounded_publication_time_must_fit_entirely_within_one_window(w):
    did, _ = posted(w, w.others[0], 30)
    at = w.store.posts[("delivery", did)]["published_at"]
    obs(w, did, 6, 100, 4)
    w.store.posts[("delivery", did)]["public_bounds"] = (at - H, at)
    assert verdicts(w)[("delivery", did)]["age_verified"]
    w.store.posts[("delivery", did)]["public_bounds"] = (at - 3 * H, at)
    assert verdicts(w)[("delivery", did)]["verdict"] == "timing_unverified"


def test_winner_placement_uses_the_layout_of_the_exact_published_render_after_retirement(w):
    did, cid = posted(w, chans(w, 1)[0], 30, lines=[(0, 2, "How did that happen?")])
    obs(w, did, 6, 100, 3)
    assert verdicts(w)[("delivery", did)]["render_layout"] is None
    split = {"layout_version": 1, "requested": "auto", "mode": "split"}
    fit = {"layout_version": 1, "requested": "auto", "mode": "fit"}
    w.store.merge_json(
        "clips", cid, "files", {"final_layout": {"en": {"1": split, "2": fit}, "es": {"1": fit}}}
    )
    # The subtitle has already moved to cold storage when final-layout observation arrives.
    (w.s.clips_dir / str(cid) / "en.srt").unlink()
    winners.describe(w.store, w.s, cid)
    assert winners.descriptor(w.store, cid)["features"]["opening_text"] == "How did that happen?"
    w.store.x("DELETE FROM clips WHERE id=?", cid)
    post = verdicts(w)[("delivery", did)]
    assert post["render_layout"] == split and post["media_version"] == 1
    # No snapshot is evidence for an unobserved re-render.
    w.store.posts[("delivery", did)]["media_version"] = 8
    assert verdicts(w)[("delivery", did)]["render_layout"] is None


def test_linking_simulcast_channels_after_both_descriptors_exist_does_not_inflate_peer_count(w):
    channels = chans(w, 5)
    ids = []
    clips = []
    for channel in channels:
        did, cid = posted(w, channel, 30)
        obs(w, did, 6, 300 if not ids else 100, 9 if not ids else 1)
        ids.append(did)
        clips.append(cid)
    before = verdicts(w)
    assert before[("delivery", ids[0])]["verdict"] == "winner"
    descriptors = [winners.descriptor(w.store, cid) for cid in clips]
    assert len({d["moment_id"] for d in descriptors}) == 5
    for channel in channels[-2:]:
        w.store.merge_json("channels", channel, "settings", {"simulcast_group": "same-creator"})
    after = verdicts(w)
    assert len(after) == 4
    assert after[("delivery", ids[0])]["verdict"] == "insufficient"
    assert [winners.descriptor(w.store, cid) for cid in clips] == descriptors  # report remains read-only


def test_the_model_is_cached_until_post_data_or_descriptors_change(w):
    build_patterns(w)
    m1 = winners._model(w.store)
    assert winners._model(w.store) is m1
    w.store.obs.append(dict(w.store.obs[0], observed_at=w.store.obs[0]["observed_at"] + 60))
    assert winners._model(w.store) is not m1
