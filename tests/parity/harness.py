"""Monitoring parity harness: drives the public modules with the same synthetic sequences the reference
characterization used (fake streamlink/ffmpeg children, scripted status answers, MockTransports; no network).

`run_all()` returns the same keys as tests/parity/data/monitoring_ref.json. Sections owned by other subsystems
(ring buffer, readiness view, detector timing) are not part of this harness.
"""
from __future__ import annotations

import asyncio
import base64
import json
import random
import shutil
import tempfile
import time
import types
from collections import deque
from pathlib import Path

import httpx

from live_clipper import capture as capmod
from live_clipper import engine as engmod
from live_clipper import source_status as ssmod

REAL_SLEEP = asyncio.sleep


class _Stop(BaseException):
    pass


class _AsyncioShim(types.SimpleNamespace):
    """Module-local replacement for `asyncio` in one module; everything not overridden is delegated."""

    def __getattr__(self, k):
        return getattr(asyncio, k)


def _env(root: Path, **over):
    import monitor_env
    s, store, _ = monitor_env.env(root, **over)
    return s, store


# ------------------------------------------------------------------------------------------------ status rules
def f_status_rules() -> dict:
    titles = ["GTA 6 day 1", "gta vi heists", "GTA-6 RP", "Grand Theft Auto VI", "grand theft auto 6",
              "GTA 5 RP", "GTA VII", "Sports talk", "gta6", "GTAVI", "Just chatting"]
    ts = {
        "live_title_gta": ssmod.topic_state({"state": "live", "title": "GTA 6!", "category": ""}),
        "live_category_only": ssmod.topic_state({"state": "live", "title": "", "category": "Grand Theft Auto VI"}),
        "live_category_only_category_off": ssmod.topic_state(
            {"state": "live", "title": "", "category": "Grand Theft Auto VI"}, False),
        "live_other_title_gta_category": ssmod.topic_state(
            {"state": "live", "title": "chatting", "category": "Grand Theft Auto VI"}),
        "live_no_metadata": ssmod.topic_state({"state": "live", "title": "", "category": ""}),
        "offline": ssmod.topic_state({"state": "offline"}),
        "error": ssmod.topic_state({"state": "error"}),
    }
    from datetime import UTC, datetime
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

    def page(*objs):
        return "".join(f'<script type="application/ld+json">{json.dumps(o)}</script>' for o in objs)

    def be(live, start, end=None, name="GTA VI live"):
        return {"@type": "BroadcastEvent", "isLiveBroadcast": live, "startDate": start,
                **({"endDate": end} if end else {}), "name": name}
    jl = {
        "open_ended_live": page({"@type": "VideoObject", "name": "GTA VI now",
                                 "publication": be(True, "2026-11-20T11:00:00Z")}),
        "ended": page(be(True, "2026-11-20T10:00:00Z", "2026-11-20T11:00:00Z")),
        "scheduled": page(be(True, "2026-11-20T13:00:00Z")),
        "naive_date_ambiguous": page(be(True, "2026-11-20T11:00:00")),
        "string_flag_ambiguous": page(be("true", "2026-11-20T11:00:00Z")),
        "no_metadata": "<html></html>",
        "two_live_titles": page(be(True, "2026-11-20T11:00:00Z", name="A"), be(True, "2026-11-20T11:00:00Z", name="B")),
    }
    return {"is_gta6": {t: ssmod.is_gta6(t) for t in titles}, "topic_state": ts,
            "parse_public_jsonld": {k: ssmod.parse_public_jsonld(v, now)["state"] for k, v in jl.items()}}


# ------------------------------------------------------------------------------------------------ streamlink probe
class _FakeStatusProc:
    def __init__(self, out, hang=False):
        self.out, self.hang = out, hang

    async def communicate(self):
        if self.hang:
            await REAL_SLEEP(10)
        return self.out, b""

    def kill(self):
        pass


