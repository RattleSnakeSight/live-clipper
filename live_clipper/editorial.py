"""Editorial check: is THIS clip a worthwhile moment of the selection format's game? Decided from the clip's
own
content, never from a stream title, category, greeting surge or viewer arrival alone.

A clip qualifies (state "qualified") as exactly one of:
  gameplay   identifiable gameplay on screen: the picture outside the streamer's camera keeps changing
  structure
             across the clip (scene motion, not lighting or a chat overlay) in at least half of the sampled
             frame
             pairs, AND the game is identified (the clip's own capture-time title/category/archive evidence,
             or the
             game named in its speech). The moment needs setup and payoff inside the clip. Silent gameplay
             qualifies
             on visual evidence alone.
  opinion    a substantive comment: the game is named in the speech with enough surrounding words to be a real
             statement, and the speech around it is not greetings/logistics.
  reaction   a hype/reaction moment over gameplay: chat reacts (not greets), gameplay is visible, with setup
  before
             and payoff after the trigger.
It is "rejected" with a short reason when the content shows the opposite (a camera/just-chatting view with no
gameplay, a static or waiting screen, an arriving-viewers greeting surge with no game content), and "unknown"
when it
cannot be judged (no media, gameplay visible but the game cannot be identified). Unknown and rejected clips
are never
sent unattended; an operator review still decides manually (live_clipper.gameplay).

Every verdict records: state, kind, a short reason, the evidence second (clip-relative and absolute), the
signals
used, the analysed window (re-checked when the cut changes) and VERSION. It is stored at trigger.editorial.

Keywords, thresholds and the game's name come from the selection format (live_clipper.formats); the module
constants below are the built-in format's values, kept for reference and compatibility.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

from . import formats

VERSION = 1
OPENING_S = 600.0            # a clip within this long after capture began is labelled "stream opening"
MIN_SETUP_S = 5.0            # seconds of clip before the trigger second (setup)
MIN_PAYOFF_S = 3.0           # ... and after it (payoff)
GAMEPLAY_RATIO = 0.5         # share of sampled frame pairs whose non-camera picture changed structure
CAMERA_VIEW_FACE_H = 0.3     # a camera face this tall (share of frame height) means the camera IS the picture
OPINION_WORDS = 14           # words of speech around a game mention for a substantive statement
OPINION_SPAN_S = 12.0
SWEEP_LIMIT = 3


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


def _fmt(fmt):
    return fmt or formats.active()


# ---------------------------------------------------------------- text evidence
def speech_evidence(transcript: dict | None, fmt: formats.SelectionFormat | None = None) -> dict:
    """Game mentions, substantive statements and greeting share from the clip's own transcript
    (clip-relative seconds)."""
    fmt = _fmt(fmt)
    kw, ed, mk = fmt.keywords, fmt.editorial, fmt.evidence_keys.mentions
    strong, game, greet_rx = formats.rx(kw.strong), formats.rx(kw.game_terms), formats.rx(kw.greet_speech)
    segs = [s for s in ((transcript or {}).get("segments") or []) if str(s.get("text") or "").strip()]
    words = []
    for s in segs:
        ws = s.get("words") or []
        if ws:
            words += [(float(w.get("start", s.get("start", 0))), _norm(w.get("word"))) for w in ws]
        else:
            toks = _norm(s.get("text")).split()
            a, b = float(s.get("start", 0)), float(s.get("end", 0))
            words += [(a + (b - a) * i / max(1, len(toks)), w) for i, w in enumerate(toks)]
    words = [(t, w) for t, w in words if w]
    text = " ".join(w for _, w in words)
    out = {"words": len(words), mk: [], "statement": None, "greeting_share": 0.0,
           "game_terms": len(game.findall(text))}
    if not words:
        return out
    greet_hits = sum(len(greet_rx.findall(_norm(s.get("text")))) for s in segs)
    out["greeting_share"] = round(min(1.0, greet_hits * 3 / max(1, len(words))), 3)
    joined, pos = "", []
    for t, w in words:                                    # char offset -> word time, for phrase matches
        pos.append((len(joined), t))
        joined += w + " "
    for m in strong.finditer(joined):
        at = max((t for off, t in pos if off <= m.start()), default=words[0][0])
        out[mk].append({"t": round(at, 2), "term": m.group(0).strip()})
    for g in out[mk]:
        near = [w for t, w in words if abs(t - g["t"]) <= ed.opinion_span_s]
        near_text = " ".join(near)
        greet = len(greet_rx.findall(near_text)) * 3 / max(1, len(near))
        if len(near) >= ed.opinion_words and greet < ed.statement_max_greeting:
            out["statement"] = {"t": g["t"], "words": len(near), "excerpt": near_text[:140]}
            break
    return out


def chat_evidence(trigger: dict | None, fmt: formats.SelectionFormat | None = None) -> dict:
    """What the chat burst was: greetings/arrivals vs reactions (from the burst's own recorded sample)."""
    fmt = _fmt(fmt)
    ed = fmt.editorial
    greet_rx, react_rx = formats.rx(fmt.keywords.greet_chat), formats.rx(fmt.keywords.react_chat)
    t = trigger or {}
    msgs = [_norm(m) for m in (t.get("sample_messages") or []) if str(m or "").strip()]
    reason = _norm(t.get("reason"))
    tops = re.findall(r"top: (.*)$", reason)
    if tops:                                  # "top: hi x5, late as always x3" weights repeated lines
        for part in tops[0].split(","):
            m = re.match(r"\s*(.+?)\s*x(\d+)\s*$", part)
            if m:
                msgs += [m.group(1)] * max(0, int(m.group(2)) - 1)
    n = len(msgs)
    greet = sum(1 for m in msgs if greet_rx.search(m))
    react = sum(1 for m in msgs if react_rx.search(m) and not greet_rx.search(m))
    return {"messages": n, "greeting_share": round(greet / n, 3) if n else 0.0,
            "reaction_share": round(react / n, 3) if n else 0.0,
            "greeting_surge": n >= ed.surge_min_messages and greet / n >= ed.surge_greeting_share
            and react / n < ed.surge_max_reaction_share,
            "kind": t.get("kind") or ""}


# ---------------------------------------------------------------- visual evidence
def _camera_mask(h: int, w: int, faces: list):
    import numpy as np
    m = np.zeros((h, w), bool)
    for f in faces or []:
        if f.get("h", 0) < 0.03:
            continue
        x0 = max(0, int((f["x"] - 1.5 * f["w"]) * w))
        x1 = min(w, int((f["x"] + 2.5 * f["w"]) * w))
        y0 = max(0, int((f["y"] - 0.6 * f["h"]) * h))
        y1 = min(h, int((f["y"] + 3.5 * f["h"]) * h))
        m[y0:y1, x0:x1] = True
    return m


def camera_faces(faces: list, min_share: float = 0.5) -> list:
    """Per frame, only the faces that belong to the streamer's camera: a face box that recurs at the same
    place and
    size in at least min_share of the frames. Game characters (cutscenes, close-ups) move and change size, so
    they
    are not the camera and never mask or veto gameplay."""
    n = len(faces)
    pts = [(i, f) for i, fl in enumerate(faces) for f in (fl or []) if f.get("h", 0) >= 0.03]
    keep = [[] for _ in range(n)]
    for i, f in pts:
        cx, cy = f["x"] + f["w"] / 2, f["y"] + f["h"] / 2
        frames = {j for j,
                  g in pts if abs(g["x"] + g["w"] / 2 - cx) <= 0.06 and abs(g["y"] + g["h"] / 2 - cy) <= 0.08
                  and 0.7 <= g["h"] / max(1e-6, f["h"]) <= 1.4}
        if len(frames) >= max(2, min_share * n):
            keep[i].append(f)
    return keep


def visual_evidence(F, times: list, faces: list | None = None, cols: int | None = None,
                    rows: int | None = None,
                    fmt: formats.SelectionFormat | None = None) -> dict:
    """Scene-structure change outside the camera between consecutive frames, per grid cell (normalised
    cross-correlation, so lighting changes and a scrolling chat column do not count as gameplay).
    F: uint8 array (n, h, w, 3) RGB; times: frame times (clip-relative s); faces: per-frame face boxes."""
    import numpy as np
    fmt = _fmt(fmt)
    vp, face_h = fmt.editorial.visual, fmt.editorial.camera_view_face_h
    cols = vp.cols if cols is None else cols
    rows = vp.rows if rows is None else rows
    n = len(F)
    out = {"frames": n, "pairs": 0, "gameplay_pairs": 0, "static_pairs": 0, "face_dominant_frames": 0,
           "gameplay_ratio": 0.0, "static_ratio": 0.0, "face_dominant_ratio": 0.0, "first_gameplay_t": None,
           "camera_view_t": None}
    if n < 2:
        return out
    faces = camera_faces(faces) if faces is not None else [[] for _ in range(n)]
    L = F.astype("float32") @ np.array([0.299, 0.587, 0.114], "float32")
    _, h, w = L.shape
    for i, fl in enumerate(faces):
        big = max((f.get("h", 0) for f in fl or []), default=0)
        if big >= face_h:
            out["face_dominant_frames"] += 1
            if out["camera_view_t"] is None:
                out["camera_view_t"] = round(float(times[i]), 2) if i < len(times) else None
    ch, cw = h // rows, w // cols
    big_face = [max((f.get("h", 0) for f in fl or []), default=0) >= face_h for fl in faces]
    for i in range(n - 1):
        if big_face[i] or big_face[i + 1]:
            out["pairs"] += 1                         # a camera view fills the frame: never gameplay
            continue
        mask = _camera_mask(h, w, faces[i]) | _camera_mask(h, w, faces[i + 1])
        active = usable = 0
        act_cols, act_rows = set(), set()
        for r in range(rows):
            for c in range(cols):
                sl = (slice(r * ch, (r + 1) * ch), slice(c * cw, (c + 1) * cw))
                if mask[sl].mean() > 0.5:
                    continue
                a, b = L[i][sl], L[i + 1][sl]
                sa, sb = float(a.std()), float(b.std())
                usable += 1
                if sa < vp.flat_std and sb < vp.flat_std:
                    continue                                  # flat in both: no structure to compare
                if sa < vp.flat_std or sb < vp.flat_std or \
                        float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb)) < vp.ncc:
                    active += 1                               # structure changed, appeared or vanished
                    act_cols.add(c)
                    act_rows.add(r)
        if usable < vp.min_usable * rows * cols:
            out["pairs"] += 1                         # too little picture outside the camera to show gameplay
            continue
        out["pairs"] += 1
        frac = active / usable
        spread = len(act_cols) >= cols // 2 and len(act_rows) >= rows // 2    # not one overlay column/strip
        if frac >= vp.active_frac and spread:
            out["gameplay_pairs"] += 1
            if out["first_gameplay_t"] is None and i + 1 < len(times):
                out["first_gameplay_t"] = round((float(times[i]) + float(times[i + 1])) / 2, 2)
        elif frac < vp.static_frac:
            out["static_pairs"] += 1
    p = max(1, out["pairs"])
    out.update(gameplay_ratio=round(out["gameplay_pairs"] / p, 3),
               static_ratio=round(out["static_pairs"] / p, 3),
               face_dominant_ratio=round(out["face_dominant_frames"] / n, 3))
    return out


