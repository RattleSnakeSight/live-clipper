"""After the cut: captions (source language first, then translation), titles, per-language
final renders with burned captions, then publishing. Also operator edits (trim, reframe,
caption correction) that reuse the clip's source.ts and transcript — never the whole stream.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from .render import layout_filter, size_for, wrap_title

REFERENCE_TITLE_FONT = "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf"   # Debian fonts-inter


def resolve_title_font() -> str | None:
    """Inter ExtraBold for the hook title on every OS: the bundled copy (SIL OFL, live_clipper/fonts), else the
    Debian system path the original design used, else None (drawtext then uses its default font)."""
    for p in (Path(__file__).parent / "fonts" / "Inter-ExtraBold.otf", Path(REFERENCE_TITLE_FONT)):
        if p.is_file():
            return p.as_posix()        # forward slashes: no backslash escaping inside the filtergraph
    return None


TITLE_FONT = resolve_title_font()
ASR_KEYS = ("language", "language_probability", "language_share_en_es", "top_language", "top_language_probability",
            "speech_s", "no_speech_prob", "speech_status", "speech_note", "windows", "check_pending", "original_check")
CHECKED = "__original_language_checked__"
LANGS = ("en", "es")


def _rel(clip_dir: Path, p: str) -> str:
    return os.path.relpath(p, clip_dir)


def get_captioner(engine):
    with engine._captioner_lock:            # called from the event loop thread and the ASR thread
        return _get_captioner(engine)


def _get_captioner(engine):
    if engine.captioner is None:
        from .captions.asr import Transcriber
        from .captions.translate import Translator
        s = engine.settings
        engine.captioner = (Transcriber(s.whisper_model, s.whisper_device, s.asr_threads,
                                        download_root=str(s.models_dir / "whisper"),
                                        compute_type=getattr(s, "whisper_compute_type", None)),
                            Translator(str(s.models_dir), threads=s.asr_threads))
    return engine.captioner


async def run_ffmpeg(args: list[str], cwd: str | None = None) -> tuple[int, str]:
    from .proc import LOW_PRIO, niced
    p = await asyncio.create_subprocess_exec(*niced(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], 8),
                                             stderr=asyncio.subprocess.PIPE, cwd=cwd, **LOW_PRIO)
    try:
        _, err = await p.communicate()
    except asyncio.CancelledError:
        if p.returncode is None:
            p.kill()
            await p.wait()
        raise
    return p.returncode, (err or b"").decode(errors="replace")[-400:]


async def render_video(engine, clip: dict, out: Path, *, height: int, lang: str | None = None,
                       title: str | None = None) -> tuple[int, str]:
    d = engine.clip_dir(clip["id"])
    f = clip["files"]
    s = engine.settings                        # optional preset values default to the original constants
    ass = str(d / clip["captions"][lang]["ass"]) if lang and clip["captions"].get(lang, {}).get("ass") else None
    hook = f"hook_{lang or 'x'}_{os.getpid()}_{time.monotonic_ns() % 10**9}.txt" if title else None
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.{time.monotonic_ns()}.part.mp4")   # unique per render
    temps: list = []
    mt = getattr(engine, "maintenance", None)
    cold = getattr(engine, "cold", None)
    if cold is not None and f.get("source"):   # an original released to the cold tier comes back (verified) first
        await cold.ensure_local_async(clip["id"], f["source"])
    try:                                       # temp files are removed on every path, exceptions included
        if hook:
            # the hook goes to a UTF-8 file with a fixed safe name; ffmpeg runs in the clip folder and reads it via
            # drawtext textfile=, so no punctuation in the text can reach the filtergraph parser
            (d / hook).write_text(wrap_title(title, getattr(s, "title_wrap", 20)), encoding="utf-8")
        from . import facecam, render
        src_path = d / f["source"]
        info = await asyncio.to_thread(facecam.probe, src_path)
        lay = clip.get("layout") or {}
        win = render.window_key(f["source"], src_path.stat().st_size if src_path.exists() else 0,
                                float(f.get("trim_in") or 0), float(f.get("duration") or 0))
        decide = render.needs_detection(lay, win)  # first final, or a different window (trim): decide on it

        def build(lay):
            sub = ass
            if sub and render.effective(lay).get("mode") == "split":   # captions below the seam, never on play
                split_name = f"split_{lang or 'x'}_{os.getpid()}_{time.monotonic_ns() % 10**9}.ass"
                sub = render.split_ass(sub, d / split_name, lay)
                temps.append(d / split_name)
            graph = layout_filter(lay, height, ass=sub, title_file=hook, font=TITLE_FONT,
                                  title_secs=getattr(s, "title_secs", 2.8),
                                  fps=render.fps_cap(s.output_fps, (info or {}).get("fps")))
            seek = ["-ss", f"{f['trim_in']:.3f}", "-i", str(d / f["source"])]
            if getattr(s, "accurate_seek", False):  # opt-in: output-side seek, video starts exactly at trim_in
                seek = seek[2:] + seek[:2]
            return [*seek, "-t", f"{f['duration']:.3f}",
                    "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", s.x264_preset, "-crf", str(int(getattr(s, "render_crf", 21))),
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                    "-movflags", "+faststart", "-f", "mp4", str(tmp)]
        args = None if decide else build(lay)
        async with (mt.media_slot() if mt else contextlib.nullcontext()):    # bounded encodes; wait at critical disk
            if decide:                             # the auto decision on the FULL window, inside the media slot
                det = await facecam.detect_async(facecam.window_samples(src_path, float(f.get("trim_in") or 0),
                                                                        float(f.get("duration") or 1)),
                                                 info, rect=lay.get("facecam"))
                lay = render.resolve(lay, det, window=win)
                clip["layout"] = lay               # the caller persists it (edits stage it with the rest)
                if not clip.get("_staged"):
                    cur = engine.store.clip(clip["id"]) or {}
                    if (cur.get("layout") or {}).get("mode") == "auto":
                        engine.store.update("clips", clip["id"], layout=lay)
                args = build(lay)
            rc, err = await run_ffmpeg(args, cwd=str(d))
        if rc == 0:
            os.replace(tmp, out)
        return rc, err
    finally:
        if hook:
            (d / hook).unlink(missing_ok=True)
        for t_ in temps:
            t_.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


async def render_final(engine, clip_id: int, lang: str, notify: bool = True) -> None:
    async with engine.render_lock(clip_id):      # one render per clip at a time (pipeline vs. operator edits)
        await _render_final(engine, clip_id, lang, notify)


async def _render_final(engine, clip_id: int, lang: str, notify: bool = True) -> None:
    store = engine.store
    clip = store.clip(clip_id)
    if not clip or clip["status"] in ("discarded", "expired"):
        return
    out = engine.clip_dir(clip_id) / f"final_{lang}.mp4"
    t0 = time.time()
    title = (clip["captions"].get(lang) or {}).get("title") or clip["title"] or None
    try:
        rc, err = await render_video(engine, clip, out, height=engine.settings.final_height, lang=lang, title=title)
    except asyncio.CancelledError:
        raise
    except Exception as e:                     # e.g. the hook file could not be written: a retryable failure
        rc, err = -1, f"{type(e).__name__}: {e}"
        if type(e).__name__ == "ColdError":    # the original is archived and could not be restored now: retryable
            rc, err = -1, str(e)
    if rc != 0:
        store.log(f"clip {clip_id}: final {lang} render failed: {err}", "error", clip_id=clip_id)
        store.merge_json("clips", clip_id, "captions", {lang: {**clip["captions"].get(lang, {}), "final_status": "failed",
                                                                "final_error": (err or f"ffmpeg exit {rc}")[-300:],
                                                                "final_failed_at": time.time()}})
        return
    store.mark(clip_id, f"final_{lang}")
    with store._lock:
        c = store.clip(clip_id)
        files = c["files"]
        files.setdefault("final", {})[lang] = out.name
        fv = files.setdefault("final_version", {})
        fv[lang] = int(fv.get(lang, 0)) + 1         # media version: lets publishing tell edited clips apart
        from .render import snapshot  # what THIS render used, immutable per media version
        files.setdefault("final_layout", {}).setdefault(lang, {})[str(fv[lang])] = snapshot(clip["layout"])
        caps = c["captions"]
        caps.setdefault(lang, {})["final_status"] = "ready"
        caps[lang].pop("final_error", None)
        caps[lang].pop("final_failed_at", None)
        caps[lang]["final_render_s"] = round(time.time() - t0, 2)
        store.update("clips", clip_id, files=files, captions=caps)
    engine.emit("clip", {"id": clip_id})
    if notify:
        _notify(engine, clip_id, [lang])


def caption_style(engine, play_res) -> dict | None:
    """The preset's ASS caption style for this resolution (None: subs.default_style)."""
    f = getattr(engine.settings, "caption_style", None)
    return f(play_res) if callable(f) else None