def f_streamlink_status() -> dict:
    cases = {
        "offline_error": json.dumps({"error": "No playable streams found on this URL: https://www.twitch.tv/x"}).encode(),
        "other_error": json.dumps({"error": "Unable to open URL: 403 Client Error"}).encode(),
        "live": json.dumps({"streams": {"best": {}}, "metadata": {"title": "GTA 6 RP", "id": "4242",
                                                                "category": "Grand Theft Auto VI"}}).encode(),
        "no_streams_no_error": json.dumps({"streams": {}, "metadata": {"title": "x"}}).encode(),
        "garbage": b"not json",
        "empty": b"",
    }
    out, calls, routed = {}, [], {}
    orig = ssmod.asyncio
    try:
        for name, payload in list(cases.items()) + [("timeout", None)]:
            async def cse(*cmd, payload=payload, **kw):
                calls.append(list(cmd[1:]))
                return _FakeStatusProc(payload, hang=payload is None)
            ssmod.asyncio = _AsyncioShim(create_subprocess_exec=cse)
            out[name] = asyncio.run(ssmod.streamlink_status("https://www.twitch.tv/x",
                                                            timeout=0.2 if payload is None else 40))
        for plat, name in (("twitch", "SomeOne"), ("kick", "@kicker")):
            async def cse(*cmd, plat=plat, **kw):
                routed[plat] = cmd[-1]
                return _FakeStatusProc(cases["offline_error"])
            ssmod.asyncio = _AsyncioShim(create_subprocess_exec=cse)
            asyncio.run(ssmod.source_status(plat, name, "", {}))
        routed["youtube"] = asyncio.run(ssmod.source_status("youtube", "@x", "", {}))
        routed["custom"] = asyncio.run(ssmod.source_status("custom", "x", "rtmp://h/live/x", {}))
        t403 = httpx.MockTransport(lambda r: httpx.Response(403, headers={"retry-after": "120"}))
        routed["rumble_403"] = asyncio.run(ssmod.source_status("rumble", "x", "", {},
                                                               client=httpx.AsyncClient(transport=t403)))
    finally:
        ssmod.asyncio = orig
    return {"results": out, "argv_after_binary": calls[0], "routing": routed}


# ------------------------------------------------------------------------------------------------ capture
class FakeProc:
    """Fake streamlink / ffmpeg child; ffmpeg writes segment files and the CSV list like `-f segment`."""

    def __init__(self, cmd, script, role):
        self.cmd, self.script, self.role = list(cmd), script, role
        self.returncode = None
        self.stderr = asyncio.StreamReader()
        self._done = asyncio.Event()
        for line in script.get("prod_err" if role == "prod" else "ff_err", []):
            self.stderr.feed_data((line + "\n").encode())
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        try:
            if self.role == "ff":
                csv = Path(self.cmd[self.cmd.index("-segment_list") + 1])
                pat = Path(self.cmd[-1])
                seg = float(self.cmd[self.cmd.index("-segment_time") + 1])
                for i in range(self.script.get("segments", 0)):
                    if i == self.script.get("hold_after"):     # deterministic interrupt point (reference: 4)
                        await self._done.wait()
                    if self.returncode is not None:
                        return
                    f = pat.parent / (pat.name % i)
                    f.write_bytes(b"\x47" * 1880)
                    with open(csv, "a") as fh:
                        fh.write(f"{f.name},{i * seg:.6f},{(i + 1) * seg:.6f}\n")
                    await REAL_SLEEP(0.02)
            await REAL_SLEEP(self.script.get("tail", 0.03))
            self._finish(self.script.get("rc", 0 if self.script.get("segments") else 1))
        except asyncio.CancelledError:
            pass

    def _finish(self, rc):
        if self.returncode is None:
            self.returncode = rc
            self.stderr.feed_eof()
            self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def send_signal(self, sig):
        self._finish(-15)

    def kill(self):
        self._finish(-9)


class CaptureWorld:
    """Installs fakes into live_clipper.capture: subprocess spawn and the reconnect backoff sleep (recorded)."""

    def __init__(self, scripts: dict[int, deque], names: dict[int, str]):
        self.scripts, self.names = scripts, names
        self.current: dict[int, dict] = {}
        self.events: dict[int, list] = {cid: [] for cid in scripts}
        self.runs_planned = {cid: len(q) for cid, q in scripts.items()}
        self.caps: dict[int, object] = {}
        self.orig = capmod.asyncio

    def cid_of(self, cmd) -> int:
        if "-segment_list" in cmd:
            return int(Path(cmd[cmd.index("-segment_list") + 1]).parent.parent.name)
        target = cmd[-2]
        return next(c for c, n in self.names.items() if target.rstrip("/").endswith(n) or n in target)

    async def spawn(self, *cmd, **kw):
        role = "ff" if cmd[0] == "ffmpeg" else "prod"
        cid = self.cid_of(cmd)
        if role == "prod" or cid not in self.current:
            self.current[cid] = self.scripts[cid].popleft() if self.scripts[cid] else {"segments": 0, "rc": 1}
        sc = self.current[cid]
        if role == "ff":
            self.current.pop(cid, None)
        if sc.get("missing") == role:
            raise FileNotFoundError(2, "No such file", cmd[0])
        return FakeProc(cmd, sc, role)

    async def sleep(self, d, *a):
        t = asyncio.current_task()
        name = t.get_name() if t else ""
        if name.startswith("lc-capture-") and d > 0:
            cid = int(name.rsplit("-", 1)[1])
            cap = self.caps.get(cid)
            if cap is not None:
                h = cap.health
                self.events[cid].append({"sleep": d, "state": h.state, "restarts": h.restarts})
                if len(self.events[cid]) >= self.runs_planned[cid]:
                    cap._stop.set()
        await REAL_SLEEP(0)

    def __enter__(self):
        capmod.asyncio = _AsyncioShim(create_subprocess_exec=self.spawn, sleep=self.sleep)
        return self

    def __exit__(self, *a):
        capmod.asyncio = self.orig


