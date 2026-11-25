"""Supervisor process: keeps one worker alive, restarts it on crash or hang, holds the single-instance lock.

Stop it by creating `state/stop` (what `live-clipper stop` does) or with SIGINT/SIGTERM/CTRL_BREAK.
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from .db import Database
from .instance_lock import InstanceLockError, acquire_lock, pid_alive, read_lock, release_lock  # noqa: F401
from .logging_setup import setup_logging
from .paths import Paths, get_paths
from .server import ensure_token
from .update_gate import RESTART_EXIT

log = logging.getLogger(__name__)


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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="live_clipper.supervisor")
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args(argv)
    paths = get_paths()
    setup_logging(paths.logs, "supervisor.log")
    try:
        acquired = acquire_lock(paths, args.port)
    except (InstanceLockError, OSError) as exc:
        log.error("could not acquire the instance lock: %s", exc)
        return 1
    if not acquired:
        log.info("already running")
        return 0
    try:
        ensure_token(paths, regenerate=True)
        sup = Supervisor(paths, args.port)
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: sup.stop())
        sup.run()
    finally:
        release_lock(paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