async def post_cut(engine, clip_id: int) -> None:
    from .language_policy import channel_pref, decide, original_language_check  # owned by the selection side
    store = engine.store
    clip = store.clip(clip_id)
    d = engine.clip_dir(clip_id)
    if not clip["files"].get("audio"):
        # no audio track: a playable draft held for review (finals can still be rendered on request)
        store.update("clips", clip_id, status="ready",
                     captions={**{l: {"status": "no_audio", "final_status": "on_request"} for l in LANGS},
                               "asr": {"speech_status": "no_speech", "speech_note": "clip has no audio track"}})
        engine.emit("clip", {"id": clip_id})
        return
    store.update("clips", clip_id, status="captioning",
                 captions={l: {"status": "pending"} for l in LANGS})
    loop = asyncio.get_running_loop()
    ready_q: asyncio.Queue = asyncio.Queue()
    ch = store.channel(clip["channel_id"]) or {"settings": {}}
    glossary = (ch["settings"] or {}).get("glossary") or []
    play_res = size_for(engine.settings.final_height)
    style = caption_style(engine, play_res)
    render_both = getattr(engine.settings, "render_languages", "original") == "both"

    holder: dict = {}

    def on_ready(lang, paths, secs):
        now = time.time()
        store.mark(clip_id, f"{lang}_ready", now)
        store.merge_json("clips", clip_id, "captions",
                         {lang: {"status": "ready", **{k: _rel(d, v) for k, v in paths.items()}}})
        loop.call_soon_threadsafe(ready_q.put_nowait, lang)

    early = engine.early_asr.pop(clip_id, None)
    if early and "future" not in early:
        early = None                               # early pass never got submitted; transcribe the whole clip

    class _Keep:                       # keeps the transcript so titles can be made before translation ends
        def __init__(self, tr): self.tr = tr
        def transcribe(self, wav):
            t = split_transcribe(self.tr, wav, early, d) if early else self.tr.transcribe(wav)
            if t.get("speech_status") == "no_speech":
                # Whisper can hallucinate text over music/game audio: keep it for audit, caption nothing
                t = {**t, "discarded_segments": t["segments"], "segments": []}
            elif t.get("speech_status") == "speech":
                t["check_pending"] = True          # original-language check runs before translation
            # persist speech status BEFORE any language-ready dispatch: the auto-post policy reads it
            store.merge_json("clips", clip_id, "captions", {"asr": {k: t.get(k) for k in ASR_KEYS}})
            holder["transcript"] = dict(t)
            return t

    def check_original(t):
        """Runs in the ASR thread after the source captions and before translation: positive per-window evidence
        that the speech is coherently in the detected language. Persisted before any automatic render."""
        if t.get("speech_status") == "speech":
            tr = get_captioner(engine)[0]
            try:
                w = tr.window_languages(wav_path, t["segments"]) if hasattr(tr, "window_languages") else {"windows": []}
            except Exception as e:
                w = {"windows": [{"start": 0, "end": 0, "lang": "", "prob": 0, "error": str(e)[:120]}]}
            t["windows"] = w["windows"]
            t["original_check"] = original_language_check(t.get("language"), w["windows"])
        t["check_pending"] = False
        store.merge_json("clips", clip_id, "captions", {"asr": {k: t.get(k) for k in ASR_KEYS}})
        loop.call_soon_threadsafe(ready_q.put_nowait, CHECKED)

    wav_path = str(d / clip["files"]["audio"])

    def work():
        from .captions.pipeline import caption_clip
        tr, tl = get_captioner(engine)
        return caption_clip(wav_path, d, _Keep(tr), tl, play_res=play_res, glossary_terms=glossary,
                            on_ready=on_ready, before_translate=check_original, style=style)

    fut = asyncio.wrap_future(engine.asr_pool.submit(work))
    ready: set[str] = set()
    checked = False
    target: Optional[str] = None
    rendered: list[str] = []

    async def maybe_render():
        # preset render_languages="both": the other language follows the original-language final automatically
        for lang in ([target] + [x for x in LANGS if x != target] if render_both and target else [target]):
            if checked and lang and lang in ready and lang not in rendered:
                # the editorial check runs on the actual clip (frames + speech + chat) BEFORE the expensive final; a
                # rejected moment is not rendered (an operator can still review it and render on request)
                if await editorial_gate(engine, clip_id, holder.get("transcript"), lang):
                    await render_final(engine, clip_id, lang, notify=False)   # dispatch waits until analysis is saved
                rendered.append(lang)

    while len(ready) < len(LANGS) or not checked:
        getter = asyncio.ensure_future(ready_q.get())
        done, _ = await asyncio.wait({getter, fut}, return_when=asyncio.FIRST_COMPLETED)
        if getter not in done:
            getter.cancel()
            if fut.exception() is not None:
                # keep whatever is usable (preview, original-language final); mark the rest failed, visibly
                e = fut.exception()
                msg = f"captions failed: {type(e).__name__}: {e}"[:300]
                missing = [l for l in LANGS if l not in ready]
                store.merge_json("clips", clip_id, "captions",
                                 {l: {"status": "failed", "error": msg} for l in missing})
                store.update("clips", clip_id, status="ready", error=f"{', '.join(missing)} {msg}")
                store.log(f"clip {clip_id}: {', '.join(missing)} {msg}", "error", clip_id=clip_id)
                _notify(engine, clip_id, rendered)
                engine.emit("clip", {"id": clip_id})
                return
            await asyncio.sleep(0)           # deliver callbacks the worker thread scheduled before finishing
            if ready_q.empty():
                break
            continue
        ev = getter.result()
        if (store.clip(clip_id) or {}).get("status") in ("discarded", "expired"):
            return
        if ev == CHECKED:
            checked = True
            asr = (store.clip(clip_id)["captions"] or {}).get("asr")
            sel = getattr(engine, "selection", None)
            fmt = sel.fmt(store.clip(clip_id)) if sel is not None else None
            pref = channel_pref((store.channel(clip["channel_id"]) or {}).get("settings"), fmt)
            dec = decide(asr, pref, fmt)
            target = dec["lang"] if dec["status"] == "ok" else None
            store.merge_json("clips", clip_id, "captions", {
                "policy": {"render_default": target, "pref_at_render": pref, "reason": dec["reason"]},
                **{l: {"final_status": "on_request"} for l in LANGS if l != target and not (render_both and target)}})
            await maybe_render()
            continue
        if not ready:
            tr = holder.get("transcript") or {"segments": []}
            _set_titles(engine, clip_id, tr, tr.get("language", ev))
            store.update("clips", clip_id, source_lang=tr.get("language", ev), status="rendering")
        ready.add(ev)
        if checked and ev != target and not (render_both and target):
            store.merge_json("clips", clip_id, "captions", {ev: {"final_status": "on_request"}})
        await maybe_render()
    res = await fut
    if (store.clip(clip_id) or {}).get("status") in ("discarded", "expired"):
        return
    tr = res["transcript"]
    asr = {k: tr.get(k) for k in ASR_KEYS}
    patch = {"asr": asr}
    if asr.get("speech_status") == "no_speech":
        patch.update({l: {"status": "no_speech", "note": asr.get("speech_note", "")} for l in LANGS})
    store.merge_json("clips", clip_id, "captions", patch)
    (d / "transcript.json").write_text(json.dumps(res["transcript"], ensure_ascii=False))
    store.merge_json("clips", clip_id, "timings", {"caption_detail": {k: round(v, 3) for k, v in res["timings"].items()
                                                                       if isinstance(v, (int, float))}})
    store.update("clips", clip_id, status="ready")
    _notify(engine, clip_id, rendered)              # speech status + language check are persisted by now
    engine.emit("clip", {"id": clip_id})


