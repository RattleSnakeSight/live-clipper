"""Worker stand-in for test_e2e_crash: every registered service on the synthetic stream; one chat spike; the
process stops at a chosen point and prints one JSON line there, then waits to be killed.
Usage: e2e_child.py <stream url> [point]

point "captioning" (default): the clip is cut, speech recognition of the full clip blocks (post-cut work).
point "notify": the clip is saved as ready, the hand-off to publishing has not run yet.
point "upload": the test destination stored the upload, the worker never heard back."""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.modules["keyring"] = None  # vault key file only
sys.path.insert(0, str(Path(__file__).parent))

from e2e_util import FakeTranscriber, FakeTranslator, chat_rows, configure, sandbox_videos, service  # noqa: E402

from live_clipper import postprocess, registry  # noqa: E402
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


async def main(url: str, point: str) -> None:
    services = load_services(registry.SERVICES)
    ctx = build_context(get_paths(), port=8765, token="test")
    configure(ctx, "auto")
    ctx.services = services
    ctx.bus.loop = asyncio.get_running_loop()
    for s in services:
        await s.start(ctx)
    entered = threading.Event()
    mon = service(ctx, "monitor")

    def report() -> None:
        clips = mon.store.q("SELECT id, status, trigger FROM clips")
        print(json.dumps({"channel": cid, "clips": [[c["id"], c["status"]] for c in clips],
                          "action_time": clips[0]["trigger"]["action_time"]}), flush=True)

    edit = service(ctx, "edit").engine
    if point == "captioning":
        edit.captioner = (BlockOnFullClip(entered), FakeTranslator())
    else:
        edit.captioner = (FakeTranscriber(), FakeTranslator())
    if point == "notify":
        def stop_before_handoff(engine, clip_id, langs):
            report()
            threading.Event().wait()             # killed here: "ready" is saved, publishing never heard of it
        postprocess._notify = stop_before_handoff
    if point == "upload":
        from live_clipper.publish.sandbox_server import ensure_sandbox
        sb = ensure_sandbox(ctx.paths.root / "sandbox")
        with sb.lock:                             # the provider stores the bytes and never answers
            sb.state["inject"].append({"times": 1, "op": "upload", "fail": "timeout_after_accept", "hang": 600})
            sb.save()
    t0 = time.time() + 3
    cid = mon.store.add_channel("custom", url, url, {"chat_adapter": "replay",
                                                     "chat_options": {"rows": chat_rows(26), "start_at": t0}},
                                url=url, display_name="streamer_alpha")
    mon.engine.arm(cid)
    while not entered.is_set():
        if point == "upload" and sandbox_videos(ctx.paths.root) == 1 and mon.store.q(
                "SELECT id FROM deliveries WHERE status='uploading'"):
            break
        await asyncio.sleep(0.1)
    report()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "captioning"))
