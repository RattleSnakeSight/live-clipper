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
