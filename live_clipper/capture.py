"""Live media capture: (streamlink | ) ffmpeg -c copy -> short MPEG-TS segments + CSV index.

Each (re)start of the capture pipeline is a new run directory and a new timeline epoch.
The capture never re-encodes, so it stays cheap and keeps running while clips render.
"""
from __future__ import annotations

import asyncio
import errno
import importlib.util
import logging
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:                                   # owned by the edit subsystem; only the reference API is used
    from .buffer import BufferManager

log = logging.getLogger(__name__)


def _streamlink_cmd() -> list[str]:
    """Streamlink as a module of this interpreter when it is installed here (the usual case), else from PATH.
    Never the `streamlink.exe` launcher pip writes next to the interpreter: it is unsigned, and Windows
    application control has refused to start it while the signed interpreter itself runs."""
    if importlib.util.find_spec("streamlink"):
        return [sys.executable, "-m", "streamlink"]
    return [shutil.which("streamlink") or "streamlink"]


def _reader_command(command: list[str]) -> list[str]:
    """Bind POSIX capture readers to the worker without a threaded preexec_fn (see _capture_exec)."""
    if sys.platform == "win32":
        return command                              # a Job Object instead (_spawn_reader)
    executable = shutil.which(command[0])
    if executable is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), command[0])
    launcher = Path(__file__).with_name("_capture_exec.py").resolve()
    mode = [] if sys.platform == "linux" else ["--watch"]  # Linux: parent-death signal; else a watchdog
    return [sys.executable, "-I", "-S", str(launcher), *mode, str(os.getpid()), executable, *command[1:]]


async def _spawn_reader(command: list[str], **kw) -> asyncio.subprocess.Process:
    """Start a capture reader (streamlink or FFmpeg) that cannot outlive this worker."""
    if sys.platform != "win32":
        return await asyncio.create_subprocess_exec(*_reader_command(command), **kw)
    from ._capture_exec import CREATE_SUSPENDED, bind_to_kill_job

    p = await asyncio.create_subprocess_exec(*command, creationflags=CREATE_SUSPENDED, **kw)
    try:
        bind_to_kill_job(p.pid)                     # resumes the reader in every case
    except Exception as e:                          # best effort: never fail a capture over it
        log.warning("capture reader %s is not tied to this worker: %s", getattr(p, "pid", "?"), e)
    return p


STREAMLINK_PLATFORMS = {"twitch": "https://www.twitch.tv/{name}",
                        "kick": "https://kick.com/{name}",
                        "youtube": "https://www.youtube.com/{name}/live"}


def source_command(platform: str, name: str, source_url: str, *, realtime_file: bool = False) -> tuple[list[str], list[str]]:
    """Return (producer_cmd, ffmpeg_input_args). producer_cmd empty => ffmpeg reads the URL itself.

    platform twitch/kick/youtube with no explicit source_url => streamlink on the channel page.
    source_url starting with streamlink:// or hls:// etc. => streamlink with that URL.
    Anything else (rtmp://, srt://, http(s)://...m3u8, file path) => creator-provided feed, ffmpeg direct.
    """
    url = source_url.strip()
    if not url and platform == "rumble":
        raise ValueError("Rumble has no supported public media path (no Streamlink plugin; rumble.com blocks cloud "
                         "requests). Set a creator-provided feed URL (RTMP/SRT/HLS) as the source URL.")
    if not url and platform in STREAMLINK_PLATFORMS:
        url = "streamlink:" + STREAMLINK_PLATFORMS[platform].format(name=name.lstrip("@") if platform != "youtube" else name if name.startswith("@") or name.startswith("channel/") else "@" + name)
    if url.startswith("streamlink:"):
        target = url[len("streamlink:"):]
        prod = [*_streamlink_cmd(), "--stdout", "--loglevel", "warning", "--hls-live-edge", "2",
                "--stream-segment-threads", "2", "--retry-open", "1", target, "best"]
        return prod, ["-i", "pipe:0"]
    inp = []
    if realtime_file or ("://" not in url and os.path.exists(url)):
        inp += ["-re"]
    if url.startswith("rtmp://") and "listen" in url:
        inp += ["-listen", "1"]
    return [], inp + ["-i", url]


