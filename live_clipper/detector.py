"""Explainable chat-spike detector.

Signal: per-user-capped message count in a rolling 10 s window (each chatter counts at most
PER_USER_CAP messages per window, so one spammer cannot fake a spike), plus unique chatters.
Baseline: median of that signal over the recent past (default 5 min), excluding burst time,
floored so quiet channels need real absolute activity. Fires when
    rate >= max(baseline * ratio, baseline + min_abs)  and  unique >= min_unique
Hysteresis keeps a sustained burst as one moment; a burst that resumes within `merge_gap`
seconds of ending is the same moment. Event time = platform send time mapped to the local
clock (skew estimated as min(received - sent)), falling back to receipt time. Duplicates
(same msg_id, e.g. re-delivered after reconnect) are ignored.
"""
from __future__ import annotations

import math
import re
import statistics
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass

from .events import ChatEvent, Trigger

SENSITIVITY = {   # ratio, min_unique, min_abs
    "low": (3.0, 8, 10.0),
    "medium": (2.2, 5, 6.0),
    "high": (1.6, 3, 4.0),
}
PER_USER_CAP = 2
WORD = re.compile(r"[\w']+", re.UNICODE)


def norm_text(t: str) -> str:
    t = t.lower().strip()
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)          # loooool -> lool
    return " ".join(WORD.findall(t))[:60]


@dataclass
class DetectorConfig:
    sensitivity: str = "medium"
    window: float = 10.0
    reaction_delay: float = 4.0
    baseline_seconds: float = 300.0
    baseline_floor: float = 3.0        # msgs/10s assumed minimum baseline (quiet channels)
    merge_gap: float = 8.0             # calm needed before a burst is over
    distinct_gap: float = 15.0         # a re-ignition this long after the previous onset is a new moment
    warmup: float = 20.0               # seconds of chat history needed before any trigger
    max_burst: float = 90.0            # a "burst" lasting longer than this is the channel's new normal
    ratio: float | None = None      # explicit overrides of the sensitivity preset
    min_unique: int | None = None
    min_abs: float | None = None
    per_user_cap: int = PER_USER_CAP   # messages per chatter counted in one window
    presets: dict | None = None     # sensitivity name -> (ratio, min_unique, min_abs); None = SENSITIVITY

    def params(self) -> tuple[float, int, float]:
        presets = self.presets or SENSITIVITY
        r, u, a = presets.get(self.sensitivity, presets.get("medium", SENSITIVITY["medium"]))
        return (self.ratio or r, self.min_unique or u, self.min_abs or a)

    @classmethod
    def from_format(cls, fmt=None, sensitivity: str | None = None,
                    reaction_delay: float | None = None) -> DetectorConfig:
        """The selection format's detector settings; a channel's sensitivity / reaction_delay
        override them."""
        from . import formats
        d = (fmt or formats.active()).detector
        return cls(sensitivity=sensitivity or d.sensitivity, window=d.window,
                   reaction_delay=d.reaction_delay if reaction_delay is None else reaction_delay,
                   baseline_seconds=d.baseline_seconds, baseline_floor=d.baseline_floor,
                   merge_gap=d.merge_gap,
                   distinct_gap=d.distinct_gap, warmup=d.warmup, max_burst=d.max_burst, ratio=d.ratio,
                   min_unique=d.min_unique, min_abs=d.min_abs, per_user_cap=d.per_user_cap,
                   presets={k: tuple(v) for k, v in d.presets.items()})


@dataclass
class _Burst:
    start: float
    peak: float = 0.0
    peak_unique: int = 0
    peak_ratio: float = 0.0
    last_hot: float = 0.0
    cold_since: float | None = None
    cooled: bool = False               # chat fell back near baseline for >= 2 s since the last peak
    first_received: float | None = None
    trigger: Trigger | None = None


