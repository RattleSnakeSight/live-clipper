"""End to end, real FFmpeg, no network: a synthetic local HLS live stream and a synthetic chat with one spike go
through capture -> ring buffer -> detector -> selection -> cut + preview -> captions/title/editorial/language
checks -> final render -> edit.clip_ready -> publishing to the labelled test destination."""

from __future__ import annotations

import contextlib

import pytest
from e2e_util import (
    FFMPEG,
    FFPROBE,
    Stream,
    add_channel,
    burst_bounds,
    deliveries,
    probe,
    published,
    running_app,
    sandbox_videos,
    service,
    wait,
    x264_crf,
)

pytestmark = [pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH"),
              pytest.mark.timeout(900)]  # real FFmpeg pipelines with restarts: minutes on a slow runner


@pytest.fixture
def streams(tmp_path):
    made = []

    def make(name="hls"):
        s = Stream(tmp_path / name).up()
        made.append(s)
        s.wait_ready()
        return s

    yield make
    for s in made:
        s.down()


@pytest.fixture
def app():
    """yields (bus events, start(mode) -> (ctx, client)): the full app, started by the test."""
    events: list[tuple[str, dict]] = []
    with contextlib.ExitStack() as stack:
        yield events, lambda mode="auto": stack.enter_context(running_app(mode, events))


def ready_events(events, clip_id=None):
    return [p for t, p in events if t == "edit.clip_ready" and (clip_id is None or p["clip_id"] == clip_id)]


def test_single_channel_spike_to_published_clip(app, streams):
    events, start = app
    stream = streams()
    ctx, client = start("auto")
    cid, t0 = add_channel(ctx, client, stream, "streamer_alpha", spike_at=26)
    store = service(ctx, "monitor").store
    pub = published(client, store)

    clips = store.q("SELECT * FROM clips")
    assert len(clips) == 1, [c["status"] for c in clips]
    c = clips[0]
    trig = c["trigger"]
    # onset of the burst (integer second on the local clock), action = onset - reaction delay 4 s
    lo, hi = burst_bounds(t0, 26)
    assert lo <= trig["burst_start"] <= hi and trig["action_time"] == trig["burst_start"] - 4.0
    assert trig["score"] >= 3.0 and trig["format_id"] == "gta-vi"
    # the cut window: [action - 18, action + 12] (the built-in format), not truncated by the ring
    assert abs(c["window_start"] - (trig["action_time"] - 18.0)) < 0.05 and not c["truncated"]
    assert abs(c["window_end"] - (trig["action_time"] + 12.0)) < 0.05
    assert c["preset_id"] == "default" and c["status"] == "ready"
    d = service(ctx, "edit").engine.clip_dir(c["id"])
    prev, final = probe(d / c["files"]["preview"]), probe(d / c["files"]["final"]["en"])
    assert (prev["width"], prev["height"]) == (720, 1280) and (final["width"], final["height"]) == (1080, 1920)
    assert abs(final["duration"] - 30.0) < 0.6 and abs(prev["duration"] - 30.0) < 0.6
    assert x264_crf(d / c["files"]["final"]["en"]) == 21.0
    # selection qualified it (opinion statement naming the game, score over the threshold, English confirmed)
    moments = [p for t, p in events if t == "select.moment"]
    assert [m["decision"] for m in moments] == ["qualified"] and moments[0]["clip_id"] == c["id"]
    ready = ready_events(events)
    assert len(ready) == 1 and ready[0]["clip_id"] == c["id"] and ready[0]["path"].endswith("final_en.mp4")
    assert (ready[0]["width"], ready[0]["height"]) == (1080, 1920) and abs(ready[0]["duration_s"] - 30) < 0.6
    assert ready[0]["format_id"] == "gta-vi" and ready[0]["preset_id"] == "default"
    assert "streamer_alpha" in ready[0]["description"] and ready[0]["title"]
    assert len(pub) == 1 and pub[0]["test"] and pub[0]["clip_id"] == c["id"]
    assert len(deliveries(client)) == 1 and sandbox_videos(ctx.paths.root) == 1