def _run_capture(platform, name, source_url, scripts, root: Path, interrupt_after_segments=None):
    from live_clipper.buffer import BufferManager
    buffers = BufferManager(root, 90.0, 2048 * 1024 * 1024)
    cid = 1
    world = CaptureWorld({cid: deque(scripts)}, {cid: name})
    logs, runs = [], []

    class RecCapture(capmod.Capture):
        async def _run_once(self):
            got = await super()._run_once()
            h = self.health
            runs.append({"run": self._run, "got_media": got, "state_after_run": h.state, "detail": h.detail,
                         "action_needed": h.action_needed, "source_available_set": h.source_available_at is not None})
            return got

    async def go():
        with world:
            cap = RecCapture(cid, platform, name, source_url, buffers, segment_seconds=2.0,
                             log=lambda m, lvl="info": logs.append((lvl, m)))
            world.caps[cid] = cap
            cap.start()
            if interrupt_after_segments:
                idx = buffers.index(cid)
                while len(idx.all_segments()) < interrupt_after_segments:
                    await REAL_SLEEP(0.01)
                await cap.interrupt()
            await asyncio.wait_for(cap._task, 30)
            return cap, buffers.index(cid)
    cap, idx = asyncio.run(go())
    seq = []
    for i, r in enumerate(runs):
        e = world.events[cid][i] if i < len(world.events[cid]) else None
        seq.append({**r, "loop_state_before_backoff": e["state"] if e else None,
                    "backoff_s": e["sleep"] if e else None, "restarts": e["restarts"] if e else cap.health.restarts})
    return {"runs": seq, "final_health": {"state": cap.health.state, "restarts": cap.health.restarts,
                                          "detail": cap.health.detail},
            "epochs": [{"id": ep.id, "reason": ep.reason, "segments": len(ep.segments)} for ep in idx.epochs],
            "run_dirs": sorted(p.name for p in buffers.channel_dir(cid).iterdir()),
            "warn_logs": [m for lvl, m in logs if lvl == "warn"]}


def f_capture(tmp: Path) -> dict:
    off = {"segments": 0, "rc": 1,
           "prod_err": ["[cli][error] No playable streams found on this URL: https://www.twitch.tv/ch"],
           "ff_err": ["pipe:0: Invalid data found when processing input"]}

    def live(n, rc=0):
        return {"segments": n, "rc": rc}
    err500 = {"segments": 0, "rc": 1, "prod_err": ["error: Unable to open URL: 500 Server Error"]}
    res = {
        "twitch_offline_x3_live_drop_offline_live": _run_capture(
            "twitch", "ch", "", [off, off, off, live(5), off, live(3)], tmp / "cap_a"),
        "twitch_generic_failure_without_media": _run_capture("twitch", "ch", "", [err500, err500], tmp / "cap_b"),
        "missing_ffmpeg": _run_capture("custom", "feed", "rtmp://example.invalid/live/feed",
                                       [{"missing": "ff"}, {"missing": "ff"}], tmp / "cap_c"),
        "rumble_without_feed": _run_capture("rumble", "r", "", [{}, {}], tmp / "cap_d"),
        "operator_reconnect_mid_stream": _run_capture(
            "custom", "feed", "rtmp://example.invalid/live/feed", [{**live(40), "hold_after": 4}, live(3)], tmp / "cap_e",
            interrupt_after_segments=4),
        "long_failure_backoff_cap": _run_capture(
            "custom", "feed", "srt://example.invalid:9000",
            [{"segments": 0, "rc": 1, "ff_err": ["Connection refused"]}] * 7, tmp / "cap_f"),
    }

    def sc(*a, **k):
        prod, inp = capmod.source_command(*a, **k)
        return [["streamlink", *prod[1:]] if prod else prod, inp]      # binary path differs per machine
    res["source_command"] = {
        "twitch": sc("twitch", "@Name", ""),
        "kick": sc("kick", "name", ""),
        "youtube_handle": sc("youtube", "name", "")[0][-2],
        "youtube_at": sc("youtube", "@name", "")[0][-2],
        "youtube_channel": sc("youtube", "channel/UCx", "")[0][-2],
        "explicit_streamlink": sc("custom", "x", "streamlink:hls://h/x.m3u8")[0][-2],
        "rtmp_listen": sc("custom", "x", "rtmp://0.0.0.0:1935/live/listen")[1],
        "srt": sc("custom", "x", "srt://h:9000")[1],
        "realtime_file_flag": sc("custom", "x", "http://h/a.m3u8", realtime_file=True)[1],
    }
    try:
        capmod.source_command("rumble", "x", "")
    except ValueError as e:
        res["source_command"]["rumble_no_url"] = f"ValueError: {e}"
    return res


