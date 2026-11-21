"""Final renders with hook titles full of punctuation, run through the REAL ffmpeg: the hook goes through a UTF-8
textfile, so apostrophes, quotes, commas, colons, percent signs, backslashes, brackets and non-Latin text can never
break the filtergraph. Each case checks the file exists AND that text was actually drawn (frame differs from a
no-hook render in the title area). Plus: failures are recorded distinctly and temp files are always removed."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from live_clipper.postprocess import render_final, render_video
from live_clipper.render import layout_filter
from tests.edit_util import make_engine, needs_ffmpeg, source_clip

HOOKS = [
    "Alpha's \"insane\" escape: 100% real, no cap",
    "GTA VI: cops, chaos & a 5-star wanted level",
    "¿Qué pasó? ¡El niño se escapó! (sí, 100%)",
    "D:\\clips\\path [brackets]; semi; colons: here",
    "Привет, мир — 'кавычки' и 50% хаоса",
    "%{pts} %{localtime} literal, not expanded",
    "trailing backslash \\",
]


def frame(path: Path, t: float = 0.5) -> bytes:
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", str(path),
                           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True, check=True).stdout


def test_layout_filter_never_contains_hook_text_and_rejects_unsafe_names():
    g = layout_filter({}, 640, title_file="hook_en_1_2.txt")
    assert "textfile=hook_en_1_2.txt:expansion=none" in g and "text='" not in g
    for bad in ("../x.txt", "a b.txt", "x:y.txt", "x';y.txt"):
        with pytest.raises(ValueError):
            layout_filter({}, 640, title_file=bad)


@needs_ffmpeg
@pytest.mark.parametrize("hook", HOOKS)
def test_real_ffmpeg_renders_punctuation_heavy_hooks_with_visible_text(hook):
    eng = make_engine()
    cid, d = source_clip(eng)
    clip = eng.store.clip(cid)
    view = eng.for_clip(clip)
    plain, titled = d / "plain.mp4", d / "titled.mp4"
    rc0, err0 = asyncio.run(render_video(view, clip, plain, height=640))
    rc, err = asyncio.run(render_video(view, clip, titled, height=640, lang="en", title=hook))
    assert rc0 == 0 and rc == 0, err
    assert titled.stat().st_size > 1000
    a, b = frame(plain), frame(titled)
    w, h = 360, 640
    top = slice(0, w * (h // 3))                          # the hook sits in the top third
    diff = sum(abs(x - y) for x, y in zip(a[top], b[top], strict=True)) / (w * (h // 3))
    assert diff > 2.0, f"no visible hook text for {hook!r} (mean diff {diff:.2f})"
    assert not list(d.glob("hook_*.txt"))                 # temp hook file removed


@needs_ffmpeg
def test_render_exception_is_a_retryable_failure_and_temp_files_are_cleaned(monkeypatch):
    eng = make_engine()
    cid, d = source_clip(eng)
    eng.store.merge_json("clips", cid, "captions", {"en": {"status": "ready", "title": "Alpha's 100%: wild, run"}})
    real = Path.write_text

    def boom(self, *a, **k):
        if self.name.startswith("hook_"):
            raise OSError(28, "No space left on device")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", boom)
    asyncio.run(render_final(eng.for_clip(cid), cid, "en", notify=False))
    c = eng.store.clip(cid)["captions"]["en"]
    assert c["final_status"] == "failed" and "No space left" in c["final_error"]   # not stuck in rendering
    assert not list(d.glob("hook_*")) and not list(d.glob("*.part.mp4"))
    monkeypatch.setattr(Path, "write_text", real)
    asyncio.run(render_final(eng.for_clip(cid), cid, "en", notify=False))       # disk fixed: succeeds
    c = eng.store.clip(cid)
    assert c["captions"]["en"]["final_status"] == "ready" and (d / "final_en.mp4").exists()
    assert "final_error" not in c["captions"]["en"] and c["files"]["final_version"]["en"] == 1