class SpikeDetector:
    def __init__(self, channel_id: int, config: DetectorConfig | None = None):
        self.channel_id = channel_id
        self.cfg = config or DetectorConfig()
        self.events: deque[tuple[float, str, str, float]] = deque()   # (t, user, normtext, received)
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.skew: float | None = None
        self.samples: deque[tuple[float, float]] = deque()            # (t, rate) baseline samples
        self.burst: _Burst | None = None
        self.last_sample_t = 0.0
        self.now = 0.0
        self.first_t: float | None = None
        self.stats = {"messages": 0, "duplicates": 0, "triggers": 0, "merged": 0}
        self.history: deque[tuple[float, float, int]] = deque(maxlen=600)   # (t, rate, unique) for UI

    # ---- input -----------------------------------------------------------
    MAX_SKEW = 10.0     # larger receive-minus-send differences are late delivery (backlog), not clock skew

    def event_time(self, ev: ChatEvent) -> float:
        if ev.sent_at is None:
            return ev.received_at
        d = ev.received_at - ev.sent_at
        if abs(d) <= self.MAX_SKEW and (self.skew is None or d < self.skew):
            self.skew = d
        return ev.sent_at + (self.skew or 0.0)

    def feed(self, ev: ChatEvent) -> list[Trigger]:
        if ev.msg_id in self.seen:
            self.stats["duplicates"] += 1
            return []
        self.seen[ev.msg_id] = None
        if len(self.seen) > 20000:
            self.seen.popitem(last=False)
        self.stats["messages"] += 1
        t = self.event_time(ev)
        if self.first_t is None:
            self.first_t = t
        src = ev.ts_source or ("platform" if ev.sent_at is not None else "receipt")
        if src != "receipt" and ev.sent_at is not None and ev.received_at - ev.sent_at > self.MAX_SKEW:
            src += "+late"
        self.events.append((t, ev.user_id, norm_text(ev.text), ev.received_at, src))
        if len(self.events) > 1 and t < self.events[-2][0]:      # late message: keep deque sorted
            self.events = deque(sorted(self.events, key=lambda e: e[0]))
        return self.tick(max(self.now, t))

    # ---- evaluation ------------------------------------------------------
    def window_stats(self, now: float) -> tuple[float, int, Counter, list]:
        lo = now - self.cfg.window
        per_user: Counter = Counter()
        texts: Counter = Counter()
        inwin = []
        for e in self.events:
            if lo < e[0] <= now:
                per_user[e[1]] += 1
                texts[e[2]] += 1
                inwin.append(e)
        rate = float(sum(min(c, self.cfg.per_user_cap) for c in per_user.values()))
        return rate, len(per_user), texts, inwin

    def baseline(self, now: float) -> float:
        vals = [r for t, r in self.samples if t > now - self.cfg.baseline_seconds]
        if len(vals) >= 10:
            b = statistics.median(vals)
        elif vals:
            b = max(vals)            # warming up: be conservative
        else:
            b = 0.0
        return max(b, self.cfg.baseline_floor)

    def warming_up(self) -> bool:
        # no learned baseline yet (fresh start / worker restart): only an overwhelming burst may fire
        recent = sum(1 for t, _ in self.samples if t > self.now - self.cfg.baseline_seconds)
        return self.first_t is None or self.now - self.first_t < self.cfg.warmup or recent < 10

    def threshold(self, base: float) -> float:
        ratio, _, min_abs = self.cfg.params()
        return max(base * ratio, base + min_abs)

    def _onset(self, inwin: list, base: float, now: float) -> float:
        """Earliest second of the current elevated run, scanning the window per second."""
        per_sec: dict[int, set] = {}
        for t, u, *_ in inwin:
            per_sec.setdefault(int(math.floor(t)), set()).add(u)
        hot_level = max(1.0, 2.0 * base / self.cfg.window)
        sec = int(math.floor(now))
        onset = now
        cold = 0
        while sec > now - self.cfg.window - 1:
            n = len(per_sec.get(sec, ()))
            if n >= hot_level:
                onset, cold = float(sec), 0
            else:
                cold += 1
                if cold >= 2 and onset < now:
                    break
            sec -= 1
        return onset

    def tick(self, now: float | None = None) -> list[Trigger]:
        now = now if now is not None else time.time()
        self.now = max(self.now, now)
        while self.events and self.events[0][0] < self.now - max(self.cfg.window * 3, 60):
            self.events.popleft()
        rate, uniq, texts, inwin = self.window_stats(self.now)
        base = self.baseline(self.now)
        thr = self.threshold(base)
        _, min_unique, _ = self.cfg.params()
        out: list[Trigger] = []
        hot = rate >= thr and uniq >= min_unique
        if self.warming_up():
            # no learned baseline yet: a busy channel's ordinary chat would look like a spike (seen live on a
            # ~200 msgs/10 s channel). Never trigger until the baseline exists; "Clip now" still works.
            hot = False
        long_burst = self.burst is not None and self.now - self.burst.start > self.cfg.max_burst
        if self.now - self.last_sample_t >= 1.0:
            self.last_sample_t = self.now
            self.history.append((self.now, rate, uniq))
            if long_burst or (not hot and (self.burst is None or rate < 0.6 * thr)):
                self.samples.append((self.now, rate))
                while self.samples and self.samples[0][0] < self.now - self.cfg.baseline_seconds:
                    self.samples.popleft()
        b = self.burst
        if b is not None and b.cold_since is not None and self.now - b.cold_since >= 2.0:
            b.cooled = True
        if hot and b is not None and b.cooled:
            onset = self._onset(inwin, base, self.now)
            if onset - b.start >= self.cfg.distinct_gap:     # chat cooled down, then a new reaction
                if b.trigger:
                    b.trigger.burst_end = b.last_hot
                b = self.burst = None
        if hot:
            if b is None:
                onset = self._onset(inwin, base, self.now)
                b = self.burst = _Burst(start=onset)
                b.first_received = min((e[3] for e in inwin if e[0] >= onset), default=None)
                srcs = Counter(e[4].split("+")[0] for e in inwin)
                late = sum(1 for e in inwin if e[4].endswith("+late"))
                basis = {"platform": "platform timestamps (skew-corrected)",
                         "fixture": "fixture-synthesized timestamps (replay; not a platform clock)",
                         "receipt": "INFERRED from worker receipt time (no platform timestamp)"}
                parts = [f"{basis.get(k, k)} {v}/{len(inwin)}" for k, v in srcs.most_common()]
                if late:
                    parts.append(f"{late} delivered late, counted at send time")
                top = [f"{t} ×{c}" if c > 1 else t for t, c in texts.most_common(4) if t]
                ratio = rate / base
                trig = Trigger(
                    channel_id=self.channel_id, kind="chat_spike",
                    reason=(f"Chat spike: {rate:.0f} msgs/10s from {uniq} chatters "
                            f"({ratio:.1f}× baseline {base:.1f})"
                            + (f"; top: {', '.join(top[:3])}" if top else "")),
                    burst_start=onset, action_time=onset - self.cfg.reaction_delay,
                    delay=self.cfg.reaction_delay, score=ratio, peak_rate=rate, unique_users=uniq,
                    baseline=base, sample_messages=[t for t, _ in texts.most_common(12) if t],
                    first_chat_received_at=b.first_received, detected_at=time.time(),
                    chat_time_basis="; ".join(parts))
                b.trigger = trig
                self.stats["triggers"] += 1
                out.append(trig)
            if self.now - b.last_hot > 2.0:
                self.stats["merged"] += 1           # burst resumed within merge_gap: same moment
            b.last_hot, b.cold_since, b.cooled = self.now, None, False
            if rate > b.peak:
                b.peak, b.peak_unique, b.peak_ratio = rate, uniq, rate / base
                if b.trigger:
                    b.trigger.peak_rate, b.trigger.score, b.trigger.unique_users = rate, rate / base, uniq
        elif b is not None:
            # hysteresis: burst continues while clearly above baseline; ends after merge_gap of calm, or once
            # it
            # has lasted max_burst (then it is the channel's new level and the baseline must catch up)
            if long_burst:
                if b.trigger:
                    b.trigger.burst_end = b.last_hot
                self.burst = None
            elif rate >= max(0.6 * thr, base * 1.5):
                b.last_hot, b.cold_since = self.now, None
            elif b.cold_since is None:
                b.cold_since = self.now
            elif self.now - b.last_hot > self.cfg.merge_gap:
                if b.trigger:
                    b.trigger.burst_end = b.last_hot
                self.burst = None
        return out

    def snapshot(self) -> dict:
        rate, uniq, texts, _ = self.window_stats(self.now) if self.now else (0.0, 0, Counter(), [])
        base = self.baseline(self.now) if self.now else self.cfg.baseline_floor
        return {"rate": rate, "unique": uniq, "baseline": base, "threshold": self.threshold(base),
                "active_burst": self.burst is not None, **self.stats,
                "history": [(round(t, 1), r, u) for t, r, u in list(self.history)[-120:]]}