async def editorial_gate(engine, clip_id: int, transcript: Optional[dict], lang: str) -> bool:
    """Run (or reuse) the clip's editorial verdict; False = do not render the final (rejected). Never raises: a failed
    check leaves the verdict missing (held, retried by the sweep) and the final is rendered as before."""
    from . import editorial
    clip = engine.store.clip(clip_id) or {}
    if ((clip.get("trigger") or {}).get("review") or {}).get("gameplay"):
        return True                                   # an operator already decided about this clip
    v = editorial.current(clip)
    sel = getattr(engine, "selection", None)
    if v is None:
        try:
            v = await editorial.check(engine, clip_id, transcript, fmt=sel.fmt(clip) if sel is not None else None)
        except Exception as e:                        # noqa: BLE001 - the sweep retries; nothing is lost
            engine.store.log(f"clip {clip_id}: editorial check failed: {type(e).__name__}: {e}", "warn",
                             clip_id=clip_id)
            return True
    if v and v.get("state") == "rejected":
        engine.store.merge_json("clips", clip_id, "captions", {lang: {
            "final_status": "held_editorial", "note": f"final not rendered: {v['reason']}"[:300]}})
        if sel is not None:
            sel.rejected(clip_id, v)
        return False
    return True


def _notify(engine, clip_id: int, langs) -> None:
    if engine.publisher:
        for lang in langs:
            try:
                engine.publisher.on_language_ready(clip_id, lang)
            except Exception as e:
                engine.store.log(f"publisher hook failed: {e}", "error", clip_id=clip_id)


