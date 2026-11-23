"""Worker process: HTTP server plus all services. Started (and restarted) by the supervisor.

Stops gracefully on SIGINT/SIGTERM/CTRL_BREAK (handled by uvicorn) or when `state/worker.stop` appears.
"""

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

import uvicorn

from . import registry
from .logging_setup import setup_logging
from .paths import get_paths
from .server import allow_remote, create_app, ensure_token
from .services import build_context, load_services
from .update_gate import RESTART_EXIT, UpdateGate

log = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = 5.0


class Heartbeat:
    """Built-in service: proves the event loop is alive and watches for a stop request."""

    name = "heartbeat"
    router = None
    essential = True  # runs while the update gate is closed: the supervisor restarts a silent worker

    def __init__(self, state_dir: Path):
        self.file = state_dir / "heartbeat"
        self.stop_file = state_dir / "worker.stop"
        self.on_stop_request: Callable[[], None] = lambda: None
        self.last_beat = 0.0
        self._task: asyncio.Task | None = None

    async def start(self, ctx) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            if self.stop_file.exists():
                log.info("stop requested")
                self.on_stop_request()
                return
            if time.time() - self.last_beat >= HEARTBEAT_INTERVAL:
                self.last_beat = time.time()
                self.file.write_text(json.dumps({"pid": os.getpid(), "ts": self.last_beat}))
            await asyncio.sleep(0.5)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def status(self) -> dict:
        return {"last_beat": self.last_beat}


def check_host(host: str) -> None:
    loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    if not loopback and not allow_remote():
        raise SystemExit(f"refusing to bind {host}: set LIVE_CLIPPER_ALLOW_REMOTE=1 to allow remote access")


def run(port: int, host: str = "127.0.0.1") -> None:
    check_host(host)
    paths = get_paths()
    setup_logging(paths.logs)
    services = load_services(registry.SERVICES)  # imports register their migrations
    ctx = build_context(paths, port=port, token=ensure_token(paths))
    heartbeat = Heartbeat(paths.state)
    ctx.services[:0] = [heartbeat, *services]
    ctx.update_gate = gate = UpdateGate(paths.state)
    server = uvicorn.Server(uvicorn.Config(create_app(ctx), host=host, port=port, log_config=None,
                                           access_log=False))
    heartbeat.on_stop_request = lambda: setattr(server, "should_exit", True)
    restart = []

    def come_back_gated() -> None:  # a release appeared while the services ran: stop them the normal way
        restart.append(True)
        server.should_exit = True

    gate.on_restart = come_back_gated
    log.info("worker %d serving on %s:%d", os.getpid(), host, port)
    server.run()
    if not server.started:
        raise SystemExit(1)
    if restart:
        raise SystemExit(RESTART_EXIT)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="live_clipper.worker")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args(argv)
    run(args.port, args.host)


if __name__ == "__main__":
    main()
