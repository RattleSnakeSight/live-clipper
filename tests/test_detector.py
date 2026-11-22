"""Detector behaviour on replayed event patterns (pure, deterministic, no I/O)."""
import random

from live_clipper.detector import DetectorConfig, SpikeDetector
from live_clipper.events import ChatEvent

T0 = 1_000_000.0


def ev(t, user, text="hi", mid=None, delay=0.2):
    return ChatEvent("replay", "c", mid or f"{user}-{t:.3f}-{text}", user, user, text,
                     received_at=T0 + t + delay,
                     sent_at=T0 + t)


def chatter(dur, rate=1.0, users=14, seed=1, start=0.0):
    r = random.Random(seed)
    t, out = start, []
    while t < start + dur:
        out.append(ev(t, f"u{r.randrange(users)}", r.choice(["hi", "lol", "nice car", "gg", "pog"])))
        t += r.uniform(0.5, 1.5) / rate
    return out


def burst(at, n_users=30, per_user=2, spread=4.0, seed=2, prefix="fan"):
    r = random.Random(seed)
    return [ev(at + r.uniform(0, spread), f"{prefix}{u}", r.choice(["LMAO", "KEKW", "no way", "WASTED"]))
            for u in range(n_users) for _ in range(per_user)]


def run(events, end=None, cfg=None):
    d = SpikeDetector(1, cfg or DetectorConfig())
    trig = []
    events = sorted(events, key=lambda e: e.received_at)
    end = end or (events[-1].received_at - T0 + 15)
    i, t = 0, 0.0
    while t <= end:                       # tick every 0.25 s, feed events as they are received
        while i < len(events) and events[i].received_at <= T0 + t:
            trig += d.feed(events[i])
            i += 1
        trig += d.tick(T0 + t)
        t += 0.25
    return d, trig


def test_onset_second_at_a_whole_second_boundary():
    """The end-to-end fixture's spike when t0 + spike sits next to a whole second (seen on CI: t0 =
    ...455.000064, spike 28 s, onset ...482.0). The skew correction (an early timer wake, or delivery delay)
    can move the first spike message into the neighbouring second; burst_bounds() accepts those onsets."""
    import math

    from e2e_util import burst_bounds, chat_rows

    spike = 28
    cases = [  # (chat start, receive delay, expected onset second)
        (1_795_387_455.000064, 0.2, math.floor(1_795_387_455.000064 + spike)),
        (1_795_387_455.000064, -0.0005, math.floor(1_795_387_455.000064 + spike) - 1),  # the CI case
        (1_795_387_455.9, 0.2, math.floor(1_795_387_455.9 + spike) + 1),
        (1_795_387_455.5, -0.0005, math.floor(1_795_387_455.5 + spike)),  # far from a boundary
    ]
    for t0, delay, onset in cases:
        evs = [ChatEvent("replay", "c", str(i), r["user"], r["user"], r["text"],
                         received_at=t0 + r["t"] + delay, sent_at=t0 + r["t"])
               for i, r in enumerate(chat_rows(spike))]
        d = SpikeDetector(1, DetectorConfig())
        d.samples.extend((t0 - 30 + s, 0.3) for s in range(30))  # a learned quiet baseline, as after warm-up
        d.first_t = t0 - 60
        trig, t = [], t0 - 30.0
        while t <= t0 + spike + 8:
            trig += [x for e in evs if t - 0.25 < e.received_at <= t for x in d.feed(e)] + d.tick(t)
            t += 0.25
        assert len(trig) == 1 and trig[0].burst_start == onset, (t0, delay, [x.burst_start for x in trig])
        lo, hi = burst_bounds(t0, spike)
        assert lo <= trig[0].burst_start <= hi
    assert cases[1][0] + spike - 1 > cases[1][2]  # the old lower bound refused the CI case
    t0 = cases[3][0]  # away from a boundary the second before is not accepted
    assert burst_bounds(t0, spike)[0] == math.floor(t0 + spike) > math.floor(t0 + spike) - 1


def test_ordinary_chatter_no_trigger():
    d, trig = run(chatter(300))
    assert trig == [], [t.reason for t in trig]


def test_quiet_channel_small_absolute_bump_no_trigger():
    # 1 msg / 20 s baseline, then 4 people say something within 10 s: not a moment
    evs = [ev(t, f"u{t % 3}") for t in range(0, 300, 20)] + [ev(200 + i, f"x{i}") for i in range(4)]
    _, trig = run(evs)
    assert trig == []


def test_spike_triggers_once_with_action_before_burst():
    evs = chatter(120) + burst(80)
    d, trig = run(evs)
    assert len(trig) == 1, [t.reason for t in trig]
    t = trig[0]
    assert 79 <= t.burst_start - T0 <= 82, t.burst_start - T0
    assert abs((t.burst_start - t.action_time) - 4.0) < 1e-6
    assert "Chat spike" in t.reason and t.unique_users >= 5


