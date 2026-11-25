import json
import os
import socket
import subprocess
import sys

from live_clipper import __version__
from live_clipper.cli import install_source


def cli(*args, timeout=90):
    return subprocess.run([sys.executable, "-m", "live_clipper", *args], capture_output=True, text=True,
                          timeout=timeout, env=os.environ.copy())


def test_version():
    r = cli("version")
    assert r.returncode == 0
    assert r.stdout.strip() == __version__


def test_doctor_reports_each_check():
    r = cli("doctor")
    assert r.returncode in (0, 1)  # 1 when e.g. ffmpeg is missing on this machine
    out = r.stdout.lower()
    for word in ("python", "ffmpeg", "ffprobe", "disk", "writable", "port"):
        assert word in out


def test_status_when_stopped():
    r = cli("status")
    assert r.returncode == 1
    assert json.loads(r.stdout)["status"] == "stopped"


def test_install_source_from_pip_metadata():
    assert install_source(None) == "live-clipper"
    vcs = {"url": "https://example.com/org/live-clipper.git", "vcs_info": {"vcs": "git", "commit_id": "abc"}}
    assert install_source(vcs) == "git+https://example.com/org/live-clipper.git"
    assert install_source({"url": "https://example.com/lc.zip", "archive_info": {}}) == "https://example.com/lc.zip"
    assert install_source({"url": "file:///src/lc", "dir_info": {"editable": True}}) is None


def test_start_status_second_start_logs_stop(home):
    # Stale discovery data pointing at this unrelated, live Python process must not prevent startup.
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "supervisor.lock").write_text(json.dumps(
        {"pid": os.getpid(), "port": 1, "lock_version": 2}))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    try:
        r = cli("start", "--no-browser", "--port", str(port))
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"http://127.0.0.1:{port}/?t=" in r.stdout

        status = json.loads(cli("status").stdout)
        assert status["status"] == "ok"

        again = cli("start", "--no-browser")
        assert again.returncode == 0 and "already running" in again.stdout.lower()
        assert f":{port}/" in again.stdout

        assert "serving on" in cli("logs").stdout
    finally:
        stopped = cli("stop")
    assert stopped.returncode == 0
    assert cli("status").returncode == 1
    assert not (home / "state" / "supervisor.lock").exists()


def test_uninstall_purge_deletes_data_and_prints_removal_command(home, monkeypatch, capsys):
    """In-process with a fake task runner, so the real Task Scheduler / launchd / systemd is never touched."""
    from live_clipper import autostart
    from live_clipper import cli as cli_mod
    calls = []
    monkeypatch.setattr(autostart, "_run", lambda *a: calls.append(a) or 1)  # 1 = "not installed"
    monkeypatch.setattr(autostart, "uninstall", lambda: calls.append("uninstall"))
    (home / "media").mkdir(parents=True)
    (home / "media" / "clip.mp4").write_bytes(b"x")
    assert cli_mod.main(["uninstall", "--purge-data", "--yes"]) == 0
    assert not home.exists()
    assert "pip uninstall live-clipper" in capsys.readouterr().out
    assert "uninstall" not in calls  # nothing was installed, so nothing was removed


def test_stop_timeout_keeps_ownership_and_never_kills_any_pid(home, monkeypatch, capsys):
    from live_clipper import cli as cli_mod
    from live_clipper.paths import get_paths

    paths = get_paths()
    lock = paths.state / "supervisor.lock"
    lock.write_text("synthetic lock")
    (paths.state / "heartbeat").write_text(json.dumps({"pid": os.getpid()}))
    monkeypatch.setattr(cli_mod, "read_lock", lambda paths: {"pid": os.getpid(), "port": 8799})
    monkeypatch.setattr(cli_mod, "_wait", lambda pred, timeout: False)
    def no_signal(*args):
        raise AssertionError("must not signal")
    monkeypatch.setattr(cli_mod.os, "kill", no_signal)
    assert cli_mod.cmd_stop() == 1
    assert lock.read_text() == "synthetic lock"
    assert (paths.state / "stop").exists()
    assert "did not stop" in capsys.readouterr().out


def test_start_reports_unresponsive_owner_without_opening_or_spawning(monkeypatch, capsys):
    from types import SimpleNamespace

    from live_clipper import cli as cli_mod

    monkeypatch.setattr(cli_mod, "read_lock", lambda paths: {"pid": 123, "port": 8799})
    monkeypatch.setattr(cli_mod, "_health", lambda port: None)
    monkeypatch.setattr(cli_mod, "_open_ui", lambda *a: (_ for _ in ()).throw(
        AssertionError("must not open")))
    monkeypatch.setattr(cli_mod.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not spawn")))
    assert cli_mod.cmd_start(SimpleNamespace(no_browser=True)) == 1
    assert "not responding" in capsys.readouterr().out


def test_update_and_uninstall_abort_when_stop_fails(monkeypatch):
    from types import SimpleNamespace

    from live_clipper import cli as cli_mod

    monkeypatch.setattr(cli_mod, "read_lock", lambda paths: {"pid": 123, "port": 8799})
    monkeypatch.setattr(cli_mod, "cmd_stop", lambda: 1)
    monkeypatch.setattr(cli_mod.subprocess, "call", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not install or start")))
    monkeypatch.setattr(cli_mod.autostart, "is_installed", lambda: (_ for _ in ()).throw(
        AssertionError("must not touch autostart")))
    assert cli_mod.cmd_update(SimpleNamespace(check=False, source="synthetic-package")) == 1
    assert cli_mod.cmd_uninstall(SimpleNamespace(purge_data=True, yes=True)) == 1


def test_restart_aborts_when_old_version_does_not_stop(monkeypatch):
    from types import SimpleNamespace

    from live_clipper import cli as cli_mod

    monkeypatch.setattr(cli_mod, "read_lock", lambda paths: {"pid": 123, "port": 8799})
    monkeypatch.setattr(cli_mod, "_health", lambda port: {"version": "0.0.1"})
    monkeypatch.setattr(cli_mod, "cmd_stop", lambda: 1)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not start")))
    assert cli_mod.cmd_start(SimpleNamespace(no_browser=True)) == 1
