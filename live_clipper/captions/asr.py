"""Speech recognition: faster-whisper wrapper, loaded once, thread-safe."""
from __future__ import annotations

import threading
import time
from pathlib import Path

LANGS = ("en", "es")
GTA_PROMPT = {
    "en": "GTA, Los Santos, LSPD, heist, Zentorno, Oppressor, wanted level, Maze Bank, Vinewood, Sandy Shores.",
    "es": "GTA, Los Santos, LSPD, atraco, Zentorno, Oppressor, nivel de búsqueda, Maze Bank, Vinewood, Sandy Shores.",
}


def pick_language(probs: dict[str, float]) -> tuple[str, float]:
    """Restrict to en/es: choose the more probable of the two."""
    en, es = probs.get("en", 0.0), probs.get("es", 0.0)
    lang = "en" if en >= es else "es"
    total = en + es
    return lang, (max(en, es) / total if total > 0 else 0.5)


def classify_speech(probs: dict[str, float], lang: str, speech_s: float, duration: float,
                    no_speech_prob: float) -> tuple[str, str]:
    """Separate 'no speech' and 'unknown language' from real (possibly weak) English/Spanish speech.

    Returns (status, note). status: "speech" | "uncertain" | "no_speech". The en/es choice is always reported with
    its GLOBAL Whisper probability (over all ~100 languages), never the en-vs-es renormalised share."""
    g = probs.get(lang, 0.0)
    top, top_p = max(probs.items(), key=lambda kv: kv[1]) if probs else ("", 0.0)
    if speech_s < 0.6 or (duration and speech_s / duration < 0.04) or no_speech_prob > 0.6:
        return "no_speech", (f"no reliable speech (voiced {speech_s:.1f}s of {duration:.0f}s, "
                             f"no-speech probability {no_speech_prob:.2f}); likely music, game audio or silence")
    if top not in LANGS and top_p > g:
        return "uncertain", f"most likely language is '{top}' ({top_p:.2f}); {lang} only {g:.2f}"
    if g < 0.5:
        return "uncertain", f"weak {lang} detection (global probability {g:.2f})"
    return "speech", f"{lang} speech, global probability {g:.2f}"


def load_audio(path) -> "np.ndarray":  # noqa: F821
    """Decode any media to 16 kHz mono float32 via ffmpeg (avoids PyAV version coupling)."""
    import subprocess

    import numpy as np
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


class Transcriber:
    def __init__(self, model: str = "base", device: str = "auto", threads: int = 4,
                 download_root: str | Path = "data/models/whisper", beam_size: int = 2,
                 compute_type: str | None = None):
        self.model_name = model
        self.threads = threads
        self.download_root = str(download_root)
        self.beam_size = beam_size
        self.device = self._resolve_device(device)
        self.compute_type = compute_type or ("float16" if self.device == "cuda" else "int8")
        self._model = None
        self._lock = threading.Lock()
        self.load_s: float | None = None

    @staticmethod
    def _resolve_device(device: str) -> str:
        if device != "auto":
            return device
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception:
            pass
        return "cpu"

    def load(self):
        with self._lock:
            return self._load_locked()

    def _load_locked(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            t0 = time.perf_counter()
            self._model = WhisperModel(self.model_name, device=self.device,
                                       compute_type=self.compute_type, cpu_threads=self.threads,
                                       download_root=self.download_root)
            self.load_s = time.perf_counter() - t0
        return self._model

    def transcribe(self, wav_path, language: str | None = None) -> dict:
        """language: skip detection (used for the tail pass of a split transcription)."""
        with self._lock:
            model = self._load_locked()
            t0 = time.perf_counter()
            audio = load_audio(wav_path)
            duration = len(audio) / 16000.0
            if language:
                lang, prob, probs = language, 1.0, {}
            else:
                lang, prob, probs = self._detect(model, audio)
            segs, _info = model.transcribe(
                audio, language=lang, beam_size=self.beam_size, word_timestamps=True,
                vad_filter=True, initial_prompt=GTA_PROMPT[lang],
                condition_on_previous_text=False)
            segments = []
            nsp = []
            for s in segs:
                text = s.text.strip()
                nsp.append(float(getattr(s, "no_speech_prob", 0.0) or 0.0))
                if not text:
                    continue
                segments.append({
                    "start": round(s.start, 3), "end": round(s.end, 3), "text": text,
                    "words": [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word}
                              for w in (s.words or [])]})
            dt = time.perf_counter() - t0
        speech_s = sum(max(0.0, x["end"] - x["start"]) for x in segments)
        no_speech = (sum(nsp) / len(nsp)) if nsp else 1.0
        status, note = classify_speech(probs, lang, speech_s, duration, no_speech) if probs else ("speech", "language given")
        top = max(probs.items(), key=lambda kv: kv[1]) if probs else (lang, None)
        return {"language": lang,
                "language_probability": round(probs.get(lang, prob), 4) if probs else None,   # GLOBAL probability
                "language_share_en_es": round(prob, 4),           # en-vs-es share only; not a certainty
                "top_language": top[0], "top_language_probability": round(top[1], 4) if top[1] is not None else None,
                "speech_s": round(speech_s, 2), "no_speech_prob": round(no_speech, 3),
                "speech_status": status, "speech_note": note, "duration": duration,
                "segments": segments,
                "timing": {"load_s": self.load_s, "transcribe_s": dt}}

    def window_languages(self, wav_path, segments: list[dict], window: float = 10.0, min_voiced: float = 2.0) -> dict:
        """Detect the language of each ~10 s window that actually contains speech, to catch clips that mix
        languages (e.g. English game dialogue + Spanish streamer). Returns {"windows": [...], "mixed": bool}."""
        with self._lock:
            model = self._load_locked()
            audio = load_audio(wav_path)
            dur = len(audio) / 16000.0
            wins, t = [], 0.0
            while t < dur - 1.0:
                e = min(dur, t + window)
                voiced = sum(max(0.0, min(e, s["end"]) - max(t, s["start"])) for s in segments)
                if voiced >= min_voiced:
                    try:
                        _l, _p, allp = model.detect_language(audio[int(t * 16000):int(e * 16000)])
                        top, p = max(dict(allp).items(), key=lambda kv: kv[1])
                    except Exception:
                        top, p = "", 0.0
                    wins.append({"start": round(t, 1), "end": round(e, 1), "lang": top, "prob": round(p, 3),
                                 "voiced_s": round(voiced, 1)})
                t = e
        confident = {w["lang"] for w in wins if w["prob"] >= 0.5}
        return {"windows": wins, "mixed": len(confident) > 1}

    def _detect(self, model, audio) -> tuple[str, float, dict]:
        try:
            _l, _p, all_probs = model.detect_language(audio, vad_filter=True)
            probs = dict(all_probs)
        except Exception:
            probs = {}
        if not probs:
            return "en", 0.5, {}
        lang, share = pick_language(probs)
        return lang, share, probs
