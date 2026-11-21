"""A real publishing worker process for the kill/restart test: python publish_worker_proc.py (uses LIVE_CLIPPER_HOME)."""
import asyncio
import sys

sys.modules["keyring"] = None  # same vault key file as the parent test

from live_clipper.paths import get_paths  # noqa: E402
from live_clipper.publish.store import Store  # noqa: E402
from live_clipper.publish.vault import Secrets  # noqa: E402
from live_clipper.publish.worker import Publisher  # noqa: E402
from live_clipper.services import build_context  # noqa: E402


async def main():
    ctx = build_context(get_paths(), port=1, token="t" * 40)
    pub = Publisher(Store(ctx.db), ctx.settings, Secrets(ctx.secrets))
    await pub.start()
    print("ready", flush=True)
    while True:
        await asyncio.sleep(1)


asyncio.run(main())
