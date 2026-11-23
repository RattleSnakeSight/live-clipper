"""Plug-in interface for subsystems (monitor, select, edit, publish, ...). See docs/ARCHITECTURE.md."""

import asyncio
import importlib
import inspect
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import APIRouter

from . import __version__
from .db import KV, Database
from .paths import Paths
from .secrets import Vault

log = logging.getLogger(__name__)
Handler = Callable[[str, Any], Any]



class Service(Protocol):
    name: str
    router: APIRouter | None

    async def start(self, ctx: "AppContext") -> None: ...

    async def stop(self) -> None: ...

    def status(self) -> dict: ...


class EventBus:
    """In-process pub/sub. Topics are dotted (`edit.clip_ready`); subscribe to exact, `prefix.*` or `*`.

    Handlers are called as handler(topic, payload). Coroutine handlers are scheduled on the running loop,
    or on `self.loop` when publishing from another thread. Handler errors are logged, never raised.
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._tasks: set[asyncio.Future] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        self._subs[topic].append(handler)
        return lambda: self._subs[topic].remove(handler)

    def publish(self, topic: str, payload: Any = None) -> None:
        for pattern, handlers in list(self._subs.items()):
            if pattern in (topic, "*") or (pattern.endswith(".*") and topic.startswith(pattern[:-1])):
                for handler in list(handlers):
                    try:
                        result = handler(topic, payload)
                        if inspect.iscoroutine(result):
                            self._schedule(result)
                    except Exception:
                        log.exception("bus handler failed for %s", topic)

    def _schedule(self, coro) -> None:
        try:
            fut = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            if self.loop is None:
                coro.close()
                raise
            fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        self._tasks.add(fut)
        fut.add_done_callback(self._tasks.discard)


@dataclass
class AppContext:
    paths: Paths
    db: Database
    settings: KV  # settings_kv table: settings.get(key, default) / settings.set(key, value)
    app_state: KV  # app_state table: wizard/tour flags
    secrets: Vault
    bus: EventBus
    port: int
    token: str
    version: str = __version__
    started_at: float = field(default_factory=time.time)
    services: list[Service] = field(default_factory=list)
    update_gate: Any = None  # update_gate.UpdateGate in the worker; None runs every service at once

    def notify(self, title: str, body: str) -> None:
        """User-facing notification. Delivered as bus topic `app.notify`; the UI/desktop layer subscribes."""
        log.info("notify: %s", title)
        self.bus.publish("app.notify", {"title": title, "body": body})


def build_context(paths: Paths, port: int, token: str) -> AppContext:
    db = Database(paths.db_file)
    db.migrate()
    return AppContext(paths=paths, db=db, settings=KV(db, "settings_kv"), app_state=KV(db, "app_state"),
                      secrets=Vault(paths.secrets), bus=EventBus(), port=port, token=token)


def load_services(specs: list[str]) -> list[Service]:
    """Each spec is "package.module:factory"; factory() (a class or function) returns a Service.

    A subsystem that fails to load is logged and skipped so one broken plug-in cannot take the app down.
    """
    services = []
    for spec in specs:
        module, _, attr = spec.partition(":")
        try:
            services.append(getattr(importlib.import_module(module), attr)())
        except Exception:
            log.exception("failed to load service %s", spec)
    return services
