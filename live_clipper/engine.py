"""Orchestration: per-channel monitors (capture + chat + detector) and the clip pipeline (port of the reference engine).

Clip pipeline (as in the original engine): capture -> ring buffer (buffer/timeline) -> chat -> detector -> trigger ->
effective selection values (live_clipper.pipeline: window, preset) -> merge/dedupe (moments) -> clip row ->
EditEngine.process: cut + preview (clipper) -> captions, title, editorial/language checks, finals (postprocess) ->
selection gate -> `edit.clip_ready` -> publishing. Capture is never blocked by clip work.

Public-product additions (docs/DEVIATIONS.md): official live status gates capture when the platform's API is
connected (Monitor._official), a recording-slot limit (settings.max_concurrent_recordings) and public_state().
"""
from __future__ import annotations

import asyncio
import collections
import logging
import shutil
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from .capture import Capture
from .config import Settings
from .events import ChatEvent, ChatHealth, Trigger
from .store import Store

log = logging.getLogger(__name__)
TERMINAL = {"ready", "failed", "discarded", "expired"}
MB = 1024 * 1024


class LiveRefused(Exception):
    """This source must not be captured live (VOD-only or retired)."""


class Backpressure(Exception):
    pass


CHANNEL_DEFAULTS = {"sensitivity": "medium", "subtitle_language": "auto",
                    "reaction_delay": None, "pre_roll": None, "post_roll": None,
                    "layout": {"mode": "auto"}, "chat_adapter": "", "chat_options": {}, "glossary": [],
                    "realtime_file": False}


def refusal(ch: dict) -> str | None:
    """Reference roster.refusal + vod.is_vod_only: why this channel must never be captured live."""
    s = ch.get("settings") or {}
    if s.get("retired"):
        return (f"channel {ch.get('platform')}/{ch.get('name')} is retired from tracking "
                f"({(s['retired'] or {}).get('reason')}): no capture, scheduling or automatic publication")
    if s.get("ingest_mode") == "vod_only":
        return (f"{ch['platform']}/{ch['name']} is VOD-only (post-stream VOD + chat replay import): "
                f"live monitoring, capture and tab share are refused for this source")
    return None


class _NullDetector:
    """Used only while the select subsystem's detector is not installed: chat is shown but never triggers."""
    stats = collections.defaultdict(int)

    def feed(self, ev):
        return []

    def tick(self, now):
        return []

    def snapshot(self):
        return {"state": "unavailable", "detail": "detector not installed"}


def _detector(channel_id: int, fmt, sensitivity, reaction_delay):
    """The selection format's detector; a channel's own sensitivity / reaction delay override it."""
    try:
        from .detector import DetectorConfig, SpikeDetector
    except ImportError:
        return _NullDetector()
    return SpikeDetector(channel_id, DetectorConfig.from_format(fmt, sensitivity=sensitivity,
                                                                reaction_delay=reaction_delay))