# ---------------------------------------------------------------- split transcription
MIN_EARLY_S = 8.0


async def early_transcribe(engine, clip_id: int, d: Path, prog: dict) -> None:
    """Pass A: speech recognition of the pre-roll that is already buffered at trigger time."""
    clip_start = prog["clip_start"]
    split = time.time() - clip_start - 1.0        # media up to ~now is in the buffer
    if split < MIN_EARLY_S:
        return
    engine.early_asr[clip_id] = {"pending": True}  # placeholder: post_cut knows an early pass is coming
    end = time.time() + 5
    while prog.get("fed_until", 0) < clip_start + split and time.time() < end:
        await asyncio.sleep(0.05)
    split = min(split, prog.get("fed_until", clip_start) - clip_start - 0.3)
    if split < MIN_EARLY_S:
        return
    wav = d / "audio_pre.wav"
    src = d / "source.ts"            # being written by the cut; holds everything fed so far
    rc, err = await run_ffmpeg(["-i", str(src), "-ss", f"{max(0.0, prog.get('trim_in', 0.0)):.3f}", "-t", f"{split:.3f}",
                                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    if rc != 0:
        raise RuntimeError(err)
    tr, _ = await asyncio.to_thread(get_captioner, engine)
    if engine.early_asr.get(clip_id, {}).get("pending") is not True:
        wav.unlink(missing_ok=True)                # cut finished first / clip gone: skip, don't waste ASR time
        return
    fut = engine.asr_pool.submit(tr.transcribe, str(wav))
    engine.early_asr[clip_id] = {"split": split, "future": fut, "wav": wav, "t0": time.time()}


def split_transcribe(tr, wav_full, early: dict, d: Path) -> dict:
    """Pass B (tail only) merged with pass A at a pause between words near the split point."""
    import subprocess
    fut = early["future"]
    if not fut.done() or fut.exception() is not None:
        return tr.transcribe(wav_full)
    a = fut.result()
    split = early["split"]
    words_a = [w for s in a["segments"] for w in s.get("words", [])]
    # cut in the widest gap between A's words inside [split-4, split-1.5] (fallback split-2)
    cut, best = split - 2.0, -1.0
    for w1, w2 in zip(words_a, words_a[1:]):
        if split - 4.0 <= w1["end"] <= split - 1.5 and w2["start"] - w1["end"] > best:
            best, cut = w2["start"] - w1["end"], (w1["end"] + w2["start"]) / 2
    b_start = max(0.0, cut - 1.0)
    tail = d / "audio_tail.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{b_start:.3f}", "-i", str(wav_full),
                    "-c:a", "pcm_s16le", str(tail)], check=True)
    b = tr.transcribe(str(tail), language=a["language"])
    segs = []
    for s in a["segments"]:
        ws = [w for w in s.get("words", []) if w["start"] < cut]
        if ws:
            segs.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": "".join(w["word"] for w in ws).strip(), "words": ws})
    for s in b["segments"]:
        ws = [{**w, "start": round(w["start"] + b_start, 3), "end": round(w["end"] + b_start, 3)} for w in s.get("words", [])]
        ws = [w for w in ws if w["start"] >= cut]
        if ws:
            segs.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": "".join(w["word"] for w in ws).strip(), "words": ws})
    for p in (tail, early["wav"]):
        try:
            p.unlink()
        except OSError:
            pass
    meta = {k: a.get(k) for k in ("language_share_en_es", "top_language", "top_language_probability", "no_speech_prob")}
    speech_s = round(sum(max(0.0, s["end"] - s["start"]) for s in segs), 2)
    from .captions.asr import classify_speech
    status, note = (classify_speech({a.get("top_language") or a["language"]: a.get("top_language_probability") or 0.0,
                                     a["language"]: a.get("language_probability") or 0.0}, a["language"], speech_s,
                                    a.get("duration", 0) + b.get("duration", 0), a.get("no_speech_prob") or 0.0)
                    if a.get("language_probability") is not None else ("speech", ""))
    return {**meta, "speech_s": speech_s, "speech_status": status, "speech_note": note,
            "language": a["language"], "language_probability": a.get("language_probability"),
            "duration": a.get("duration", 0) + b.get("duration", 0), "segments": segs,
            "timing": {"split_at_s": round(cut, 2), "pre_roll_s": a["timing"]["transcribe_s"],
                       "tail_s": b["timing"]["transcribe_s"]}}


