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


def test_disarm_while_capture_stop_is_in_progress_restarts_nothing(tmp_path, monkeypatch):
    from live_clipper.capture import Capture
    from live_clipper.engine import Monitor

    s, store, sec = env(tmp_path, topic_observe_seconds=0)
    cid = store.add_channel("custom", "feed", "srt://example.invalid:9000",
                            {"topic_gate": {"enabled": True}, "chat_adapter": "none"})
    runs = {"started": 0, "running": 0}

    async def run_once(self):                    # a capture run whose processes take a moment to die
        runs["started"] += 1
        runs["running"] += 1
        try:
            await asyncio.sleep(3600)
        finally:
            try:
                await asyncio.sleep(0.3)
            finally:
                runs["running"] -= 1
        return True

    answers = iter([{"state": "live", "title": "GTA 6 day"}] + [{"state": "offline"}]
                   + [{"state": "live", "title": "GTA 6 day"}] * 1000)

    async def status(self):
        return next(answers)

    monkeypatch.setattr(Capture, "_run_once", run_once)
    monkeypatch.setattr(Monitor, "_status", status)
    eng = Engine(s, store, sec)

    async def go():
        eng.arm(cid)
        m = eng.monitors[cid]
        m.watch["poll_seconds"] = 0.05
        for _ in range(200):                     # live -> capture; offline -> the watch is stopping capture
            await asyncio.sleep(0.01)
            if runs["started"] and not m.gate_open:
                break
        assert runs["started"] == 1 and not m.gate_open
        await eng.disarm(cid)                    # while the watch's capture.stop() waits for the run to end
        await asyncio.sleep(0.5)
        assert m._watch_task.done() and m.capture._task.done()
        assert runs == {"started": 1, "running": 0}  # nothing restarted, nothing left running
        await eng.stop()

    asyncio.run(go())
