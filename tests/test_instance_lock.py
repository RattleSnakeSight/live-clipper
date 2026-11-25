"""All locks and processes here belong to a temporary test home, never the installed app."""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

from live_clipper import instance_lock as locks
from live_clipper.paths import get_paths
from live_clipper.supervisor import spawn_kwargs

OWNER = """
import os
import sys
from live_clipper.paths import get_paths
from live_clipper.instance_lock import acquire_lock, release_lock
paths = get_paths()
got = acquire_lock(paths, 8799)
print(f'owned:{os.getpid()}' if got else 'busy', flush=True)
try:
    if sys.stdin.readline().strip() == "crash":
        os._exit(17)
finally:
    if got:
        release_lock(paths)
"""


def owner():
    return subprocess.Popen([sys.executable, "-c", OWNER],
                            **{**spawn_kwargs(), "stdin": subprocess.PIPE, "stdout": subprocess.PIPE},
                            text=True)


def finish(proc):
    proc.communicate(timeout=15)
    assert proc.returncode == 0


def test_competing_processes_use_one_guard_and_crash_releases_it():
    paths = get_paths()
    first, second = owner(), owner()
    try:
        answers = [first.stdout.readline().strip(), second.stdout.readline().strip()]
        assert sum(a.startswith("owned:") for a in answers) == 1 and "busy" in answers
        index = next(i for i, a in enumerate(answers) if a.startswith("owned:"))
        winner = (first, second)[index]
        assert locks.read_lock(paths)["pid"] == int(answers[index].split(":")[1])
        assert not locks.acquire_lock(paths, 8800)
        # Exit the actual interpreter abruptly, including behind a Windows venv launcher.
        winner.stdin.write("crash\n")
        winner.stdin.flush()
        winner.wait(timeout=10)
        assert winner.returncode == 17
        assert (paths.state / "supervisor.lock").exists()
        assert locks.read_lock(paths) is None
        assert locks.acquire_lock(paths, 8800)
        locks.release_lock(paths)
        assert (paths.state / "supervisor.guard").exists()  # stable inode across restarts
    finally:
        for proc in (first, second):
            if proc.poll() is None:
                finish(proc)
            else:
                proc.communicate()


def test_surviving_worker_prevents_duplicate_start_and_can_be_stopped():
    from test_cli import cli
    from test_supervisor import wait_for

    from live_clipper.cli import _health

    paths = get_paths()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    parent = owner()
    child = None
    try:
        assert parent.stdout.readline().strip().startswith("owned:")
        child = subprocess.Popen([sys.executable, "-m", "live_clipper.worker", "--port", str(port)],
                                 **spawn_kwargs())
        wait_for(lambda: _health(port), timeout=30)
        parent.stdin.write("crash\n")
        parent.stdin.flush()
        parent.wait(timeout=10)
        info = locks.read_lock(paths)
        assert info["role"] == "worker" and info["port"] == port
        assert not locks.acquire_lock(paths, port + 1)
        again = cli("start", "--no-browser")
        assert again.returncode == 1 and "surviving worker" in again.stdout
        assert _health(port)["pid"] == info["pid"]  # the original worker is the only one serving
        assert cli("stop").returncode == 0
        child.wait(timeout=15)
        assert locks.read_lock(paths) is None
        assert locks.acquire_lock(paths, port)
        locks.release_lock(paths)
    finally:
        (paths.state / "worker.stop").write_text("")
        if child is not None:
            child.wait(timeout=30)
        if parent.poll() is None:
            finish(parent)
        else:
            parent.communicate()


def test_stale_metadata_cannot_claim_an_unrelated_live_pid():
    paths = get_paths()
    lock = paths.state / "supervisor.lock"
    # This Python interpreter is alive but does not hold this home's guard.
    lock.write_text(json.dumps({"pid": os.getpid(), "port": 8799, "lock_version": 2}))
    assert locks.read_lock(paths) is None
    assert locks.acquire_lock(paths, 8800)
    locks.release_lock(paths)


def test_legacy_recycled_pid_is_stale_even_if_the_process_is_alive():
    paths = get_paths()
    lock = paths.state / "supervisor.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "port": 8799}))
    started = locks.process_started(os.getpid())
    assert started is not None
    os.utime(lock, (started - 30, started - 30))
    assert locks.read_lock(paths) is None
    assert locks.acquire_lock(paths, 8800)
    locks.release_lock(paths)