def _set_titles(engine, clip_id: int, transcript: dict, src_lang: str) -> None:
    from .titles import make_title
    clip = engine.store.clip(clip_id)
    trig = clip["trigger"]
    msgs = trig.get("sample_messages") or []
    titles = {}
    sel = getattr(engine, "selection", None)  # effective selection format + title template (live_clipper.pipeline)
    bases = {}
    for lang in LANGS:
        try:
            if sel is not None:
                bases[lang], titles[lang] = sel.title(clip, transcript, msgs, trig.get("reason", ""), lang)
            else:
                titles[lang] = make_title(transcript, msgs, trig.get("reason", ""), lang=lang)
        except Exception as e:
            engine.store.log(f"title ({lang}) failed: {e}", "warn", clip_id=clip_id)
    if titles:
        caps = engine.store.clip(clip_id)["captions"]
        for lang, t in titles.items():
            caps.setdefault(lang, {})["title"] = t
            if lang in bases:
                caps[lang]["title_base"] = bases[lang]
        engine.store.update("clips", clip_id, title=titles.get("en") or titles.get(src_lang, ""), captions=caps)


# ---------------------------------------------------------------- operator edits
class EditError(ValueError):
    """Invalid edit input; nothing was scheduled or changed."""


EDIT_LANGS = ("en", "es")
TITLE_MAX = 100
LAYOUT_MODES = ("fit", "crop", "stack", "split", "auto")
MAX_SRT_BYTES = 200_000


def _num(v, name: str, lo: float, hi: float) -> float:
    import math
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise EditError(f"{name} must be a finite number")
    v = float(v)
    if not lo <= v <= hi:
        raise EditError(f"{name} must be between {lo:g} and {hi:g}")
    return v


def contained(base: Path, rel: str) -> Path:
    """Resolve rel inside base or raise (no writes outside the clip directory)."""
    p = (base / rel).resolve()
    if base.resolve() not in p.parents:
        raise EditError(f"path escapes the clip directory: {rel!r}")
    return p