@dataclass
class CaptureHealth:
    state: str = "stopped"          # stopped|starting|waiting_for_live|live|reconnecting|error
    detail: str = ""
    last_segment_at: float | None = None
    source_available_at: float | None = None
    restarts: int = 0
    action_needed: str = ""


class Capture:
    def __init__(self, channel_id: int, platform: str, name: str, source_url: str,
                 buffers: BufferManager, segment_seconds: float = 2.0,
                 on_segment: Callable | None = None, log: Callable[[str, str], None] = lambda m, l="info": None,
                 realtime_file: bool = False, ffmpeg: str = "ffmpeg"):
        self.channel_id, self.platform, self.name, self.source_url = channel_id, platform, name, source_url
        self.buffers = buffers
        self.index = buffers.index(channel_id)
        self.segment_seconds = segment_seconds
        self.on_segment = on_segment
        self.log = log
        self.realtime_file = realtime_file
        self.ffmpeg = ffmpeg                        # setting `ffmpeg_path`, else "ffmpeg" from PATH
        self.health = CaptureHealth()
        self._task: asyncio.Task | None = None
        self._procs: list[asyncio.subprocess.Process] = []
        self._stop = asyncio.Event()
        self._run = 0
        self._ended = False

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name=f"lc-capture-{self.channel_id}")

    async def stop(self) -> None:
        self._stop.set()
        await self._kill()
        if self._task:
            self._task.cancel()
            # bounded: a cancelled caller still gets its CancelledError from wait(); a stuck task can't hang stop
            done, _ = await asyncio.wait([self._task], timeout=10)
            if self._task in done and not self._task.cancelled():
                self._task.exception()                # retrieve it so asyncio doesn't warn; already logged by run
        self.health.state = "stopped"

    async def interrupt(self) -> None:
        """Kill the pipeline as if the network dropped (used by tests and 'reconnect' button)."""
        await self._kill()

    async def _kill(self) -> None:
        for p in self._procs:
            if p.returncode is None:
                try:
                    p.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for p in self._procs:
            try:
                await asyncio.wait_for(p.wait(), 3)
            except TimeoutError:
                p.kill()
                await p.wait()

    async def _youtube_bot_check(self) -> bool:
        """True when YouTube reports the channel live but blocks this host with a sign-in/bot check."""
        try:
            import httpx
            name = self.name if self.name.startswith(("@", "channel/")) else "@" + self.name
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as c:
                r = await c.get(f"https://www.youtube.com/{name}/live")
            t = r.text
            return '"isLive":true' in t and "LOGIN_REQUIRED" in t
        except Exception:
            return False

    async def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            got = await self._run_once()
            if self._stop.is_set():
                break
            if self.realtime_file and got and self._ended:
                self.health.state, self.health.detail = "stopped", "source file ended"
                break
            self.health.restarts += 1
            if got:
                backoff = 1.0
                self.health.state = "reconnecting"
            else:
                self.health.state = "waiting_for_live" if self.health.state in ("waiting_for_live", "starting", "reconnecting") else "error"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _run_once(self) -> bool:
        while True:                                 # never a folder an earlier worker wrote: its index is stale
            self._run += 1
            run_dir = self.buffers.channel_dir(self.channel_id) / f"run{self._run:04d}"
            try:
                run_dir.mkdir(parents=True)
                break
            except FileExistsError:
                continue
        csv = run_dir / "segments.csv"
        try:
            prod, inp = source_command(self.platform, self.name, self.source_url, realtime_file=self.realtime_file)
        except ValueError as e:
            self.health.state, self.health.detail, self.health.action_needed = "error", str(e), str(e).split(". ")[-1]
            return False
        ff = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-fflags", "+genpts",
              *inp, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
              "-f", "segment", "-segment_time", str(self.segment_seconds), "-segment_format", "mpegts",
              "-segment_list", str(csv), "-segment_list_type", "csv", "-segment_list_flags", "live",
              "-reset_timestamps", "0", str(run_dir / "s%06d.ts")]
        self.health.state = "starting" if self.health.last_segment_at is None else "reconnecting"
        self._procs = []
        stderr_tail: list[str] = []
        try:
            if prod:
                r, w = os.pipe()
                try:
                    p1 = await _spawn_reader(prod, stdout=w, stderr=asyncio.subprocess.PIPE)
                    self._procs = [p1]
                    p2 = await _spawn_reader(ff, stdin=r, stderr=asyncio.subprocess.PIPE)
                    self._procs = [p1, p2]
                finally:
                    os.close(r); os.close(w)
            else:
                p2 = await _spawn_reader(ff, stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                self._procs = [p2]
        except FileNotFoundError as e:
            await self._kill()                      # don't orphan streamlink if ffmpeg is missing
            self.health.state, self.health.detail = "error", f"missing binary: {e.filename}"
            self.health.action_needed = f"Install {e.filename} on the worker"
            return False
        except OSError as e:                        # the system refused to start a reader: say so and retry
            await self._kill()
            code = getattr(e, "winerror", None) or e.errno
            self.health.state, self.health.detail = "error", f"a capture reader could not start: {e.strerror} ({code})"
            self.health.action_needed = "Check that FFmpeg and Streamlink may run on this computer (on Windows: " \
                                        "Smart App Control or antivirus events)"
            self.log(self.health.detail, "error")
            return False
        except asyncio.CancelledError:
            await self._kill()
            raise
        epoch_started = False
        got = False
        pos = 0

        prod_tail: list[str] = []

        async def drain(proc, tail):
            async for line in proc.stderr:
                t = line.decode(errors="replace").strip()
                if t:
                    tail.append(t)
                    del tail[:-8]
        drains = [asyncio.create_task(drain(p, prod_tail if (prod and p is self._procs[0]) else stderr_tail))
                  for p in self._procs]
        exit_task = asyncio.create_task(p2.wait())
        try:
            while True:
                done = p2.returncode is not None
                if csv.exists():
                    with open(csv) as f:
                        f.seek(pos)
                        chunk = f.read()
                    lines = chunk.split("\n")
                    complete, rest = lines[:-1], lines[-1]
                    pos += len(chunk) - len(rest)
                    for line in complete:
                        parts = line.strip().split(",")
                        if len(parts) < 3:
                            continue
                        now = time.time()
                        path = str(run_dir / parts[0])
                        if not epoch_started:
                            self.index.new_epoch(now, "capture start" if self._run == 1 else "capture restart")
                            epoch_started = True
                            if self.health.source_available_at is None:
                                self.health.source_available_at = now
                        try:
                            size = os.path.getsize(path)
                            seg = self.index.add(path, float(parts[1]), float(parts[2]), now, size,
                                                 require_file=True)
                        except FileNotFoundError:
                            continue                # removed as a stale orphan before it could be adopted
                        got = True
                        self.health.state, self.health.detail, self.health.action_needed = "live", "", ""
                        self.health.last_segment_at = now
                        if self.on_segment:
                            self.on_segment(seg)
                if done:
                    break
                # Retain one subprocess waiter across all index polls in a long stream.
                await asyncio.wait((exit_task,), timeout=0.1)
        finally:
            try:
                await self._kill()
            finally:
                for task in [exit_task, *drains]:
                    task.cancel()
                await asyncio.gather(exit_task, *drains, return_exceptions=True)
        await asyncio.sleep(0)
        msg = " | ".join(prod_tail[-2:] or stderr_tail[-3:])
        if not got:
            low = msg.lower()
            if "no playable streams" in low or "offline" in low or "no streams found" in low or "404" in low:
                self.health.state, self.health.detail = "waiting_for_live", "channel offline; retrying"
                if self.platform == "youtube" and not self.source_url and await self._youtube_bot_check():
                    self.health.state = "error"
                    self.health.detail = "YouTube is live but refuses playback to this worker (\"confirm you're not a bot\")"
                    self.health.action_needed = ("Use a creator-provided feed (RTMP/SRT/HLS URL) for this channel, "
                                                 "or run the worker from a network YouTube allows")
            else:
                self.health.detail = msg[:300] or "capture exited before producing media"
        else:
            self._ended = p2.returncode == 0
            self.health.detail = f"capture exited ({p2.returncode}) {msg[:200]}".strip()
            self.log(f"capture run {self._run} ended: {self.health.detail}", "warn")
        return got