# ------------------------------------------------------------------------------------------------ evidence
def _bare_monitor(settings, platform="twitch"):
    m = engmod.Monitor.__new__(engmod.Monitor)
    m.gated, m.watch, m.id = False, {}, 1
    m.ch = {"id": 1, "platform": platform, "name": "x", "source_url": "", "settings": {}}
    m.engine = types.SimpleNamespace(settings=settings, secrets=None, hub=None)
    m.capture = types.SimpleNamespace(health=types.SimpleNamespace(restarts=0))
    m.observed, m.observed_good, m.boundary_at, m.observe_started_at = {}, {}, 0.0, 0.0
    return m


def f_evidence() -> dict:
    S = types.SimpleNamespace(topic_observe_seconds=300, topic_category_evidence=True)
    real_time = engmod.time.time
    T0 = 2_000_000_000.0
    seq = [
        (0, {"state": "live", "title": "GTA 6 RP", "category": "", "id": "b1"}, 300, 0),
        (300, {"state": "error", "detail": "status check timed out"}, 60, 0),
        (360, {"state": "error", "detail": "status check timed out"}, 120, 0),
        (480, {"state": "live", "title": "Just chatting", "category": "", "id": "b1"}, 300, 0),
        (780, {"state": "offline"}, 300, 0),
        (1080, {"state": "live", "title": "", "category": "Grand Theft Auto VI", "id": "b2"}, 300, 0),
        (1100, {"state": "error"}, 60, 1),
    ]
    m = _bare_monitor(S)
    rows = []
    try:
        for dt, st, wait, restarts in seq:
            m.capture.health.restarts = restarts
            r = m.note_status(st, wait, 300, T0 + dt)
            for probe in (0, 30, 749, 751):
                engmod.time.time = lambda probe=probe, dt=dt: T0 + dt + probe
                ev = m.topic_evidence()
                rows.append({"answer_at": dt, "answer": st["state"], "note_status_returned": r,
                             "evaluated_at": dt + probe, "evidence_state": ev["state"], "kind": ev.get("kind", ""),
                             "boundary_at": (m.boundary_at - T0) if m.boundary_at else None})
    finally:
        engmod.time.time = real_time
    return {"sequence": rows, "freshness_rule": "fresh while age <= 2.5 * interval (interval = base for live answers)"}


# ------------------------------------------------------------------------------------------------ gated watch
WATCH_ANSWERS = [
    {"state": "offline"},
    {"state": "live", "title": "Just chatting", "category": "Grand Theft Auto VI"},
    {"state": "live", "title": "GTA 6 tonight", "category": ""},
    {"state": "error", "detail": "status check timed out"},
    {"state": "live", "title": "GTA 6 tonight", "category": ""},
    {"state": "unavailable", "detail": "HTTP 429", "retry_after": 600.0},
    {"state": "unavailable", "detail": "HTTP 429", "retry_after": None},
    {"state": "unavailable"}, {"state": "unavailable"}, {"state": "unavailable"},
    {"state": "live", "title": "GTA VI heists", "category": ""},
    {"state": "offline"},
]


def f_watch() -> dict:
    def run(tab_hold: bool):
        answers = deque(WATCH_ANSWERS)

        async def fake_status(*a, **k):
            if not answers:
                raise _Stop()
            return dict(answers.popleft())
        m = engmod.Monitor.__new__(engmod.Monitor)
        m.ch = {"id": 7, "platform": "twitch", "name": "x", "source_url": "", "settings": {}}
        m.id, m.gated, m.gate_open, m.tab_hold = 7, True, False, tab_hold
        m.watch = {"enabled": True, "state": "starting", "poll_seconds": 120}
        m.engine = types.SimpleNamespace(secrets=None, hub=None)
        m.official = {}
        m._log = lambda msg, level="info": None
        actions, rows = [], []
        m._start_media_and_chat = lambda: actions.append("start_capture_and_chat")

        async def stop(purge):
            actions.append(f"stop_capture_and_chat(purge={purge})")
        m._stop_media_and_chat = stop

        async def rec_sleep(d, *a):
            rows.append({"label": m.watch["state"], "gate_open": m.gate_open, "wait_s": d, "fails": m.watch["fails"],
                         "actions": list(actions)})
            actions.clear()
            await REAL_SLEEP(0)
        orig_ss, orig_aio = ssmod.source_status, engmod.asyncio
        ssmod.source_status, engmod.asyncio = fake_status, _AsyncioShim(sleep=rec_sleep)
        try:
            asyncio.run(m._watch())
        except _Stop:
            pass
        finally:
            ssmod.source_status, engmod.asyncio = orig_ss, orig_aio
        return rows
    return {"answers": WATCH_ANSWERS, "poll_seconds": 120, "without_tab_share": run(False),
            "with_tab_share_holding_gate": run(True)}


