"""Media timeline mapping, gaps and the buffer disk ceiling / sweeper (pure, no ffmpeg). Ported from the original
design's timeline, recovery, maintenance and transient-ring tests."""
import os
import threading
import time

import pytest

from live_clipper.buffer import RING_MARKER, BufferManager
from live_clipper.timeline import SegmentIndex

T = 1_000_000.0


def feed(idx, pts_from, n, recv_from, seg=2.0, lat=0.3):
    for i in range(n):
        p0 = pts_from + i * seg
        idx.add(f"/x/{p0}", p0, p0 + seg, recv_from + i * seg + seg + lat)


def test_offset_is_min_receipt_latency():
    idx = SegmentIndex()
    feed(idx, 0, 5, T, lat=1.0)
    idx.add("/x/burst", 10, 12, T + 12 + 0.2)          # faster delivery improves the estimate
    s = idx.all_segments()
    assert abs(s[0].t_start - (T + 0.2)) < 1e-6 and len(idx.epochs) == 1


def test_pts_jump_starts_new_epoch_and_is_never_joined():
    idx = SegmentIndex()
    feed(idx, 0, 10, T)
    feed(idx, 500, 10, T + 20)                          # discontinuity
    assert len(idx.epochs) == 2
    w = idx.resolve(T + 10, T + 30)
    assert all(s.epoch == w.epoch for s in w.segments)


def test_hidden_stall_with_continuous_pts_starts_new_epoch():
    # ffmpeg smoothed a 30 s outage: PTS continues at 20 s but the media arrives 30 s late
    idx = SegmentIndex()
    feed(idx, 0, 10, T)
    feed(idx, 20, 10, T + 20 + 30)
    assert len(idx.epochs) == 2 and "receipt gap" in idx.epochs[1].reason
    late = idx.epochs[1].segments[0]
    assert abs(late.t_start - (T + 50 + 0.3)) < 0.5     # mapped to when it really aired for us


def test_resolve_prefers_run_containing_action():
    idx = SegmentIndex()
    feed(idx, 0, 6, T)                                   # old epoch covers T..T+12
    feed(idx, 900, 6, T + 15)                            # restarted capture covers T+15..T+27
    w = idx.resolve(T, T + 26, prefer=T + 20)
    assert w.epoch == 1 and w.truncated_reason == "gap_before"
    w2 = idx.resolve(T, T + 26)                          # without preference: most coverage
    assert w2.epoch in (0, 1)


def test_disk_ceiling_deletes_oldest_unpinned_first(tmp_path):
    b = BufferManager(tmp_path / "buf", buffer_seconds=1e9, ceiling_bytes=5000)
    b.startup_sweep()
    idx = b.index(1)
    d = b.channel_dir(1)
    for i in range(10):
        p = d / f"s{i}.ts"
        p.write_bytes(b"x" * 1000)
        idx.add(str(p), i * 2.0, i * 2.0 + 2, T + i * 2 + 2.3, size=1000)
    pinned = idx.all_segments()[1]
    b.pin(1, 99, [pinned])
    b.enforce(T + 30)
    left = [os.path.basename(s.path) for s in idx.all_segments()]
    assert b.stats["bytes"] <= 5000 and "s1.ts" in left and "s0.ts" not in left
    b.release(1, 99)


# ------------------------------------------------------------------ retention off the event loop
def seg_files(b, n, size=1000, base=1000.0):
    idx, d = b.index(1), b.channel_dir(1)
    for i in range(n):
        p = d / f"s{i}.ts"
        p.write_bytes(b"x" * size)
        idx.add(str(p), i * 2.0, i * 2.0 + 2, base + i * 2.0, size=size)
    return idx, d


def test_tick_enforce_never_walks_the_tree_when_the_sweeper_runs(tmp_path, monkeypatch):
    b = BufferManager(tmp_path / "buf", buffer_seconds=1e9, ceiling_bytes=5000)
    b.startup_sweep()
    idx, d = seg_files(b, 10)
    b.start_sweeper(interval=3600)
    try:
        monkeypatch.setattr("live_clipper.buffer.os.walk",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("walk")))
        b.enforce(1100)                                     # ceiling from in-memory sizes, no directory walk
        assert sum(sg.size for sg in idx.all_segments()) <= 5000 and b.stats["bytes"] <= 5000
    finally:
        monkeypatch.undo()
        b.stop_sweeper()


def test_sweeper_bounds_unindexed_segments_while_the_loop_is_blocked(tmp_path):
    b = BufferManager(tmp_path / "buf", buffer_seconds=30, ceiling_bytes=3000)
    b.startup_sweep()
    b.force_release_ok = lambda: False                     # as in the engine unless host disk is critical
    idx, d = seg_files(b, 2, base=time.time())
    b.pin(1, 9, idx.all_segments())                        # an active cut: must survive
    old = time.time() - 600
    for i in range(6):                                     # written by ffmpeg but never indexed (loop stalled)
        p = d / f"orphan{i}.ts"
        p.write_bytes(b"y" * 1000)
        os.utime(p, (old + i, old + i))
    fresh = d / "writing.ts"                               # the segment ffmpeg is writing right now
    fresh.write_bytes(b"z" * 5000)
    b.start_sweeper(interval=0.05)
    try:
        time.sleep(0.5)                                    # the event loop is "blocked": nobody calls enforce()
        left = sorted(os.listdir(d))
        assert not [n for n in left if n.startswith("orphan")]
        assert "s0.ts" in left and "s1.ts" in left and "writing.ts" in left
        assert b.stats.get("swept_at")
    finally:
        b.stop_sweeper()