# ---------------------------------------------------------------- decision
def game_context(trigger: dict | None, speech: dict,
                 fmt: formats.SelectionFormat | None = None) -> str | None:
    """How the game is identified for THIS clip (None = it is not)."""
    fmt = _fmt(fmt)
    ts, game = fmt.topic_states, fmt.labels.game
    t = trigger or {}
    topic = t.get("topic") or {}
    labels = {ts.title: f"capture-time {game} title", ts.category: f"capture-time {game} category",
              ts.archive: f"official archive {game} chapter"}
    if topic.get("state") in labels:
        return labels[topic["state"]]
    vod = t.get("vod") or {}
    vk = fmt.evidence_keys.vod_metadata
    if vod.get(vk):
        return f"VOD metadata names {game} ({str(vod.get(vk))[:60]})"
    if speech.get(fmt.evidence_keys.mentions):
        return f"{game} named in the clip's speech"
    return None


def decide(clip: dict, visual: dict | None, speech: dict, chat: dict, window: dict | None = None,
           now: float | None = None, fmt: formats.SelectionFormat | None = None) -> dict:
    fmt = _fmt(fmt)
    ed, game, mk = fmt.editorial, fmt.labels.game, fmt.evidence_keys.mentions
    t = clip.get("trigger") or {}
    files = clip.get("files") or {}
    t0 = float(files.get("source_t0") or clip.get("window_start") or 0)
    trim = float(files.get("trim_in") or 0)
    dur = float(files.get("duration") or 0) or max(0.0, float(clip.get("window_end") or 0) - float(
        clip.get("window_start") or 0))
    act = clip.get("action_time")
    act_rel = (float(act) - t0 - trim) if act else None            # trigger second inside the cut
    since = None
    sa = (clip.get("timings") or {}).get("source_available")
    if act and sa:
        since = float(act) - float(sa)
    opening = since is not None and 0 <= since < ed.opening_s
    ctx = game_context(t, speech, fmt)
    vis = visual or {}
    gameplay = vis.get("pairs", 0) >= ed.min_pairs and vis.get("gameplay_ratio", 0) >= ed.gameplay_ratio
    setup_ok = act_rel is None or (act_rel >= ed.min_setup_s and dur - act_rel >= ed.min_payoff_s)
    reaction = chat.get("reaction_share", 0) >= ed.reaction_share and not chat.get("greeting_surge")
    signals = {"visual": vis, "speech": {k: speech.get(k) for k in ("words", mk, "statement",
                                                                    "greeting_share", "game_terms")},
               "chat": chat, "game_context": ctx, "trigger_s": None if act_rel is None else round(act_rel, 2),
               "duration_s": round(dur, 2), "since_capture_start_s": None if since is None else round(since,
                                                                                                      1)}
    pre = f"stream opening ({int(since)} s after capture began): " if opening else ""

    def verdict(state, kind, reason, at_rel):
        at_rel = None if at_rel is None else round(float(at_rel), 2)
        return {"state": state, "kind": kind, "reason": (pre + reason)[:240],
                "evidence_s": at_rel,                                     # seconds into the cut
                "evidence_at": None if at_rel is None else round(t0 + trim + at_rel,
                                                                 2),   # absolute moment time
                "signals": signals, "window": window, "version": VERSION, "at": round(now or time.time(), 3)}

    st = speech.get("statement")
    if st:
        return verdict("qualified", "opinion", f"substantive {game} talk: \"{st['excerpt'][:90]}\"",
                       float(st["t"]) - trim)
    if gameplay and ctx:
        g_at = vis.get("first_gameplay_t")
        g_rel = None if g_at is None else float(g_at) - trim
        if chat.get("greeting_surge") and not reaction:
            return verdict("rejected", "",
                           f"arriving-viewer greetings over gameplay with no reaction or {game} talk; "
                           "a greeting surge alone is not a moment", act_rel)
        if not setup_ok:
            return verdict("rejected", "",
                           f"the trigger is {act_rel:.1f} s into a {dur:.1f} s cut: not enough setup "
                           f"or payoff to understand the moment", act_rel)
        kind = "reaction" if reaction else "gameplay"
        return verdict("qualified", kind,
                       (f"chat reaction over {game} gameplay ({ctx})" if kind == "reaction" else
                                           f"{game} gameplay on screen ({ctx}; scene change in "
                                           f"{vis['gameplay_pairs']}/{vis['pairs']} frame pairs)"), g_rel)
    if gameplay and not ctx:
        return verdict("unknown", "",
                       f"gameplay is visible but {game} is not identifiable for this clip (no {game} "
                       f"title/category/archive evidence and no {game} speech): operator review", None)
    if not vis.get("pairs") and not speech.get("words"):
        return verdict("unknown", "", "no frames or speech could be analysed for this clip", None)
    cam_t = vis.get("camera_view_t")
    if vis.get("face_dominant_ratio", 0) >= ed.face_dominant_ratio:
        why = "camera/just-chatting view with no gameplay on screen"
        at = None if cam_t is None else float(cam_t) - trim
    elif vis.get("static_ratio", 0) >= ed.static_ratio:
        why, at = "static or waiting screen: no gameplay on screen", act_rel
    else:
        why, at = f"no identifiable {game} gameplay on screen", act_rel
    if chat.get("greeting_surge"):
        why += "; the chat spike is arriving-viewer greetings"
    if speech.get("words") and speech.get("greeting_share", 0) >= ed.speech_greeting_share:
        why += "; the speech is greetings/logistics"
    if speech.get(mk) and not st:
        why += f"; {game} is only mentioned in passing"
    return verdict("rejected", "", why, at)