def f_observe() -> dict:
    answers = deque([{"state": "live", "title": "GTA 6", "id": "b1"}] + [{"state": "error"}] * 6 +
                    [{"state": "live", "title": "GTA 6", "id": "b1"}, {"state": "offline"}])
    m = engmod.Monitor.__new__(engmod.Monitor)
    m.ch = {"id": 3, "platform": "kick", "name": "x", "source_url": "", "settings": {}}
    m.id, m.gated = 3, False
    m.observed, m.observed_good, m.boundary_at, m.observe_started_at = {}, {}, 0.0, 0.0
    m.capture = types.SimpleNamespace(health=types.SimpleNamespace(restarts=0))
    m.engine = types.SimpleNamespace(secrets=None, hub=None,
                                     settings=types.SimpleNamespace(topic_observe_seconds=300,
                                                                    topic_category_evidence=True),
                                     late_topic_evidence=lambda mon: 0)
    m._log = lambda *a, **k: None
    rows = []

    async def fake_status(*a, **k):
        if not answers:
            raise _Stop()
        return dict(answers.popleft())

    async def rec_sleep(d, *a):
        rows.append(round(d, 3))
        await REAL_SLEEP(0)
    orig = (ssmod.source_status, engmod.asyncio, random.uniform)
    ssmod.source_status, engmod.asyncio, random.uniform = fake_status, _AsyncioShim(sleep=rec_sleep), (lambda a, b: a)
    try:
        asyncio.run(m._observe())
    except _Stop:
        pass
    finally:
        ssmod.source_status, engmod.asyncio, random.uniform = orig
    return {"sleeps_with_jitter_at_min": rows,
            "note": "first sleep = initial spread uniform(1,12) pinned to 1; later sleeps = wait + uniform(0,10) pinned to 0"}


