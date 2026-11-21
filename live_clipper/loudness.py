"""Optional audio-loudness trigger (off by default, per channel) and media probes.

This is an AUDIO trigger, not chat: it fires when a buffered segment's mean loudness rises well above the channel's
recent audio baseline ("loudness spike"), e.g. a crash, gunfire or the streamer shouting. Rate is bounded by a
cooldown; triggers feed the normal Engine.on_trigger path (same windows, merging and admission as chat spikes).

Segments are measured after they land in the buffer with one short, niced `ffmpeg -af volumedetect` run each, never
on the event loop. The same measurement says whether a segment carries an audio stream at all.
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

DEFAULTS = {"enabled": False, "rise_db": 12.0, "cooldown_s": 45.0}
_MEAN = re.compile(rb"mean_volume:\s*(-?[\d.]+|-inf) dB")
_MAX = re.compile(rb"max_volume:\s*(-?[\d.]+|-inf) dB")


def clean_settings(v) -> dict:
    """Validated channel setting `audio_trigger`: {"enabled": bool, "rise_db": 6-30, "cooldown_s": 15-600}."""
    if not isinstance(v, dict):
        raise ValueError("audio_trigger must be an object")
    out = {"enabled": bool(v.get("enabled", False))}
    for k, lo, hi in (("rise_db", 6.0, 30.0), ("cooldown_s", 15.0, 600.0)):
        x = v.get(k, DEFAULTS[k])
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not lo <= float(x) <= hi:
            raise ValueError(f"audio_trigger.{k} must be a number {lo:g}-{hi:g}")
        out[k] = float(x)
    return out


def config_of(settings: dict | None) -> dict:
    return {**DEFAULTS, **((settings or {}).get("audio_trigger") or {})}


@dataclass
class Level:
    has_audio: bool
    mean_db: Optional[float] = None
    max_db: Optional[float] = None


def _db(m) -> Optional[float]:
    if not m:
        return None
    v = m.group(1)
    return -120.0 if v == b"-inf" else float(v)


async def _reap(p) -> None:
    if p.returncode is None:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        await asyncio.shield(p.wait())


async def measure(path: str) -> Level:
    """Mean/max loudness (dBFS) of a segment's first audio stream; has_audio False when it has none."""
    from .proc import LOW_PRIO, niced
    p = await asyncio.create_subprocess_exec(
        *niced(["ffmpeg", "-hide_banner", "-nostdin", "-i", path, "-map", "0:a:0?", "-vn", "-af", "volumedetect",
                "-f", "null", "-"], 10), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        **LOW_PRIO)
    try:
        _, err = await asyncio.wait_for(p.communicate(), 20)
    except asyncio.TimeoutError:
        await _reap(p)
        return Level(False)
    except BaseException:
        await _reap(p)
        raise
    mean = _db(_MEAN.search(err or b""))
    return Level(mean is not None, mean, _db(_MAX.search(err or b"")))


async def probe_playable(path: str) -> dict:
    """ffprobe a produced clip: playable = a video AND an audio stream, each with non-zero duration."""
    p = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,duration:format=duration",
        "-of", "json", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), 30)
    except asyncio.TimeoutError:
        await _reap(p)
        return {"playable": False, "detail": "ffprobe timed out"}
    except BaseException:
        await _reap(p)
        raise
    try:
        d = json.loads(out or b"{}")
    except ValueError:
        d = {}
    fmt = float((d.get("format") or {}).get("duration") or 0)
    dur = {}
    for s in d.get("streams") or []:
        t = s.get("codec_type")
        if t in ("video", "audio") and t not in dur:
            dur[t] = float(s.get("duration") or fmt or 0)
    ok = dur.get("video", 0) > 0 and dur.get("audio", 0) > 0
    why = "" if ok else ("no audio stream" if "audio" not in dur else "no video stream" if "video" not in dur
                         else "zero-duration stream") if d.get("streams") else (err or b"").decode(errors="replace")[:200]
    return {"playable": ok, "video_s": round(dur.get("video", 0), 2), "audio_s": round(dur.get("audio", 0), 2),
            "detail": why or "video + audio streams with non-zero duration"}


class LoudnessTrigger:
    """Per-channel loudness spike detector over consecutive segment levels.

    Fires when a segment's mean level >= max(FLOOR_DB, baseline + rise_db), where baseline is the median mean level
    of the previous BASELINE segments (spikes excluded), after WARMUP segments, at most once per cooldown."""
    FLOOR_DB = -35.0
    WARMUP = 10
    BASELINE = 60

    def __init__(self, channel_id: int, rise_db: float = DEFAULTS["rise_db"], cooldown_s: float = DEFAULTS["cooldown_s"]):
        self.channel_id = channel_id
        self.rise_db, self.cooldown_s = float(rise_db), float(cooldown_s)
        self.levels: deque[float] = deque(maxlen=self.BASELINE)
        self.last_fire: Optional[float] = None
        self.stats = {"segments": 0, "triggers": 0, "suppressed_by_cooldown": 0}
        self.last: dict = {}

    def baseline(self) -> Optional[float]:
        return statistics.median(self.levels) if len(self.levels) >= self.WARMUP else None

    def feed(self, t_start: float, t_end: float, mean_db: Optional[float], now: float | None = None):
        from .events import Trigger  # the shared Trigger lives with the detector (see docs/CONTRACTS.md)
        now = time.time() if now is None else now
        if mean_db is None:
            return None
        self.stats["segments"] += 1
        base = self.baseline()
        self.last = {"mean_db": round(mean_db, 1), "baseline_db": None if base is None else round(base, 1), "at": now}
        if base is None or mean_db < max(self.FLOOR_DB, base + self.rise_db):
            self.levels.append(mean_db)
            return None
        if self.last_fire is not None and now - self.last_fire < self.cooldown_s:
            self.stats["suppressed_by_cooldown"] += 1
            return None
        self.last_fire = now
        self.stats["triggers"] += 1
        mid = (t_start + t_end) / 2
        return Trigger(channel_id=self.channel_id, kind="audio_spike",
                       reason=(f"Audio loudness spike (audio trigger, not chat): {mean_db:.1f} dBFS, "
                               f"{mean_db - base:+.1f} dB over the {base:.1f} dBFS baseline"),
                       burst_start=t_start, action_time=mid, delay=0.0, score=0.0, peak_rate=round(mean_db - base, 1),
                       baseline=round(base, 1), detected_at=now,
                       chat_time_basis="no chat: audio level of the buffered segment (stream timeline)")

    def snapshot(self) -> dict:
        return {"rise_db": self.rise_db, "cooldown_s": self.cooldown_s, **self.stats, **self.last,
                "last_trigger_at": self.last_fire}