def test_sweep_ceiling_deletes_oldest_unindexed_but_not_recent_or_pinned(tmp_path):
    b = BufferManager(tmp_path / "buf", buffer_seconds=1e9, ceiling_bytes=4000)
    b.startup_sweep()
    b.force_release_ok = lambda: False
    now = time.time()
    idx, d = seg_files(b, 2, base=now)
    b.pin(1, 9, idx.all_segments())
    for i in range(5):
        p = d / f"u{i}.ts"
        p.write_bytes(b"u" * 1000)
        os.utime(p, (now - 60 + i, now - 60 + i))
    (d / "w.ts").write_bytes(b"w" * 1000)                  # mtime now: maybe still being written
    b.sweep(now)
    left = sorted(os.listdir(d))
    assert "s0.ts" in left and "s1.ts" in left and "w.ts" in left
    assert "u0.ts" not in left and b.stats["bytes"] <= 4000


def test_segment_registered_and_pinned_mid_sweep_is_never_deleted(tmp_path):
    """Pause the sweep between its directory walk and deletion, register + pin the 'orphan', then resume."""
    b = BufferManager(tmp_path / "buf", buffer_seconds=30, ceiling_bytes=10**9)
    b.startup_sweep()
    idx, d = b.index(1), b.channel_dir(1)
    p = d / "late.ts"
    p.write_bytes(b"x" * 1000)
    old = time.time() - 600
    os.utime(p, (old, old))                                 # stale and unindexed when the walk sees it
    walked, resume = threading.Event(), threading.Event()
    b._sweep_hook = lambda: (walked.set(), resume.wait(5))
    t = threading.Thread(target=b.sweep)
    t.start()
    assert walked.wait(5)
    seg = idx.add(str(p), 0.0, 2.0, time.time(), size=1000, require_file=True)   # the loop catches up
    b.pin(1, 77, [seg])
    resume.set()
    t.join(5)
    assert p.exists() and idx.has_path(str(p)) and seg.pins == 1


def test_orphan_deleted_first_cannot_be_adopted_afterwards(tmp_path):
    b = BufferManager(tmp_path / "buf", buffer_seconds=30, ceiling_bytes=10**9)
    b.startup_sweep()
    idx, d = b.index(1), b.channel_dir(1)
    p = d / "gone.ts"
    p.write_bytes(b"x")
    os.utime(p, (time.time() - 600,) * 2)
    b.sweep()
    assert not p.exists()
    with pytest.raises(FileNotFoundError):                  # capture skips it instead of indexing a missing file
        idx.add(str(p), 0.0, 2.0, time.time(), size=1, require_file=True)
    assert not idx.all_segments()


def test_sweep_does_no_disk_io_or_index_rebuild_under_the_shared_lock(tmp_path, monkeypatch):
    b = BufferManager(tmp_path / "buf", buffer_seconds=1e9, ceiling_bytes=1500)
    b.startup_sweep()
    b.force_release_ok = lambda: False
    idx, d = seg_files(b, 3, base=time.time() - 100)
    for i in range(20):
        q = d / f"o{i}.ts"
        q.write_bytes(b"o" * 100)
        os.utime(q, (time.time() - 50 + i,) * 2)
    real_unlink = os.unlink
    under_lock = []

    def unlink(path, *a, **k):
        under_lock.append(b._lock._is_owned())
        return real_unlink(path, *a, **k)
    monkeypatch.setattr("live_clipper.buffer.os.unlink", unlink)
    monkeypatch.setattr("live_clipper.timeline.os.unlink", unlink)
    b.sweep()
    assert under_lock and not any(under_lock)              # every unlink happened outside the shared lock
    assert b.stats["bytes"] <= 1500


def test_buffer_never_force_releases_active_cut_pins_unless_disk_is_critical(tmp_path):
    b = BufferManager(tmp_path / "buf", buffer_seconds=1e9, ceiling_bytes=2500)
    b.startup_sweep()
    b.force_release_ok = lambda: False
    idx, d = b.index(1), b.channel_dir(1)
    for i in range(5):
        p = d / f"s{i}.ts"
        p.write_bytes(b"x" * 1000)
        idx.add(str(p), i * 2.0, i * 2.0 + 2, 1000 + i * 2.0, size=1000)
    b.pin(1, 7, idx.all_segments()[:4])                                  # active cut holds 4 kB > ceiling
    b.enforce(1100)
    assert len(idx.all_segments()) == 4 and b.stats["forced_releases"] == 0 and b.stats["pinned_over_ceiling"] == 1
    b.force_release_ok = lambda: True                                    # host disk critical: last resort
    b.enforce(1101)
    assert b.stats["forced_releases"] >= 1 and b.stats["bytes"] <= 2500


def test_sweeps_keep_the_marker_and_never_follow_links(tmp_path):
    ring = tmp_path / "ring"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious")
    b = BufferManager(ring, 90, 10 * 1024 * 1024)
    b.marker_text = "owned by live clipper"
    b.startup_sweep()
    (ring / "3").mkdir()
    (ring / "3" / "old.ts").write_bytes(b"x" * 100)
    (ring / "stray.ts").write_bytes(b"x")
    try:
        (ring / "link").symlink_to(outside, target_is_directory=True)
    except OSError:                                                     # Windows without symlink rights
        pass
    b.startup_sweep()
    assert sorted(p.name for p in ring.iterdir()) == [RING_MARKER] and (outside / "keep.txt").exists()
    os.utime(ring / RING_MARKER, (1, 1))                                # very old: still never an orphan
    (ring / "5").mkdir()
    (ring / "5" / "orphan.ts").write_bytes(b"x")
    os.utime(ring / "5" / "orphan.ts", (1, 1))
    b.sweep()
    assert (ring / RING_MARKER).exists() and not (ring / "5" / "orphan.ts").exists()
