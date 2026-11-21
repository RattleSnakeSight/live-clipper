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


def test_busy_kick_webhook_port_is_reported_not_fatal(tmp_path):
    import socket

    from live_clipper.monitor_service import MonitorService
    from live_clipper.paths import get_paths
    from live_clipper.services import build_context

    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    try:
        ctx = build_context(get_paths(), port=8765, token="t" * 40)
        ctx.settings.set("monitor.min_free_mb", 0)
        ctx.settings.set("monitor.kick_webhook_url", "https://example.com/kick")
        ctx.settings.set("monitor.kick_webhook_port", port)
        svc = MonitorService()
        ctx.services.append(svc)

        async def go():
            await svc.start(ctx)
            for _ in range(100):
                await asyncio.sleep(0.05)
                if svc.status().get("kick_webhook"):
                    break
            st = svc.status()
            await svc.stop()
            return st

        st = asyncio.run(go())
    finally:
        busy.close()
    kw = st["kick_webhook"]
    assert st["state"] == "running" and kw["state"] == "needs_action"
    assert str(port) in kw["detail"] and "kick_webhook_port" in kw["fix"]


def test_one_bad_channel_does_not_stop_the_others_from_arming(tmp_path, monkeypatch):
    from live_clipper.capture import Capture
    from live_clipper.engine import Monitor
    from live_clipper.monitor_service import MonitorService

    monkeypatch.setattr(Capture, "start", lambda self: None)
    real_init = Monitor.__init__

    def init(self, engine, ch):
        if ch["name"].startswith("bad"):
            raise ValueError("broken channel settings")
        real_init(self, engine, ch)

    monkeypatch.setattr(Monitor, "__init__", init)
    s, store, sec = env(tmp_path, topic_observe_seconds=0)
    ids = [store.add_channel("custom", n, f"srt://example.invalid:{9000 + i}") for i, n in
           enumerate(("good1", "bad", "good2"))]
    for c in ids:
        store.update("channels", c, armed=1)
    eng = Engine(s, store, sec)
    fresh = store.add_channel("custom", "bad2", "srt://example.invalid:9100")

    async def go():
        await eng.start()
        try:
            eng.arm(fresh)
        except ValueError:
            pass
        else:
            raise AssertionError("arming a broken channel must fail")
        svc = MonitorService()
        svc.engine, svc.store, svc.hub = eng, store, None
        views = {c: svc.channel_view(store.channel(c)) for c in ids}
        await eng.stop()
        return views

    views = asyncio.run(go())
    assert set(eng.monitors) == {ids[0], ids[2]}
    assert store.channel(fresh)["armed"] == 0             # nothing saved for a monitor that was never built
    bad = views[ids[1]]
    assert bad["state"] == "needs_action" and "broken channel settings" in bad["detail"] and bad["fix"]


def test_failed_engine_start_leaves_no_engine(tmp_path, monkeypatch):
    from live_clipper.monitor_service import MonitorService
    from live_clipper.paths import get_paths
    from live_clipper.services import build_context

    async def boom(self):
        raise RuntimeError("buffer not installed")

    monkeypatch.setattr(Engine, "start", boom)
    ctx = build_context(get_paths(), port=8765, token="t" * 40)
    svc = MonitorService()
    ctx.services.append(svc)

    async def go():
        await svc.start(ctx)
        await svc.stop()

    asyncio.run(go())
    assert svc.engine is None
    assert svc.status() == {"state": "error", "detail": "RuntimeError: buffer not installed"}


def test_housekeeping_is_cancelled_before_monitors_stop(tmp_path, monkeypatch):
    from live_clipper.capture import Capture

    s, store, sec = env(tmp_path, topic_observe_seconds=0)
    cid = store.add_channel("custom", "feed", "srt://example.invalid:9000", {"chat_adapter": "none"})
    passes = []

    monkeypatch.setattr(Capture, "start", lambda self: None)

    async def slow_stop(self):                   # processes take a while to exit
        await asyncio.sleep(1.2)

    monkeypatch.setattr(Capture, "stop", slow_stop)
    eng = Engine(s, store, sec)

    async def enforce():
        passes.append(stopping["on"])

    stopping = {"on": False}
    monkeypatch.setattr(eng.maintenance, "enforce_producers", enforce)

    async def go():
        eng.arm(cid)
        await eng.start()
        await asyncio.sleep(0.1)
        stopping["on"] = True
        await eng.stop()

    asyncio.run(go())
    assert passes and True not in passes          # no housekeeping pass (restarts, triggers) while stopping