def validate_edit(engine, clip: dict, body: dict) -> dict:
    """Validate an edit request completely before anything is scheduled. Returns the normalized edit."""
    from .captions.subs import parse_srt
    if not isinstance(body, dict):
        raise EditError("edit body must be an object")
    unknown = set(body) - {"layout", "trim", "srt", "title"}
    if unknown:
        raise EditError(f"unknown edit fields: {sorted(unknown)}")
    out: dict = {}
    d = engine.clip_dir(clip["id"])
    files = clip["files"] or {}
    if not files.get("source") or not (d / files["source"]).is_file():
        cold = getattr(engine, "cold", None)
        if cold is not None and files.get("source") and cold.archived(clip["id"], files["source"]):
            raise EditError("cold: this clip's original is archived in the cold tier and is not restored yet")
        raise EditError("this clip has no source media any more (expired or discarded)")
    src_len = _duration(d / files["source"])
    if body.get("trim") is not None:
        t = body["trim"]
        if not isinstance(t, (list, tuple)) or len(t) != 2:
            raise EditError("trim must be [start, end] in seconds relative to the current clip start")
        a = _num(t[0], "trim start", -600, 600)
        b = _num(t[1], "trim end", -600, 600)
        new_in = files["trim_in"] + a
        if b - a < 2:
            raise EditError("trimmed clip would be shorter than 2 s")
        if b - a > engine.settings.max_clip_seconds:
            raise EditError(f"trimmed clip would be longer than {engine.settings.max_clip_seconds:g} s")
        if new_in < -0.05 or new_in + (b - a) > src_len + 0.05:
            raise EditError(f"trim must stay inside the captured source ({-files['trim_in']:.1f} s to "
                            f"{src_len - files['trim_in']:.1f} s relative to the clip start)")
        out["trim"] = (a, b)
    if body.get("layout") is not None:
        L = body["layout"]
        if not isinstance(L, dict) or not L:
            raise EditError("layout must be an object")
        bad = set(L) - {"mode", "zoom", "dy", "cx", "facecam", "gameplay", "facecam_frac", "pin"}
        if bad:
            raise EditError(f"unknown layout fields: {sorted(bad)}")
        n = {}
        if "mode" in L:
            if L["mode"] not in LAYOUT_MODES:
                raise EditError(f"layout mode must be one of {LAYOUT_MODES}")
            n["mode"] = L["mode"]
        for k, lo, hi in (("zoom", 1.0, 4.0), ("dy", -0.5, 0.5), ("cx", 0.0, 1.0), ("facecam_frac", 0.15, 0.6)):
            if k in L:
                n[k] = _num(L[k], f"layout {k}", lo, hi)
        for k in ("facecam", "gameplay"):
            if k in L:
                r = L[k]
                if not isinstance(r, (list, tuple)) or len(r) != 4:
                    raise EditError(f"layout {k} must be [x, y, w, h] as fractions of the source")
                x, y, w, h = (_num(v, f"layout {k}", 0, 1) for v in r)
                if w <= 0 or h <= 0 or x + w > 1.0001 or y + h > 1.0001:
                    raise EditError(f"layout {k} rectangle must lie inside the frame")
                n[k] = [x, y, w, h]
        if "pin" in L:
            if not isinstance(L["pin"], bool):
                raise EditError("layout pin must be true or false")
            n["pin"] = L["pin"]
        if n.get("mode") == "split" and "facecam" not in L and not (clip.get("layout") or {}).get("facecam"):
            raise EditError("split needs facecam coordinates [x, y, w, h] (or use auto)")
        out["layout"] = n
    if body.get("srt") is not None:
        S = body["srt"]
        if not isinstance(S, dict) or not S:
            raise EditError("srt must be an object like {\"en\": \"...\"}")
        dur = (out["trim"][1] - out["trim"][0]) if "trim" in out else files.get("duration") or src_len
        norm = {}
        for lang, text in S.items():
            if lang not in EDIT_LANGS:
                raise EditError(f"caption language must be one of {EDIT_LANGS}, not {lang!r}")
            if not isinstance(text, str) or not text.strip():
                raise EditError(f"{lang} captions must be non-empty SRT text")
            if len(text.encode()) > MAX_SRT_BYTES:
                raise EditError(f"{lang} captions are too large")
            try:
                cues = parse_srt(text)
            except Exception as e:
                raise EditError(f"{lang} captions are not valid SRT: {e}")
            if not cues:
                raise EditError(f"{lang} captions contain no cues")
            for c in cues:
                if not (0 <= c["start"] < c["end"] <= dur + 1.0):
                    raise EditError(f"{lang} cue {c['start']:.2f}-{c['end']:.2f} s is outside the clip (0-{dur:.1f} s)")
            cap = (clip["captions"] or {}).get(lang) or {}
            contained(d, cap.get("srt") or f"captions/{lang}.srt")
            norm[lang] = text
        out["srt"] = norm
    if body.get("title") is not None:
        T = body["title"]
        if not isinstance(T, dict) or not T:
            raise EditError('title must be an object like {"es": "..."}')
        titles = {}
        for lang, text in T.items():
            if lang not in EDIT_LANGS:
                raise EditError(f"title language must be one of {EDIT_LANGS}")
            if not isinstance(text, str):
                raise EditError(f"{lang} title must be text")
            t = " ".join(text.split())
            if not 1 <= len(t) <= TITLE_MAX:
                raise EditError(f"{lang} title must be 1-{TITLE_MAX} characters")
            if any(ord(ch) < 32 for ch in t):
                raise EditError(f"{lang} title contains control characters")
            if ((clip["captions"] or {}).get(lang) or {}).get("status") != "ready":
                raise EditError(f"no {lang} captions to title")
            titles[lang] = t
        out["title"] = titles
    if not out:
        raise EditError("nothing to change: send trim, layout, srt or title")
    return out