class Monitor:
    def __init__(self, engine: Engine, ch: dict):
        self.engine = engine
        self.ch = ch
        self.id = ch["id"]
        s = {**CHANNEL_DEFAULTS, **ch["settings"]}
        self.s = s
        cfg = engine.settings
        own = ch["settings"] or {}
        self._fmt = engine.channel_format(self.id)  # topic matching, topic state names and the detector
        self.detector = _detector(self.id, self._fmt, own.get("sensitivity"), own.get("reaction_delay"))
        self.capture = Capture(self.id, ch["platform"], ch["name"], ch["source_url"] or "", engine.buffers,
                               segment_seconds=cfg.segment_seconds, log=self._log,
                               realtime_file=bool(s.get("realtime_file")), ffmpeg=cfg.ffmpeg_path)
        self.chat = None
        self.recent_chat: deque[dict] = deque(maxlen=40)
        self.started_at = time.time()
        gate = s.get("topic_gate") or {}
        self.gated = bool(gate.get("enabled"))
        self.gate_open = not self.gated
        self.tab_hold = False
        self.watch: dict = {"enabled": self.gated, "state": "starting" if self.gated else "off",
                            "poll_seconds": int(gate.get("poll_seconds") or 120)}
        self._watch_task: asyncio.Task | None = None
        self.observed: dict = {}
        self.observed_good: dict = {}
        self.observe_started_at: float = 0.0
        self.boundary_at: float = 0.0
        self._observe_task: asyncio.Task | None = None
        self._official_task: asyncio.Task | None = None
        self.official: dict = {}                      # last official live-status answer (official API connected)
        self.media_on = False                         # capture + chat requested
        self.slot_wait = False                        # waiting for a recording slot

    @property
    def fmt(self):
        """The channel's effective selection format (the active format for a monitor built without one)."""
        from .formats import active
        return self.__dict__.get("_fmt") or active()

    def official_active(self) -> bool:
        hub = self.engine.hub
        return bool(hub is not None and not self.ch["source_url"] and hub.connected(self.ch["platform"]))

    def status_source(self) -> str:
        from .sources import OFFICIAL, STREAM_PROBE
        if self.ch["source_url"]:
            return "creator-provided feed"
        if self.official_active():
            return OFFICIAL[self.ch["platform"]]
        return STREAM_PROBE

    async def _status(self) -> dict:
        from . import source_status as ss
        creds = self.engine.secrets.chat_creds(self.ch["platform"]) if self.engine.secrets else {}
        return await ss.source_status(self.ch["platform"], self.ch["name"], self.ch["source_url"] or "", creds,
                                      hub=self.engine.hub)

    async def _observe(self) -> None:
        """Passive title/category observation for an ungated Twitch/Kick source: never starts, stops or gates capture;
        it only gives clips capture-time game evidence. One public status lookup per interval, backing off on errors."""
        import random
        base = max(60, int(self.engine.settings.topic_observe_seconds))
        wait, retry = base, 60
        self.observe_started_at = time.time()
        await asyncio.sleep(random.uniform(1, 12))
        while True:
            try:
                st = await self._status()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                st = {"state": "error", "detail": f"{type(e).__name__}: {e}"[:200]}
            ok = st.get("state") in ("live", "offline")
            wait, retry = (base, 60) if ok else (min(900, retry), min(900, retry * 2))
            if self.note_status(st, wait, base):
                try:
                    self.engine.late_topic_evidence(self)
                except Exception as e:
                    self._log(f"late topic evidence failed: {type(e).__name__}: {e}", "warn")
            await asyncio.sleep(wait + random.uniform(0, 10))

    async def _official(self) -> None:
        """Official live status gates capture (ungated channels with the platform's API connected): a live answer
        starts the reference capture loop, an offline answer stops it (no stream-probe retries while offline).
        Errors and rejected credentials change nothing; they are reported through `self.official`."""
        hub = self.engine.hub
        plat, name = self.ch["platform"], self.ch["name"]
        self.observe_started_at = time.time()
        after = 0.0
        while True:
            st = await hub.next(plat, name, after)
            after = st["checked_at"]
            self.official = st
            iv = max(60.0, hub.interval(plat))
            if self.note_status(st, iv, iv, st["checked_at"]):
                try:
                    self.engine.late_topic_evidence(self)
                except Exception as e:
                    self._log(f"late topic evidence failed: {type(e).__name__}: {e}", "warn")
            if st["state"] == "live" and not self.media_on and not self.slot_wait:
                self._log(f"official status: live ({st.get('title', '')[:80]}); capture started")
                self._start_media_and_chat()
            elif st["state"] == "offline" and (self.media_on or self.slot_wait):
                await self._stop_media_and_chat(purge=False)
                self._log("official status: offline; capture stopped")

    def capture_epoch(self):
        h = getattr(getattr(self, "capture", None), "health", None)
        return getattr(h, "restarts", None)

    def note_status(self, st: dict, wait: float, base: float, now: float | None = None) -> bool:
        """Record one status lookup. Returns True for a live answer. A broadcast boundary invalidates the remembered
        live answer and is remembered itself: an OFFLINE answer, a live answer with a different broadcast id, or a
        capture that reconnected since the remembered answer."""
        now = now or time.time()
        epoch = self.capture_epoch()
        self.observed = {"state": st.get("state"), "title": st.get("title", ""), "category": st.get("category", ""),
                         "detail": st.get("detail", ""), "checked_at": now, "interval": wait,
                         "broadcast": str(st.get("id") or ""), "capture_epoch": epoch}
        good = getattr(self, "observed_good", None) or {}
        if st.get("state") == "offline":
            self.observed_good, self.boundary_at = {}, now
            return False
        if st.get("state") != "live":
            return False
        bid = self.observed["broadcast"]
        if good and ((bid and good.get("broadcast") and bid != good["broadcast"])
                     or good.get("capture_epoch") != epoch):
            self.boundary_at = now
        self.observed_good = dict(self.observed, interval=base)
        return True

    def topic_evidence(self) -> dict:
        """Game evidence for a clip triggered now: {"state", "title", "category"}."""
        from .source_status import topic_state
        unverified = self.fmt.topic_states.unverified
        if self.gated:
            return {"state": self.watch.get("state"), "title": str(self.watch.get("title") or "")[:160],
                    "category": str(self.watch.get("category") or "")[:80]}
        now = time.time()
        base = max(60, int(getattr(self.engine.settings, "topic_observe_seconds", 300) or 300))
        last = self.observed or {}
        o = last
        if last.get("state") not in ("live", "offline"):
            o = getattr(self, "observed_good", None) or {}
            if o and o.get("capture_epoch") != self.capture_epoch():
                o = {}
        age = now - float(o["checked_at"]) if o.get("checked_at") else None
        fresh = age is not None and age <= 2.5 * float(o.get("interval") or base)
        state = topic_state(o, self.engine.settings.topic_category_evidence, self.fmt) if fresh else unverified
        if state != unverified:
            return {"state": state, "title": str(o.get("title") or "")[:160],
                    "category": str(o.get("category") or "")[:80], "observed_at": round(float(o["checked_at"]), 1)}
        if not last:
            kind, why = "no_lookup_yet", "no status lookup had completed yet for this source (monitoring had just started)"
        elif last.get("state") not in ("live", "offline"):
            kind = "lookup_failed"
            why = f"the status lookup failed ({str(last.get('detail') or last.get('state'))[:120]})" + (
                "" if age is None else f" and the last live answer was {int(age)} s old")
        elif not fresh:
            kind, why = "stale", f"the last status lookup was {int(age)} s old"
        elif o.get("state") == "offline":
            kind, why = "offline", "the platform's status lookup answered offline while the clip was captured"
        else:
            kind, why = "no_metadata", "the platform answered live but returned no title or category"
        return {"state": unverified, "title": "", "category": "", "why": why, "kind": kind,
                "capture_epoch": self.capture_epoch()}

    def _log(self, msg: str, level: str = "info") -> None:
        self.engine.store.log(msg, level, channel_id=self.id)

    def on_chat(self, ev: ChatEvent) -> None:
        if not self.gate_open:
            return
        self.recent_chat.append({"t": ev.received_at, "user": ev.user_name, "text": ev.text[:200]})
        for trig in self.detector.feed(ev):
            self.engine.on_trigger(self, trig)

    def start(self) -> None:
        if self.gated:                                # topic-gated watch: capture/chat only while on topic
            self._watch_task = self.engine._spawn(self._watch())
            return
        if self.official_active():                    # official status gates capture (no probe spam offline)
            self._official_task = self.engine._spawn(self._official())
            return
        if self.ch["platform"] in ("twitch", "kick") and int(self.engine.settings.topic_observe_seconds) > 0:
            self._observe_task = self.engine._spawn(self._observe())
        self._start_media_and_chat()

    async def _watch(self) -> None:
        """Rate-aware availability/topic watch. Opens the gate (fresh capture, no backlog) only while the live
        title evidences the topic; closes it (stop capture, purge this channel's buffer) otherwise."""
        from .source_status import is_gta6
        backoff = self.watch["poll_seconds"]
        while True:
            try:
                st = await self._status()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                st = {"state": "error", "detail": f"{type(e).__name__}: {e}"[:200]}
            if st.get("source") == "official":
                self.official = st
            on_topic = st["state"] == "live" and is_gta6(st.get("title"), fmt=self.fmt)
            ts = self.fmt.topic_states
            label = {"live": ts.title if on_topic else ts.off_topic}.get(st["state"], st["state"])
            now = time.time()
            ok = st["state"] in ("live", "offline")
            backoff = self.watch["poll_seconds"] if ok else min(900, max(backoff * 2, self.watch["poll_seconds"]))
            self.watch["fails"] = 0 if ok else self.watch.get("fails", 0) + 1
            wait = max(backoff, float(st.get("retry_after") or 0))
            self.watch.update(state=label, title=st.get("title", ""), category=st.get("category", ""),
                              detail=st.get("detail", ""), checked_at=now, next_check_at=now + wait)
            if on_topic and not self.gate_open:
                self.gate_open = True
                self._log(f"watch: live with a qualifying title ({st.get('title', '')[:80]}); capture requested "
                          f"(media capture is reported separately)")
                self._start_media_and_chat()
            elif not on_topic and self.gate_open and not (self.tab_hold and st["state"] not in ("live", "offline")):
                self.gate_open = False
                await self._stop_media_and_chat(purge=True)
                self._log(f"watch: {label}; capture stopped and buffer purged")
            await asyncio.sleep(wait)

    async def _stop_media_and_chat(self, purge: bool) -> None:
        self.media_on = self.slot_wait = False
        await self.capture.stop()
        if self.chat:
            await self.chat.stop()
            self.chat = None
        if purge:
            idx = self.engine.buffers.indexes.get(self.id)
            if idx:
                for seg in idx.all_segments():
                    if seg.pins == 0:
                        self.engine.buffers._delete(idx, seg)

    def _start_media_and_chat(self) -> None:
        limit = int(self.engine.settings.max_concurrent_recordings or 0)
        if limit and sum(1 for m in self.engine.monitors.values() if m is not self and m.media_on) >= limit:
            if not self.slot_wait:
                self._log(f"waiting for a recording slot ({limit} recordings at once)")
            self.slot_wait = True                     # housekeeping retries when a slot frees
            return
        self.slot_wait, self.media_on = False, True
        self.capture.start()
        adapter = self.s.get("chat_adapter") or self.ch["platform"]
        from .chat.registry import make_adapter
        if adapter == "replay":
            from .chat.replay import ReplayChat
            self.chat = ReplayChat(self.ch["name"], self.on_chat, options=dict(self.s.get("chat_options") or {}))
        elif adapter in ("twitch", "kick", "youtube"):
            creds = self.engine.secrets.chat_creds(self.ch["platform"]) if self.engine.secrets else {}
            opts = {**(self.s.get("chat_options") or {}),
                    "public_url": self.engine.settings.kick_webhook_url or self.engine.settings.public_url}
            if self.engine.tokens is not None:
                opts["token_source"] = self.engine.tokens
            if adapter == "twitch" and self.engine.twitch_hub is not None and "eventsub_url" not in opts:
                opts["eventsub_hub"] = self.engine.twitch_hub
            try:
                self.chat = make_adapter(adapter, self.ch["name"], self.on_chat, creds, opts)
            except Exception as e:
                self._log(f"chat adapter could not start: {e}", "error")
        if self.chat:
            self.chat.start()

    async def stop(self) -> None:
        tasks = [t for t in (self._watch_task, self._observe_task, self._official_task) if t]
        for t in tasks:
            t.cancel()
        if tasks:                                     # a watch must not restart capture after this point
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.engine.hub is not None:
            self.engine.hub.unwatch(self.ch["platform"], self.ch["name"])
        self.media_on = self.slot_wait = False
        await self.capture.stop()
        if self.chat:
            await self.chat.stop()

    def status(self) -> dict:
        idx = self.engine.buffers.index(self.id)
        if self.chat:
            chat_h = asdict(self.chat.health)
        elif self.gated and not self.gate_open:
            chat_h = asdict(ChatHealth("stopped", "waiting for the stream to be live and on topic"))
        elif self._official_task and not self.media_on:
            chat_h = asdict(ChatHealth("stopped", "waiting for the stream to be live"))
        else:
            chat_h = asdict(ChatHealth("unavailable", "no chat adapter for this platform"))
        return {"capture": asdict(self.capture.health), "chat": chat_h,
                "buffer_seconds": round(idx.buffered_seconds(), 1), "buffer_target": self.engine.settings.buffer_seconds,
                "epochs": len(idx.epochs), "detector": self.detector.snapshot(), "recent_chat": list(self.recent_chat)[-15:],
                "watch": dict(self.watch), "gate_open": self.gate_open, "official": dict(self.official),
                "status_source": self.status_source(), "slot_wait": self.slot_wait,
                "off_topic": self.fmt.topic_states.off_topic}