# ------------------------------------------------------------------------------------------------ chat
def f_chat() -> dict:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from live_clipper.chat._util import Backoff
    from live_clipper.chat.kick import KickChat
    from live_clipper.chat.twitch import TwitchChat, irc_emote_names, parse_irc
    from live_clipper.chat.youtube import YouTubeChat
    out = {}
    got = []
    tw = TwitchChat("SomeChan", got.append, {}, {})
    line = ("@badge-info=;display-name=Bob;emotes=25:0-4/1902:6-10;id=abc-1;tmi-sent-ts=1700000000123;user-id=42 "
            ":bob!bob@bob.tmi.twitch.tv PRIVMSG #somechan :Kappa Keepo hi")
    asyncio.run(tw._irc_line(None, line))
    asyncio.run(tw._irc_line(None, line))
    tw._on_notification({"metadata": {"message_id": "m-9", "message_timestamp": "2023-11-14T22:13:20.5Z"},
                         "payload": {"subscription": {"type": "channel.chat.message"},
                                     "event": {"message_id": "e-9", "chatter_user_id": "7", "chatter_user_login": "al",
                                               "message": {"text": "LUL wow", "fragments": [
                                                   {"type": "emote", "text": "LUL"},
                                                   {"type": "text", "text": " wow"}]}}}})
    out["twitch"] = [{k: v for k, v in e.to_dict().items() if k != "received_at"} for e in got]
    out["twitch_irc_parse"] = list(parse_irc(line)[2:])
    out["twitch_emotes"] = list(irc_emote_names("25:0-4/1902:6-10", "Kappa Keepo hi"))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    kgot = []
    kc = KickChat("SlugName", kgot.append, {}, {"public_key_pem": pem, "public_url": "https://example.invalid"})

    def signed(mid, body, etype="chat.message.sent"):
        ts = "2026-11-20T12:00:00Z"
        sig = base64.b64encode(key.sign(f"{mid}.{ts}.".encode() + body, padding.PKCS1v15(), hashes.SHA256())).decode()
        return {"Kick-Event-Message-Id": mid, "Kick-Event-Message-Timestamp": ts, "Kick-Event-Signature": sig,
                "Kick-Event-Type": etype}
    body = json.dumps({"message_id": "k1", "broadcaster": {"channel_slug": "slugname", "user_id": 5},
                       "sender": {"user_id": 9, "username": "viewer"}, "content": "hi [emote:123:catJAM] [emote:9:catJAM]",
                       "created_at": "2026-11-20T11:59:59.25Z"}).encode()
    codes = {"valid": asyncio.run(kc.handle_webhook(signed("d1", body), body)),
             "replayed_same_id": asyncio.run(kc.handle_webhook(signed("d1", body), body)),
             "bad_signature": asyncio.run(kc.handle_webhook({**signed("d2", body), "Kick-Event-Signature": "AAAA"}, body)),
             "missing_id": asyncio.run(kc.handle_webhook({}, body))}
    other = json.dumps({"message_id": "k2", "broadcaster": {"channel_slug": "someoneelse", "user_id": 6},
                        "sender": {"user_id": 1, "username": "u"}, "content": "x"}).encode()
    codes["other_channel"] = asyncio.run(kc.handle_webhook(signed("d3", other), other))
    codes["other_event_type"] = asyncio.run(kc.handle_webhook(signed("d4", body, "channel.followed"), body))
    bad = b'{"broadcaster": {"channel_slug": "slugname"}}'
    codes["malformed_chat_body"] = asyncio.run(kc.handle_webhook(signed("d5", bad), bad))
    out["kick_webhook_codes"] = codes
    out["kick"] = [{k: v for k, v in e.to_dict().items() if k != "received_at"} for e in kgot]
    out["kick_health_after"] = {"state": kc.health.state, "detail": kc.health.detail}
    kc2 = KickChat("x", lambda e: None, {}, {})
    asyncio.run(kc2.run())
    out["kick_without_public_url"] = {"state": kc2.health.state, "action": kc2.health.action_needed}

    started = time.time()

    def iso(t):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"
    pages = deque([
        {"nextPageToken": "p2", "pollingIntervalMillis": 2500,
         "items": [{"id": "old", "snippet": {"publishedAt": iso(started - 60), "displayMessage": "old"},
                    "authorDetails": {"channelId": "UC1", "displayName": "A"}},
                   {"id": "y1", "snippet": {"publishedAt": iso(started), "displayMessage": "hello"},
                    "authorDetails": {"channelId": "UC2", "displayName": "B"}}]},
        {"pollingIntervalMillis": 300, "items": [
            {"id": "y1", "snippet": {"publishedAt": iso(started), "displayMessage": "hello"}, "authorDetails": {}},
            {"id": "s1", "snippet": {"type": "superChatEvent", "publishedAt": iso(started)}, "authorDetails": {}},
            {"id": "y2", "snippet": {"publishedAt": iso(started + 1), "textMessageDetails": {"messageText": "x"}},
             "authorDetails": {"channelId": "UC3", "displayName": "C"}}]},
    ])
    reqs = []

    def yt(req):
        reqs.append((req.url.path, dict(req.url.params)))
        if req.url.path.endswith("/videos"):
            return httpx.Response(200, json={"items": [{"liveStreamingDetails": {"activeLiveChatId": "CHAT1"}}]})
        if pages:
            return httpx.Response(200, json=pages.popleft())
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}], "message": "q"}})
    ygot, sleeps, healths = [], [], []

    async def ysleep(d):
        sleeps.append(d)
        healths.append(yc.health.state)
        if len(sleeps) >= 3:
            raise _Stop()
    yc = YouTubeChat("vid00000001", ygot.append, {"api_key": "SYNTHETIC"},
                     {"transport": httpx.MockTransport(yt), "sleep": ysleep})
    try:
        asyncio.run(yc.run())
    except _Stop:
        pass
    out["youtube"] = {"emitted": [[e.msg_id, e.user_name, e.text] for e in ygot], "sleeps": sleeps,
                      "health_at_each_sleep": healths, "final_action": yc.health.action_needed,
                      "message_params": {k: v for k, v in reqs[1][1].items() if k != "key"}}
    b = Backoff()
    out["chat_backoff_sequence"] = [b.next() for _ in range(9)]
    out["twitch_hub_retry_later_s"] = [min(900, 30.0 * 2 ** (f - 1)) for f in range(1, 8)]
    return out


