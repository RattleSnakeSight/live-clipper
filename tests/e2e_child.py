"""Worker stand-in for test_e2e_crash: every registered service on the synthetic stream; one chat spike; the clip
is cut, then speech recognition of the full clip blocks (post-cut work in progress) until the process is
killed. Prints one JSON line when that point is reached. Usage: e2e_child.py <stream url>"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.modules["keyring"] = None  # vault key file only
sys.path.insert(0, str(Path(__file__).parent))

from e2e_util import FakeTranscriber, FakeTranslator, chat_rows, configure, service  # noqa: E402

from live_clipper import registry  # noqa: E402
from live_clipper.paths import get_paths  # noqa: E402
from live_clipper.services import build_context, load_services  # noqa: E402


class BlockOnFullClip(FakeTranscriber):
    """The early pass over the pre-roll answers; the full-clip pass hangs."""

    def __init__(self, hit: threading.Event):
        super().__init__()
        self.hit = hit

    def transcribe(self, wav, language=None):
        if not Path(wav).name.startswith("audio_pre"):
            self.hit.set()
            threading.Event().wait()
        return super().transcribe(wav, language)


async def main(url: str) -> None:
    services = load_services(registry.SERVICES)
    ctx = build_context(get_paths(), port=8765, token="test")
    configure(ctx, "auto")
    ctx.services = services
    ctx.bus.loop = asyncio.get_running_loop()
    for s in services:
        await s.start(ctx)
    entered = threading.Event()
    service(ctx, "edit").engine.captioner = (BlockOnFullClip(entered), FakeTranslator())
    mon = service(ctx, "monitor")
    t0 = time.time() + 3
    cid = mon.store.add_channel("custom", url, url, {"chat_adapter": "replay",
                                                     "chat_options": {"rows": chat_rows(26), "start_at": t0}},
                                url=url, display_name="streamer_alpha")
    mon.engine.arm(cid)
    while not entered.is_set():
        await asyncio.sleep(0.1)
    clips = mon.store.q("SELECT id, status, trigger FROM clips")
    print(json.dumps({"channel": cid, "clips": [[c["id"], c["status"]] for c in clips],
                      "action_time": clips[0]["trigger"]["action_time"]}), flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
