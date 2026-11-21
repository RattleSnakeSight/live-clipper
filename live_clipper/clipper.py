"""Streaming cut from the rolling buffer.

As soon as a moment is detected the encoder starts on the pre-roll that is already buffered,
and each new segment is fed to it the moment capture finishes writing it. When the segment
containing the clip end arrives, only that last bit still has to be encoded, so the playable
preview lands ~1 s after the source media exists. One ffmpeg process emits:
  preview.mp4   9:16 H.264/AAC, faststart (no burned captions; captions are separate tracks)
  audio.wav     16 kHz mono for speech recognition of exactly this window
  source.ts     the untouched source segments (lets trim/reframe/recut run without the buffer)
Footage is never joined across an epoch change or a timestamp gap: the clip is truncated and
the reason recorded.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .buffer import BufferManager
from .render import layout_filter
from .timeline import GAP_TOLERANCE, Segment, SegmentIndex


@dataclass
class CutResult:
    ok: bool
    start: float = 0.0             # timeline start actually covered
    end: float = 0.0
    source_t0: float = 0.0         # timeline time of source.ts start
    trim_in: float = 0.0           # offset of clip start inside source.ts
    duration: float = 0.0
    truncated: str = ""
    epoch: int = -1
    last_segment_received_at: float = 0.0
    first_feed_at: float = 0.0
    done_at: float = 0.0
    encode_tail_s: float = 0.0     # time from last needed segment to playable preview
    error: str = ""
    files: dict = field(default_factory=dict)
    layout: Optional[dict] = None   # the clip layout with its auto decision stored (None: unchanged)


async def _wait_for(pred: Callable[[], object], timeout: float, poll: float = 0.05):
    end = time.time() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.time() >= end:
            return None
        await asyncio.sleep(poll)


def _next_after(index: SegmentIndex, seg: Segment) -> Optional[Segment]:
    for s in index.all_segments():
        if s.seq > seg.seq:
            return s
    return None


async def cut_clip(buffers: BufferManager, channel_id: int, clip_id: int, start: float, end: float,
                   out_dir: Path, *, layout: dict | None = None, out_h: int = 1280,
                   preset: str = "veryfast", segment_timeout: float = 20.0, prefer: Optional[float] = None,
                   fps: int = 0, crf: int = 23,
                   on_event: Callable[[str, float], None] = lambda k, t: None) -> CutResult:
    index = buffers.index(channel_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. first segment: the one containing `start`, or the earliest buffered (partial buffer)
    def first_seg():
        w = index.resolve(start, end, prefer)
        return w.segments[0] if w else None
    seg = await _wait_for(first_seg, segment_timeout)
    if seg is None:
        return CutResult(False, error="no buffered media for the requested window")
    w = index.resolve(start, end, prefer)
    truncated = w.truncated_reason if w and w.truncated_reason in ("buffer_start", "gap_before") else ""
    buffers.pin(channel_id, clip_id, w.segments if w else [seg])

    clip_start = max(start, seg.t_start)
    duration = end - clip_start
    trim_in = clip_start - seg.t_start
    preview, wav, src = out_dir / "preview.mp4", out_dir / "audio.wav", out_dir / "source.ts"
    from . import facecam, render
    segs = w.segments if w else [seg]
    info = await asyncio.to_thread(facecam.probe, segs[0].path)
    resolved = None
    if isinstance(layout, dict) and layout.get("mode") == "auto" and not layout.get("resolved"):
        layout = resolved = render.preview_layout(layout)   # provisional; no frame probing on the ingest path
    graph = layout_filter(layout, out_h, fps=render.fps_cap(fps, (info or {}).get("fps")))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", "pipe:0",
           "-ss", f"{trim_in:.3f}", "-t", f"{duration:.3f}",
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", preset, "-crf", str(int(crf)), "-pix_fmt", "yuv420p", "-g", "60",
           "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart", str(preview),
           "-ss", f"{trim_in:.3f}", "-t", f"{duration:.3f}", "-map", "0:a:0?", "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    from .proc import LOW_PRIO, niced
    proc = await asyncio.create_subprocess_exec(*niced(cmd, 5), stdin=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, **LOW_PRIO)
    res = CutResult(True, start=clip_start, source_t0=seg.t_start, trim_in=trim_in, epoch=seg.epoch,
                    truncated=truncated, first_feed_at=time.time(), layout=resolved)
    on_event("cut_started", res.first_feed_at)
    on_event("trim_in", trim_in)
    on_event("clip_start", clip_start)
    last = None
    encoder_done = False
    try:
        with open(src, "wb") as srcf:
            cur: Optional[Segment] = seg
            while cur is not None:
                buffers.pin(channel_id, clip_id, [cur])
                try:
                    with open(cur.path, "rb") as f:
                        data = f.read()
                except FileNotFoundError:          # evicted under disk pressure: stop here, honestly
                    res.truncated = "segment_evicted"
                    break
                srcf.write(data)
                srcf.flush()
                last = cur
                on_event("fed_until", cur.t_end)
                if proc.returncode is None and not encoder_done:
                    try:
                        proc.stdin.write(data)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        encoder_done = True       # reached -t: it already has everything it needs
                if cur.t_end >= end - 0.02:
                    break
                prev = cur
                nxt = await _wait_for(lambda: _next_after(index, prev), segment_timeout)
                if nxt is None:
                    res.truncated = "stream_ended"
                    break
                if nxt.epoch != prev.epoch or abs(nxt.pts_start - prev.pts_end) > GAP_TOLERANCE:
                    res.truncated = "gap_after"       # never join across a gap/discontinuity
                    break
                cur = nxt
        if last is None:
            raise RuntimeError("first segment disappeared before it could be read")
        res.last_segment_received_at = last.received_at
        on_event("last_segment_available", last.received_at)
        try:
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        _, err = await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        buffers.release(channel_id, clip_id)
    res.done_at = time.time()
    res.end = min(end, last.t_end) if last else clip_start
    res.duration = res.end - clip_start
    res.encode_tail_s = res.done_at - res.last_segment_received_at
    if proc.returncode != 0 or not preview.exists():
        res.ok = False
        res.error = (err or b"").decode(errors="replace")[-400:]
        return res
    res.files = {"preview": preview.name, "audio": wav.name if wav.exists() and wav.stat().st_size > 1000 else "",
                 "source": src.name}
    return res