def public_state(st: dict | None, armed: bool) -> dict:
    """Internal reference states -> UI states: offline, waiting, recording, reconnecting, needs_action, disabled.
    needs_action always carries a readable detail and the fix."""
    if not armed:
        return {"state": "disabled", "detail": "not monitoring", "fix": ""}
    st = st or {}
    cap, watch, off = st.get("capture") or {}, st.get("watch") or {}, st.get("official") or {}
    if off.get("action_needed"):
        return {"state": "needs_action", "detail": off.get("detail") or "live status check failed",
                "fix": off["action_needed"]}
    if cap.get("action_needed") or cap.get("state") == "error":
        return {"state": "needs_action", "detail": cap.get("detail") or "capture failed",
                "fix": cap.get("action_needed") or "Check the channel URL and that FFmpeg and Streamlink are installed"}
    if cap.get("state") == "live":
        return {"state": "recording", "detail": "", "fix": ""}
    if cap.get("state") == "reconnecting":
        return {"state": "reconnecting", "detail": cap.get("detail") or "", "fix": ""}
    if off.get("state") == "offline" or watch.get("state") == "offline" or (
            cap.get("state") == "waiting_for_live" and "offline" in (cap.get("detail") or "")):
        return {"state": "offline", "detail": off.get("detail") or watch.get("detail") or cap.get("detail") or "",
                "fix": ""}
    if st.get("slot_wait"):
        return {"state": "waiting", "detail": "waiting for a free recording slot", "fix": ""}
    if watch.get("state") == st.get("off_topic", "live_off_topic"):
        return {"state": "waiting", "detail": "live, but the title is not on topic", "fix": ""}
    return {"state": "waiting", "detail": cap.get("detail") or watch.get("detail") or off.get("detail") or "",
            "fix": ""}


