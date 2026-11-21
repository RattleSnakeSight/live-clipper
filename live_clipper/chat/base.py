"""Chat adapter contract. Implementations: replay (fixtures), twitch, kick, youtube.

An adapter runs as an asyncio task, calls `emit(ChatEvent)` for every message as soon as it
arrives, keeps `self.health` current, and reconnects on its own with backoff. It must never
log tokens. Credentials come from `creds` (a dict resolved by live_clipper.sources (vault entry `source.<platform>`)) — never env vars
read directly inside adapters.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..events import ChatEvent, ChatHealth

Emit = Callable[[ChatEvent], None]


class ChatAdapter:
    platform = "custom"

    def __init__(self, channel: str, emit: Emit, creds: dict | None = None, options: dict | None = None):
        self.channel = channel
        self.emit = emit
        self.creds = creds or {}
        self.options = options or {}
        self.health = ChatHealth()
        self._task: asyncio.Task | None = None

    async def run(self) -> None:          # override: connect, loop, reconnect until cancelled
        raise NotImplementedError

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.health.state = "connecting"
            self._task = asyncio.create_task(self._guard(), name=f"lc-chat-{self.platform}-{self.channel}")

    async def _guard(self) -> None:
        try:
            await self.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:                      # adapter bug: surface, don't crash the worker
            self.health.state, self.health.detail = "unavailable", f"{type(e).__name__}: {e}"[:300]

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.health.state = "stopped"