async def rerender_preview(engine, clip_id: int) -> None:
    clip = engine.store.clip(clip_id)
    d = engine.clip_dir(clip_id)
    async with engine.render_lock(clip_id):
        rc, err = await render_video(engine, clip, d / "preview.mp4", height=engine.settings.preview_height)
    if rc != 0:
        raise RuntimeError(err)


async def apply_edit(engine, clip_id: int, *, layout: dict | None = None, trim: tuple[float, float] | None = None,
                     srt: dict | None = None, title: dict | None = None) -> None:
    """Transactional edit: new captions and videos are produced in a staging area and swapped in only when
    every render succeeded. On any failure the existing clip (files, captions, DB row) is left untouched.
    Inputs must already be validated by validate_edit()."""
    from .captions.pipeline import regenerate_from_srt
    from .captions.subs import parse_srt, to_srt
    store = engine.store
    d = engine.clip_dir(clip_id)
    stage = d / ".edit"
    play_res = size_for(engine.settings.final_height)
    style = caption_style(engine, play_res)
    async with engine.render_lock(clip_id):
        clip = store.clip(clip_id)
        if clip["status"] in ("discarded", "expired", "failed"):
            raise EditError(f"clip is {clip['status']}")
        shutil.rmtree(stage, ignore_errors=True)
        (stage / "captions").mkdir(parents=True)
        new = json.loads(json.dumps(clip))           # in-memory candidate row
        new["_staged"] = True                        # renders stage an auto decision with the rest of the edit
        caps = new["captions"]
        langs: set[str] = set()
        try:
            if layout is not None:
                merged = {**new["layout"], **layout}
                if {"mode", "facecam"} & set(layout):  # an explicit operator choice: decided again from it
                    merged.pop("resolved", None)
                elif "facecam_frac" in layout and merged.get("resolved"):
                    merged["resolved"] = {**merged["resolved"], "facecam_frac": layout["facecam_frac"]}
                new["layout"] = merged
                langs |= {l for l in LANGS if caps.get(l, {}).get("final_status") == "ready"}
            shift = 0.0
            if trim is not None:
                a, b = trim
                f = new["files"]
                shift = a
                f.update(trim_in=f["trim_in"] + a, duration=b - a)
                new["window_start"] = clip["window_start"] + a
                new["window_end"] = new["window_start"] + (b - a)
                for lang in LANGS:
                    c = caps.get(lang) or {}
                    if c.get("srt") and (lang not in (srt or {})):
                        cues = _shift(parse_srt(contained(d, c["srt"]).read_text(encoding="utf-8")), -shift, b - a)
                        (stage / f"{lang}.in.srt").write_text(to_srt(cues), encoding="utf-8")
                        regenerate_from_srt(stage / f"{lang}.in.srt", stage, lang, style=style, play_res=play_res)
                        langs.add(lang)
            for lang, text in (srt or {}).items():
                (stage / f"{lang}.in.srt").write_text(text, encoding="utf-8")
                regenerate_from_srt(stage / f"{lang}.in.srt", stage, lang, style=style, play_res=play_res)
                caps[lang] = {**caps.get(lang, {}), "status": "ready", "edited": True}
                langs.add(lang)
            for lang, t in (title or {}).items():    # a new hook title re-renders that language's final only
                caps[lang] = {**caps.get(lang, {}), "title": t, "title_edited": True}
                if caps[lang].get("final_status") == "ready" or (new["files"].get("final") or {}).get(lang):
                    langs.add(lang)
            for lang in langs:                       # renders read the staged ASS
                if (stage / "captions" / f"{lang}.ass").exists():
                    caps.setdefault(lang, {})["ass"] = f".edit/captions/{lang}.ass"
            outs = {}
            if layout is not None or trim is not None:
                rc, err = await render_video(engine, new, stage / "preview.mp4", height=engine.settings.preview_height)
                if rc != 0:
                    raise RuntimeError(f"preview render failed: {err}")
                outs["preview.mp4"] = stage / "preview.mp4"
            for lang in sorted(langs):
                title = (caps.get(lang) or {}).get("title") or new["title"] or None
                rc, err = await render_video(engine, new, stage / f"final_{lang}.mp4", height=engine.settings.final_height,
                                             lang=lang, title=title)
                if rc != 0:
                    raise RuntimeError(f"{lang} render failed: {err}")
                outs[f"final_{lang}.mp4"] = stage / f"final_{lang}.mp4"
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        # ---- commit: journal first (so a crash can be rolled forward), then swap files, then update the row
        files = new["files"]
        fv = files.setdefault("final_version", {})
        for lang in langs:
            caps[lang] = {**caps[lang], "srt": f"captions/{lang}.srt", "vtt": f"captions/{lang}.vtt",
                          "ass": f"captions/{lang}.ass", "final_status": "ready"}
            if f"final_{lang}.mp4" in outs:
                files.setdefault("final", {})[lang] = f"final_{lang}.mp4"
                fv[lang] = int(fv.get(lang, 0)) + 1
                from .render import snapshot
                files.setdefault("final_layout", {}).setdefault(lang, {})[str(fv[lang])] = snapshot(new["layout"])
        files["edit_count"] = int(files.get("edit_count", 0)) + 1
        files.pop("editing", None)
        files.pop("edit_incomplete", None)
        moves = [[f".edit/captions/{p.name}", f"captions/{p.name}"] for p in (stage / "captions").iterdir()]
        moves += [[f".edit/{name}", name] for name in outs]
        journal = {"moves": moves, "db": {"files": files, "captions": caps, "layout": new["layout"],
                                          "window_start": new["window_start"], "window_end": new["window_end"]}}
        _write_journal(stage, journal)
        try:
            commit_edit(engine, clip_id)
        except Exception as e:                    # same handling as a crash mid-swap: flag, hold deliveries
            store.merge_json("clips", clip_id, "files", {"edit_incomplete": f"{e}; re-run an edit to rebuild the media"})
            raise
    engine.emit("clip", {"id": clip_id})
    if engine.publisher:
        for lang in sorted(langs):
            engine.publisher.on_language_ready(clip_id, lang)