def test_legacy_running_app_needs_authenticated_status(monkeypatch):
    paths = get_paths()
    info = {"pid": os.getpid(), "port": 8799}
    (paths.state / "supervisor.lock").write_text(json.dumps(info))
    (paths.state / "token").write_text("synthetic-test-token")

    def status(url, **kw):
        assert url == "http://127.0.0.1:8799/api/status"  # not public /health
        assert kw["headers"] == {"Authorization": "Bearer synthetic-test-token"}
        assert kw["trust_env"] is False and kw["follow_redirects"] is False
        return httpx.Response(200, json={"pid": 123, "version": "0.4.1"})

    monkeypatch.setattr(locks.httpx, "get", status)
    assert locks.read_lock(paths) == info
    assert not locks.acquire_lock(paths, 8800)
    assert json.loads((paths.state / "supervisor.lock").read_text()) == info


def test_legacy_ambiguous_owner_is_not_killed_or_overwritten(monkeypatch):
    paths = get_paths()
    info = {"pid": 424242, "port": 8799}
    lock = paths.state / "supervisor.lock"
    lock.write_text(json.dumps(info))
    monkeypatch.setattr(locks, "pid_alive", lambda pid: True)  # includes access denied
    monkeypatch.setattr(locks, "process_started", lambda pid: None)
    with pytest.raises(locks.InstanceLockError, match="older instance cannot be verified"):
        locks.acquire_lock(paths, 8800)
    assert json.loads(lock.read_text()) == info
    # Failure must not leave our guard locked.
    monkeypatch.setattr(locks, "pid_alive", lambda pid: False)
    assert locks.acquire_lock(paths, 8800)
    locks.release_lock(paths)


def test_legacy_lock_before_boot_does_not_belong_to_a_protected_recycled_pid(monkeypatch):
    paths = get_paths()
    lock = paths.state / "supervisor.lock"
    lock.write_text(json.dumps({"pid": 424242, "port": 8799}))
    monkeypatch.setattr(locks, "pid_alive", lambda pid: True)
    monkeypatch.setattr(locks, "process_started", lambda pid: None)  # access denied
    monkeypatch.setattr(locks, "system_boot_time", lambda: lock.stat().st_mtime + 60)
    assert locks.read_lock(paths) is None
    assert locks.acquire_lock(paths, 8800)
    locks.release_lock(paths)


def test_start_retries_a_brief_status_probe_instead_of_abandoning_startup():
    paths = get_paths()
    probe, busy = locks._try_guard(paths, create=True)
    assert probe is not None and not busy
    release = threading.Timer(0.2, probe.close)
    release.start()
    try:
        assert locks.acquire_lock(paths, 8800)
        assert locks.read_lock(paths)["pid"] == os.getpid()
    finally:
        release.join(timeout=5)
        locks.release_lock(paths)


def test_supervisor_reports_ownership_failure_without_starting_a_worker(monkeypatch):
    from live_clipper import supervisor

    def refuse(*args):
        raise locks.InstanceLockError("synthetic ownership failure")

    monkeypatch.setattr(supervisor, "acquire_lock", refuse)
    assert supervisor.main(["--port", "8799"]) == 1
    assert "synthetic ownership failure" in (get_paths().logs / "supervisor.log").read_text()


@pytest.mark.parametrize("value", ["{", "[]", '{"pid":true,"port":8799}', '{"pid":1,"port":"8799"}'])
def test_bad_metadata_is_recoverable_only_without_an_owner(value):
    paths = get_paths()
    (paths.state / "supervisor.lock").write_text(value)
    assert locks.read_lock(paths) is None
    assert locks.acquire_lock(paths, 8800)
    locks.release_lock(paths)


def test_active_guard_with_bad_metadata_does_not_allow_a_second_owner():
    paths = get_paths()
    proc = owner()
    try:
        assert proc.stdout.readline().strip().startswith("owned:")
        (paths.state / "supervisor.lock").write_text("{")
        with pytest.raises(locks.InstanceLockError, match="not ready"):
            locks.read_lock(paths)
        assert not locks.acquire_lock(paths, 8800)
    finally:
        finish(proc)


def test_windows_process_times_uses_current_process_identity():
    started = locks.process_started(os.getpid())
    assert started is not None and 0 < started <= time.time()