def test_housekeeping_survives_errors_even_when_logging_fails(tmp_path, monkeypatch, caplog):
    s, store, sec = env(tmp_path)
    eng = Engine(s, store, sec)
    passes = []

    async def broken():
        passes.append(1)
        raise RuntimeError("disk check failed")

    def log_fails(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(eng.maintenance, "enforce_producers", broken)

    async def go():
        await eng.start()
        monkeypatch.setattr(store, "log", log_fails)
        await asyncio.sleep(1.3)
        alive = not eng._bg[0].done()
        monkeypatch.undo()
        await eng.stop()
        return alive

    assert asyncio.run(go()) and len(passes) >= 3
    assert "housekeeping" in caplog.text and "disk check failed" in caplog.text


def test_disk_resume_only_restarts_monitors_that_want_media(tmp_path, monkeypatch):
    from live_clipper.capture import Capture

    starts = []
    monkeypatch.setattr(Capture, "start", lambda self: starts.append(self.name))
    s, store, sec = env(tmp_path, topic_observe_seconds=0)
    names = ("recording", "went_offline", "waiting_slot")
    ids = [store.add_channel("custom", n, f"srt://example.invalid:{9000 + i}", {"chat_adapter": "none"})
           for i, n in enumerate(names)]
    eng = Engine(s, store, sec)

    async def go():
        for c in ids:
            eng.arm(c)
        mons = [eng.monitors[c] for c in ids]
        mons[1].media_on = False                  # official status answered offline while paused
        mons[2].media_on, mons[2].slot_wait = False, True
        starts.clear()
        eng.maintenance.paused = set(ids)
        await eng.maintenance.enforce_producers()  # disk space is back
        await eng.stop()

    asyncio.run(go())
    assert starts == ["recording"]


def test_trigger_merge_does_not_lose_a_concurrent_trigger_write(tmp_path, monkeypatch):
    import threading
    import time

    from live_clipper import moments
    from live_clipper.capture import Capture
    from live_clipper.events import Trigger

    monkeypatch.setattr(Capture, "start", lambda self: None)
    s, store, sec = env(tmp_path, topic_observe_seconds=0)
    cid = store.add_channel("custom", "feed", "srt://example.invalid:9000", {"chat_adapter": "none"})
    now = time.time()
    clip_id = store.create_clip(cid, "k", "chat_spike", {"reason": "first"}, action_time=now,
                                window_start=now - 18, window_end=now + 12)
    eng = Engine(s, store, sec)
    real = moments.merge_target

    def racing_merge_target(rows, *a):           # another writer (e.g. a review request) lands right now
        t = threading.Thread(target=lambda: store.merge_json("clips", clip_id, "trigger",
                                                             {"review": {"gameplay": True}}))
        t.start()
        t.join(0.3)
        return real(rows, *a)

    monkeypatch.setattr(moments, "merge_target", racing_merge_target)

    async def go():
        eng.arm(cid)
        trig = Trigger(channel_id=cid, reason="again", kind="chat_spike", burst_start=now + 2,
                       action_time=now + 1, delay=3.0, detected_at=now + 2)
        assert eng.on_trigger(eng.monitors[cid], trig) is None
        await asyncio.sleep(0.5)
        await eng.stop()

    asyncio.run(go())
    t = store.clip(clip_id)["trigger"]
    assert t["review"] == {"gameplay": True} and [m["reason"] for m in t["merged"]] == ["again"]


def test_slow_disk_checks_do_not_block_the_event_loop(tmp_path, monkeypatch):
    import collections
    import shutil
    import time

    s, store, sec = env(tmp_path)
    eng = Engine(s, store, sec)
    real = shutil.disk_usage
    Usage = collections.namedtuple("Usage", "total used free")

    def slow_disk_usage(path):                   # e.g. a sleeping USB disk or a network share
        time.sleep(0.4)
        return Usage(*real(path))

    monkeypatch.setattr(shutil, "disk_usage", slow_disk_usage)

    async def go():
        await eng.start()
        worst, last = 0.0, time.monotonic()
        for _ in range(60):
            await asyncio.sleep(0.02)
            now = time.monotonic()
            worst, last = max(worst, now - last), now
        assert eng.maintenance.admit_clip() == ""  # answered from the last measurement
        await eng.stop()
        return worst

    assert asyncio.run(go()) < 0.3


def test_titles_do_not_overwrite_captions_finished_meanwhile():
    import threading

    from edit_util import make_clip, make_engine

    eng = make_engine()
    cid = make_clip(eng, captions={"en": {"status": "ready"}, "es": {"status": "pending"}})
    real_update = eng.store.update
    fired = []

    def update(table, row_id, **fields):          # the translation thread finishes right now
        if "captions" in fields and not fired:
            fired.append(1)
            t = threading.Thread(target=lambda: eng.store.merge_json(
                "clips", cid, "captions", {"es": {"status": "ready", "srt": "captions/es.srt"}}))
            t.start()
            fired.append(t)
            t.join(0.3)
        real_update(table, row_id, **fields)

    eng.store.update = update
    postprocess._set_titles(eng, cid, {"segments": [{"start": 0, "end": 2, "text": "what a play"}],
                                       "language": "en"}, "en")
    eng.store.update = real_update
    fired[1].join(10)                             # the racing writer may still be finishing on a slow machine
    caps = eng.store.clip(cid)["captions"]
    assert caps["es"]["status"] == "ready" and caps["es"]["title"] and caps["en"]["title"]
