"""Integration: real FFmpeg capture of a local HLS stream (FFmpeg-generated, served over local HTTP).

recording -> ring segments -> stream drops (server and generator gone) -> reconnecting -> stream back ->
recording again in a new epoch. Loopback only; skipped when FFmpeg is not installed.
"""

import asyncio
import functools
import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from monitor_env import env

from live_clipper.engine import Engine, public_state

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")


class Stream:
    """FFmpeg writes a live HLS playlist (1 s segments) into `root`; a threaded HTTP server serves it."""

    def __init__(self, root: Path):
        self.root, self.port, self.proc, self.httpd = root, 0, None, None

    def up(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        self.proc = subprocess.Popen(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=25",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "25",
                "-c:a",
                "aac",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "6",
                "-hls_flags",
                "delete_segments+omit_endlist",
                str(self.root / "live.m3u8"),
            ],
            stdin=subprocess.DEVNULL,
        )
        handler = functools.partial(QuietHandler, directory=str(self.root))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def down(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc.wait(10)
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/live.m3u8"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def test_local_hls_recording_disconnect_reconnect(tmp_path):
    stream = Stream(tmp_path / "hls")
    stream.up()
    s, store, sec = env(tmp_path, ffmpeg_path=FFMPEG, segment_seconds=1.0)
    eng = Engine(s, store, sec)
    seen: list[tuple[str, str]] = []

    async def until(cond, timeout):
        for _ in range(int(timeout / 0.1)):
            st = m.status()
            pair = (st["capture"]["state"], public_state(st, True)["state"])
            if not seen or seen[-1] != pair:
                seen.append(pair)
            if cond():
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"timeout; transitions so far: {seen}")

    async def go():
        nonlocal m
        for _ in range(100):  # first playlist written
            if (stream.root / "live.m3u8").exists():
                break
            await asyncio.sleep(0.1)
        cid = store.add_channel("custom", stream.url, stream.url)
        eng.arm(cid)
        m = eng.monitors[cid]
        idx = eng.buffers.index(cid)
        await until(lambda: m.capture.health.state == "live" and len(idx.all_segments()) >= 3, 45)
        files = [Path(sg.path) for sg in idx.all_segments()]
        assert all(f.exists() and f.stat().st_size > 0 and f.suffix == ".ts" for f in files)
        assert all(Path(s.buffer_dir) in f.parents for f in files)  # ring under the buffer dir
        stream.down()  # network drop
        await until(
            lambda: (
                m.capture.health.state in ("reconnecting", "waiting_for_live")
                and m.capture.health.restarts >= 1
            ),
            90,
        )
        stream.up()  # stream is back
        await until(lambda: m.capture.health.state == "live" and len(idx.epochs) >= 2, 90)
        epochs = [ep.reason for ep in idx.epochs]
        await eng.stop()
        return epochs

    m = None
    try:
        epochs = asyncio.run(go())
    finally:
        stream.down()
    seen = [x for x in seen if x[0] != "stopped"]  # before the capture task first ran
    states = [c for c, _ in seen]
    public = [p for _, p in seen]
    assert states[0] == "starting" and "live" in states
    print("transitions:", seen)
    first_live = states.index("live")
    assert "reconnecting" in states[first_live:]
    assert states[-1] == "live" and epochs[0] == "capture start" and epochs[-1] == "capture restart"
    assert public[public.index("recording") :].count("recording") >= 2 and "reconnecting" in public, seen