JOURNAL = "COMMIT.json"


def _write_journal(stage: Path, journal: dict) -> None:
    tmp = stage / (JOURNAL + ".tmp")
    with open(tmp, "w") as f:
        json.dump(journal, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, stage / JOURNAL)              # the edit is committed from here on


def commit_edit(engine, clip_id: int) -> str:
    """Apply (or finish applying) a journaled edit: move staged files into place, then write the row.
    Idempotent: safe to run again after a crash at any point. Returns "committed" or raises."""
    d = engine.clip_dir(clip_id)
    stage = d / ".edit"
    j = json.loads((stage / JOURNAL).read_text())
    (d / "captions").mkdir(exist_ok=True)
    missing = []
    for src, dst in j["moves"]:
        s, t = contained(d, src), contained(d, dst)
        if s.exists():
            os.replace(s, t)
        elif not t.exists():
            missing.append(dst)                       # neither staged nor in place: cannot be made consistent
    if missing:
        raise RuntimeError(f"edit swap incomplete, missing: {', '.join(missing)}")
    db = j["db"]
    engine.store.update("clips", clip_id, files=db["files"], captions=db["captions"], layout=db["layout"],
                        window_start=db["window_start"], window_end=db["window_end"])
    shutil.rmtree(stage, ignore_errors=True)
    return "committed"


def recover_edit(engine, clip_id: int) -> str:
    """At startup, for a clip whose edit reservation survived a crash.
    - no journal: the edit never committed -> drop staging; the last good media and row are untouched
    - journal:    roll forward (finish moves, write the row) so files and metadata agree
    - journal that cannot complete: keep the clip, flag it, and keep deliveries off it until it is fixed
    Returns "rolled_back" | "rolled_forward" | "incomplete" | "cleared"."""
    store = engine.store
    d = engine.clip_dir(clip_id)
    stage = d / ".edit"
    result = "cleared"
    if (stage / JOURNAL).exists():
        try:
            commit_edit(engine, clip_id)
            result = "rolled_forward"
        except Exception as e:
            store.merge_json("clips", clip_id, "files", {"editing": False,
                                                        "edit_incomplete": f"{e}; re-run an edit to rebuild the media"})
            store.log(f"clip {clip_id}: interrupted edit could not be completed ({e}); deliveries held", "error",
                      clip_id=clip_id)
            return "incomplete"
    elif stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
        result = "rolled_back"
    store.merge_json("clips", clip_id, "files", {"editing": False})
    store.log(f"clip {clip_id}: edit interrupted by a restart -> {result.replace('_', ' ')}", "warn", clip_id=clip_id)
    return result


def _shift(cues: list[dict], delta: float, dur: float) -> list[dict]:
    out = []
    for c in cues:
        s, e = c["start"] + delta, c["end"] + delta
        if e > 0 and s < dur:
            out.append({**c, "start": max(0.0, s), "end": min(dur, e)})
    return out


def _duration(p: Path) -> float:
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)
