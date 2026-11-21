"""Helpers for edit tests: an EditEngine on the per-test data dir and synthetic source clips."""

from __future__ import annotations

import itertools
import shutil
import subprocess

import pytest

import live_clipper.edit_service  # noqa: F401  (registers the `edit` migrations before the db is built)
from live_clipper.edit_service import EditEngine
from live_clipper.paths import get_paths
from live_clipper.services import build_context

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)
FAST = {"encode": {"x264_preset": "ultrafast"}}
_KEYS = itertools.count(1)


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


def make_engine() -> EditEngine:
    ctx = build_context(get_paths(), port=8765, token="test-token")
    eng = EditEngine(ctx.paths, ctx.db, ctx.settings, ctx.bus)
    eng.presets.seed()
    return eng


def make_clip(eng: EditEngine, *, layout=None, overrides=None, status="ready", channel_id=1, **fields) -> int:
    cid = eng.store.create_clip(
        channel_id,
        f"test:{next(_KEYS)}",
        "chat_spike",
        {"reason": "synthetic"},
        layout=layout or {},
        overrides=overrides if overrides is not None else FAST,
        status=status,
        **fields,
    )
    eng.clip_dir(cid).mkdir(parents=True, exist_ok=True)
    return cid


def source_clip(eng: EditEngine, seconds: float = 3, size: str = "640x360", **kw) -> tuple[int, object]:
    """A clip whose source.ts is a short colour + tone file (trim_in 0, duration seconds - 1)."""
    cid = make_clip(eng, **kw)
    d = eng.clip_dir(cid)
    ff(
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x406080:s={size}:r=30:d={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=f=440:d={seconds}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-shortest",
        str(d / "source.ts"),
    )
    eng.store.merge_json(
        "clips", cid, "files", {"source": "source.ts", "trim_in": 0.0, "duration": seconds - 1.0}
    )
    return cid, d