class Maintenance:
    """Reference admission + disk producers (the rest of storage maintenance belongs to the ops subsystem)."""

    def __init__(self, engine: Engine):
        self.e, self.s = engine, engine.settings
        self.ops = None                                # ops storage maintenance (live_clipper.maintenance), if running
        self.paused: set[int] = set()
        self.refused = 0
        self.last_refusal_log: dict[int, float] = {}
        self._free: tuple[float, int] | None = None    # (when, bytes): the last disk measurement

    FREE_FRESH_S = 1.0                                 # housekeeping re-measures every 0.5 s off the event loop

    def thresholds(self) -> tuple[int, int]:
        lo = max(0, int(self.s.min_free_mb)) * MB
        return lo, min(lo, max(0, int(self.s.critical_free_mb)) * MB)

    def free_bytes(self) -> int:
        """Free bytes under data_dir; a measurement under FREE_FRESH_S old is reused (disk_usage can block)."""
        last = self._free
        if last is not None and time.monotonic() - last[0] < self.FREE_FRESH_S:
            return last[1]
        free = shutil.disk_usage(self.s.data_dir).free
        self._free = (time.monotonic(), free)
        return free

    def ring_free_bytes(self) -> int:
        return self.free_bytes()                       # the ring always lives under data_dir here

    def admit_clip(self) -> str:
        lo, crit = self.thresholds()
        free = self.free_bytes()
        if free < lo:
            return f"free disk {free // MB} MB is below {lo // MB} MB"
        if self.paused:
            return (f"captures are paused for disk space (capture ring volume: {self.ring_free_bytes() // MB} MB "
                    f"free, restart at {lo // MB} MB)")
        active = len(self.e.clip_tasks)
        if active >= max(1, self.s.max_active_clips):
            return f"{active} clips already in progress (limit {self.s.max_active_clips})"
        if self.ops is not None:                       # the storage service: disk cap, its pause, active clips
            ok, why = self.ops.admit_clip(active)
            if not ok:
                return why or "refused by storage maintenance"
        return ""

    def ops_paused(self) -> bool:
        return bool(self.ops is not None and getattr(self.ops, "captures_paused", False))

    def note_refusal(self, channel_id: int, reason: str) -> None:
        self.refused += 1
        now = time.time()
        if now - self.last_refusal_log.get(channel_id, 0) > 30:
            self.last_refusal_log[channel_id] = now
            self.e.store.log(f"backpressure: trigger not clipped ({reason}); {self.refused} refused so far", "warn",
                             channel_id=channel_id)

    @staticmethod
    def _capturing(m) -> bool:
        t = getattr(m.capture, "_task", None)
        return bool(t is not None and not t.done())

    async def enforce_producers(self) -> None:
        """Stop captures before the disk is exhausted; restart them once space is back (hysteresis)."""
        self._free = None
        free = await asyncio.to_thread(self.ring_free_bytes)  # a slow disk must not stall the event loop
        lo, crit = self.thresholds()
        if free < crit or self.ops_paused():
            for m in list(self.e.monitors.values()):
                if self._capturing(m):
                    await m.capture.stop()
                    m.capture.health.detail = "paused: low disk space (restarts automatically)"
                    if m.id not in self.paused:
                        self.paused.add(m.id)
                        self.e.store.log(f"disk: {free // MB} MB free (< {crit // MB} MB); capture paused, restarts "
                                         f"at {lo // MB} MB", "warn", channel_id=m.id)
        elif free >= lo and self.paused and not self.ops_paused():
            for cid in sorted(self.paused):
                self.paused.discard(cid)
                m = self.e.monitors.get(cid)
                if m and m.media_on and not m.slot_wait:   # not stopped meanwhile (offline, gate, slot)
                    m.capture.start()
                    self.e.store.log(f"disk: {free // MB} MB free; capture resumed", channel_id=cid)


