"""Bounded rolling buffer: retention by time, pin leases for active cuts, global disk ceiling.

Ground truth for disk usage is a directory scan, so files left by a crashed capture or a
segment still being written are counted too. On startup the whole buffer dir is cleared:
a buffer from a previous process has no valid time mapping and would only consume disk.

Two paths (production stall fix): enforce() runs on the event loop every tick and only touches memory (time
retention + ceiling from indexed segment sizes plus the orphan bytes of the last scan). The directory walk, orphan
deletion and the ceiling for unindexed files run in sweep(), on a dedicated thread (start_sweeper), so retention
keeps working even if the event loop is slow, and the loop never walks the tree. Capture processes keep writing
while the loop is busy; their not-yet-indexed segments are exactly what the sweeper bounds.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import RING_MARKER  # ownership marker inside an operator-placed transient ring (never swept)
from .timeline import Segment, SegmentIndex


@dataclass
class Lease:
    clip_id: int
    segments: list[Segment]
    expires_at: float


class BufferManager:
    def __init__(self, root: Path, buffer_seconds: float, ceiling_bytes: int,
                 log: Callable[[str, str], None] = lambda m, l="info": None):
        self.root = Path(root)
        self.buffer_seconds = buffer_seconds
        self.ceiling = ceiling_bytes
        self.log = log
        self.indexes: dict[int, SegmentIndex] = {}
        self.leases: dict[tuple[int, int], Lease] = {}     # (channel_id, clip_id)
        self._lock = threading.RLock()
        self.stats = {"bytes": 0, "files": 0, "deleted": 0, "forced_releases": 0, "peak_bytes": 0, "pinned_over_ceiling": 0}
        # whether pins of active cuts may be force-released to meet the ceiling; the engine allows it only when host
        # free space is critical (captures are stopped then), so active cuts are otherwise protected
        self.force_release_ok: Callable[[], bool] = lambda: True
        self._orphan_bytes = 0
        self.marker_text: str | None = None       # set for an operator-placed transient ring: its ownership marker
        self._sweeper: threading.Thread | None = None
        self._stop_sweeper = threading.Event()

    def startup_sweep(self) -> None:
        """Empty the ring at start. Only this directory's own entries are removed (a link is unlinked, never
        followed) and the ownership marker of a transient ring directory is kept."""
        self._ensure_root()
        for p in self.root.iterdir():
            if p.name == RING_MARKER:
                continue
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
            except OSError:
                pass

    def _ensure_root(self) -> None:
        """The ring directory exists (a cleaned temp volume is repaired) and, when it is an operator-placed
        transient ring, carries its ownership marker again."""
        if not self.root.is_dir():
            self.root.mkdir(parents=True, exist_ok=True)
            if self.marker_text:
                os.chmod(self.root, 0o700)
        if self.marker_text and not (self.root / RING_MARKER).exists():
            (self.root / RING_MARKER).write_text(self.marker_text, encoding="utf-8")

    def channel_dir(self, channel_id: int) -> Path:
        d = self.root / str(channel_id)
        if not d.is_dir():
            self._ensure_root()
            d.mkdir(parents=True, exist_ok=True)
        return d

    def index(self, channel_id: int) -> SegmentIndex:
        with self._lock:
            return self.indexes.setdefault(channel_id, SegmentIndex())

    # ---- pins ------------------------------------------------------------
    def pin(self, channel_id: int, clip_id: int, segments: list[Segment], ttl: float = 180.0) -> None:
        with self._lock:
            key = (channel_id, clip_id)
            old = self.leases.get(key)
            have = {id(s) for s in old.segments} if old else set()
            new = [s for s in segments if id(s) not in have]
            for s in new:
                s.pins += 1
            segs = (old.segments if old else []) + new
            self.leases[key] = Lease(clip_id, segs, time.time() + ttl)

    def release(self, channel_id: int, clip_id: int) -> None:
        with self._lock:
            lease = self.leases.pop((channel_id, clip_id), None)
            if lease:
                for s in lease.segments:
                    s.pins = max(0, s.pins - 1)

    def _expire_leases(self, now: float) -> None:
        for key, lease in list(self.leases.items()):
            if lease.expires_at < now:
                self.log(f"pin lease for clip {lease.clip_id} expired; releasing", "warn")
                self.release(*key)

    # ---- enforcement -----------------------------------------------------
    def _delete(self, idx: SegmentIndex, seg: Segment) -> None:
        idx.remove(seg)
        try:
            os.unlink(seg.path)
            self.stats["deleted"] += 1
        except FileNotFoundError:
            pass

    def scan_bytes(self) -> tuple[int, int]:
        total = files = 0
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                try:
                    total += os.stat(os.path.join(dirpath, n)).st_size
                    files += 1
                except FileNotFoundError:
                    pass
        return total, files

    def _indexed(self) -> list[tuple[int, Segment]]:
        with self._lock:
            items = list(self.indexes.items())
        return [(cid, sg) for cid, idx in items for sg in idx.all_segments()]

    def enforce(self, now: float | None = None, scan: bool | None = None) -> None:
        """Time retention + disk ceiling. scan=None walks the directory only when no sweeper thread does it."""
        now = now or time.time()
        if scan is None:
            scan = not self.sweeper_running
        with self._lock:
            self._expire_leases(now)
            # 1. time retention per channel (relative to that channel's live edge)
            for cid, idx in list(self.indexes.items()):
                edge = idx.live_edge()
                for s in idx.all_segments():
                    stale_epoch = idx.current is not None and s.epoch != idx.current.id and s.received_at < now - self.buffer_seconds
                    too_old = edge is not None and s.t_end < edge - self.buffer_seconds
                    if (too_old or stale_epoch) and s.pins == 0:
                        self._delete(idx, s)
        if scan:
            self.sweep(now)                       # walks the tree: orphans + ceiling for unindexed files
            return
        total = sum(sg.size or 0 for _, sg in self._indexed()) + self._orphan_bytes
        total = self._enforce_ceiling(total)
        self.stats.update(bytes=total, peak_bytes=max(self.stats["peak_bytes"], total))

    def _ceiling_victims(self, total: int) -> tuple[int, list[str]]:
        """Under self._lock: pick indexed segments to drop (oldest first; unpinned, then pinned only if allowed)
        and remove them from their index. Returns the new total and the files to unlink AFTER the lock is released,
        so bulk disk I/O never runs under the lock the event loop uses."""
        paths: list[str] = []
        if total <= self.ceiling:
            return total, paths
        segs = sorted(((sg.received_at, cid, sg) for cid, sg in self._indexed()), key=lambda t: t[0])
        passes = (False, True) if self.force_release_ok() else (False,)
        for forced in passes:
            for _, cid, sg in segs:
                if total <= self.ceiling:
                    break
                if sg.pins and not forced:
                    continue
                if sg.pins and forced:
                    self.stats["forced_releases"] += 1
                    for key, lease in list(self.leases.items()):
                        if sg in lease.segments:
                            self.log(f"disk ceiling: force-releasing pins of clip {lease.clip_id}", "error")
                            self.release(*key)
                idx = self.indexes.get(cid)
                if idx is not None and idx.has_path(sg.path):
                    idx.remove(sg)
                    total -= sg.size or 0
                    paths.append(sg.path)
        if total > self.ceiling:                  # only pinned segments of active cuts are left over the ceiling
            self.stats["pinned_over_ceiling"] += 1
        return total, paths

    def _unlink_all(self, paths: list[str]) -> None:
        for p in paths:
            try:
                os.unlink(p)
                self.stats["deleted"] += 1
            except FileNotFoundError:
                pass

    def _enforce_ceiling(self, total: int) -> int:
        with self._lock:
            total, paths = self._ceiling_victims(total)
        self._unlink_all(paths)
        return total

    def _index_for(self, path: str):
        """The index that could adopt this file: buffer/<channel id>/..."""
        try:
            cid = int(os.path.relpath(path, self.root).split(os.sep, 1)[0])
        except (ValueError, IndexError):
            return None
        with self._lock:
            return self.indexes.get(cid)

    def _drop_orphan(self, path: str) -> bool:
        """Delete an unindexed file atomically against adoption (SegmentIndex.add(require_file=True) takes the same
        per-channel lock): a segment registered (and possibly pinned) after the walk is never deleted."""
        idx = self._index_for(path)
        if idx is not None:
            ok = idx.unlink_if_unindexed(path)
        else:                                     # no index for that directory: nothing can adopt the file
            try:
                os.unlink(path)
                ok = True
            except FileNotFoundError:
                ok = False
        if ok:
            self.stats["deleted"] += 1
        return ok

    def sweep(self, now: float | None = None) -> None:
        """Directory walk (off the event loop): delete orphan files (not indexed) older than the retention window,
        bound unindexed bytes by the ceiling (oldest first, never a file still being written), measure real usage.
        No lock is held during the walk; each deletion is a short per-file check-and-unlink."""
        now = now or time.time()
        min_age = max(30.0, self.buffer_seconds)
        entries: list[tuple[str, float, int]] = []
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                if n.endswith(".csv") or n == RING_MARKER:
                    continue
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except FileNotFoundError:
                    continue
                entries.append((p, st.st_mtime, st.st_size))
        hook = getattr(self, "_sweep_hook", None)
        if hook:
            hook()                                 # tests: pause between the walk and any deletion
        total = 0
        orphans: list[tuple[float, int, str]] = []
        for p, mtime, size in entries:
            idx = self._index_for(p)
            if idx is not None and idx.has_path(p):
                total += size
                continue
            if mtime < now - min_age and self._drop_orphan(p):
                continue
            if idx is not None and idx.has_path(p):    # adopted meanwhile
                total += size
                continue
            orphans.append((mtime, size, p))
            total += size
        total = self._enforce_ceiling(total)
        # still over: unindexed segments (e.g. written while the loop could not index them), oldest first;
        # files modified in the last 10 s may still be open for writing and are left alone
        kept = 0
        for mtime, size, p in sorted(orphans):
            if total > self.ceiling and mtime <= now - 10 and self._drop_orphan(p):
                total -= size
            else:
                kept += size
        self._orphan_bytes = kept
        self.stats.update(bytes=total, files=len(entries), peak_bytes=max(self.stats["peak_bytes"], total),
                          swept_at=now)

    # ---- sweeper thread ----------------------------------------------------
    @property
    def sweeper_running(self) -> bool:
        return self._sweeper is not None and self._sweeper.is_alive()

    def start_sweeper(self, interval: float = 5.0) -> None:
        if self.sweeper_running:
            return
        self._stop_sweeper.clear()

        def run():
            last_err = 0.0
            while not self._stop_sweeper.wait(interval):
                try:
                    self.sweep()
                except Exception as e:            # never let the sweeper die; log at most once a minute
                    if time.time() - last_err > 60:
                        last_err = time.time()
                        self.log(f"buffer sweep error: {type(e).__name__}: {e}", "error")
        self._sweeper = threading.Thread(target=run, name="live-clipper-buffer-sweeper", daemon=True)
        self._sweeper.start()

    def stop_sweeper(self) -> None:
        self._stop_sweeper.set()
        if self._sweeper:
            self._sweeper.join(timeout=5)
        self._sweeper = None
