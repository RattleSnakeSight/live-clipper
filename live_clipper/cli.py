"""Command line: `live-clipper <command>` (same as `python -m live_clipper`)."""

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx

from . import __version__, autostart, skillinstall
from .paths import get_paths
from .supervisor import read_lock, spawn_kwargs

DEFAULT_PORT = 8765


def _wait(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(port: int) -> int:
    if _port_free(port):
        return port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health(port: int) -> dict | None:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2).json()
    except (httpx.HTTPError, ValueError):
        return None


def _open_ui(port: int, no_browser: bool) -> None:
    token = (get_paths().state / "token").read_text().strip()
    url = f"http://127.0.0.1:{port}/?t={token}"
    print(f"Live Clipper: {url}")
    if not no_browser:
        webbrowser.open(url)


def cmd_start(args) -> int:
    info = read_lock(get_paths())
    if info:
        print("Live Clipper is already running.")
        _open_ui(info["port"], args.no_browser)
        return 0
    port = _pick_port(args.port)
    if args.foreground:
        from . import supervisor

        if not args.no_browser:
            opener = lambda: _wait(lambda: _health(port), 60) and _open_ui(port, False)  # noqa: E731
            threading.Thread(target=opener, daemon=True).start()
        supervisor.main(["--port", str(port)])
        return 0
    subprocess.Popen([sys.executable, "-m", "live_clipper.supervisor", "--port", str(port)], **spawn_kwargs())
    if not _wait(lambda: _health(port), 60):
        print("Live Clipper did not start. See `live-clipper logs`.")
        return 1
    _open_ui(port, args.no_browser)
    return 0


def _is_app_process(pid: int) -> bool:
    """True only if `pid` is alive and looks like Live Clipper (a python or live-clipper image), so a recycled
    PID belonging to some other program is never killed."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if not out.startswith('"'):
                return False
            image = out.splitlines()[0].split('","')[0].strip('"').lower()
            return image.startswith(("python", "live-clipper"))
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "args="], capture_output=True, text=True,
                             timeout=10).stdout.lower()
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return "python" in out or "live_clipper" in out or "live-clipper" in out


def cmd_stop(args=None) -> int:
    paths = get_paths()
    info = read_lock(paths)
    if not info:
        print("Live Clipper is not running.")
        return 0
    (paths.state / "stop").write_text("")
    if not _wait(lambda: read_lock(paths) is None, 30):
        print("Not responding; forcing it to stop.")
        pids = [info["pid"]]
        try:
            pids.append(json.loads((paths.state / "heartbeat").read_text())["pid"])
        except (OSError, ValueError, KeyError):
            pass
        for pid in pids:
            if not _is_app_process(pid):
                continue
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
        (paths.state / "supervisor.lock").unlink(missing_ok=True)
    print("Live Clipper stopped.")
    return 0


def cmd_status(args) -> int:
    info = read_lock(get_paths())
    health = _health(info["port"]) if info else None
    if health:
        print(json.dumps({**health, "port": info["port"], "supervisor_pid": info["pid"]}, indent=2))
        return 0
    print(json.dumps({"status": "not responding" if info else "stopped"}))
    return 1


def cmd_logs(args) -> int:
    path = get_paths().logs / "live-clipper.log"
    if not path.exists():
        print("No logs yet.")
        return 0
    data = path.read_bytes()
    print("\n".join(data.decode("utf-8", "replace").splitlines()[-200:]))
    pos = len(data)
    try:
        while args.follow:
            time.sleep(0.5)
            size = path.stat().st_size if path.exists() else 0
            pos = 0 if size < pos else pos  # file was rotated
            if size > pos:
                with open(path, "rb") as f:  # reopen each time so log rotation is never blocked
                    f.seek(pos)
                    chunk = f.read()
                pos += len(chunk)
                print(chunk.decode("utf-8", "replace"), end="", flush=True)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_doctor(args) -> int:
    paths = get_paths()
    try:
        probe = paths.root / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    free_gb = shutil.disk_usage(paths.root).free / 1e9
    running = read_lock(paths)
    port_ok = _port_free(DEFAULT_PORT) or bool(running and running["port"] == DEFAULT_PORT)
    missing = "not found: install ffmpeg"
    checks = [
        ("Python >= 3.11", sys.version_info >= (3, 11), platform.python_version()),
        ("ffmpeg on PATH", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or missing),
        ("ffprobe on PATH", bool(shutil.which("ffprobe")), shutil.which("ffprobe") or missing),
        ("Data dir writable", writable, str(paths.root)),
        ("Disk space (>= 2 GB free)", free_gb >= 2, f"{free_gb:.1f} GB free"),
        (f"Port {DEFAULT_PORT}", port_ok, "free" if port_ok else "in use; another free port will be used"),
    ]
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def install_source(direct_url: dict | None) -> str | None:
    """What to pass to pip, from pip's PEP 610 record of the original install. None = editable checkout."""
    if not direct_url:
        return "live-clipper"
    if "vcs_info" in direct_url:
        return f"{direct_url['vcs_info']['vcs']}+{direct_url['url']}"
    if direct_url.get("dir_info", {}).get("editable"):
        return None
    return direct_url["url"]


