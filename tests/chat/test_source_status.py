"""Rumble public JSON-LD live status: timezone-aware broadcast intervals, strict boolean, fail closed.
An archived or scheduled GTA stream must never open the title gate."""
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from live_clipper.source_status import is_gta6, parse_public_jsonld

NOW = datetime(2026, 11, 19, 18, 0, tzinfo=UTC)


def iso(delta_hours: float, tz=UTC) -> str:
    return (NOW + timedelta(hours=delta_hours)).astimezone(tz).isoformat()


def page(*items) -> str:
    return "".join(f'<script type="application/ld+json">{json.dumps(i)}</script>' for i in items)


def top_event(**kw):
    return {"@context": "https://schema.org", "@type": "BroadcastEvent", "name": "GTA 6 heists all night", **kw}


def video(pub, name="GTA VI first look", **kw):
    return {"@context": "https://schema.org", "@type": "VideoObject", "name": name, "publication": pub, **kw}


def bev(**kw):
    return {"@type": "BroadcastEvent", **kw}


def state(html):
    return parse_public_jsonld(html, now=NOW)


# ------------------------------------------------------------------ archived / scheduled top-level BroadcastEvent
def test_archived_top_level_gta_broadcast_is_not_live():
    st = state(page(top_event(isLiveBroadcast=True, startDate=iso(-30), endDate=iso(-26))))   # ended yesterday
    assert st["state"] == "offline" and st["title"] == ""
    assert not (st["state"] == "live" and is_gta6(st["title"]))


def test_scheduled_top_level_broadcast_is_not_live():
    st = state(page(top_event(isLiveBroadcast=True, startDate=iso(+20))))                      # starts tomorrow
    assert st["state"] == "offline" and "scheduled" in st["detail"]


def test_end_exactly_now_is_ended():
    assert state(page(top_event(isLiveBroadcast=True, startDate=iso(-2), endDate=iso(0))))["state"] == "offline"


# ------------------------------------------------------------------ current
def test_current_top_level_broadcast_open_ended_is_live():
    st = state(page(top_event(isLiveBroadcast=True, startDate=iso(-1))))
    assert st == {"state": "live", "title": "GTA 6 heists all night", "category": "", "detail": ""}


def test_video_object_in_progress_with_future_end_is_live():
    """Previously ignored: a VideoObject whose live publication has a future endDate."""
    st = state(page(video(bev(isLiveBroadcast=True, startDate=iso(-1), endDate=iso(+2)), genre="Gaming")))
    assert st["state"] == "live" and st["title"] == "GTA VI first look" and st["category"] == "Gaming"


def test_offsets_are_compared_as_instants():
    est = timezone(timedelta(hours=-5))
    # start 30 min ago expressed in UTC-5, end in 1 h expressed in UTC+9
    st = state(page(video(bev(isLiveBroadcast=True, startDate=iso(-0.5, est),
                              endDate=iso(+1, timezone(timedelta(hours=9)))))))
    assert st["state"] == "live"
    # same wall-clock digits but in UTC-5 -> actually 5 h in the future -> scheduled
    wall = (NOW - timedelta(minutes=30)).replace(tzinfo=None).isoformat() + "-05:00"
    assert state(page(video(bev(isLiveBroadcast=True, startDate=wall))))["state"] == "offline"


def test_video_object_with_ended_publication_is_offline():
    st = state(page(video([bev(isLiveBroadcast=True, startDate=iso(-50), endDate=iso(-47))])))
    assert st["state"] == "offline"


def test_graph_and_list_containers():
    g = {"@context": "https://schema.org", "@graph": [{"@type": "Organization", "name": "x"},
                                                       top_event(isLiveBroadcast=True, startDate=iso(-1))]}
    assert state(page(g))["state"] == "live"
    assert state(page([video(bev(isLiveBroadcast=True, startDate=iso(-1)))]))["state"] == "live"


def test_current_live_wins_over_archived_items():
    html = page(video(bev(isLiveBroadcast=True, startDate=iso(-80), endDate=iso(-77)), name="old GTA 6 stream"),
                video(bev(isLiveBroadcast=True, startDate=iso(-1)), name="GTA VI tonight"))
    assert state(html)["title"] == "GTA VI tonight"


