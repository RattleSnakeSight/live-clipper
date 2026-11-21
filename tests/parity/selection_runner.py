"""Selection parity runner (public side).

Feeds the synthetic inputs (tests/parity/data/selection_inputs.json, schema_version 1) through the public
selection
code and returns the observed decisions in the same JSON shape as the reference harness output
(tests/parity/data/selection_ref.json). Covered sections are the ones this subsystem owns; loudness, speech
classification and VOD import belong to other subsystems and are compared there.

The detector driver and the live merge/dedupe replay mirror the reference harness exactly: every message is
fed when
its receive time is reached, then every channel's detector ticks (0.5 s housekeeping), and each trigger goes
through
the engine's same-moment rule (moments.merge_target) and creator-moment id (moments.moment_id) on a scratch
store.
"""
from __future__ import annotations

import math
import os
import sqlite3
import tempfile
from types import SimpleNamespace

COVERED = ("detector_cases", "pipeline_cases", "topic_cases", "chat_evidence_cases", "speech_evidence_cases",
           "language_cases", "moments_cases", "candidate_cases", "title_cases", "camera_face_cases",
           "explore_hours")
NOT_COVERED = {"loudness_cases": "edit subsystem (loudness)",
               "vod_cases": "VOD import (not part of this subsystem)",
               "language_cases.classify_speech": "edit subsystem (captions/asr)"}

CHANNEL_DEFAULTS = {"sensitivity": "medium", "reaction_delay": None, "pre_roll": None, "post_roll": None}


