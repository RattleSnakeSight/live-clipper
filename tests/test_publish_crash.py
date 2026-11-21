"""Kill a real worker process mid-upload, restart it, and prove the provider got exactly one video."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from publish_util import World, admin, make_clip, no_keyring, sb, videos  # noqa: F401

PROC = str(Path(__file__).with_name("publish_worker_proc.py"))


def spawn():
    p = subprocess.Popen([sys.executable, PROC], stdout=subprocess.PIPE, text=True, env=dict(os.environ))
    assert p.stdout.readline().strip() == "ready"
    return p


def wait_for(cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("fault, provider_stored", [("timeout_after_accept", True), ("timeout", False)])
def test_worker_killed_mid_upload_then_restart_no_duplicate(sb, fault, provider_stored):
    w = World(sb)
    w.dest({"timeout": 60.0, "backoff_base": 0.01})
    did = w.pub.enqueue_clip(make_clip(w.ctx))[0]
    admin(sb, "/_admin/inject", {"op": "upload", "fail": fault, "hang": 30})
    proc = spawn()
    try:
        # the request is in flight: the provider either stored the bytes or never will, the worker never heard back
        assert wait_for(lambda: w.row(did)["status"] == "uploading" and not sb.state["inject"])
        if provider_stored:
            assert wait_for(lambda: len(videos(sb)) == 1)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert w.row(did)["status"] == "uploading" and w.row(did)["reconcile"] == 1
    proc2 = spawn()
    try:
        assert wait_for(lambda: w.row(did)["status"] == "published")
    finally:
        proc2.kill()
        proc2.wait(timeout=10)
    d = w.row(did)
    hist = w.store.q1("SELECT history FROM publish_deliveries WHERE id=?", did)["history"]
    assert any(h["to"] == "uncertain" and "restarted" in h["detail"] for h in hist)
    assert len(videos(sb)) == 1                       # exactly one create call reached the provider's store
    assert d["attempts"] == (1 if provider_stored else 2)
