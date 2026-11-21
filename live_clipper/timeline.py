"""Media time mapping and the segment index for one channel's rolling buffer.

Capture writes short MPEG-TS segments and an ffmpeg CSV segment list (name,start_pts,end_pts).
Each segment gets mapped onto the stream timeline (local wall clock when the worker "saw"
the media). Mapping per epoch:   timeline = pts + offset
  offset = min over segments of (receipt_wall - end_pts)
The minimum is the best estimate because receipt latency is never negative; it keeps the
mapping stable when segments arrive in bursts (HLS fetches several at once).

An *epoch* is a run of media with continuous timestamps. A new epoch starts when the capture
process restarts, or when PTS jumps (discontinuity) by more than GAP_TOLERANCE. Clips are
never assembled across epochs or across gaps.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Optional

GAP_TOLERANCE = 0.75       # seconds of PTS discontinuity tolerated inside an epoch
RECEIPT_GAP = 8.0          # media arriving this much later than its PTS predicts = hidden gap


@dataclass
class Segment:
    epoch: int
    seq: int                  # monotonically increasing per channel
    path: str
    pts_start: float
    pts_end: float
    received_at: float        # local wall clock when segment completed on disk
    size: int = 0
    pins: int = 0
    t_start: float = 0.0      # timeline time (filled by index)
    t_end: float = 0.0

    @property
    def duration(self) -> float:
        return self.pts_end - self.pts_start


@dataclass
class Epoch:
    id: int
    offset: Optional[float] = None
    started_at: float = 0.0
    reason: str = "start"
    segments: list[Segment] = field(default_factory=list)


@dataclass
class Window:
    """A resolved contiguous range of segments for a requested clip window."""
    epoch: int
    segments: list[Segment]
    start: float              # timeline start actually covered (clamped)
    end: float
    requested_start: float
    requested_end: float
    truncated_reason: str = ""   # "", "buffer_start", "gap_before", "gap_after", "stream_ended"

    @property
    def complete(self) -> bool:
        return bool(self.segments) and self.segments[-1].t_end >= self.requested_end - 0.05


class SegmentIndex:
    """Thread-safe index of buffered segments for one channel."""

    def __init__(self):
        self._lock = threading.RLock()
        self.epochs: list[Epoch] = []
        self._seq = 0
        self.last_pts_end: Optional[float] = None
        self._paths: set[str] = set()          # indexed file paths (O(1) adoption/orphan checks)

    # ---- ingestion -------------------------------------------------------
    def new_epoch(self, now: float, reason: str) -> Epoch:
        with self._lock:
            ep = Epoch(id=len(self.epochs), started_at=now, reason=reason)
            self.epochs.append(ep)
            self.last_pts_end = None
            return ep

    @property
    def current(self) -> Optional[Epoch]:
        return self.epochs[-1] if self.epochs else None

    def add(self, path: str, pts_start: float, pts_end: float, received_at: float, size: int = 0,
            require_file: bool = False) -> Segment:
        with self._lock:
            if require_file and not os.path.exists(path):
                # the buffer sweeper removed it as an orphan (under this lock) before it could be adopted
                raise FileNotFoundError(path)
            ep = self.current or self.new_epoch(received_at, "start")
            if self.last_pts_end is not None and abs(pts_start - self.last_pts_end) > GAP_TOLERANCE:
                ep = self.new_epoch(received_at, f"pts discontinuity {self.last_pts_end:.2f}->{pts_start:.2f}")
            cand = received_at - pts_end
            if ep.offset is not None and cand - ep.offset > RECEIPT_GAP:
                # ffmpeg/streamlink can smooth over a long stall or skipped segments (PTS stays
                # continuous): media arriving far later than predicted means footage is missing.
                ep = self.new_epoch(received_at, f"receipt gap {cand - ep.offset:.1f}s")
            if ep.offset is None or cand < ep.offset:
                ep.offset = cand
                for s in ep.segments:          # re-map earlier segments with the better estimate
                    s.t_start, s.t_end = s.pts_start + ep.offset, s.pts_end + ep.offset
            seg = Segment(ep.id, self._seq, path, pts_start, pts_end, received_at, size,
                          t_start=pts_start + ep.offset, t_end=pts_end + ep.offset)
            self._seq += 1
            ep.segments.append(seg)
            self._paths.add(path)
            self.last_pts_end = pts_end
            return seg

    def remove(self, seg: Segment) -> None:
        with self._lock:
            ep = self.epochs[seg.epoch]
            if seg in ep.segments:
                ep.segments.remove(seg)
                self._paths.discard(seg.path)

    def has_path(self, path: str) -> bool:
        with self._lock:
            return path in self._paths

    def unlink_if_unindexed(self, path: str) -> bool:
        """Atomic against add(require_file=True): delete the file only while no segment refers to it."""
        with self._lock:
            if path in self._paths:
                return False
            try:
                os.unlink(path)
                return True
            except FileNotFoundError:
                return False

    # ---- queries ---------------------------------------------------------
    def all_segments(self) -> list[Segment]:
        with self._lock:
            return [s for ep in self.epochs for s in ep.segments]

    def buffered_seconds(self) -> float:
        with self._lock:
            ep = self.current
            if not ep or not ep.segments:
                return 0.0
            return ep.segments[-1].t_end - ep.segments[0].t_start

    def live_edge(self) -> Optional[float]:
        with self._lock:
            ep = self.current
            return ep.segments[-1].t_end if ep and ep.segments else None

    def resolve(self, start: float, end: float, prefer: Optional[float] = None) -> Optional[Window]:
        """Choose the epoch/contiguous run for [start, end].

        A run containing `prefer` (the estimated action time) always wins; otherwise the run with
        the most coverage. Clamps to buffer start and to gaps. Segments not yet received are
        simply absent; callers wait and call again (see Window.complete).
        """
        with self._lock:
            best: Optional[Window] = None
            best_rank = (False, -1.0)
            for ep in self.epochs:
                runs: list[list[Segment]] = []
                for s in ep.segments:
                    if runs and abs(s.pts_start - runs[-1][-1].pts_end) <= GAP_TOLERANCE:
                        runs[-1].append(s)
                    else:
                        runs.append([s])
                for run in runs:
                    sel = [s for s in run if s.t_end > start and s.t_start < end]
                    if not sel:
                        continue
                    cov = min(end, sel[-1].t_end) - max(start, sel[0].t_start)
                    reason = ""
                    if sel[0].t_start > start + 0.05:
                        reason = "buffer_start" if sel[0] is run[0] and run is runs[0] and ep.id == 0 else "gap_before"
                    if sel[-1] is run[-1] and run is not runs[-1] and sel[-1].t_end < end - 0.05:
                        reason = "gap_after"
                    if sel[-1] is run[-1] and ep is not self.epochs[-1] and run is runs[-1] and sel[-1].t_end < end - 0.05:
                        reason = "gap_after"
                    w = Window(ep.id, sel, max(start, sel[0].t_start), min(end, sel[-1].t_end), start, end, reason)
                    has = prefer is not None and run[0].t_start <= prefer <= run[-1].t_end
                    rank = (has, cov)
                    if best is None or rank > best_rank:
                        best, best_rank = w, rank
            return best
