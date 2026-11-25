import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from live_clipper.db import Database
from live_clipper.paths import get_paths
from live_clipper.supervisor import Supervisor, acquire_lock, pid_alive, read_lock, release_lock

TINY = str(Path(__file__).parent / "tiny_worker.py")


def test_lock_is_released_although_stop_is_reading_it():
    """`live-clipper stop` polls the lock file; on Windows a delete during that read is refused."""
    paths = get_paths()
    assert acquire_lock(paths, 1)
    reader = open(paths.state / "supervisor.lock", encoding="utf-8")
    threading.Timer(0.3, reader.close).start()
    release_lock(paths)
    assert not (paths.state / "supervisor.lock").exists()
    assert read_lock(paths) is None


def wait_for(pred, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def run_supervisor(mode, **kw):
    sup = Supervisor(get_paths(), port=0, cmd=[sys.executable, TINY, mode], poll=0.05, **kw)
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    return sup, t


def restart_events():
    db = Database(get_paths().db_file)
    return [json.loads(r[0]) for r in db.execute("SELECT data_json FROM events WHERE source='supervisor'")]


def test_restarts_killed_worker_and_records_it():
    sup, t = run_supervisor("ok")
    try:
        first = wait_for(lambda: sup.proc)
        wait_for(lambda: (get_paths().state / "heartbeat").exists())
        first.kill()  # TerminateProcess on Windows, SIGKILL on POSIX
        second = wait_for(lambda: sup.proc is not first and sup.proc and sup.proc.poll() is None and sup.proc)
        assert second.pid != first.pid
        assert sup.restarts == 1
        events = restart_events()
        assert events and events[0]["reason"] == "exited"
    finally:
        sup.stop()
        t.join(30)
    assert not t.is_alive()
    assert second.poll() is not None  # worker stopped with the supervisor


def test_restarts_worker_with_stale_heartbeat():
    sup, t = run_supervisor("hang", heartbeat_timeout=1.0, stop_grace=0.5)
    try:
        first = wait_for(lambda: sup.proc)
        wait_for(lambda: sup.restarts >= 1)
        assert first.poll() is not None  # the hung worker was killed
        wait_for(lambda: sup.proc is not first)
        assert restart_events()[0]["reason"] == "stale heartbeat"
    finally:
        sup.stop()
        t.join(30)
    assert not t.is_alive()


def test_stop_file_stops_supervisor_gracefully():
    sup, t = run_supervisor("ok")
    proc = wait_for(lambda: sup.proc)
    (get_paths().state / "stop").write_text("")
    t.join(30)
    assert not t.is_alive()
    assert proc.returncode == 0  # exited on its own via worker.stop, not killed
    assert sup.restarts == 0


def test_single_instance_lock():
    paths = get_paths()
    assert read_lock(paths) is None
    assert acquire_lock(paths, port=8765)
    assert not acquire_lock(paths, port=9999)
    info = read_lock(paths)
    assert info["pid"] == os.getpid() and info["port"] == 8765
    assert info["lock_version"] == 2 and info["instance"]
    release_lock(paths)
    assert read_lock(paths) is None


def test_stale_lock_from_dead_process_is_taken_over():
    paths = get_paths()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert not pid_alive(dead.pid)
    assert pid_alive(os.getpid())
    (paths.state / "supervisor.lock").write_text(json.dumps({"pid": dead.pid, "port": 1}))
    assert read_lock(paths) is None
    assert acquire_lock(paths, port=8765)
    release_lock(paths)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_worker_serves_health_beats_and_stops_on_request():
    paths = get_paths()
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "live_clipper.worker", "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def healthy():
            try:
                return httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).json()
            except httpx.HTTPError:
                return None

        health = wait_for(healthy, timeout=30)
        beat = json.loads(wait_for(lambda: (paths.state / "heartbeat").exists() and
                                   (paths.state / "heartbeat").read_text()))
        # Not proc.pid: on Windows a venv python.exe is a launcher that runs the interpreter as a child.
        assert beat["pid"] == health["pid"]
        (paths.state / "worker.stop").write_text("")
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_worker_refuses_non_loopback_bind(monkeypatch):
    from live_clipper.worker import check_host

    check_host("127.0.0.1")
    check_host("localhost")
    with pytest.raises(SystemExit):
        check_host("0.0.0.0")
    monkeypatch.setenv("LIVE_CLIPPER_ALLOW_REMOTE", "1")
    check_host("0.0.0.0")
