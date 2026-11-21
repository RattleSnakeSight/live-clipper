"""Speech recognition wiring: settings keys asr.model / asr.device / asr.compute_type pick the faster-whisper model,
which downloads into <data dir>/models/whisper on first use. The real run is opt-in (LIVE_CLIPPER_ASR_TEST=1): it
synthesizes speech (Windows SAPI or espeak-ng) and downloads a model once (cached under .pytest_cache)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from live_clipper import postprocess
from live_clipper.captions import subs
from tests.edit_util import make_engine

TEXT = "Okay chat, I am taking the car straight through the police roadblock. Oh no, I just crashed."


def test_captioner_follows_the_asr_settings_and_downloads_into_the_data_dir():
    eng = make_engine()
    tr = postprocess.get_captioner(eng.for_clip(None))[0]
    assert (tr.model_name, tr.compute_type in ("int8", "float16")) == ("small", True)     # original defaults
    assert Path(tr.download_root) == eng.models_dir / "whisper" and eng.models_dir.parent == eng.paths.root
    eng2 = make_engine()
    eng2.kv.set("asr.model", "base")
    eng2.kv.set("asr.device", "cpu")
    eng2.kv.set("asr.compute_type", "int8")
    tr2 = postprocess.get_captioner(eng2.for_clip(None))[0]
    assert (tr2.model_name, tr2.device, tr2.compute_type) == ("base", "cpu", "int8")
    assert postprocess.get_captioner(eng2.for_clip(None))[0] is tr2                     # loaded once, shared
    st = eng2.asr_status()
    assert st["model"] == "base" and not st["model_downloaded"] and "first clip" in st["detail"]


def _speech_wav(out: Path) -> Path | None:
    raw = out.with_suffix(".raw.wav")
    if sys.platform == "win32":
        ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SetOutputToWaveFile('{raw}'); $s.Speak('{TEXT}'); $s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=60)
    elif shutil.which("espeak-ng"):
        subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(raw), TEXT], capture_output=True, timeout=60)
    if not raw.exists() or raw.stat().st_size < 10_000:
        return None
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-ar", "16000", "-ac", "1", str(out)],
                   check=True)
    return out


@pytest.mark.asr
@pytest.mark.skipif(os.environ.get("LIVE_CLIPPER_ASR_TEST") != "1", reason="opt-in: LIVE_CLIPPER_ASR_TEST=1")
def test_real_faster_whisper_transcribes_synthetic_speech(tmp_path):
    pytest.importorskip("faster_whisper")
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    wav = _speech_wav(tmp_path / "speech.wav")
    if wav is None:
        pytest.skip("no speech synthesizer (Windows SAPI or espeak-ng)")
    from live_clipper.captions.asr import Transcriber
    root = Path(os.environ.get("LIVE_CLIPPER_ASR_TEST_MODELS") or Path(__file__).parent.parent / ".pytest_cache" / "models")
    tr = Transcriber(os.environ.get("LIVE_CLIPPER_ASR_TEST_MODEL", "base"), device="cpu", threads=4,
                     download_root=root / "whisper")
    t = tr.transcribe(str(wav))
    text = " ".join(s["text"] for s in t["segments"]).lower()
    assert t["language"] == "en" and t["speech_status"] in ("speech", "uncertain"), t["speech_note"]
    assert "roadblock" in text and "crashed" in text, text
    assert t["segments"][0]["words"] and t["timing"]["load_s"] is not None
    cues = subs.build_cues(t["segments"])
    assert cues and all(len(line) <= 32 for c in cues for line in c["text"].split("\n"))