class Engine:
    def __init__(self, settings: Settings, store: Store, secrets=None, hub=None, edit=None):
        from .buffer import BufferManager  # edit subsystem (reference API)
        from .edit_service import EditEngine
        self.settings = settings
        self.store = store
        self.secrets = secrets
        self.hub = hub
        settings.ensure_dirs()
        self.buffers = BufferManager(settings.buffer_dir, settings.buffer_seconds,
                                     settings.disk_ceiling_mb * 1024 * 1024,
                                     log=lambda m, lvl="info": store.log(m, lvl))
        self.monitors: dict[int, Monitor] = {}
        self.arm_errors: dict[int, str] = {}           # armed channels whose monitor could not start
        self.tasks: set[asyncio.Task] = set()
        # cutting, rendering and the hand-off to publishing; standalone (tests) when no edit service runs
        self.edit = edit or EditEngine(None, store.db, store=store, clips_dir=settings.clips_dir,
                                       models_dir=settings.data_dir / "models")
        self.selection = self.edit.selection
        self.probes: dict = {}
        from .chat_tokens import TokenRefresher
        self.tokens = TokenRefresher(secrets) if secrets is not None else None
        from .chat.twitch_hub import TwitchEventSubHub
        self.twitch_hub = (TwitchEventSubHub(lambda: secrets.chat_creds("twitch"), token_source=self.tokens)
                           if secrets is not None else None)
        self.clip_tasks: dict[int, asyncio.Task] = {}
        self.recovery: collections.deque[int] = collections.deque()
        self.recovering: set[int] = set()
        self.maintenance = Maintenance(self)
        self.buffers.force_release_ok = lambda: self.maintenance.ring_free_bytes() < self.maintenance.thresholds()[1]
        self.listeners: list[Callable[[str, dict], None]] = []
        self._bg: list[asyncio.Task] = []

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self.buffers.startup_sweep()                  # a ring from a previous process has no valid time mapping
        self.buffers.start_sweeper()
        self._recover()
        self._bg.append(asyncio.create_task(self._housekeeping(), name="lc-housekeeping"))
        for ch in self.store.channels():
            if ch["armed"] and (why := refusal(ch)):  # retired / VOD-only: never re-armed after a restart
                self.store.update("channels", ch["id"], armed=0)
                kind = "VOD-only: not re-armed (no live capture)" if "VOD-only" in why else \
                    "retired from tracking: not re-armed"
                self.store.log(f"{ch['platform']}/{ch['name']} is {kind}", "warn", channel_id=ch["id"])
            elif ch["armed"]:
                try:                                  # one broken channel must not keep the others unmonitored
                    self.arm(ch["id"])
                except Exception as e:
                    self.arm_errors[ch["id"]] = f"monitoring could not start: {type(e).__name__}: {e}"[:300]
                    self.store.log(self.arm_errors[ch["id"]], "error", channel_id=ch["id"])

    async def stop(self) -> None:
        for t in self._bg:                            # no restarts or new clips while monitors stop
            t.cancel()
        if self._bg:
            await asyncio.wait(self._bg, timeout=10)
        for m in list(self.monitors.values()):
            await m.stop()
        if self.twitch_hub is not None:
            await self.twitch_hub.close()
        if self.hub is not None:
            await self.hub.close()
        pending = self._bg + list(self.tasks)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=10)
        self.buffers.stop_sweeper()

    def _recover(self) -> None:
        """After a crash/restart: never duplicate a clip; resume what can be resumed."""
        self.edit.recover()                           # edit reservations of a dead process
        self._reconcile_orphan_renders()
        self._resume_handoffs()
        queued = 0
        for c in self.store.q("SELECT * FROM clips WHERE status NOT IN ('ready','failed','discarded','expired') "
                              "ORDER BY id"):
            if self._resumable(c):
                if c["id"] not in self.recovery:
                    self.recovery.append(c["id"])
                    queued += 1
            elif (c["trigger"] or {}).get("input") == "vod_import":
                self._fail(c["id"], "worker restarted before the VOD cut finished; Import / Resume re-cuts it")
            else:
                self._fail(c["id"], "worker restarted before the cut finished; buffer was lost")
        if queued:
            self.store.log(f"{queued} unfinished clips queued for recovery (at most "
                           f"{self.recovery_slots()} at a time; live clips keep priority)")

    def _reconcile_orphan_renders(self) -> int:
        n = 0
        for c in self.store.q("SELECT id, captions, files FROM clips WHERE status='ready'"):
            caps, files, d = c["captions"] or {}, c["files"] or {}, self.clip_dir(c["id"])
            for lang in ("en", "es"):
                if (caps.get(lang) or {}).get("final_status") != "rendering":
                    continue
                f = (files.get("final") or {}).get(lang)
                if f and (d / f).exists():
                    patch = {"final_status": "ready"}
                else:
                    patch = {"final_status": "failed", "final_error": "render interrupted by a restart",
                             "final_failed_at": time.time()}
                self.store.merge_json("clips", c["id"], "captions", {lang: patch})
                self.store.log(f"clip {c['id']}: {lang} final left rendering by a restart -> {patch['final_status']}",
                               clip_id=c["id"])
                n += 1
        return n

    def _resume_handoffs(self) -> int:
        """Ready finals that never reached publishing (killed between saving "ready" and the hand-off, or a failed
        hand-off): no selection decision for the language, or a qualified one without a delivery. The hand-off is
        idempotent (publishing keeps one delivery per clip, destination and language)."""
        n = 0
        for c in self.store.q("SELECT id, trigger, captions, files FROM clips WHERE status='ready'"):
            sel = (c["trigger"] or {}).get("selection") or {}
            for lang in (c["files"] or {}).get("final") or {}:
                if ((c["captions"] or {}).get(lang) or {}).get("final_status") != "ready":
                    continue
                d = sel.get(lang)
                if d and (d.get("decision") != "qualified" or self.store.q1(
                        "SELECT id FROM deliveries WHERE clip_id=? AND language=?", c["id"], lang)):
                    continue
                try:
                    sent = self.edit.publisher.on_language_ready(c["id"], lang)
                except Exception as e:
                    self.store.log(f"clip {c['id']}: {lang} hand-off to publishing failed again: {e}", "error",
                                   clip_id=c["id"])
                    continue
                self.store.log(f"clip {c['id']}: {lang} final handed to selection/publishing after a restart"
                               + ("" if sent else " (not published)"), clip_id=c["id"])
                n += 1
        return n

    def _resumable(self, c: dict) -> bool:
        d = self.clip_dir(c["id"])
        return (d / "preview.mp4").exists() and (d / "source.ts").exists() and c["status"] != "cutting"

    def recovery_slots(self) -> int:
        return max(1, int(self.settings.max_active_clips) // 3)

    def pump_recovery(self) -> int:
        """Start recovered clips while recovery uses at most recovery_slots(), one clip slot stays free for live
        triggers and free disk is above the backpressure threshold. Idempotent."""
        limit = max(1, int(self.settings.max_active_clips))
        reserve = 1 if limit >= 2 else 0
        started = 0
        while self.recovery:
            running = sum(1 for cid in self.recovering if cid in self.clip_tasks)
            if running >= self.recovery_slots() or len(self.clip_tasks) >= limit - reserve:
                break
            if self.maintenance.free_bytes() < self.maintenance.thresholds()[0]:
                break
            cid = self.recovery.popleft()
            c = self.store.clip(cid)
            if cid in self.clip_tasks or not c or c["status"] in TERMINAL:
                continue
            if not self._resumable(c):
                self._fail(cid, "unfinished clip lost its preview/source before it could be resumed")
                continue
            self.store.log(f"resuming clip {cid} after restart ({len(self.recovery)} more queued)", clip_id=cid)
            self.recovering.add(cid)
            self.clip_tasks[cid] = self._spawn(self._post_cut(cid))

            def done(_t, c=cid):
                self.clip_tasks.pop(c, None)
                self.recovering.discard(c)
            self.clip_tasks[cid].add_done_callback(done)
            started += 1
        return started

    async def _housekeeping(self) -> None:
        while True:
            try:
                self.buffers.enforce()
                await self.maintenance.enforce_producers()
                self.pump_recovery()
                now = time.time()
                for m in list(self.monitors.values()):
                    if m.slot_wait:
                        m._start_media_and_chat()
                    for trig in m.detector.tick(now):
                        self.on_trigger(m, trig)
            except Exception as e:
                log.exception("housekeeping pass failed; continuing")
                try:
                    self.store.log(f"housekeeping error: {e}", "error")
                except Exception:
                    log.exception("housekeeping error could not be stored")
            await asyncio.sleep(0.5)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def emit(self, kind: str, data: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn(kind, data)
            except Exception:
                log.exception("engine listener failed")

    # ---- channels --------------------------------------------------------
    def arm(self, channel_id: int) -> None:
        ch = self.store.channel(channel_id)
        if not ch:
            raise KeyError(channel_id)
        if why := refusal(ch):
            self.store.update("channels", channel_id, armed=0)
            raise LiveRefused(why)
        if channel_id not in self.monitors:
            m = Monitor(self, ch)                     # built (and started) before "armed" is saved
            self.monitors[channel_id] = m
            try:
                m.start()
            except Exception:
                self.monitors.pop(channel_id, None)
                raise
            self.store.log(f"monitoring {ch['platform']}/{ch['name']}", channel_id=channel_id)
        self.arm_errors.pop(channel_id, None)
        self.store.update("channels", channel_id, armed=1)

    async def disarm(self, channel_id: int) -> None:
        self.store.update("channels", channel_id, armed=0)
        self.arm_errors.pop(channel_id, None)
        m = self.monitors.pop(channel_id, None)
        if m:
            await m.stop()
        idx = self.buffers.indexes.get(channel_id)
        if idx:
            for seg in idx.all_segments():
                if seg.pins == 0:
                    self.buffers._delete(idx, seg)

    def clip_dir(self, clip_id: int) -> Path:
        return self.edit.clip_dir(clip_id)

    # ---- triggers --------------------------------------------------------
    def channel_format(self, channel_id: int):
        """The selection format in effect for this channel (its format, then rules that match the channel)."""
        from . import formats
        return formats.effective_format(self.selection.resolve(channel_id))

    def window_for(self, m: Monitor, action_time: float, values: dict | None = None) -> tuple[float, float]:
        """The cut window from the effective selection values (`window.pre` / `window.post`)."""
        from . import moments
        v = values if values is not None else self.selection.resolve(m.id)["values"]
        return moments.window_for(action_time, v["window.pre"], v["window.post"])

    def clip_now(self, channel_id: int, delay: float = 3.0) -> int | None:
        m = self.monitors.get(channel_id)
        if not m:
            raise KeyError("channel not armed")
        if not m.gate_open:
            raise KeyError("the topic watch is closed (offline or not on topic); nothing is being captured")
        why = self.maintenance.admit_clip()
        if why:
            raise Backpressure(f"not clipped to protect the host: {why}")
        now = time.time()
        trig = Trigger(channel_id=channel_id, reason="Manual clip by operator", kind="manual",
                       burst_start=now, action_time=now - delay, delay=delay, detected_at=now)
        return self.on_trigger(m, trig)

    LATE_EVIDENCE_S = 420

    def late_topic_evidence(self, m: Monitor) -> int:
        """A clip triggered while its source had NO answer yet receives the evidence of the SAME source's next live
        answer, within LATE_EVIDENCE_S, with no broadcast boundary in between, never overwriting an answer."""
        ev = m.topic_evidence()
        ts = m.fmt.topic_states
        if ev.get("state") not in (ts.title, ts.category, ts.off_topic) or getattr(m, "gated", False):
            return 0
        at = float(ev.get("observed_at") or time.time())
        floor = max(at - self.LATE_EVIDENCE_S, float(getattr(m, "boundary_at", 0) or 0),
                    float(getattr(m, "observe_started_at", 0) or 0))
        epoch = m.capture_epoch()
        n = 0
        for c in self.store.q("SELECT id, trigger, created_at FROM clips WHERE channel_id=? AND created_at>? AND "
                              "created_at<=?", m.id, floor, at):
            trig = c["trigger"] or {}
            topic = trig.get("topic") or {}
            if topic.get("state") != ts.unverified or trig.get("input") == "vod_import" or (trig.get("review") or {}):
                continue
            if topic.get("kind") not in ("no_lookup_yet", "lookup_failed") or topic.get("capture_epoch") != epoch:
                continue
            late = round(at - float(c["created_at"]), 1)
            self.store.merge_json("clips", c["id"], "trigger", {"topic": {
                "state": ev["state"], "title": ev.get("title", ""), "category": ev.get("category", ""),
                "observed_at": ev.get("observed_at"), "why": "", "kind": "late",
                "basis": f"first live status answer {late} s after the trigger, same broadcast and capture",
                "unverified_because": str(topic.get("why") or "")[:200]}})
            self.store.log(f"clip {c['id']}: game evidence {ev['state']} from the source's status answer "
                           f"{late} s after the trigger", channel_id=m.id, clip_id=c["id"])
            n += 1
        return n

    def on_trigger(self, m: Monitor, trig: Trigger) -> int | None:
        if not m.gate_open:
            return None
        why = self.maintenance.admit_clip()
        if why:
            self.maintenance.note_refusal(m.id, why)
            return None
        from . import moments
        ev = m.topic_evidence()
        res = self.selection.resolve(m.id, kind=trig.kind, topic=ev)
        v = res["values"]
        start, end = self.window_for(m, trig.action_time, v)
        ids = self.simulcast_channels(m)
        with self.store._lock:                        # read-modify-write of the trigger, like merge_json
            rows = self.store.q(f"SELECT id, channel_id, action_time, window_end, trigger FROM clips WHERE "
                                f"channel_id IN ({','.join('?' * len(ids))}) AND action_time>?", *ids,
                                trig.action_time - 120)
            c = moments.merge_target(rows, trig.kind, trig.action_time, start, end)
            if c is not None:
                merged = c["trigger"].setdefault("merged", [])
                merged.append({"reason": trig.reason, "action_time": trig.action_time, "channel_id": m.id})
                self.store.update("clips", c["id"], trigger=c["trigger"])
        if c is not None:
            via = "" if c["channel_id"] == m.id else f" (same moment on simulcast channel {c['channel_id']})"
            self.store.log(f"trigger merged into clip {c['id']}{via}: {trig.reason}", channel_id=m.id,
                           clip_id=c["id"])
            m.detector.stats["merged"] += 1
            return None
        key = moments.dedupe_key(m.id, trig.kind, trig.action_time)
        from .render import channel_layout
        clip_id = self.edit.create_clip(
            m.id, start, end, trig.action_time, kind=trig.kind, trigger=trig.to_dict(),
            layout=channel_layout(m.s.get("layout")), preset_id=self.selection.preset_id(v, self.edit.presets),
            overrides={"window": {"pre_roll_s": v["window.pre"], "post_roll_s": v["window.post"]}},
            dedupe_key=key, delay=trig.delay)
        if clip_id is None:
            return None
        moments.moment_id(self.store, clip_id)
        self.store.merge_json("clips", clip_id, "trigger", {"format_id": res["format_id"]})
        plat = m.ch.get("platform") or ""
        if plat not in ("twitch", "kick") and not m.gated and not m._official_task and "why" in ev:
            ev["why"] = f"no public title/category lookup exists for {plat or 'this kind of'} sources"
        elif "why" in ev and not m.gated and int(self.settings.topic_observe_seconds) <= 0:
            ev["why"] = "title/category observation is switched off (topic_observe_seconds=0)"
        self.store.merge_json("clips", clip_id, "trigger", {"topic": ev})
        t = {"trigger": trig.detected_at or time.time(), "moment": trig.action_time, "window_end": end}
        if m.capture.health.source_available_at:
            t["source_available"] = m.capture.health.source_available_at
        if trig.first_chat_received_at:
            t["chat_first_received"] = trig.first_chat_received_at
        self.store.update("clips", clip_id, timings=t)
        self.store.log(f"clip {clip_id}: {trig.reason}", channel_id=m.id, clip_id=clip_id)
        self.emit("clip", {"id": clip_id})
        self.clip_tasks[clip_id] = self._spawn(self._process(m, clip_id, start, end, trig.action_time))
        self.clip_tasks[clip_id].add_done_callback(lambda _t, c=clip_id: self.clip_tasks.pop(c, None))
        return clip_id

    def simulcast_channels(self, m: Monitor) -> list[int]:
        from . import moments
        return moments.simulcast_channels(self.store, m.id)

    # ---- pipeline (edit subsystem) ------------------------------------------
    def render_lock(self, clip_id: int) -> asyncio.Lock:
        return self.edit.render_lock(clip_id)

    def busy(self, clip_id: int) -> bool:
        return clip_id in self.clip_tasks or self.edit.busy(clip_id) or self.render_lock(clip_id).locked()

    def _fail(self, clip_id: int, error: str) -> None:
        """Failed clips keep their row (for the user) but free their media."""
        self.edit._fail(clip_id, error)

    async def _process(self, m: Monitor, clip_id: int, start: float, end: float, action: float | None = None) -> None:
        """Cut, preview, captions, title, editorial check, finals and the hand-off to publishing (edit + select)."""
        await self.edit.process(self.buffers, m.id, clip_id, start, end, action)

    async def _post_cut(self, clip_id: int) -> None:
        """Post-cut work of a clip resumed after a restart."""
        await self.edit.resume(clip_id)