def test_two_channels_at_once_held_in_manual_mode_until_posted(app, streams):
    events, start = app
    a, b = streams("hls_a"), streams("hls_b")
    ctx, client = start("manual")
    ca, ta = add_channel(ctx, client, a, "streamer_alpha", spike_at=26)
    cb, tb = add_channel(ctx, client, b, "streamer_beta", spike_at=28)
    store = service(ctx, "monitor").store
    held = wait(lambda: len([d for d in deliveries(client) if d["status"] == "held"]) == 2 and deliveries(client),
                240, "two held deliveries")
    clips = {c["channel_id"]: c for c in store.q("SELECT * FROM clips")}
    assert sorted(clips) == sorted([ca, cb]) and len(store.q("SELECT id FROM clips")) == 2
    for cid, t0, spike in ((ca, ta, 26), (cb, tb, 28)):
        c = clips[cid]
        trig = c["trigger"]
        lo, hi = burst_bounds(t0, spike)
        assert lo <= trig["burst_start"] <= hi, (t0, spike, trig["burst_start"])
        assert abs(c["window_start"] - (trig["burst_start"] - 22.0)) < 0.05
        assert abs(c["window_end"] - (trig["burst_start"] + 8.0)) < 0.05
    assert {d["clip_id"] for d in held} == {clips[ca]["id"], clips[cb]["id"]}
    assert sorted(p["channel_id"] for p in ready_events(events)) == sorted([ca, cb])
    import time

    time.sleep(1.0)  # manual mode: nothing moves until the user clicks Post
    assert all(d["status"] == "held" for d in deliveries(client)) and sandbox_videos(ctx.paths.root) == 0
    for d in held:
        assert client.post(f"/api/publish/deliveries/{d['id']}/post").status_code == 200
    done = published(client, store, n=2, timeout=60)
    assert len(done) == 2 and len(deliveries(client)) == 2 and sandbox_videos(ctx.paths.root) == 2


def test_custom_format_rule_and_clip_override_change_the_rendered_output(app, streams):
    events, start = app
    stream = streams()
    ctx, client = start("auto")
    # the customer's format (shorter window), an editing preset, and a rule that picks it for this streamer
    fmt = {"id": "short-moments", "name": "Short moments", "window": {"pre": 8, "post": 6}}
    assert client.post("/api/formats", json=fmt).status_code == 201
    preset = {"id": "compact", "name": "Compact", "encode": {"final_height": 960, "render_crf": 26}}
    assert client.post("/api/presets", json=preset).status_code == 201
    rule = {"id": "alpha-compact", "name": "Compact clips for streamer_alpha",
            "streamers": {"channels": ["streamer_alpha"]}, "actions": {"preset_id": "compact"}}
    assert client.post("/api/rules", json=rule).status_code == 201
    cid, t0 = add_channel(ctx, client, stream, "streamer_alpha", spike_at=22, format_id="short-moments")
    store = service(ctx, "monitor").store
    published(client, store)

    c = store.q("SELECT * FROM clips")[0]
    trig = c["trigger"]
    assert trig["format_id"] == "short-moments" and c["preset_id"] == "compact"
    assert abs(c["window_start"] - (trig["action_time"] - 8.0)) < 0.05      # the format's window
    assert abs(c["window_end"] - (trig["action_time"] + 6.0)) < 0.05
    d = service(ctx, "edit").engine.clip_dir(c["id"])
    final = d / c["files"]["final"]["en"]
    m = probe(final)
    assert (m["width"], m["height"]) == (540, 960) and abs(m["duration"] - 14.0) < 0.6   # the rule's preset
    assert x264_crf(final) == 26.0
    first = ready_events(events, c["id"])[-1]
    assert first["format_id"] == "short-moments" and first["preset_id"] == "compact"
    assert (first["width"], first["height"]) == (540, 960)

    # per-clip override (clip editor): an encode value and a selection value (title template)
    r = client.post(f"/api/clips/{c['id']}/render", json={
        "overrides": {"encode": {"render_crf": 30}}, "override": {"output.title_template": "{channel}: {title}"}})
    assert r.status_code == 200, r.text
    wait(lambda: not client.get(f"/api/clips/{c['id']}").json()["rendering"]
         and x264_crf(final) == 30.0, 120, "the re-render")
    m2 = probe(final)
    assert (m2["width"], m2["height"]) == (540, 960) and abs(m2["duration"] - 14.0) < 0.6
    last = ready_events(events, c["id"])[-1]
    assert last["title"].startswith("streamer_alpha: ") and not first["title"].startswith("streamer_alpha: ")
    c2 = store.clip(c["id"])
    assert c2["captions"]["en"]["title"] == last["title"] and c2["trigger"]["override"]
    assert len(deliveries(client)) == 1 and sandbox_videos(ctx.paths.root) == 1   # no second publication