def test_single_spammer_does_not_trigger():
    spam = [ev(60 + i * 0.2, "spammer", "BUY FOLLOWERS", mid=f"s{i}") for i in range(100)]
    _, trig = run(chatter(120) + spam)
    assert trig == []


def test_delayed_duplicates_are_ignored():
    base = chatter(120)
    dups = [ChatEvent(e.platform, e.channel, e.msg_id, e.user_id, e.user_name, e.text,
                      received_at=e.received_at + 30, sent_at=e.sent_at) for e in base[40:100]]
    d, trig = run(base + dups)
    assert trig == [] and d.stats["duplicates"] == 60


def test_late_delivery_counts_at_send_time():
    # burst messages delivered 3 s late (reconnect backlog): onset still located at send time
    evs = chatter(120) + [ChatEvent(e.platform, e.channel, e.msg_id, e.user_id, e.user_name, e.text,
                                    received_at=e.received_at + 3, sent_at=e.sent_at) for e in burst(80)]
    _, trig = run(evs)
    assert len(trig) == 1 and 79 <= trig[0].burst_start - T0 <= 82.5


def test_two_nearby_bursts_merge_into_one_moment():
    _, trig = run(chatter(140) + burst(80, spread=3) + burst(86, spread=3, seed=5, prefix="b"))
    assert len(trig) == 1


def test_two_distinct_moments_both_trigger():
    _, trig = run(chatter(200) + burst(80) + burst(140, seed=9, prefix="b"))
    assert len(trig) == 2 and 55 < trig[1].burst_start - trig[0].burst_start < 65


def test_sustained_burst_is_one_trigger():
    long = [ev(80 + i * 0.25, f"fan{i % 40}", "LUL") for i in range(160)]   # 40 s of hype
    _, trig = run(chatter(160) + long)
    assert len(trig) == 1


def test_sensitivity_controls():
    evs = chatter(160, seed=3) + burst(100, n_users=9, per_user=1, spread=5)
    _, lo = run(evs, cfg=DetectorConfig(sensitivity="low"))
    _, hi = run(evs, cfg=DetectorConfig(sensitivity="high"))
    assert lo == [] and len(hi) == 1


def test_warmup_ignores_startup_chatter():
    _, trig = run(chatter(60, rate=1.2))
    assert trig == []


def test_second_reaction_twenty_seconds_later_is_distinct():
    # first reaction tails off ~12 s, chat cools briefly, a new reaction starts 20 s after the first
    _, trig = run(chatter(200) + burst(80, spread=8) + burst(100, spread=6, seed=4, prefix="b"))
    assert len(trig) == 2, [round(t.burst_start - T0, 1) for t in trig]


def test_backlog_after_reconnect_keeps_original_moment_time():
    # a fresh detector (worker restart) receives the whole backlog 60 s late, all at once
    late = [ChatEvent(e.platform, e.channel, e.msg_id, e.user_id, e.user_name, e.text,
                      received_at=T0 + 200, sent_at=e.sent_at) for e in sorted(chatter(120) + burst(80),
                                                                               key=lambda e: e.sent_at)]
    d = SpikeDetector(1)
    trig = []
    for e in late:
        trig += d.feed(e)
    trig += d.tick(T0 + 200)
    assert len(trig) == 1 and 79 <= trig[0].burst_start - T0 <= 82, [t.burst_start - T0 for t in trig]


def test_busy_channel_ordinary_chat_never_triggers_during_warmup_and_baseline_learns():
    # live finding (a busy channel, ~200 msgs/10 s): warm-up must not fire, and the baseline must reach the
    # real level
    r = random.Random(11)
    evs = []
    for i in range(int(300 / 0.05)):
        t = i * 0.05 + r.uniform(0, 0.04)
        evs.append(ev(t, f"u{r.randrange(400)}", r.choice(["lol", "jaja", "xd", "omg", "gg"])))
    d, trig = run(evs, end=300)
    assert trig == [], [x.reason for x in trig]
    assert d.baseline(T0 + 300) > 100                     # learned, not stuck at the 3.0 floor


def test_busy_channel_real_spike_after_warmup_triggers_once():
    r = random.Random(12)
    evs = [ev(i * 0.05, f"u{r.randrange(400)}", "lol") for i in range(int(200 / 0.05))]
    evs += [ev(150 + r.uniform(0, 6), f"s{u}", "WTF") for u in range(500)]     # ~2.5x for a few seconds
    _, trig = run(evs, end=200)
    assert len(trig) == 1 and 149 <= trig[0].burst_start - T0 <= 153


def test_long_burst_releases_baseline():
    r = random.Random(13)
    evs = chatter(100) + [ev(100 + i * 0.1, f"x{r.randrange(80)}", "hype") for i in range(int(200 / 0.1))]
    d, trig = run(evs, end=300)
    assert len(trig) <= 3 and d.baseline(T0 + 300) > 30