def r6(x):
    if isinstance(x, float):
        return None if math.isnan(x) else round(x, 6)
    if isinstance(x, dict):
        return {k: r6(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [r6(v) for v in x]
    return x


def rel(x, t0):
    return None if x is None else round(float(x) - t0, 6)


def trig_view(d: dict, t0: float) -> dict:
    keep = ("kind", "reason", "score", "peak_rate", "unique_users", "baseline", "delay", "sample_messages",
            "chat_time_basis")
    out = {k: r6(d.get(k)) for k in keep}
    for k in ("burst_start", "action_time", "burst_end", "first_chat_received_at"):
        out[k + "_rel"] = rel(d.get(k), t0)
    return out


# ---------------------------------------------------------------- a scratch clip store (reference Store
# subset)
class ScratchStore:
    """The few Store methods the creator-moment code reads (channels, clips, merge_json)."""

    def __init__(self, path: str):
        import json
        self._json = json
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
            CREATE TABLE channels(id INTEGER PRIMARY KEY, platform TEXT, name TEXT, settings TEXT NOT NULL);
            CREATE TABLE clips(id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                               dedupe_key TEXT NOT NULL UNIQUE,
              kind TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT '{}', window_start REAL, window_end REAL,
              action_time REAL, delay REAL);
        """)

    def _row(self, r):
        if r is None:
            return None
        d = dict(r)
        for k in ("settings", "trigger"):
            if k in d and isinstance(d[k], str):
                d[k] = self._json.loads(d[k])
        return d

    def q(self, sql, *args):
        if "clip_ledger" in sql:
            raise sqlite3.OperationalError("no clip_ledger in the scratch store")
        return [self._row(r) for r in self.c.execute(sql, args).fetchall()]

    def q1(self, sql, *args):
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def add_channel(self, platform, name, settings):
        return self.c.execute("INSERT INTO channels(platform, name, settings) VALUES (?,?,?)",
                              (platform, name, self._json.dumps(settings))).lastrowid

    def channel(self, cid):
        return self.q1("SELECT * FROM channels WHERE id=?", cid)

    def channels(self):
        return self.q("SELECT * FROM channels ORDER BY id")

    def clip(self, cid):
        return self.q1("SELECT * FROM clips WHERE id=?", cid)

    def create_clip(self, channel_id, dedupe_key, kind, trigger, **f):
        try:
            return self.c.execute(
                "INSERT INTO clips(channel_id, dedupe_key, kind, trigger, window_start, window_end, "
                "action_time, "
                "delay) VALUES (?,?,?,?,?,?,?,?)", (channel_id, dedupe_key, kind, self._json.dumps(trigger),
                                                     f.get("window_start"), f.get("window_end"),
                                                     f.get("action_time"), f.get("delay"))).lastrowid
        except sqlite3.IntegrityError:
            return None

    def set_trigger(self, cid, trigger):
        self.c.execute("UPDATE clips SET trigger=? WHERE id=?", (self._json.dumps(trigger), cid))

    def merge_json(self, table, row_id, col, patch):
        assert table == "clips" and col == "trigger"
        cur = (self.clip(row_id) or {}).get("trigger") or {}
        cur.update(patch)
        self.set_trigger(row_id, cur)
        return cur

    def close(self):
        self.c.close()


def on_trigger(store, m, trig, defaults):
    """The engine's live trigger handling, as far as selection decides it (gate open, admission granted)."""
    from live_clipper import moments
    pre = m.s["pre_roll"] if m.s.get("pre_roll") is not None else defaults["pre_roll"]
    post = m.s["post_roll"] if m.s.get("post_roll") is not None else defaults["post_roll"]
    start, end = moments.window_for(trig.action_time, pre, post)
    ids = moments.simulcast_channels(store, m.id)
    rows = store.q(f"SELECT id, channel_id, action_time, window_end, trigger FROM clips WHERE channel_id IN "
                   f"({','.join('?' * len(ids))}) AND action_time>?", *ids, trig.action_time - 120)
    c = moments.merge_target(rows, trig.kind, trig.action_time, start, end)
    if c is not None:
        c["trigger"].setdefault("merged", []).append({"reason": trig.reason, "action_time": trig.action_time,
                                                      "channel_id": m.id})
        store.set_trigger(c["id"], c["trigger"])
        m.detector.stats["merged"] += 1
        return None
    clip_id = store.create_clip(m.id, moments.dedupe_key(m.id, trig.kind, trig.action_time), trig.kind,
                                trig.to_dict(), window_start=start, window_end=end,
                                action_time=trig.action_time,
                                delay=trig.delay)
    if clip_id is None:
        return None
    moments.moment_id(store, clip_id)
    ev = m.topic_evidence()
    plat = m.ch.get("platform") or ""
    if plat not in ("twitch", "kick") and not m.gated and "why" in ev:
        ev["why"] = f"no public title/category lookup exists for {plat or 'this kind of'} sources"
    elif "why" in ev and not m.gated and int(defaults["topic_observe_seconds"]) <= 0:
        ev["why"] = "title/category observation is switched off (topic_observe_seconds=0)"
    store.merge_json("clips", clip_id, "trigger", {"topic": ev})
    return clip_id


def run_detector_case(case, defaults, t0, tmpdir, fmt=None):
    from live_clipper.detector import DetectorConfig, SpikeDetector
    from live_clipper.events import ChatEvent
    store = ScratchStore(os.path.join(tmpdir, f"{case['id']}.db"))
    mons, keys, emitted = {}, {}, []
    for ch in case["channels"]:
        cid = store.add_channel(ch["platform"], ch["name"], dict(ch["settings"]))
        s = {**CHANNEL_DEFAULTS, **ch["settings"]}
        delay = s["reaction_delay"] if s["reaction_delay"] is not None else defaults["reaction_delay"]
        cfg = DetectorConfig.from_format(fmt, sensitivity=s.get("sensitivity") or defaults["sensitivity"],
                                         reaction_delay=delay)
        topic = dict(ch["topic"])
        mons[ch["key"]] = SimpleNamespace(id=cid, s=s, ch={"platform": ch["platform"]}, gated=False,
                                          detector=SpikeDetector(cid, cfg),
                                          topic_evidence=lambda topic=topic: dict(topic))
        keys[cid] = ch["key"]
    evs = []
    dd = case.get("delivery_delay_s", defaults["delivery_delay_s"])
    for ch in case["channels"]:
        for m in ch["messages"]:
            t, user, text = m[0], m[1], m[2]
            delay = m[3] if len(m) > 3 and m[3] is not None else dd
            mid = m[4] if len(m) > 4 and m[4] else f"{user}-{t:.3f}-{text}"
            evs.append((t0 + t + delay, len(evs), ch["key"],
                        ChatEvent("replay", ch["name"], mid, user, user, text, received_at=t0 + t + delay,
                                  sent_at=t0 + t)))
    evs.sort(key=lambda e: (e[0], e[1]))
    trig_objs = []

    def handle(key, trigs, at):
        for tr in trigs:
            snap = tr.to_dict()
            clip_id = on_trigger(store, mons[key], tr, defaults)
            emitted.append({"channel": key, "emitted_at_rel": round(at - t0, 3), "clip_id": clip_id,
                            **trig_view(snap, t0)})
            trig_objs.append((key, tr))

    tick, i, step = case.get("tick_s", defaults["tick_s"]), 0, 0
    while step * tick <= case["end_s"] + 1e-9:
        now = t0 + step * tick
        while i < len(evs) and evs[i][0] <= now:
            _, _, key, ev = evs[i]
            handle(key, mons[key].detector.feed(ev), ev.received_at)
            i += 1
        for key in mons:
            handle(key, mons[key].detector.tick(now), now)
        step += 1
    clips = []
    for c in store.q("SELECT id, channel_id, kind, action_time, window_start, window_end, dedupe_key, "
                     "trigger "
                     "FROM clips ORDER BY id"):
        tg = c["trigger"] or {}
        clips.append({"clip_id": c["id"], "channel": keys[c["channel_id"]], "kind": c["kind"],
                      "action_time_rel": rel(c["action_time"],
                                             t0), "window_start_rel": rel(c["window_start"], t0),
                      "window_end_rel": rel(c["window_end"], t0),
                      "dedupe_key": c["dedupe_key"].replace(str(c["channel_id"]) + ":",
                                                            keys[c["channel_id"]] + ":",
                                                            1),
                      "score_stored": r6(tg.get("score")), "moment_id": tg.get("moment_id"),
                      "moment_creator": (tg.get("moment_creator") or "").replace(
                          f"channel:{c['channel_id']}", f"channel:{keys[c['channel_id']]}"),
                      "topic_state": (tg.get("topic") or {}).get("state"),
                      "merged": [{"channel": keys.get(x.get("channel_id")),
                                  "action_time_rel": rel(x["action_time"], t0),
                                  "reason": x["reason"]} for x in tg.get("merged") or []]})
    per_channel = {}
    for key, m in mons.items():
        d = m.detector
        snap = d.snapshot()
        per_channel[key] = {"stats": dict(d.stats), "final_baseline": r6(d.baseline(d.now)),
                            "final_threshold": r6(snap["threshold"]), "final_rate": r6(snap["rate"]),
                            "final_unique": snap["unique"], "active_burst": snap["active_burst"]}
    finals = [{"channel": k, **{f: r6(getattr(tr, f)) for f in ("score", "peak_rate", "unique_users")},
               "burst_end_rel": rel(tr.burst_end, t0)} for k, tr in trig_objs]
    store.close()
    return {"triggers_at_emission": emitted, "triggers_final_mutated": finals, "clips": clips,
            "detector": per_channel}


def trigger_for(pc, det_out, t0):
    src = pc["trigger"]
    if "explicit" in src:
        e = src["explicit"]
        return {"kind": e["kind"], "reason": e.get("reason", ""), "score": e.get("score", 0.0),
                "delay": e["delay"],
                "burst_start": t0 + e["burst_start_rel"], "action_time": t0 + e["action_time_rel"],
                "sample_messages": e.get("sample_messages", []), "unique_users": e.get("unique_users", 0)}
    rows = [x for x in det_out[src["from_case"]]["triggers_at_emission"] if x["channel"] == src["channel"]]
    x = rows[src["index"]]
    t = {k: x[k] for k in ("kind", "reason", "score", "peak_rate", "unique_users", "baseline", "delay",
                           "sample_messages", "chat_time_basis")}
    t["burst_start"], t["action_time"] = t0 + x["burst_start_rel"], t0 + x["action_time_rel"]
    return t


def run_pipeline_case(pc, det_out, defaults, t0, fmt=None):
    from live_clipper import candidates, editorial, formats, gameplay, winners
    from live_clipper.language_policy import auto_ineligible, decide
    fmt = fmt or formats.active()
    trig = trigger_for(pc, det_out, t0)
    trig["topic"] = dict(pc["topic"])
    if pc.get("review"):
        trig["review"] = pc["review"]
    act = trig["action_time"]
    pre, post = defaults["pre_roll"], defaults["post_roll"]
    ct = pc.get("clip_timing") or {}
    ws, we = act - pre, act + post
    src_t0 = act + ct["source_t0_offset_s"] if "source_t0_offset_s" in ct else ws
    dur = ct.get("duration", we - ws)
    clip = {"id": 1, "channel_id": 1, "status": "ready", "kind": trig["kind"], "action_time": act,
            "window_start": ws, "window_end": we, "created_at": act,
            "files": {"source": "source.ts", "source_t0": src_t0, "trim_in": ct.get("trim_in", 0.0),
                      "duration": dur},
            "timings": ({"source_available": act - pc["source_available_offset_s"]}
                        if pc.get("source_available_offset_s") is not None else {}),
            "trigger": trig}
    now = t0 + pc["now_rel"]
    tr = pc.get("transcript")
    if tr and tr.get("speech_status") == "no_speech":   # editorial.assess: no speech counts as empty
        tr = {"segments": []}
    sp = editorial.speech_evidence(tr, fmt)
    ch = editorial.chat_evidence(trig, fmt)
    v = editorial.decide(clip, pc.get("visual"), sp, ch, window=editorial.window_of(clip), now=now, fmt=fmt)
    clip["trigger"] = {**trig, "editorial": v}
    g = gameplay.evidence(clip, fmt)
    asr = pc.get("asr")
    lang = decide(asr, pc["channel_pref"], fmt)
    target = lang["lang"] if lang["status"] == "ok" else None
    clip["captions"] = {"asr": asr, "policy": {"render_default": target}}
    auto = auto_ineligible(asr, pc["channel_pref"], target,
                           fmt) if target else f"held for review: {lang['reason']}"
    below = candidates.below_threshold(clip, pc["min_score"])
    rx, rx_why = candidates.reaction(clip, fmt)
    fr, age = candidates.freshness(clip, now, fmt)
    if not target:
        state = ("not_eligible", "no final render yet (language policy hold: " + lang["reason"] + ")")
    elif g["state"] == "rejected":
        state = ("not_eligible", f"not {fmt.labels.game_short} gameplay: {g['detail']}")
    elif auto:
        state = ("not_eligible", auto)
    elif g["state"] != "approved":
        state = ("needs_review", f"needs review: {g['detail']}")
    elif below:
        state = ("held_rule", "no account will post it unattended: " + below)
    else:
        state = ("eligible", f"gameplay: {g['detail']}")
    feats, labels, missing = winners.features(None, clip)
    sig = v["signals"]
    return {"window": {"start_rel": rel(ws, t0), "end_rel": rel(we, t0), "action_rel": rel(act, t0),
                       "source_t0_rel": rel(src_t0, t0), "duration": dur},
            "speech_evidence": r6(sp), "chat_evidence": r6(ch),
            "game_context": editorial.game_context(trig, sp, fmt), "gameplay_context": gameplay.context(clip,
                                                                                                        fmt),
            "editorial": {"state": v["state"], "kind": v["kind"], "reason": v["reason"],
                          "evidence_s": r6(v["evidence_s"]), "evidence_at_rel": rel(v["evidence_at"], t0),
                          "trigger_s": r6(sig["trigger_s"]), "duration_s": r6(sig["duration_s"]),
                          "since_capture_start_s": r6(sig["since_capture_start_s"]), "version": v["version"]},
            "gameplay_evidence": g, "hold_reason": gameplay.hold_reason(clip, fmt),
            "language_decide": {k: lang[k] for k in ("status", "lang", "reason")}, "auto_ineligible": auto,
            "below_threshold": below, "reaction": r6(rx), "reaction_why": rx_why, "freshness": r6(fr),
            "age_h": r6(age), "base_priority": r6(rx * fr),
            "harness_derived_eligibility": {"state": state[0], "reason": state[1]},
            "winners_labels": labels, "winners_missing": missing,
            "winners_payoff_at_s": feats.get("payoff_at_s")}


def run(inputs: dict, fmt=None) -> dict:
    from live_clipper import candidates, editorial, formats, gameplay, moments, titles, winners
    from live_clipper.language_policy import decide, original_language_check
    fmt = fmt or formats.active()
    t0 = inputs["t0"]
    D = inputs["defaults"]
    out = {"schema_version": inputs["schema_version"], "generated_by": "runner.py (public)"}
    det_out = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for case in inputs["detector_cases"]:
            det_out[case["id"]] = run_detector_case(case, D, t0, tmp, fmt)
    out["detector_cases"] = det_out
    out["pipeline_cases"] = {pc["id"]: run_pipeline_case(pc, det_out, D, t0,
                                                         fmt) for pc in inputs["pipeline_cases"]}
    topics = []
    for tc in inputs["topic_cases"]:
        st = {"state": "live", **tc}
        ts, ts_nocat = formats.topic_state(st, True, fmt), formats.topic_state(st, False, fmt)
        trig = {"topic": {"state": ts, "title": tc["title"], "category": tc["category"]}}
        topics.append({**tc, "is_gta6_title": formats.title_matches(tc["title"], fmt),
                       "is_gta6_category": formats.category_matches(tc["category"], fmt),
                       "topic_state": ts, "topic_state_category_off": ts_nocat,
                       "gameplay_context": gameplay.context({"trigger": trig}, fmt)["state"],
                       "editorial_game_context": editorial.game_context(trig, {}, fmt),
                       "gate_opens_capture": formats.title_matches(tc["title"], fmt)})
    out["topic_cases"] = topics
    out["chat_evidence_cases"] = {c["id"]: editorial.chat_evidence(c["trigger"], fmt)
                                  for c in inputs["chat_evidence_cases"]}
    out["speech_evidence_cases"] = {c["id"]: r6(editorial.speech_evidence(c["transcript"], fmt))
                                    for c in inputs["speech_evidence_cases"]}
    L = inputs["language_cases"]
    out["language_cases"] = {
        "decide": [{k: d[k] for k in ("status", "lang", "reason")} for d in (decide(c["asr"], c["pref"], fmt)
                                                                           for c in L["decide"])],
        "original_language_check": [original_language_check(c["global"], c["windows"], fmt)
                                    for c in L["original_language_check"]]}
    M = inputs["moments_cases"]
    out["moments_cases"] = {"same": [moments._same(c["a"], c["b"]) for c in M["same"]],
                            "creator_earliest": [moments._creator_earliest(c["events"], c["moment"],
                                                                           c["after"])
                                                 for c in M["creator_earliest"]]}
    cands = []
    for cc in inputs["candidate_cases"]:
        clip = {k: v for k, v in cc["clip"].items() if k != "action_time_rel"}
        clip["action_time"] = t0 + cc["clip"]["action_time_rel"]
        now = t0 + cc["now_rel"]
        rx, why = candidates.reaction(clip, fmt)
        fr, age = candidates.freshness(clip, now, fmt)
        cands.append({"reaction": r6(rx), "why": why, "freshness": r6(fr), "age_h": r6(age),
                      "priority": r6(candidates.priority(clip, now, fmt))})
    out["candidate_cases"] = cands
    out["title_cases"] = [titles.make_title(c["transcript"], c["chat"], c["reason"], lang=c["lang"], fmt=fmt)
                          for c in inputs["title_cases"]]
    out["camera_face_cases"] = {c["id"]: [len(x) for x in editorial.camera_faces(c["faces"])]
                                for c in inputs["camera_face_cases"]}
    out["explore_hours"] = {str(h): winners.exploring(t0 + h * 3600) for h in inputs["explore_hours"]}
    return out


def expected_view(ref: dict) -> dict:
    """The reference output restricted to what this subsystem covers."""
    out = {k: ref[k] for k in COVERED}
    out["language_cases"] = {k: v for k, v in ref["language_cases"].items() if k != "classify_speech"}
    return out


def diff(a, b, path="") -> list[str]:
    """Human-readable differences between two JSON values (empty = identical)."""
    if type(a) is not type(b):                        # 22 vs 22.0 serializes differently: a difference
        return [f"{path or '/'}: {a!r} != {b!r}"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}/{k}: {'missing on the left' if k not in a else 'missing on the right'}")
            else:
                out += diff(a[k], b[k], f"{path}/{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += diff(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [f"{path or '/'}: {a!r} != {b!r}"]
