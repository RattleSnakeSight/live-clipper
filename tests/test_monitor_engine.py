"""Engine additions of the public product: official status gates capture, recording slots, public states."""

import asyncio

import httpx
import pytest
from monitor_env import env

from live_clipper.capture import Capture
from live_clipper.engine import Engine, LiveRefused, public_state
from live_clipper.monitor_service import parse_channel_url
from live_clipper.sources import STREAM_PROBE, StatusHub


@pytest.fixture
def fake_capture(monkeypatch):
    """Capture.start/stop without processes; records every start (a streamlink attempt in real life)."""
    calls = []

    def start(self):
        calls.append(("start", self.name))
        self._task = asyncio.get_running_loop().create_future()
        self.health.state = "live"

    async def stop(self):
        calls.append(("stop", self.name))
        self._task = None
        self.health.state = "stopped"

    monkeypatch.setattr(Capture, "start", start)
    monkeypatch.setattr(Capture, "stop", stop)
    return calls


async def wait_for(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def test_official_status_gates_capture_and_maps_states(tmp_path, fake_capture):
    s, store, sec = env(tmp_path)
    sec.update("source", "twitch", {"client_id": "c", "client_secret": "s"})
    live = {"on": False}

    def h(req):
        if req.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "APP", "expires_in": 3600})
        data = [
            {
                "user_login": "streamer_alpha",
                "type": "live",
                "title": "GTA 6 day",
                "game_name": "",
                "id": "b1",
            }
        ]
        return httpx.Response(200, json={"data": data if live["on"] else []})

    hub = StatusHub(sec, transport=httpx.MockTransport(h), intervals={"twitch": 0.05})
    eng = Engine(s, store, sec, hub=hub)
    cid = store.add_channel("twitch", "streamer_alpha")

    async def go():
        eng.arm(cid)
        m = eng.monitors[cid]
        await wait_for(lambda: m.official.get("state") == "offline")
        assert fake_capture == []  # offline: no capture attempt at all
        st = m.status()
        assert st["status_source"] == "Twitch Helix Get Streams"
        assert public_state(st, True)["state"] == "offline"
        live["on"] = True
        await wait_for(lambda: m.media_on)
        assert public_state(m.status(), True)["state"] == "recording"
        assert m.topic_evidence()["state"] == "live_gta"  # official title is clip evidence too
        live["on"] = False
        await wait_for(lambda: not m.media_on)
        await eng.stop()

    asyncio.run(go())
    assert fake_capture == [
        ("start", "streamer_alpha"),
        ("stop", "streamer_alpha"),
        ("stop", "streamer_alpha"),
    ]


def test_without_official_api_the_reference_capture_loop_runs(tmp_path, fake_capture):
    s, store, sec = env(tmp_path, topic_observe_seconds=0)
    hub = StatusHub(sec)
    eng = Engine(s, store, sec, hub=hub)
    cid = store.add_channel("kick", "streamer_beta")

    async def go():
        eng.arm(cid)
        st = eng.monitors[cid].status()
        await eng.stop()
        return st

    st = asyncio.run(go())
    assert fake_capture[0] == ("start", "streamer_beta") and st["status_source"] == STREAM_PROBE


def test_recording_slot_limit_queues_and_releases(tmp_path, fake_capture):
    s, store, sec = env(tmp_path, max_concurrent_recordings=2, topic_observe_seconds=0)
    eng = Engine(s, store, sec)
    ids = [store.add_channel("custom", f"feed{i}", f"srt://example.invalid:900{i}") for i in range(3)]

    async def go():
        for c in ids:
            eng.arm(c)
        waiting = eng.monitors[ids[2]]
        assert waiting.slot_wait and not waiting.media_on
        assert public_state(waiting.status(), True) == {
            "state": "waiting",
            "fix": "",
            "detail": "waiting for a free recording slot",
        }
        await eng.disarm(ids[0])
        await eng.start()  # housekeeping retries queued monitors
        await wait_for(lambda: waiting.media_on)
        await eng.stop()

    asyncio.run(go())
    assert sum(1 for k, _ in fake_capture if k == "start") == 3


def test_vod_only_and_retired_channels_are_refused(tmp_path):
    s, store, sec = env(tmp_path)
    eng = Engine(s, store, sec)
    vod = store.add_channel("custom", "a", "srt://h:1", {"ingest_mode": "vod_only"})
    ret = store.add_channel("custom", "b", "srt://h:2", {"retired": {"at": 1, "reason": "test"}})
    for cid in (vod, ret):
        with pytest.raises(LiveRefused):
            eng.arm(cid)
        assert store.channel(cid)["armed"] == 0


def test_public_state_mapping():
    assert public_state({}, False)["state"] == "disabled"
    assert public_state({"capture": {"state": "starting"}}, True)["state"] == "waiting"
    assert (
        public_state({"capture": {"state": "reconnecting", "detail": "capture exited (0)"}}, True)["state"]
        == "reconnecting"
    )
    off = {"capture": {"state": "waiting_for_live", "detail": "channel offline; retrying"}}
    assert public_state(off, True)["state"] == "offline"
    err = public_state(
        {
            "capture": {
                "state": "error",
                "detail": "missing binary: ffmpeg",
                "action_needed": "Install ffmpeg on the worker",
            }
        },
        True,
    )
    assert err == {
        "state": "needs_action",
        "detail": "missing binary: ffmpeg",
        "fix": "Install ffmpeg on the worker",
    }
    creds = public_state(
        {
            "capture": {"state": "stopped"},
            "official": {
                "state": "error",
                "detail": "Kick rejected the access token (HTTP 401)",
                "action_needed": "Check the Client ID",
            },
        },
        True,
    )
    assert creds["state"] == "needs_action" and creds["fix"] == "Check the Client ID"
    assert (
        public_state({"watch": {"state": "live_off_topic"}, "capture": {"state": "stopped"}}, True)["state"]
        == "waiting"
    )


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.twitch.tv/streamer_alpha", ("twitch", "streamer_alpha", "")),
        ("twitch.tv/streamer_alpha/videos", ("twitch", "streamer_alpha", "")),
        ("https://kick.com/streamer-beta", ("kick", "streamer-beta", "")),
        ("https://www.youtube.com/@streamer_gamma/live", ("youtube", "@streamer_gamma", "")),
        (
            "https://youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa",
            ("youtube", "channel/UCaaaaaaaaaaaaaaaaaaaaaa", ""),
        ),
        ("srt://example.com:9000", ("custom", "srt://example.com:9000", "srt://example.com:9000")),
        (
            "https://example.com/live/index.m3u8",
            ("custom", "https://example.com/live/index.m3u8", "https://example.com/live/index.m3u8"),
        ),
    ],
)
def test_parse_channel_url(url, expected):
    assert parse_channel_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=vid00000001",
        "https://twitch.tv/",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "x" * 600,
    ],
)
def test_parse_channel_url_rejects(url):
    with pytest.raises(ValueError):
        parse_channel_url(url)
