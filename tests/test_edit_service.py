"""HTTP API of the edit service: presets, clips, clip media (with ranges) and operator re-renders."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from live_clipper import registry
from live_clipper.edit_service import EditService
from live_clipper.paths import get_paths
from live_clipper.server import create_app, ensure_token
from live_clipper.services import build_context
from tests.edit_util import ff, make_clip, needs_ffmpeg, source_clip

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def api():
    svc = EditService()
    paths = get_paths()
    ctx = build_context(paths, port=8765, token=ensure_token(paths, regenerate=True))
    ctx.services = [svc]
    topics = []
    ctx.bus.subscribe("edit.*", lambda t, p: topics.append((t, p)))
    with TestClient(create_app(ctx), base_url=BASE, headers={"Authorization": f"Bearer {ctx.token}"}) as c:
        yield c, svc.engine, topics


def wait_idle(c, cid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        v = c.get(f"/api/clips/{cid}").json()
        if not v["rendering"] and not v["files"].get("editing"):
            return v
        time.sleep(0.1)
    raise AssertionError("render did not finish")


def test_registered_and_reports_status(api):
    c, eng, _ = api
    assert "live_clipper.edit_service:EditService" in registry.SERVICES
    st = c.get("/api/status").json()["services"]["edit"]
    assert st["running"] and st["asr"]["model"] == "small" and st["asr"]["device"] == "auto"
    assert "detail" in st["asr"]


def test_preset_endpoints(api):
    c, _, _ = api
    assert [p["id"] for p in c.get("/api/presets").json()] == ["default"]
    r = c.post("/api/presets", json={"name": "Big Captions", "captions": {"size_frac": 0.08}})
    assert r.status_code == 201 and r.json()["id"] == "big-captions"
    assert c.post("/api/presets", json={"id": "big-captions", "name": "x"}).status_code == 409
    bad = c.post("/api/presets", json={"name": "x", "encode": {"render_crf": 77}})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid"
    assert c.put("/api/presets/big-captions", json={"title": {"duration_s": 3.5}}).json()["title"]["duration_s"] == 3.5
    assert c.put("/api/presets/nope", json={}).status_code == 404
    ex = c.get("/api/presets/big-captions/export")
    assert "attachment" in ex.headers["content-disposition"] and ex.json()["captions"]["size_frac"] == 0.08
    imp = c.post("/api/presets/import", json=[ex.json(), {"name": "Second"}])
    assert imp.status_code == 201 and [p["id"] for p in imp.json()] == ["big-captions-2", "second"]
    assert c.post("/api/presets/big-captions/duplicate", json={"name": "Mine"}).json()["id"] == "mine"
    assert c.put("/api/presets/default", json={"encode": {"render_crf": 18}}).json()["encode"]["render_crf"] == 18
    assert c.post("/api/presets/default/reset").json()["encode"]["render_crf"] == 21
    assert c.delete("/api/presets/default").status_code == 400
    assert c.delete("/api/presets/mine").json() == {"ok": True}
    assert c.get("/api/presets/mine").status_code == 404


def test_clip_list_get_and_media_with_ranges(api):
    c, eng, _ = api
    cid = make_clip(eng)
    (eng.clip_dir(cid) / "preview.mp4").write_bytes(bytes(range(256)) * 4)
    eng.store.merge_json("clips", cid, "files", {"preview": "preview.mp4", "final": {"en": "final_en.mp4"}})
    lst = c.get("/api/clips").json()
    assert [x["id"] for x in lst] == [cid] and lst[0]["media"]["preview"] == f"/api/clips/{cid}/media/preview"
    v = c.get(f"/api/clips/{cid}").json()
    assert v["values"]["encode"]["x264_preset"] == "ultrafast" and v["srt"] == {}
    assert c.get("/api/clips/999").status_code == 404
    full = c.get(f"/api/clips/{cid}/media/preview")
    assert full.status_code == 200 and len(full.content) == 1024 and full.headers["accept-ranges"] == "bytes"
    part = c.get(f"/api/clips/{cid}/media/preview", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == bytes(range(10, 20))
    assert c.get(f"/api/clips/{cid}/media/final?lang=en").status_code == 404      # listed but not on disk
    assert c.get(f"/api/clips/{cid}/media/bogus").status_code == 422
    eng.store.merge_json("clips", cid, "files", {"preview": "../../outside.mp4"})
    assert c.get(f"/api/clips/{cid}/media/preview").status_code == 404           # never outside the clip dir


@needs_ffmpeg
def test_thumbnail_is_made_once_from_the_preview(api):
    c, eng, _ = api
    cid = make_clip(eng)
    ff("-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=2", "-c:v", "libx264", "-preset", "ultrafast",
       str(eng.clip_dir(cid) / "preview.mp4"))
    eng.store.merge_json("clips", cid, "files", {"preview": "preview.mp4"})
    r = c.get(f"/api/clips/{cid}/media/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"


@needs_ffmpeg
def test_rerender_with_trim_title_and_preset(api):
    c, eng, topics = api
    from live_clipper.captions.pipeline import regenerate_from_srt
    cid, d = source_clip(eng, seconds=6)
    (d / "en.in.srt").write_text("1\n00:00:00,500 --> 00:00:02,000\nhello chat\n", encoding="utf-8")
    regenerate_from_srt(d / "en.in.srt", d, "en", play_res=(1080, 1920))
    eng.store.update("clips", cid, window_start=1000.0, window_end=1005.0,
                     captions={"en": {"status": "ready", "title": "Old title", "srt": "captions/en.srt",
                                      "vtt": "captions/en.vtt", "ass": "captions/en.ass"}})
    assert c.post(f"/api/clips/{cid}/render", json={"lang": "en"}).json()["status"] == "rendering"
    v = wait_idle(c, cid)
    assert v["captions"]["en"]["final_status"] == "ready" and (d / "final_en.mp4").exists()
    assert c.post(f"/api/clips/{cid}/render", json={"lang": "en"}).json()["status"] == "already_rendered"
    ready = [p for t, p in topics if t == "edit.clip_ready"]
    assert ready and ready[-1]["clip_id"] == cid and (ready[-1]["width"], ready[-1]["height"]) == (1080, 1920)

    r = c.post(f"/api/clips/{cid}/render", json={"trim": [0.5, 3.0], "title": {"en": "New title"}})
    assert r.json() == {"ok": True, "status": "rendering"}
    v = wait_idle(c, cid)
    assert v["files"]["trim_in"] == 0.5 and v["files"]["duration"] == 2.5 and v["window_start"] == 1000.5
    assert v["captions"]["en"]["title"] == "New title" and v["files"]["final_version"]["en"] == 2
    assert (d / "preview.mp4").exists()

    c.post("/api/presets", json={"id": "small", "name": "Small", "encode": {"final_height": 640}})
    assert c.post(f"/api/clips/{cid}/render", json={"preset_id": "small"}).status_code == 200
    v = wait_idle(c, cid)
    assert v["preset_id"] == "small" and v["files"]["final_version"]["en"] == 3
    assert [p for t, p in topics if t == "edit.clip_ready"][-1]["height"] == 640


@needs_ffmpeg
def test_render_requests_are_validated_before_scheduling(api):
    c, eng, _ = api
    cid, _ = source_clip(eng)
    for body, code in (({}, 400), ({"trim": [0, 1]}, 400), ({"preset_id": "nope"}, 400), ({"lang": "fr"}, 400),
                       ({"lang": "es"}, 400), ({"overrides": {"encode": {"render_crf": 99}}}, 400),
                       ({"overrides": "x"}, 400)):
        assert c.post(f"/api/clips/{cid}/render", json=body).status_code == code, body
    eng.store.update("clips", cid, status="cutting")
    assert c.post(f"/api/clips/{cid}/render", json={"preset_id": "default"}).status_code == 409
    assert c.post("/api/clips/999/render", json={"lang": "en"}).status_code == 404
