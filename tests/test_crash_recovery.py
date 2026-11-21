"""Worker crash (process killed) and restart: channels re-armed, choices intact, no duplicate clip."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from crash_child import CHANNELS, prepare

from live_clipper.events import Trigger
from live_clipper.monitor_service import MonitorService
from live_clipper.paths import get_paths
from live_clipper.services import build_context


def test_killed_worker_restarts_with_channels_rearmed_and_no_duplicate_clip(home):
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("crash_child.py"))],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "LIVE_CLIPPER_HOME": str(home)},
    )
    try:
        info = json.loads(child.stdout.readline())
    finally:
        child.kill()  # crash: no graceful stop, no cleanup
        child.wait(30)
    ids, clip_id = info["ids"], info["clip"]

    async def restart():
        for attempt in range(50):  # Windows releases a killed process's file handles late
            try:
                ctx = build_context(get_paths(), port=8765, token="test")
                break
            except sqlite3.OperationalError:
                if attempt == 49:
                    raise
                time.sleep(0.2)
        prepare(ctx)
        svc = MonitorService()
        await svc.start(ctx)
        e = svc.engine
        try:
            assert sorted(e.monitors) == sorted(ids)
            for cid, (name, gate, fmt, pubs) in zip(ids, CHANNELS, strict=True):
                ch = e.store.channel(cid)
                assert ch["armed"] == 1 and ch["display_name"] == name
                assert ch["settings"]["topic_gate"] == gate and e.monitors[cid].gated == gate["enabled"]
                assert ch["format_id"] == fmt and ch["publish_platforms"] == pubs
            for _ in range(100):  # housekeeping pumps the recovery queue
                if e.store.clip(clip_id)["status"] == "ready":
                    break
                await asyncio.sleep(0.05)
            assert e.store.clip(clip_id)["status"] == "ready"  # resumed, not failed
            same = Trigger(
                channel_id=ids[0],
                reason="Manual clip",
                kind="manual",
                burst_start=info["action_time"] + 3,
                action_time=info["action_time"],
                delay=3.0,
                detected_at=info["action_time"] + 3,
            )
            assert e.on_trigger(e.monitors[ids[0]], same) is None  # already-cut moment: dedupe key holds
            assert len(e.store.q("SELECT id FROM clips")) == 1
            views = [svc.channel_view(e.store.channel(c)) for c in ids]
            assert all(v["enabled"] and v["state"] != "disabled" for v in views)
        finally:
            await svc.stop()

    asyncio.run(restart())
