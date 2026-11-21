"""Supervisor-style crash: the worker process is killed in the middle of a clip's post-cut work; the restarted
app re-arms monitoring, resumes the clip, publishes it once, and neither a repeated trigger nor another restart
duplicates the clip or the publication to the test destination."""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from e2e_util import (
    FFMPEG,
    FFPROBE,
    Stream,
    deliveries,
    published,
    running_app,
    sandbox_videos,
    service,
    wait,
)

from live_clipper.db import Database
from live_clipper.events import Trigger
from live_clipper.paths import get_paths
from live_clipper.store import Store

pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH")


def read_line(proc, timeout: float) -> dict:
    q: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: q.put(proc.stdout.readline()), daemon=True).start()
    try:
        line = q.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError("the worker never reached the post-cut work") from None
    assert line, "the worker exited early"
    return json.loads(line)


def test_killed_worker_mid_pipeline_resumes_without_duplicates(home, tmp_path):
    stream = Stream(tmp_path / "hls").up()
    try:
        stream.wait_ready()
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("e2e_child.py")), stream.url],
                                 stdout=subprocess.PIPE, text=True,
                                 env={**os.environ, "LIVE_CLIPPER_HOME": str(home)})
        try:
            info = read_line(child, 150)
        finally:
            child.kill()  # crash: no graceful stop, no cleanup
            child.wait(30)
        cid, ((clip_id, status),) = info["channel"], info["clips"]
        assert status == "captioning"  # cut and preview done, captions in progress when killed

        # a restarted platform chat does not replay old messages: the replay feed restarts empty
        for attempt in range(50):  # Windows releases a killed process's file handles late
            try:
                Store(Database(get_paths().db_file)).merge_json("channels", cid, "settings",
                                                                {"chat_options": {"rows": [{"t": 3600, "user": "viewer0", "text": "later"}]}})
                break
            except sqlite3.OperationalError:
                if attempt == 49:
                    raise
                time.sleep(0.2)

        with running_app("auto") as (ctx, client):
            mon = service(ctx, "monitor")
            e = mon.engine
            assert cid in e.monitors and e.store.channel(cid)["armed"] == 1  # monitoring re-armed
            done = published(client, e.store)
            c = e.store.clip(clip_id)
            assert c["status"] == "ready" and (c["files"].get("final") or {}).get("en")
            assert len(done) == 1 and done[0]["clip_id"] == clip_id
            wait(lambda: e.monitors[cid].capture.health.state == "live", 120,
                 lambda: f"capture to record again: {e.monitors[cid].status()['capture']}")
            same = Trigger(channel_id=cid, reason="same moment again", kind="chat_spike",
                           burst_start=info["action_time"] + 4, action_time=info["action_time"], delay=4.0,
                           detected_at=time.time())
            assert e.on_trigger(e.monitors[cid], same) is None  # merged into the existing moment
            assert len(e.store.q("SELECT id FROM clips")) == 1
            time.sleep(1.0)
            assert len(deliveries(client)) == 1 and sandbox_videos(ctx.paths.root) == 1

        with running_app("auto") as (ctx, client):  # a second (graceful) restart changes nothing
            e = service(ctx, "monitor").engine
            time.sleep(3.0)
            assert len(e.store.q("SELECT id FROM clips")) == 1 and e.store.clip(clip_id)["status"] == "ready"
            ds = deliveries(client)
            assert len(ds) == 1 and ds[0]["status"] == "published" and sandbox_videos(ctx.paths.root) == 1
    finally:
        stream.down()
