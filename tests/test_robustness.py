"""Crash safety and error handling of the engine, capture and monitor service (review findings)."""

import asyncio

from monitor_env import env

from live_clipper import postprocess
from live_clipper.engine import Engine


def ready_clip(store, cid, n, selection=None):
    clip_id = store.create_clip(cid, f"k{n}", "chat_spike", {"selection": selection} if selection else {})
    store.update("clips", clip_id, status="ready", files={"final": {"en": "final_en.mp4"}},
                 captions={"en": {"final_status": "ready"}})
    return clip_id


def test_failed_handoff_is_retried_at_the_next_start(tmp_path):
    s, store, sec = env(tmp_path, topic_observe_seconds=0)
    cid = store.add_channel("custom", "feed", "srt://example.invalid:9000")
    calls = []

    class Flaky:
        def on_language_ready(self, clip_id, lang, force=False):
            calls.append((clip_id, lang))
            if len(calls) == 1:
                raise RuntimeError("bus down")
            return True

    eng = Engine(s, store, sec)
    eng.edit.publisher = Flaky()
    lost = ready_clip(store, cid, 1)
    held = ready_clip(store, cid, 2, {"en": {"decision": "held"}})
    postprocess._notify(eng.edit, lost, ["en"])           # the hand-off fails: only logged at that moment
    eng2 = Engine(s, store, sec)
    eng2.edit.publisher = eng.edit.publisher

    async def go():
        await eng2.start()
        await eng2.stop()

    asyncio.run(go())
    assert calls == [(lost, "en"), (lost, "en")] and held not in [c for c, _ in calls]
