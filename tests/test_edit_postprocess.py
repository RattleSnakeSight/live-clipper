"""Operator edits (ported from the original design's review tests): validation before scheduling, transactional
apply_edit, crash recovery of a journaled edit, per-language title edits."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math

import pytest

from live_clipper import postprocess
from live_clipper.postprocess import EditError, apply_edit, contained, validate_edit
from tests.edit_util import ff, make_clip, make_engine, needs_ffmpeg, source_clip
from tests.test_hook_render import frame


def md5(p):
    return hashlib.md5(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------- crash recovery of an edit
def ready_clip(eng):
    """A previously ready clip: final EN v1 on disk and captions."""
    cid = make_clip(eng)
    d = eng.clip_dir(cid)
    (d / "final_en.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"OLD" * 2000)
    (d / "captions").mkdir()
    (d / "captions" / "en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nold\n")
    eng.store.update("clips", cid, status="ready", window_start=1000.0, window_end=1030.0, layout={"mode": "fit"},
                     files={"final": {"en": "final_en.mp4"}, "final_version": {"en": 1}, "source": "source.ts",
                            "trim_in": 0.0, "duration": 30.0},
                     captions={"en": {"status": "ready", "srt": "captions/en.srt", "final_status": "ready"}})
    return cid, d


def crash_mid_edit(store, cid, d, journal: bool, moved: int = 0, lose_src: bool = False):
    """Leave the exact on-disk/DB state a crash at that point of apply_edit would leave."""
    store.merge_json("clips", cid, "files", {"editing": "deadbeef"})        # reservation from the dead process
    st = d / ".edit"
    (st / "captions").mkdir(parents=True)
    (st / "final_en.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"NEW" * 2000)
    (st / "captions" / "en.srt").write_text("1\n00:00:00,500 --> 00:00:01,500\nnew\n")
    if not journal:
        return
    files = {**store.clip(cid)["files"], "final_version": {"en": 2}, "edit_count": 1, "trim_in": 2.0, "duration": 20.0}
    files.pop("editing")
    caps = {"en": {"status": "ready", "srt": "captions/en.srt", "vtt": "captions/en.vtt", "ass": "captions/en.ass",
                   "final_status": "ready", "edited": True}}
    moves = [[".edit/captions/en.srt", "captions/en.srt"], [".edit/final_en.mp4", "final_en.mp4"]]
    (st / "COMMIT.json").write_text(json.dumps({"moves": moves, "db": {"files": files, "captions": caps,
                                    "layout": {"mode": "crop"}, "window_start": 1002.0, "window_end": 1022.0}}))
    for src, dst in moves[:moved]:
        (d / src).replace(d / dst)
    if lose_src:
        (st / "final_en.mp4").unlink()
        (d / "final_en.mp4").unlink()


def test_crash_before_commit_keeps_last_good_media():
    eng = make_engine()
    cid, d = ready_clip(eng)
    before_row, before_md5 = eng.store.clip(cid), md5(d / "final_en.mp4")
    crash_mid_edit(eng.store, cid, d, journal=False)
    make_engine().recover()                                      # a new process starts
    c = eng.store.clip(cid)
    assert not (d / ".edit").exists() and not c["files"]["editing"]
    assert md5(d / "final_en.mp4") == before_md5 and (d / "captions" / "en.srt").read_text().endswith("old\n")
    assert {k: v for k, v in c["files"].items() if k != "editing"} == before_row["files"]
    assert c["window_start"] == 1000.0 and c["layout"] == {"mode": "fit"}


def test_crash_mid_swap_rolls_forward_consistently():
    eng = make_engine()
    cid, d = ready_clip(eng)
    crash_mid_edit(eng.store, cid, d, journal=True, moved=1)        # captions moved, video still staged
    make_engine().recover()
    c = eng.store.clip(cid)
    assert not (d / ".edit").exists() and not c["files"].get("editing")
    assert b"NEW" in (d / "final_en.mp4").read_bytes() and (d / "captions" / "en.srt").read_text().endswith("new\n")
    assert c["files"]["final_version"] == {"en": 2} and c["files"]["trim_in"] == 2.0     # bytes and row agree
    assert c["window_start"] == 1002.0 and c["layout"] == {"mode": "crop"}


def test_unrecoverable_swap_is_surfaced():
    eng = make_engine()
    cid, d = ready_clip(eng)
    crash_mid_edit(eng.store, cid, d, journal=True, moved=1, lose_src=True)
    make_engine().recover()
    c = eng.store.clip(cid)
    assert "missing" in c["files"]["edit_incomplete"] and not c["files"]["editing"]


# ---------------------------------------------------------------- validation / transactional edits
@pytest.fixture
def edit_clip():
    eng = make_engine()
    cid = make_clip(eng)
    d = eng.clip_dir(cid)
    ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=15:d=12", "-f", "lavfi", "-i", "sine=d=12", "-shortest",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(d / "source.ts"))
    (d / "captions").mkdir()
    (d / "captions" / "en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    eng.store.update("clips", cid, status="ready", window_start=1000.0, window_end=1010.0, layout={"mode": "fit"},
                     files={"source": "source.ts", "trim_in": 1.0, "duration": 10.0, "final": {}},
                     captions={"en": {"status": "ready", "srt": "captions/en.srt"}})
    return eng, cid


@needs_ffmpeg
@pytest.mark.parametrize("body,msg", [
    ({"srt": {"fr": "1\n00:00:01,000 --> 00:00:02,000\nx\n"}}, "language"),
    ({"srt": {"../../x": "1\n00:00:01,000 --> 00:00:02,000\nx\n"}}, "language"),
    ({"srt": {"en": "not an srt"}}, "cue"),
    ({"srt": {"en": "1\n00:00:01,000 --> 00:09:00,000\nx\n"}}, "outside"),
    ({"trim": [0]}, "trim"), ({"trim": [True, 5]}, "finite"), ({"trim": ["a", "b"]}, "finite"),
    ({"trim": [0, math.inf]}, "finite"), ({"trim": [0, 1]}, "shorter"), ({"trim": [-5, 4]}, "inside"),
    ({"layout": {"mode": "spin"}}, "mode"), ({"layout": {"facecam": [0.9, 0, 0.5, 0.5]}}, "inside"),
    ({"layout": {"zoom": float("nan")}}, "finite"), ({"bogus": 1}, "unknown"), ({}, "nothing"),
])
def test_invalid_edits_rejected_before_scheduling(edit_clip, body, msg):
    eng, cid = edit_clip
    before = eng.store.clip(cid)
    with pytest.raises(EditError, match=msg):
        validate_edit(eng.for_clip(cid), before, body)
    assert eng.store.clip(cid) == before


@needs_ffmpeg
def test_failed_render_leaves_clip_untouched(edit_clip, monkeypatch):
    eng, cid = edit_clip
    before = eng.store.clip(cid)
    srt_before = (eng.clip_dir(cid) / "captions" / "en.srt").read_text()

    async def boom(*a, **k):
        return 1, "simulated encoder failure"
    monkeypatch.setattr(postprocess, "render_video", boom)
    view = eng.for_clip(cid)
    edit = validate_edit(view, before, {"trim": [1, 7], "srt": {"en": "1\n00:00:00,500 --> 00:00:01,500\nnew\n"}})
    with pytest.raises(RuntimeError, match="simulated"):
        asyncio.run(apply_edit(view, cid, **edit))
    assert eng.store.clip(cid) == before
    assert (eng.clip_dir(cid) / "captions" / "en.srt").read_text() == srt_before
    assert not (eng.clip_dir(cid) / ".edit").exists()


def test_contained_rejects_escape(tmp_path):
    with pytest.raises(EditError):
        contained(tmp_path, "../outside.srt")
    assert contained(tmp_path, "captions/en.srt").parent.name == "captions"


# ---------------------------------------------------------------- per-language title edits
@needs_ffmpeg
def test_title_edit_validation():
    eng = make_engine()
    cid, _ = source_clip(eng)
    eng.store.merge_json("clips", cid, "captions", {"en": {"status": "ready"}})
    clip = eng.store.clip(cid)
    view = eng.for_clip(cid)
    assert validate_edit(view, clip, {"title": {"en": "  Alpha   escapes  "}}) == {"title": {"en": "Alpha escapes"}}
    for bad in ({"title": {"fr": "x"}}, {"title": {"en": ""}}, {"title": {"en": "x" * 101}}, {"title": {"es": "hola"}},
                {"title": "x"}, {"title": {"en": 5}}):
        with pytest.raises(EditError):
            validate_edit(view, clip, bad)


@needs_ffmpeg
def test_title_edit_re_renders_only_that_language_and_bumps_its_version():
    eng = make_engine()
    cid, d = source_clip(eng)
    asyncio.run(postprocess.render_final(eng.for_clip(cid), cid, "en", notify=False))
    eng.store.merge_json("clips", cid, "captions", {"en": {"status": "ready", "title": "Old cut-off ti"}})
    v0 = eng.store.clip(cid)["files"].get("final_version", {}).get("en", 1)
    before = frame(d / "final_en.mp4")
    view = eng.for_clip(cid)
    edit = validate_edit(view, eng.store.clip(cid), {"title": {"en": "streamer_alpha's full GTA title, repaired"}})
    asyncio.run(apply_edit(view, cid, **edit))
    c = eng.store.clip(cid)
    assert c["captions"]["en"]["title"] == "streamer_alpha's full GTA title, repaired"
    assert c["captions"]["en"]["title_edited"]
    assert c["files"]["final_version"]["en"] == v0 + 1                           # versioned: a repost needs consent
    assert frame(d / "final_en.mp4") != before and not (d / ".edit").exists()