# ---------------------------------------------------------------- running it on a clip
def window_of(clip: dict) -> dict | None:
    f = (clip or {}).get("files") or {}
    if not f.get("source"):
        return None
    return {"source": f.get("source"), "trim_in": round(float(f.get("trim_in") or 0), 3),
            "duration": round(float(f.get("duration") or 0), 3)}


def current(clip: dict | None) -> dict | None:
    """The stored verdict if it is valid for the clip's current cut and this VERSION, else None."""
    v = ((clip or {}).get("trigger") or {}).get("editorial")
    if not isinstance(v, dict) or v.get("version") != VERSION:
        return None
    w = window_of(clip)
    if w is not None and v.get("window") is not None and v["window"] != w:
        return None
    return v


def _transcript(clip_dir: Path, transcript: dict | None) -> dict | None:
    if transcript is not None:
        return transcript
    p = clip_dir / "transcript.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None
    return None


def assess(clip: dict, clip_dir: Path, transcript: dict | None = None, now: float | None = None,
           fmt: formats.SelectionFormat | None = None) -> dict:
    """Blocking (run off the event loop, inside the media slot): frames + faces + transcript + chat ->
    verdict.
    Frame sampling and face boxes come from the editing subsystem (live_clipper.facecam / faces)."""
    from . import facecam, faces
    w = window_of(clip)
    vis = None
    if w and (clip_dir / w["source"]).exists():
        src = clip_dir / w["source"]
        info = facecam.probe(src)
        if info:
            samples = facecam.window_samples(src, w["trim_in"], w["duration"] or 1.0)
            start = min(t for _, t in samples) - 0.15
            end = max(t for _, t in samples) + 0.25
            F, times, rd = facecam.frames_timed(samples, info["w"], info["h"], aw=320, window=(start, end),
                                                offset=float(info.get("start") or 0))
            fl = None
            if len(F) >= 2 and faces.available():
                try:
                    fl = faces.faces_per_frame(F)
                except Exception:                              # noqa: BLE001 - faces are optional evidence
                    fl = None
            vis = visual_evidence(F, times, fl, fmt=fmt)
            vis["reads"] = rd
    tr = _transcript(clip_dir, transcript)
    if tr and tr.get("speech_status") == "no_speech":
        tr = {"segments": []}
    sp = speech_evidence(tr, fmt)
    ch = chat_evidence(clip.get("trigger"), fmt)
    return decide(clip, vis, sp, ch, window=w, now=now, fmt=fmt)


