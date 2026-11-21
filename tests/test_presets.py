"""Editing presets: validation, seeding without overwriting edits, CRUD/import/export, render settings."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from live_clipper import presets as P
from live_clipper.captions.subs import default_style
from live_clipper.render import DEFAULT_LAYOUT, SPLIT_FRAC
from tests.edit_util import make_engine, needs_ffmpeg, source_clip


def test_shipped_default_holds_the_original_constants():
    s = P.EditSettings(P.shipped(), "models")
    assert (s.clip_pre_roll, s.clip_post_roll, s.max_clip_seconds) == (18.0, 12.0, 60.0)
    assert (s.preview_height, s.final_height, s.x264_preset, s.output_fps) == (1280, 1920, "veryfast", 30)
    assert (s.preview_crf, s.render_crf, s.accurate_seek) == (23, 21, False)
    assert (s.title_secs, s.title_wrap, s.render_languages) == (2.8, 20, "original")
    assert (s.whisper_model, s.whisper_device, s.whisper_compute_type) == ("small", "auto", None)
    for res in ((720, 1280), (1080, 1920), (640, 360)):
        assert s.caption_style(res) == default_style(res)
    assert s.window_for(100.0) == (82.0, 112.0)
    assert s.new_clip_layout(None) == {"mode": "auto"} and s.new_clip_layout(dict(DEFAULT_LAYOUT)) == {
        "mode": "auto"
    }
    assert s.new_clip_layout({"mode": "crop", "cx": 0.3}) == {"mode": "crop", "cx": 0.3}


def test_partial_values_merge_onto_the_default_and_bad_values_are_rejected():
    v = P.validate({"id": "x", "name": "X", "encode": {"render_crf": 18}, "facecam": {"split_frac": 0.4}})
    assert v["encode"]["render_crf"] == 18 and v["encode"]["preview_crf"] == 23
    s = P.EditSettings(v, "models")
    assert s.new_clip_layout(None) == {"mode": "auto", "facecam_frac": 0.4}
    off = P.EditSettings(P.merge(P.shipped(), {"facecam": {"detect": False}}), "models")
    assert off.new_clip_layout(None)["mode"] == "fit"
    assert SPLIT_FRAC == P.shipped()["facecam"]["split_frac"]
    for bad in (
        {"encode": {"render_crf": 99}},
        {"encode": {"x264_preset": "warp"}},
        {"layout": {"mode": "spin"}},
        {"captions": {"font": "A, B"}},
        {"captions": {"primary": "white"}},
        {"unknown": 1},
        {"id": "Bad Id!"},
        {"languages": {"render": "fr"}},
    ):
        with pytest.raises(ValidationError):
            P.validate(P.merge(P.shipped(), bad))


def test_seed_crud_duplicate_reset_import_export():
    eng = make_engine()
    st = eng.presets
    assert [p["id"] for p in st.list()] == ["default"] and st.list()[0]["builtin"]
    st.update("default", {"encode": {"render_crf": 19}})
    st.seed()  # a restart never overwrites the customer's edit
    assert st.get("default")["encode"]["render_crf"] == 19
    assert st.reset("default")["encode"]["render_crf"] == 21
    a = st.create({"name": "Fast Drafts", "encode": {"x264_preset": "ultrafast"}})
    assert a["id"] == "fast-drafts" and a["encode"]["final_height"] == 1920
    with pytest.raises(KeyError):
        st.create({"id": "fast-drafts", "name": "again"})
    b = st.duplicate("fast-drafts")
    assert b["id"] == "fast-drafts-copy" and b["encode"]["x264_preset"] == "ultrafast"
    imp = st.import_({**a, "name": "Imported"})  # same id: renamed, never silently replaced
    assert imp["id"] == "fast-drafts-2" and st.get("fast-drafts")["name"] == "Fast Drafts"
    st.import_({**a, "name": "Replaced"}, replace=True)
    assert st.get("fast-drafts")["name"] == "Replaced"
    with pytest.raises(PermissionError):
        st.delete("default")
    with pytest.raises(PermissionError):
        st.reset("fast-drafts")
    st.delete("fast-drafts-copy")
    assert st.get("fast-drafts-copy") is None


def test_effective_values_follow_preset_then_clip_override():
    eng = make_engine()
    eng.presets.create({"id": "hq", "name": "HQ", "encode": {"render_crf": 16, "final_height": 1280}})
    from tests.edit_util import make_clip

    cid = make_clip(eng, overrides={"title": {"duration_s": 4.0}})
    eng.store.update("clips", cid, preset_id="hq")
    s = eng.for_clip(cid).settings
    assert (s.render_crf, s.final_height, s.title_secs, s.x264_preset) == (16, 1280, 4.0, "veryfast")
    s2 = eng.for_clip(cid, overrides={"encode": {"render_crf": 30}}).settings
    assert s2.render_crf == 30 and s2.title_secs == 4.0


@needs_ffmpeg
def test_accurate_seek_moves_ss_after_the_input_and_crf_title_come_from_the_preset(monkeypatch):
    from live_clipper import postprocess

    eng = make_engine()
    seen = []

    async def spy(args, cwd=None):
        seen.append(list(args))
        return 1, "not run"

    monkeypatch.setattr(postprocess, "run_ffmpeg", spy)
    for accurate in (False, True):
        cid, d = source_clip(
            eng,
            overrides={"encode": {"accurate_seek": accurate, "render_crf": 25}, "title": {"duration_s": 1.5}},
        )
        view = eng.for_clip(cid)
        asyncio.run(
            postprocess.render_video(view, eng.store.clip(cid), d / "out.mp4", height=640, title="Hi")
        )
    plain, accurate = seen
    assert plain[:4] == ["-ss", "0.000", "-i", plain[3]] and plain[4] == "-t"  # input-side seek (original)
    assert accurate[:4] == ["-i", accurate[1], "-ss", "0.000"] and accurate[4] == "-t"
    assert (
        plain[plain.index("-crf") + 1] == "25"
        and "enable='lt(t,1.5)'" in plain[plain.index("-filter_complex") + 1]
    )