# ------------------------------------------------------------------------------------------------ engine: 3 channels
def f_engine_three_channels(tmp: Path) -> dict:
    from live_clipper.events import Trigger
    s, store = _env(tmp / "eng3")
    eng = engmod.Engine(s, store, None)
    ids = [store.add_channel("custom", f"feed{i}", f"rtmp://example.invalid/live/feed{i}") for i in (1, 2, 3)]

    def live(n):
        return {"segments": n, "rc": 0}
    fail = {"segments": 0, "rc": 1, "ff_err": ["rtmp://example.invalid/live/feed2: Connection refused"]}
    scripts = {ids[0]: deque([live(6), live(3)]), ids[1]: deque([fail, fail, live(4)]), ids[2]: deque([live(12)])}
    world = CaptureWorld(scripts, {c: f"feed{i}" for i, c in zip((1, 2, 3), ids, strict=True)})
    result = {}

    async def go():
        with world:
            for c in ids:
                eng.arm(c)
                world.caps[c] = eng.monitors[c].capture
            eng.arm(ids[0])
            result["capture_tasks"] = sorted(t.get_name() for t in asyncio.all_tasks()
                                             if t.get_name().startswith("lc-capture-"))
            result["engine_tasks_spawned"] = len(eng.tasks)
            await asyncio.wait_for(asyncio.gather(*[eng.monitors[c].capture._task for c in ids]), 30)
            result["per_channel"] = {}
            for c in ids:
                st = eng.monitors[c].status()
                result["per_channel"][store.channel(c)["name"]] = {
                    "capture_state": st["capture"]["state"], "restarts": st["capture"]["restarts"],
                    "epochs": st["epochs"], "buffer_seconds": st["buffer_seconds"], "chat": st["chat"]["state"],
                    "chat_detail": st["chat"]["detail"], "watch": st["watch"], "gate_open": st["gate_open"],
                    "backoffs": [e["sleep"] for e in world.events[c]]}
            m = eng.monitors[ids[2]]

            async def no_cut(*a, **k):
                return None
            eng._process = no_cut
            t0 = time.time()

            def mk(dt, kind="chat_spike"):
                return Trigger(channel_id=ids[2], reason=f"{kind}@{dt}", kind=kind, burst_start=t0 + dt + 4,
                               action_time=t0 + dt, delay=4.0, detected_at=t0 + dt + 6)
            r = {"t+0": eng.on_trigger(m, mk(0)), "t+5_merged": eng.on_trigger(m, mk(5)),
                 "t+13.9_inside_window_end+2": eng.on_trigger(m, mk(13.9)),
                 "t+5_manual": eng.on_trigger(m, mk(5, "manual")), "t+30_new": eng.on_trigger(m, mk(30))}
            await REAL_SLEEP(0.05)
            c0 = store.clip(r["t+0"])
            result["triggers"] = {k: (v is not None) for k, v in r.items()}
            result["clip_t0"] = {"status": c0["status"], "dedupe_key_shape": c0["dedupe_key"].split(":")[1],
                                 "window_rel": [round(c0["window_start"] - t0, 3), round(c0["window_end"] - t0, 3)],
                                 "merged": [x["reason"] for x in c0["trigger"].get("merged", [])],
                                 "topic": c0["trigger"]["topic"]}
            result["dedupe_same_key_returns_none"] = store.create_clip(ids[2], c0["dedupe_key"], "chat_spike", {}) is None
            m.gate_open = False
            result["trigger_with_gate_closed"] = eng.on_trigger(m, mk(60))
            for c in ids:
                await eng.disarm(c)
            result["after_disarm"] = {"monitors": len(eng.monitors),
                                      "armed_flags": [store.channel(c)["armed"] for c in ids],
                                      "segments_left": [len(eng.buffers.index(c).all_segments()) for c in ids]}
            await eng.stop()
    asyncio.run(go())
    return result


