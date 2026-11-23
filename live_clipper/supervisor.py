"""Supervisor process: keeps one worker alive, restarts it on crash or hang, holds the single-instance lock.

Stop it by creating `state/stop` (what `live-clipper stop` does) or with SIGINT/SIGTERM/CTRL_BREAK.
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from .db import Database
from .logging_setup import setup_logging
from .paths import Paths, get_paths
from .server import ensure_token
from .update_gate import RESTART_EXIT

log = logging.getLogger(__name__)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: exists, owned by someone else
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)  # never on Windows: there os.kill terminates the process
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock(paths: Paths) -> dict | None:
    """{"pid", "port"} of the running supervisor, or None if none is running."""
    try:
        info = json.loads((paths.state / "supervisor.lock").read_text())
    except (OSError, ValueError):
        return None
    return info if pid_alive(info["pid"]) else None


def acquire_lock(paths: Paths, port: int) -> bool:
    lock = paths.state / "supervisor.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            if read_lock(paths) is not None:
                return False
            lock.unlink(missing_ok=True)  # stale: its owner is gone
            continue
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "port": port}, f)
        return True
    return False


def release_lock(paths: Paths) -> None:
    info = read_lock(paths)
    if not info or info["pid"] != os.getpid():
        return
    # Windows refuses to delete a file that another process has open, and `live-clipper stop` opens the lock
    # five times a second to see whether we are gone. That read takes a moment, so try again for a few
    # seconds instead of leaving a stale lock behind.
    for _ in range(50):
        try:
            (paths.state / "supervisor.lock").unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.1)


def spawn_kwargs() -> dict:
    """Background child: no console window, own process group so terminal Ctrl+C reaches only the parent."""
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    return kw


class Supervisor:
    def __init__(self, paths: Paths, port: int, cmd: list[str] | None = None, *, heartbeat_timeout=60.0,
                 max_backoff=60.0, healthy_reset=300.0, poll=1.0, stop_grace=10.0):
        self.paths, self.port = paths, port
        self.cmd = cmd or [sys.executable, "-m", "live_clipper.worker", "--port", str(port)]
        self.heartbeat_timeout, self.max_backoff = heartbeat_timeout, max_backoff
        self.healthy_reset, self.poll, self.stop_grace = healthy_reset, poll, stop_grace
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self._stop = threading.Event()
        self._heartbeat = paths.state / "heartbeat"
        self._worker_stop = paths.state / "worker.stop"
        self._stop_request = paths.state / "stop"

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        db = Database(self.paths.db_file)
        db.migrate()
        self._stop_request.unlink(missing_ok=True)
        backoff = 1.0
        try:
            while not self._stop.is_set():
                self._worker_stop.unlink(missing_ok=True)
                self._heartbeat.unlink(missing_ok=True)
                started = time.time()
                self.proc = subprocess.Popen(self.cmd, **spawn_kwargs())
                log.info("worker started pid=%d", self.proc.pid)
                reason = self._watch(started)
                if reason == "stop":
                    break
                if reason == "exited" and self.proc.returncode == RESTART_EXIT:
                    log.info("worker asked for a restart (update check)")
                    if self._stop.wait(1.0):
                        break
                    continue
                if reason == "stale heartbeat":
                    self._kill()
                if time.time() - started >= self.healthy_reset:
                    backoff = 1.0
                data = {"reason": reason, "returncode": self.proc.returncode, "delay": backoff}
                log.warning("worker %s (rc=%s); restarting in %.0fs", reason, self.proc.returncode, backoff)
                db.event("warning", "supervisor", f"worker {reason}; restarting", data)
                self.restarts += 1
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, self.max_backoff)
        finally:
            self._stop_worker()
            self._stop_request.unlink(missing_ok=True)
            log.info("supervisor stopped")

    def _watch(self, started: float) -> str:
        while True:
            if self._stop.wait(self.poll) or self._stop_request.exists():
                return "stop"
            if self.proc.poll() is not None:
                return "exited"
            try:
                last = max(started, self._heartbeat.stat().st_mtime)
            except OSError:
                last = started
            if time.time() - last > self.heartbeat_timeout:
                return "stale heartbeat"

    def _stop_worker(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self._worker_stop.write_text("")
        try:
            self.proc.wait(self.stop_grace)
        except subprocess.TimeoutExpired:
            log.warning("worker did not stop in %.0fs; killing", self.stop_grace)
            self._kill()

    def _kill(self) -> None:
        self.proc.kill()
        self.proc.wait()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="live_clipper.supervisor")
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args(argv)
    paths = get_paths()
    setup_logging(paths.logs, "supervisor.log")
    if not acquire_lock(paths, args.port):
        log.info("already running")
        return
    try:
        ensure_token(paths, regenerate=True)
        sup = Supervisor(paths, args.port)
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: sup.stop())
        sup.run()
    finally:
        release_lock(paths)


if __name__ == "__main__":
    main()
