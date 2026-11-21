"""Helpers for the end-to-end tests: a synthetic local HLS "live stream" (FFmpeg testsrc + sine served over
loopback HTTP), a synthetic chat feed with one spike, a deterministic speech recogniser (no model download), the
full app with every registered service, and ffprobe measurements. No network beyond 127.0.0.1."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # belt and braces: no model download from any test using this
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
SPEECH = ("honestly I think gta 6 is going to be the best game ever made and this new map looks absolutely "
          "massive compared to everything we have seen before")


class Stream:
    """FFmpeg publishes a live HLS stream (1 s segments) by HTTP PUT to a threaded loopback server that keeps the
    playlist and segments in memory and serves them. Nothing touches the disk, so there is no playlist rename
    that a reader or a virus scanner holding the file can make fail (Windows: FFmpeg's HLS muxer stops on it)."""

    def __init__(self, root: Path | None = None, size: str = "320x240"):
        self.root, self.size, self.port, self.proc, self.httpd = root, size, 0, None, None
        self.files: dict[str, bytes] = {}
        self.lock = threading.Lock()

    def up(self) -> Stream:
        self.files.clear()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), HLSHandler)
        self.httpd.daemon_threads = True
        self.httpd.stream = self
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", f"testsrc=size={self.size}:rate=25",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "aac",
             "-f", "hls", "-method", "PUT", "-hls_time", "1", "-hls_list_size", "6",
             "-hls_flags", "delete_segments+omit_endlist", f"http://127.0.0.1:{self.port}/live.m3u8"],
            stdin=subprocess.DEVNULL,
        )
        return self

    def ready(self) -> bool:
        with self.lock:
            return "live.m3u8" in self.files

    def wait_ready(self, timeout: float = 15) -> None:
        end = time.time() + timeout
        while not self.ready():
            if time.time() > end:
                raise AssertionError("the synthetic stream did not start")
            time.sleep(0.1)

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


class HLSHandler(http.server.BaseHTTPRequestHandler):
    """PUT/POST stores a file, DELETE drops it, GET serves it (FFmpeg's HLS muxer over HTTP, then any reader)."""

    def log_message(self, *a):
        pass

    def _name(self) -> str:
        return self.path.split("?", 1)[0].lstrip("/")

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() != "chunked":
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))
        out = bytearray()
        while True:
            size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while self.rfile.readline().strip():  # trailers up to the blank line
                    pass
                return bytes(out)
            out += self.rfile.read(size)
            self.rfile.readline()

    def _reply(self, code: int, body: bytes = b"", ctype: str = "") -> None:
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_PUT(self):
        body = self._body()
        st = self.server.stream
        with st.lock:
            st.files[self._name()] = body
        self._reply(201)

    do_POST = do_PUT

    def do_DELETE(self):
        self._body()
        st = self.server.stream
        with st.lock:
            st.files.pop(self._name(), None)
        self._reply(204)

    def do_GET(self):
        st = self.server.stream
        with st.lock:
            body = st.files.get(self._name())
        if body is None:
            return self._reply(404)
        ctype = "application/vnd.apple.mpegurl" if self._name().endswith(".m3u8") else "video/mp2t"
        self._reply(200, body, ctype)


def chat_rows(spike_at: float, quiet_until: float | None = None) -> list[dict]:
    """A quiet chat (one line every 5 s, at the detector's baseline floor) with one reaction burst at spike_at:
    12 chatters in 3 s. The detector's first crossing scores above the built-in auto-post threshold (3.0)."""
    quiet_until = spike_at - 3 if quiet_until is None else quiet_until
    rows = [{"t": float(t), "user": f"viewer{i % 4}", "text": "nice run"}
            for i, t in enumerate(range(0, int(quiet_until), 5))]
    words = ["no way", "lol", "wtf", "insane", "lmao", "no way", "omg", "clip it", "lol", "pog", "wtf", "insane"]
    rows += [{"t": spike_at + i * 0.25, "user": f"fan{i}", "text": w} for i, w in enumerate(words)]
    return sorted(rows, key=lambda r: r["t"])


class FakeTranscriber:
    """English speech naming the game (an opinion statement), with confident per-window language evidence."""

    def __init__(self, block: threading.Event | None = None, entered: threading.Event | None = None):
        self.block, self.entered = block, entered

    def transcribe(self, wav, language=None):
        if self.entered is not None:
            self.entered.set()
        if self.block is not None:
            self.block.wait()
        words = SPEECH.split()
        step = 10.0 / len(words)
        seg = {"start": 2.0, "end": 12.0, "text": SPEECH,
               "words": [{"start": round(2 + i * step, 3), "end": round(2 + (i + 1) * step, 3), "word": " " + w}
                         for i, w in enumerate(words)]}
        return {"language": "en", "language_probability": 0.99, "language_share_en_es": 0.99, "top_language": "en",
                "top_language_probability": 0.99, "speech_s": 10.0, "no_speech_prob": 0.01,
                "speech_status": "speech", "speech_note": "", "duration": 30.0, "segments": [seg],
                "timing": {"load_s": 0.0, "transcribe_s": 0.01}}

    def window_languages(self, wav, segments):
        return {"windows": [{"start": 0.0, "end": 12.0, "lang": "en", "prob": 0.98}]}