# ------------------------------------------------------------------ ambiguous / malformed -> unavailable, never live
@pytest.mark.parametrize("ev", [
    dict(isLiveBroadcast="true", startDate=iso(-1)),                 # string, not bool
    dict(isLiveBroadcast=1, startDate=iso(-1)),                      # int, not bool
    dict(isLiveBroadcast=True),                                      # no startDate
    dict(isLiveBroadcast=True, startDate="2026-11-19T17:00:00"),     # naive
    dict(isLiveBroadcast=True, startDate="2026-11-19"),              # date only
    dict(isLiveBroadcast=True, startDate="yesterday-ish"),           # unparsable
    dict(isLiveBroadcast=True, startDate=12345),                     # not a string
    dict(isLiveBroadcast=True, startDate=iso(-1), endDate="2026-11-20T01:00:00"),  # naive end
    dict(isLiveBroadcast=True, startDate=iso(-1), endDate=iso(-2)),  # end before start
])
def test_ambiguous_broadcasts_fail_closed(ev):
    for html in (page(top_event(**ev)), page(video(bev(**ev)))):
        st = state(html)
        assert st["state"] == "unavailable" and st["title"] == "", (ev, st)


def test_two_live_broadcasts_with_different_titles_is_ambiguous():
    html = page(video(bev(isLiveBroadcast=True, startDate=iso(-1)), name="Just chatting"),
                video(bev(isLiveBroadcast=True, startDate=iso(-1)), name="GTA 6 now"))
    assert state(html)["state"] == "unavailable"


def test_live_without_title_is_ambiguous():
    assert state(page(top_event(isLiveBroadcast=True, startDate=iso(-1), name="")))["state"] == "unavailable"


def test_ambiguous_item_beside_ended_ones_is_not_offline():
    html = page(video(bev(isLiveBroadcast=True, startDate=iso(-50), endDate=iso(-47))),
                video(bev(isLiveBroadcast="yes", startDate=iso(-1))))
    assert state(html)["state"] == "unavailable"


@pytest.mark.parametrize("html", ["", "<html><body>Access denied</body></html>",
                                  page({"@type": "Organization", "name": "Channel"}),
                                  page({"@type": "VideoObject", "name": "GTA 6 VOD"}),
                                  '<script type="application/ld+json">{not json</script>'])
def test_unrecognizable_response_is_unavailable_not_offline(html):
    st = state(html)
    assert st["state"] == "unavailable" and st["title"] == ""


def test_explicit_not_live_broadcast_is_offline():
    assert state(page(top_event(isLiveBroadcast=False, startDate=iso(-1))))["state"] == "offline"


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        parse_public_jsonld("", now=datetime(2026, 11, 19))


# ------------------------------------------------------------------ title-only rule on top of a live status
def test_title_only_rule_category_never_qualifies():
    st = state(page(video(bev(isLiveBroadcast=True, startDate=iso(-1)), name="Talking politics",
                          genre="Grand Theft Auto VI")))
    assert st["state"] == "live" and not is_gta6(st["title"], st["category"])
    st = state(page(video(bev(isLiveBroadcast=True, startDate=iso(-1)), name="Grand Theft Auto VI day 1")))
    assert is_gta6(st["title"])


# ------------------------------------------------------------------ engine: qualifying title != capturing media
def test_rumble_qualifying_title_without_media_input_is_not_reported_as_capturing(tmp_path, monkeypatch):
    import asyncio

    from monitor_env import env

    from live_clipper import source_status
    from live_clipper.engine import Engine
    s, store, _ = env(tmp_path)
    eng = Engine(s, store)

    async def fake_status(*a, **k):
        return {"state": "live", "title": "GTA 6 tonight", "category": "", "detail": ""}
    monkeypatch.setattr(source_status, "source_status", fake_status)
    cid = store.add_channel("rumble", "somechannel", "", {"topic_gate": {"enabled": True, "poll_seconds": 60}})

    async def go():
        eng.arm(cid)
        m = eng.monitors[cid]
        for _ in range(100):
            if m.gate_open and m.capture.health.state == "error":
                break
            await asyncio.sleep(0.02)
        st = m.status()
        await eng.disarm(cid)
        return st
    st = asyncio.run(go())
    assert st["watch"]["state"] == "live_gta" and st["gate_open"]
    assert st["capture"]["state"] == "error" and "no supported public media path" in st["capture"]["detail"]
    msgs = [r["message"] for r in store.q("SELECT message FROM log WHERE message LIKE 'watch:%'")]
    assert msgs and all("capture requested" in x and "capture started" not in x for x in msgs)


