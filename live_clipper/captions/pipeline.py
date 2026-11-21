"""caption_clip: transcribe -> source-language captions asap -> translate -> other language."""
from __future__ import annotations

import time
from pathlib import Path

from . import subs


def _write(out_dir: Path, lang: str, cues, style, play_res) -> dict:
    d = Path(out_dir) / "captions"
    d.mkdir(parents=True, exist_ok=True)
    paths = {"srt": d / f"{lang}.srt", "vtt": d / f"{lang}.vtt", "ass": d / f"{lang}.ass"}
    paths["srt"].write_text(subs.to_srt(cues), encoding="utf-8")
    paths["vtt"].write_text(subs.to_vtt(cues), encoding="utf-8")
    paths["ass"].write_text(subs.to_ass(cues, style, play_res), encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}


def regenerate_from_srt(srt_path, out_dir, lang, style=None, play_res=(720, 1280)) -> dict:
    """Operator edited cue text/times in an SRT: rebuild vtt/ass (and keep the srt)."""
    cues = subs.parse_srt(Path(srt_path).read_text(encoding="utf-8"))
    return _write(Path(out_dir), lang, cues, style, play_res)


def caption_clip(wav_path, out_dir, transcriber, translator, play_res=(720, 1280), style=None,
                 glossary_terms=(), on_ready=None, before_translate=None) -> dict:
    """before_translate(transcript): optional hook run after the source-language captions are ready and BEFORE
    the (optional) translation, e.g. to evaluate the original language first."""
    t0 = time.perf_counter()
    out_dir = Path(out_dir)
    if glossary_terms:
        from .glossary import Glossary
        translator.glossary = Glossary(glossary_terms)
    transcript = transcriber.transcribe(wav_path)
    g = getattr(translator, "glossary", None)
    if g is not None:
        for seg in transcript["segments"]:
            seg["text"] = g.correct_asr(seg["text"])
            for w in seg.get("words", []):
                w["word"] = g.correct_asr(w["word"])
    t_asr = time.perf_counter() - t0
    src = transcript["language"]
    tgt = "es" if src == "en" else "en"
    cues: dict[str, list] = {}
    files: dict[str, dict] = {}
    timings = {"asr_s": t_asr, "asr_detail": transcript.get("timing", {})}

    cues[src] = subs.build_cues(transcript["segments"])
    files[src] = _write(out_dir, src, cues[src], style, play_res)
    timings[f"ready_{src}_s"] = time.perf_counter() - t0
    if on_ready:
        on_ready(src, files[src], timings[f"ready_{src}_s"])
    if before_translate:
        t_chk = time.perf_counter()
        before_translate(transcript)
        timings["original_check_s"] = time.perf_counter() - t_chk

    t1 = time.perf_counter()
    translated = translator.translate_segments(transcript["segments"], src, tgt)
    timings["translate_s"] = time.perf_counter() - t1
    cues[tgt] = subs.build_cues(translated)
    files[tgt] = _write(out_dir, tgt, cues[tgt], style, play_res)
    timings[f"ready_{tgt}_s"] = time.perf_counter() - t0
    if on_ready:
        on_ready(tgt, files[tgt], timings[f"ready_{tgt}_s"])
    timings["total_s"] = time.perf_counter() - t0
    return {"source_lang": src, "transcript": transcript, "cues": cues, "files": files,
            "timings": timings}