# ------------------------------------------------------------------------------------------------ crash + restart
def f_restart(tmp: Path) -> dict:
    s, store = _env(tmp / "restart", max_active_clips=6)
    ch_live = store.add_channel("custom", "armed_feed", "rtmp://example.invalid/live/armed_feed")
    ch_ret = store.add_channel("custom", "retired_feed", "rtmp://example.invalid/live/r",
                               {"retired": {"at": 1.0, "reason": "test"}})
    ch_vod = store.add_channel("custom", "vod_feed", "rtmp://example.invalid/live/v", {"ingest_mode": "vod_only"})
    ch_off = store.add_channel("custom", "idle_feed", "rtmp://example.invalid/live/i")
    for c in (ch_live, ch_ret, ch_vod):
        store.update("channels", c, armed=1)
    now = time.time()
    n = [0]

    def clip(status, files=(), trigger=None):
        n[0] += 1
        cid = store.create_clip(ch_live, f"{ch_live}:chat_spike:{int(now) - 1000 + n[0]}", "chat_spike",
                                trigger or {}, action_time=now - 1000 + n[0])
        store.update("clips", cid, status=status)
        d = s.clips_dir / str(cid)
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).write_bytes(b"x")
        return cid
    c = {"pending_no_media": clip("pending"),
         "cutting_with_files": clip("cutting", ("preview.mp4", "source.ts")),
         "preview_ready_resumable": clip("preview_ready", ("preview.mp4", "source.ts")),
         "captioning_resumable": clip("captioning", ("preview.mp4", "source.ts")),
         "rendering_resumable": clip("rendering", ("preview.mp4", "source.ts")),
         "captioning_missing_source": clip("captioning", ("preview.mp4",)),
         "vod_import_cutting": clip("cutting", (), {"input": "vod_import"}),
         "ready": clip("ready", ("preview.mp4", "source.ts")),
         "discarded": clip("discarded")}
    leftover = s.buffer_dir / str(ch_live) / "run0007"
    leftover.mkdir(parents=True)
    (leftover / "s000001.ts").write_bytes(b"x")
    eng = engmod.Engine(s, store, None)
    world = CaptureWorld({ch_live: deque([{"segments": 2, "rc": 0}])}, {ch_live: "armed_feed"})
    started = []

    async def fake_post_cut(cid):
        started.append(cid)
        await REAL_SLEEP(0.01)
    eng._post_cut = fake_post_cut
    res = {}

    async def go():
        with world:
            await eng.start()
            if ch_live in eng.monitors:
                world.caps[ch_live] = eng.monitors[ch_live].capture
            res["ring_after_startup_sweep"] = sorted(p.name for p in s.buffer_dir.iterdir())
            res["monitors_after_restart"] = sorted(store.channel(x)["name"] for x in eng.monitors)
            res["armed_flags"] = {store.channel(x)["name"]: store.channel(x)["armed"]
                                  for x in (ch_live, ch_ret, ch_vod, ch_off)}
            res["recovery_queue"] = [k for k, v in c.items() if v in eng.recovery]
            res["recovery_slots"] = eng.recovery_slots()
            await REAL_SLEEP(1.3)
            await eng.stop()
    asyncio.run(go())
    res["statuses_after"] = {k: [store.clip(v)["status"], store.clip(v)["error"]] for k, v in c.items()}
    res["post_cut_started_order"] = [k for x in started for k, v in c.items() if v == x]
    eng2 = engmod.Engine(s, store, None)
    eng2._recover()
    res["second_recover_queue"] = [k for k, v in c.items() if v in eng2.recovery]
    return res


# ------------------------------------------------------------------------------------------------ disk producers
def f_disk_producers(tmp: Path) -> dict:
    s, store = _env(tmp / "disk", min_free_mb=4000, critical_free_mb=1000)
    s.min_free_mb, s.critical_free_mb = 4000, 1000
    eng = engmod.Engine(s, store, None)
    MB = 1024 * 1024
    free = {"v": 50_000 * MB}
    eng.maintenance.free_bytes = lambda: free["v"]

    class FC:
        def __init__(self):
            self._task, self.starts, self.stops = None, 0, 0
            self.health = types.SimpleNamespace(detail="")

        def start(self):
            self.starts += 1
            self._task = asyncio.get_running_loop().create_future()

        async def stop(self):
            self.stops += 1
            self._task = None
    rows = []

    async def go():
        cid = store.add_channel("custom", "a", "rtmp://x")
        fc = FC()
        fc.start()
        eng.monitors[cid] = types.SimpleNamespace(id=cid, capture=fc, gated=False, gate_open=True)
        for mb in (50_000, 900, 2000, 3999, 4000, 900):
            free["v"] = mb * MB
            await eng.maintenance.enforce_producers()
            rows.append({"free_mb": mb, "starts": fc.starts, "stops": fc.stops, "paused": bool(eng.maintenance.paused),
                         "admit_clip": eng.maintenance.admit_clip() or "ok"})
        eng.monitors.clear()
    asyncio.run(go())
    return {"min_free_mb": 4000, "critical_free_mb": 1000, "sequence": rows}


def run_all(tmp: Path | None = None) -> dict:
    tmp = Path(tmp or tempfile.mkdtemp(prefix="lc-parity-"))
    try:
        return {"status_rules": f_status_rules(), "streamlink_status": f_streamlink_status(),
                "capture": f_capture(tmp), "evidence": f_evidence(), "watch_gated": f_watch(),
                "observe_ungated": f_observe(), "chat": f_chat(), "engine_three_channels": f_engine_three_channels(tmp),
                "crash_restart": f_restart(tmp), "disk_producers": f_disk_producers(tmp)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