def record(store, clip_id: int, verdict: dict) -> dict:
    store.merge_json("clips", clip_id, "trigger", {"editorial": verdict})
    store.log(f"clip {clip_id}: editorial {verdict['state']}"
              + (f" ({verdict['kind']})" if verdict.get("kind") else "") + f": {verdict['reason']}"
              + (f" [evidence at {verdict['evidence_s']:.1f} s]"
                 if verdict.get("evidence_s") is not None else ""),
              "info", clip_id=clip_id)
    return verdict


async def check(engine, clip_id: int, transcript: dict | None = None) -> dict | None:
    """Assess one clip inside the shared media slot and persist the verdict."""
    import asyncio
    import contextlib
    clip = engine.store.clip(clip_id)
    if not clip:
        return None
    mt = getattr(engine, "maintenance", None)
    async with (mt.media_slot() if mt else contextlib.nullcontext()):
        v = await asyncio.to_thread(assess, clip, engine.clip_dir(clip_id), transcript)
    return record(engine.store, clip_id, v)


def needs_check(clip: dict | None, now: float, max_age_s: float) -> bool:
    if not clip or clip.get("status") != "ready" or current(clip) is not None:
        return False
    at = clip.get("action_time") or clip.get("created_at") or 0
    return now - float(at) < max_age_s and bool(window_of(clip))