def _direct_url() -> dict | None:
    try:
        text = importlib.metadata.distribution("live-clipper").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    return json.loads(text) if text else None


def cmd_update(args) -> int:
    source = args.source or install_source(_direct_url())
    if source is None:
        print("This is an editable (developer) install: update it with `git pull`.")
        return 0
    was_running = read_lock(get_paths()) is not None
    if was_running:
        cmd_stop()
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", source])
    if was_running:
        subprocess.call([sys.executable, "-m", "live_clipper", "start", "--no-browser"])
    return rc


def cmd_uninstall(args) -> int:
    cmd_stop()
    if autostart.is_installed():
        autostart.uninstall()
        print("Removed start-at-login.")
    if args.purge_data:
        root = get_paths(create=False).root
        if root == Path.home() or root.parent == root:
            print(f"Refusing to delete {root}: it is not a dedicated data folder.")
            return 1
        if args.yes or input(f"Delete ALL Live Clipper data in {root}? [y/N] ").strip().lower() == "y":
            shutil.rmtree(root, ignore_errors=True)
            print("Data deleted.")
        else:
            print("Data kept.")
    print("To remove the program itself run one of:\n"
          "  pipx uninstall live-clipper\n"
          f"  \"{sys.executable}\" -m pip uninstall live-clipper")
    return 0


def cmd_service(args) -> int:
    if args.action == "install":
        autostart.install()
        print("Live Clipper will start at login.")
    elif args.action == "uninstall":
        autostart.uninstall()
        print("Start at login removed.")
    else:
        print("installed" if autostart.is_installed() else "not installed")
    return 0


def cmd_skill(args) -> int:
    if args.action == "path":
        print(skillinstall.SOURCE)
        return 0
    folders = skillinstall.targets(args.agent, args.dest, args.project, Path.home(), Path.cwd())
    for folder in skillinstall.install(folders):
        print(f"Installed {folder / 'SKILL.md'}")
    print("Restart your agent so it picks the skill up.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="live-clipper", description="Local live-stream clipper.")
    p.add_argument("--home", help="data folder to use (same as setting LIVE_CLIPPER_HOME)")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start", help="start in the background and open the browser")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--foreground", action="store_true", help="run in this terminal (Ctrl+C stops)")
    s.set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the running app").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="print health as JSON").set_defaults(func=cmd_status)
    s = sub.add_parser("logs", help="print recent log lines")
    s.add_argument("--follow", "-f", action="store_true")
    s.set_defaults(func=cmd_logs)
    sub.add_parser("doctor", help="check this machine can run Live Clipper").set_defaults(func=cmd_doctor)
    s = sub.add_parser("update", help="upgrade the program (data is kept)")
    s.add_argument("--source", help="pip requirement or URL to install from")
    s.set_defaults(func=cmd_update)
    s = sub.add_parser("uninstall", help="stop, remove start-at-login, optionally delete data")
    s.add_argument("--purge-data", action="store_true")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.set_defaults(func=cmd_uninstall)
    s = sub.add_parser("service", help="start at login")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.set_defaults(func=cmd_service)
    s = sub.add_parser("skill", help="install the agent skill or print its path")
    s.add_argument("action", choices=["install", "path"])
    s.add_argument("--agent", choices=[*skillinstall.FOLDERS, "all"], default="claude")
    s.add_argument("--project", action="store_true", help="install the Claude skill into ./.claude/skills")
    s.add_argument("--dest", type=Path, help="skills folder to use instead of the agent's default")
    s.set_defaults(func=cmd_skill)
    sub.add_parser("version").set_defaults(func=lambda a: print(__version__) or 0)
    args = p.parse_args(argv)
    if args.home:
        os.environ["LIVE_CLIPPER_HOME"] = args.home
    return args.func(args)