class FakeTranslator:
    glossary = None

    def translate_segments(self, segments, src, tgt):
        return [{**s, "text": f"[{tgt}] {s['text']}", "words": []} for s in segments]


def probe(path) -> dict:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True, timeout=60).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    return {"width": v["width"], "height": v["height"], "duration": float(d["format"]["duration"])}


def x264_crf(path) -> float | None:
    """The CRF recorded in the x264 SEI options string of an encoded file."""
    m = re.search(rb"crf=(\d+(?:\.\d+)?)", Path(path).read_bytes()[:400_000])
    return float(m.group(1)) if m else None


def service(ctx, name: str):
    return next(s for s in ctx.services if getattr(s, "name", "") == name)


def configure(ctx, mode: str = "auto") -> None:
    """Settings for a run on the synthetic stream: FFmpeg from PATH, 1 s capture segments, the labelled test
    destination (nothing leaves the machine), the posting mode."""
    s = ctx.settings
    s.set("ffmpeg_path", FFMPEG)
    s.set("monitor.segment_seconds", 1.0)
    s.set("monitor.topic_observe_seconds", 0)
    s.set("publish.test_mode", True)
    s.set("publish.mode", mode)


def add_channel(ctx, client, stream: Stream, name: str, spike_at: float, *, format_id: str | None = None,
                lead: float = 3.0, rows: list | None = None) -> tuple[int, float]:
    """Add and arm a feed channel whose chat replays a spike `spike_at` s after the chat starts. Returns
    (channel id, chat start time)."""
    body = {"url": stream.url, "display_name": name, "enabled": False}
    if format_id:
        body["format_id"] = format_id
    r = client.post("/api/channels", json=body)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    t0 = time.time() + lead  # capture gets a head start over the chat
    store = service(ctx, "monitor").store
    store.merge_json("channels", cid, "settings", {
        "chat_adapter": "replay", "chat_options": {"rows": rows or chat_rows(spike_at), "start_at": t0}})
    assert client.post(f"/api/channels/{cid}/arm").status_code == 200
    return cid, t0


def install_fake_asr(ctx, transcriber: FakeTranscriber | None = None) -> None:
    service(ctx, "edit").engine.captioner = (transcriber or FakeTranscriber(), FakeTranslator())


def wait(cond, timeout: float, what: str, every: float = 0.25):
    end = time.time() + timeout
    while time.time() < end:
        with contextlib.suppress(Exception):
            v = cond()
            if v:
                return v
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what() if callable(what) else what}")


def published(client, store, n: int = 1, timeout: float = 240) -> list[dict]:
    """Wait until n deliveries are published; fail fast when a clip fails or captions break."""
    end = time.time() + timeout
    while time.time() < end:
        bad = [(c["id"], c["status"], c["error"]) for c in store.q("SELECT id, status, error FROM clips")
               if c["status"] == "failed" or c["error"]]
        assert not bad, bad
        done = [d for d in deliveries(client) if d["status"] == "published"]
        if len(done) >= n:
            return done
        time.sleep(0.5)
    raise AssertionError(f"timed out: {[(c['id'], c['status']) for c in store.q('SELECT * FROM clips')]} "
                         f"deliveries {[(d['clip_id'], d['status']) for d in deliveries(client)]}")


def deliveries(client) -> list[dict]:
    return client.get("/api/publish/deliveries").json()["deliveries"]


def sandbox_videos(root: Path) -> int:
    p = root / "sandbox" / "state.json"
    return len(json.loads(p.read_text())["videos"]) if p.exists() else 0


@contextlib.contextmanager
def running_app(mode: str = "auto", events: list | None = None, transcriber: FakeTranscriber | None = None):
    """The worker's app with every registered service (as `live_clipper.worker.run` builds it), served
    in-process; yields (ctx, client)."""
    import sqlite3

    from fastapi.testclient import TestClient

    from live_clipper import registry
    from live_clipper.paths import get_paths
    from live_clipper.server import create_app, ensure_token
    from live_clipper.services import build_context, load_services

    services = load_services(registry.SERVICES)  # imports register their migrations
    paths = get_paths()
    for attempt in range(50):  # Windows releases a killed process's file handles late
        try:
            ctx = build_context(paths, port=8765, token=ensure_token(paths, regenerate=True))
            break
        except sqlite3.OperationalError:
            if attempt == 49:
                raise
            time.sleep(0.2)
    if events is not None:
        ctx.bus.subscribe("*", lambda t, p: events.append((t, p)))
    configure(ctx, mode)
    ctx.services = services
    edit = service(ctx, "edit")
    edit_start = edit.start

    async def start_with_fake_asr(c):  # before the monitor starts: a resumed clip must never load a real model
        await edit_start(c)
        install_fake_asr(c, transcriber)

    edit.start = start_with_fake_asr
    headers = {"Authorization": f"Bearer {ctx.token}"}
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8765", headers=headers) as client:
        yield ctx, client