async def sweep(engine, now: float, limit: int = SWEEP_LIMIT) -> list[int]:
    """Recent ready clips without a valid verdict get one (bounded, media slot). A clip that qualifies but
    whose final
    was skipped earlier (held_editorial) or never rendered gets its original-language final now."""
    from . import freshness
    from .postprocess import render_final
    store = engine.store
    mx = freshness.max_age_s(engine.settings)
    done: list[int] = []
    for r in store.q("SELECT id FROM clips WHERE status='ready' AND action_time>? ORDER BY action_time DESC",
                     now - mx):
        if len(done) >= limit:
            break
        if engine.busy(r["id"]) if hasattr(engine, "busy") else False:
            continue
        clip = store.clip(r["id"])
        if not needs_check(clip, now,
                           mx) or ((clip.get("trigger") or {}).get("review") or {}).get("gameplay"):
            continue
        v = await check(engine, r["id"])
        done.append(r["id"])
        if v and v.get("state") == "qualified":
            clip = store.clip(r["id"]) or {}
            caps = clip.get("captions") or {}
            lang = (caps.get("policy") or {}).get("render_default")
            if lang and not ((clip.get("files") or {}).get("final") or {}).get(lang) and \
                    (caps.get(lang) or {}).get("status") == "ready":
                await render_final(engine, r["id"], lang)
    return done
