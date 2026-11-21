"""Facecam-top / gameplay-bottom split layout: conservative detection on real FFmpeg fixtures (webcam inset with
camera noise, deep and light skin tones, no camera, a camera that disappears, portrait, grey and coloured lighting,
an empty webcam, a cartoon HUD portrait and a bordered minimap), the rendered output checked with pixel samples at
the first, middle and last frame, ffprobe dimensions / codecs / fps / A/V duration, the persisted per-clip decision
and the per-render record, and the policy for older presets and explicit coordinates. Synthetic media only."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from live_clipper import facecam, render
from tests.edit_util import make_clip, make_engine

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg missing")
D = 6
CAM = (20, 20, 180, 140)                     # inset incl. 2 px border, in the 640x360 source


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


def _game(extra=""):
    return (f"[0:v]drawbox=x=0:y=0:w=14:h=360:color=red:t=fill,drawbox=x=626:y=0:w=14:h=360:color=0x00ff00:t=fill,"
            f"drawbox=x=500:y=12:w=120:h=30:color=white:t=fill{extra}[g]")


def _cam_file(out, face, room, light="", noise=18, rate=30, size="640x360"):
    _ff("-f", "lavfi", "-i", f"testsrc2=s={size}:r={rate}:d={D}", "-f", "lavfi", "-i", f"color=c={room}:s=156x116:r={rate}:d={D}",
        "-f", "lavfi", "-i", f"color=c={face}:s=54x70:r={rate}:d={D}", "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={D}",
        "-filter_complex", f"[1:v][2:v]overlay=x='50+15*sin(t*2)':y='25+5*cos(t*3)',noise=alls={noise}:allf=t{light}[cam];"
        f"[cam]pad=160:120:2:2:color=white[camb];{_game()};[g][camb]overlay=x={CAM[0]}:y={CAM[1]}[v]",
        "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-t", str(D), str(out))


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    d = tmp_path_factory.mktemp("layoutfx")
    _cam_file(d / "cam.mp4", "0xE0AC69", "0x2a2a40")
    _cam_file(d / "dark.mp4", "0x5A3825", "0x1e1e2a")
    _cam_file(d / "light.mp4", "0xF3D3BD", "0x3a3a50")
    _cam_file(d / "grey.mp4", "0xC8A080", "0x404040", ",hue=s=0.15")
    _cam_file(d / "purple.mp4", "0xC8A080", "0x2a2a40", ",colorchannelmixer=rr=0.8:bb=1.4:gg=0.6")
    _cam_file(d / "empty.mp4", "0x2a2a40", "0x2a2a40")
    _cam_file(d / "cam60.mp4", "0xE0AC69", "0x2a2a40", rate=60)
    _ff("-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={D}", "-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=48000:d={D}",
        "-filter_complex", _game(), "-map", "[g]", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt",
        "yuv420p", "-c:a", "aac", "-t", str(D), str(d / "nocam.mp4"))
    _ff("-i", str(d / "cam.mp4"), "-i", str(d / "nocam.mp4"), "-filter_complex",
        f"[0:v]trim=0:{D / 2},setpts=PTS-STARTPTS[a];[1:v]trim={D / 2}:{D},setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1[v]",
        "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(d / "gone.mp4"))
    _ff("-i", str(d / "cam.mp4"), "-filter_complex",
        f"[0:v]split[s0][s1];[s0]trim=0:{D / 2},setpts=PTS-STARTPTS[a];[s1]trim={D / 2}:{D},setpts=PTS-STARTPTS,"
        f"hflip,vflip[b];[a][b]concat=n=2:v=1[v]", "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-preset",
        "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(d / "moved.mp4"))
    _ff("-i", str(d / "cam.mp4"), "-vf", "scale=360:-2,pad=360:640:0:150:color=black", "-c:v", "libx264", "-preset",
        "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(d / "portrait.mp4"))
    _ff("-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={D}", "-f", "lavfi", "-i", f"color=c=0x3050a0:s=116x116:r=30:d={D}",
        "-f", "lavfi", "-i", f"color=c=0xF0B080:s=46x56:r=30:d={D}", "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={D}",
        "-filter_complex", "[1:v][2:v]overlay=x='35+20*sin(t*1.3)':y='30+15*cos(t*1.7)'[p];[p]pad=120:120:2:2:color=0xffd700[pb];"
        "[0:v][pb]overlay=x=500:y=220[v]", "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", str(D), str(d / "cartoon.mp4"))
    _ff("-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={D}", "-f", "lavfi", "-i", "mandelbrot=s=116x116:r=30",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={D}",
        "-filter_complex", "[1:v]pad=120:120:2:2:color=white[m];[0:v][m]overlay=x=15:y=225[v]", "-map", "[v]",
        "-map", "2:a", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", str(D),
        str(d / "minimap.mp4"))
    return d


def detect(path):
    info = facecam.probe(path)
    return facecam.detect(facecam.window_samples(path, 0, D, 8), info)


def detect_rect(path, rect):
    info = facecam.probe(path)
    return facecam.detect(facecam.window_samples(path, 0, D, 8), info, rect=rect)


def frame(path, t, w, h):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path),
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], capture_output=True,
                       check=True, timeout=60)
    return np.frombuffer(p.stdout, np.uint8).reshape(h, w, 3)


def probe_all(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,"
                        "avg_frame_rate,duration", "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return {s["codec_type"]: s for s in json.loads(p.stdout)["streams"]}


def render_to(src, layout, out, height=1280, fps=30, ass=None):
    info = facecam.probe(src)
    g = render.layout_filter(layout, height, fps=render.fps_cap(fps, info["fps"]), ass=ass)
    _ff("-i", str(src), "-filter_complex", g, "-map", "[v]", "-map", "0:a:0?", "-c:v", "libx264", "-preset",
        "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(out))
    return probe_all(out)


# ---------------------------------------------------------------- detection: accept real cameras, reject the rest
@pytest.mark.parametrize("name", ["cam", "dark", "light", "cam60"])
def test_a_real_webcam_inset_is_found_for_different_skin_tones_and_frame_rates(fx, name):
    r = detect(fx / f"{name}.mp4")
    x, y, w, h = r["facecam"]
    assert r["confidence"] >= facecam.MIN_CONF and r["source"] == [640, 360]
    assert CAM[0] / 640 - 0.01 <= x <= CAM[0] / 640 + 0.03 and CAM[1] / 360 - 0.01 <= y <= CAM[1] / 360 + 0.04
    assert 0.18 <= w <= 0.27 and 0.25 <= h <= 0.36                       # the inset, never a quadrant


@pytest.mark.parametrize("name,why", [
    ("nocam", "no reliable"), ("gone", "whole frame"), ("portrait", "portrait"), ("grey", "no reliable"),
    ("purple", "no reliable"), ("empty", "no reliable"), ("cartoon", "no reliable"), ("minimap", "no reliable")])
def test_no_camera_changing_camera_portrait_weak_evidence_artwork_and_hud_keep_the_whole_frame(fx, name, why):
    r = detect(fx / f"{name}.mp4")
    assert r["facecam"] is None and why in r["reason"]


def test_flat_artwork_is_rejected_even_with_skin_tones_and_motion():
    rng = np.random.default_rng(1)
    F = np.zeros((8, 144, 256, 3), np.uint8)
    F[:] = rng.integers(0, 255, (8, 144, 256, 3), dtype=np.uint8) // 2 + 60          # noisy game texture
    for i in range(8):                                  # a flat bordered portrait box with a moving flat face
        F[i, 10:70, 10:80] = (48, 80, 160)
        F[i, 9, 9:81] = F[i, 70, 9:81] = 255
        F[i, 9:71, 9] = F[i, 9:71, 80] = 255
        F[i, 20 + i:50 + i, 30 + i:55 + i] = (240, 176, 128)
    assert facecam.analyze(F, 1280, 720)["facecam"] is None


# ---------------------------------------------------------------- rendering: real FFmpeg output checks
def _skin_share(px):
    return float(facecam._skin(px[None])[0].mean())


def _camera_texture(px):
    """Fine detail energy (camera noise / texture): |pixel - 3x3 mean| of the luminance."""
    L = px.astype("float32") @ np.array([0.299, 0.587, 0.114], "float32")
    m = sum(L[1 + dy:L.shape[0] - 1 + dy, 1 + dx:L.shape[1] - 1 + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)) / 9
    return float(np.abs(L[1:-1, 1:-1] - m).mean())


def test_split_render_places_the_face_on_top_and_the_whole_gameplay_below(fx, tmp_path):
    det = detect(fx / "cam.mp4")
    lay = render.resolve({"mode": "auto"}, det)
    assert lay["resolved"]["mode"] == "split" and lay["resolved"]["source"] == [640, 360]
    out = tmp_path / "final.mp4"
    st = render_to(fx / "cam.mp4", lay, out)
    v, a = st["video"], st["audio"]
    assert (v["codec_name"], v["width"], v["height"], v["avg_frame_rate"]) == ("h264", 720, 1280, "30/1")
    assert a["codec_name"] == "aac" and abs(float(v["duration"]) - D) < 0.15 and abs(float(a["duration"]) - D) < 0.15
    fh = render._even(1280 * render.SPLIT_FRAC)                                    # 410
    gp_h = 720 * 360 // 640                                                        # 405: whole frame, aspect kept
    pad = (1280 - fh - gp_h) // 2                                                  # centred: equal insets
    for t in (0.1, D / 2, D - 0.2):                                               # first / middle / last
        f = frame(out, t, 720, 1280)
        top, gp = f[:fh], f[fh + pad:fh + pad + gp_h]
        rows = np.where(np.abs(np.diff(f[fh:, 4:12, 0].astype(int).mean(axis=1))) > 60)[0]   # red HUD edge rows
        assert abs(rows.min() - pad) <= 3 and abs((1280 - fh) - rows.max() - pad) <= 4       # balanced insets
        assert _skin_share(top) > 0.08                                             # the streamer is on top
        assert gp[:, 2:10, 0].mean() > 180 and gp[:, 2:10, 1].mean() < 90          # red left HUD edge visible
        assert gp[:, -10:-2, 1].mean() > 180 and gp[:, -10:-2, 0].mean() < 90     # green right HUD edge visible
        inset = gp[int(CAM[1] / 360 * gp_h) + 6:int(CAM[3] / 360 * gp_h) - 6,
                   int(CAM[0] / 640 * 720) + 6:int(CAM[2] / 640 * 720) - 6]
        s = frame(fx / "cam.mp4", t, 640, 360)[CAM[1] + 8:CAM[3] - 8, CAM[0] + 8:CAM[2] - 8]
        assert _camera_texture(inset) < 0.35 * _camera_texture(s)                # the camera is not shown twice
        backdrop = f[fh + gp_h + 20:]
        assert backdrop.mean() < gp.mean()                                         # darkened backdrop below


def test_captions_sit_below_the_gameplay_inside_the_safe_area(fx, tmp_path):
    from live_clipper.captions import subs
    ass = tmp_path / "en.ass"
    ass.write_text(subs.to_ass([{"start": 0.0, "end": float(D), "text": "IIIIIIIIIIII"}], None, (720, 1280)),
                   encoding="utf-8")
    lay = render.resolve({"mode": "auto"}, detect(fx / "cam.mp4"))
    out = tmp_path / "cap.mp4"
    render_to(fx / "cam.mp4", lay, out, ass=render.split_ass(ass, tmp_path / "split.ass", lay))
    f = frame(out, D / 2, 720, 1280).astype(int)
    white = (f.min(axis=2) > 225)
    fh = render._even(1280 * render.SPLIT_FRAC)
    gp_h = 720 * 360 // 640
    gp_top = fh + (1280 - fh - gp_h) // 2
    rows = np.where(white[:, 150:570].sum(axis=1) > 20)[0]
    cap_rows = rows[(rows > fh) & (rows < gp_top)]
    assert cap_rows.size and cap_rows.min() > fh and cap_rows.max() < gp_top          # in the padding, not on play
    assert not ((rows >= gp_top) & (rows < gp_top + gp_h) & (white[rows, 300:420].sum(axis=1) > 60)).any()


def test_frame_rate_cap_never_duplicates_frames(fx, tmp_path):
    assert render.fps_cap(30, 30.0) == 0 and render.fps_cap(60, 30.0) == 0 and render.fps_cap(30, 60.0) == 30
    assert render.fps_cap(30, None) == 30 and render.fps_cap(0, 60.0) == 0
    lay = render.resolve({"mode": "auto"}, detect(fx / "cam60.mp4"))
    assert render_to(fx / "cam60.mp4", lay, tmp_path / "a.mp4", fps=30)["video"]["avg_frame_rate"] == "30/1"
    assert render_to(fx / "cam.mp4", lay, tmp_path / "b.mp4", fps=60)["video"]["avg_frame_rate"] == "30/1"
    assert render_to(fx / "cam60.mp4", lay, tmp_path / "c.mp4", fps=60)["video"]["avg_frame_rate"] == "60/1"


@pytest.mark.parametrize("name", ["portrait", "nocam"])
def test_fallback_renders_keep_the_whole_source_without_a_fake_camera(fx, tmp_path, name):
    lay = render.resolve({"mode": "auto"}, detect(fx / f"{name}.mp4"))
    assert lay["resolved"]["mode"] == "fit" and lay["resolved"]["fallback"]
    st = render_to(fx / f"{name}.mp4", lay, tmp_path / "f.mp4")
    assert (st["video"]["width"], st["video"]["height"]) == (720, 1280)
    f = frame(tmp_path / "f.mp4", D / 2, 720, 1280)
    if name == "nocam":                                                         # whole width: both HUD edges
        mid = f[640 - 100:640 + 100]
        assert mid[:, 2:8, 0].mean() > 150 and mid[:, -8:-2, 1].mean() > 150


# ---------------------------------------------------------------- policy, persistence and the render record
def test_channel_policy_keeps_presets_and_explicit_choices():
    assert render.channel_layout({}) == {"mode": "auto"} and render.channel_layout({"mode": "fit"}) == {"mode": "auto"}
    assert render.channel_layout(None) == {"mode": "auto"}
    assert render.channel_layout({"mode": "fit", "pin": True})["mode"] == "fit"
    assert render.channel_layout({"mode": "fit", "zoom": 1.2})["mode"] == "fit"                # customised: kept
    stack = {"mode": "stack", "facecam": [0, 0, 0.3, 0.3]}
    assert render.channel_layout(stack) == stack and render.channel_layout({"mode": "crop"}) == {"mode": "crop"}
    cal = render.resolve({"mode": "auto", "facecam": [0.7, 0.0, 0.3, 0.35]}, None)   # calibration, not validated
    assert cal["resolved"]["mode"] == "fit" and "not validated" in cal["resolved"]["fallback"]
    exp = {"mode": "split", "facecam": [0.7, 0.0, 0.3, 0.35]}                          # explicit operator choice
    assert render.resolve(exp, None) == exp and not render.needs_detection(exp, {"source": "x"})
    assert render.effective({"mode": "split"})["mode"] == "split"
    assert "vstack" not in render.layout_filter({"mode": "split"}, 1280)                       # no invented camera
    assert render.layout_filter({"mode": "auto"}, 1280) == render.layout_filter({"mode": "fit"}, 1280)


def _engine(tmp_path, src, layout, langs=("en",), duration=float(D), trim_in=0.0):
    base = make_engine()
    cid = make_clip(base, layout=layout, overrides={"encode": {"x264_preset": "veryfast"}})
    d = base.clip_dir(cid)
    shutil.copy(src, d / "source.mp4")
    base.store.update("clips", cid, status="ready",
                      files={"source": "source.mp4", "trim_in": trim_in, "duration": duration},
                      captions={"policy": {"render_default": langs[0]}, **{lang: {} for lang in langs}})
    return base.for_clip(cid), base.store, cid, d


def _count_detect(monkeypatch):
    calls = []
    real = facecam.detect

    def spy(samples, info, rect=None):
        calls.append({"samples": list(samples), "rect": rect})
        return real(samples, info, rect=rect)
    monkeypatch.setattr(facecam, "detect", spy)
    return calls


def test_the_decision_is_stored_once_and_every_render_records_what_it_used(fx, tmp_path, monkeypatch):
    from live_clipper import postprocess
    eng, st, cid, d = _engine(tmp_path, fx / "cam.mp4", render.channel_layout({"mode": "fit"}))
    asyncio.run(postprocess.render_final(eng, cid, "en", notify=False))
    c = st.clip(cid)
    assert c["layout"]["mode"] == "auto" and c["layout"]["resolved"]["mode"] == "split"
    assert c["layout"]["resolved"]["window"] == render.window_key("source.mp4", (d / "source.mp4").stat().st_size,
                                                                  0.0, float(D))
    rec = c["files"]["final_layout"]["en"]["1"]
    assert rec["mode"] == "split" and rec["requested"] == "auto" and rec["layout_version"] == render.LAYOUT_VERSION
    assert rec["facecam_frac"] == render.SPLIT_FRAC and rec["fallback"] is None and rec["confidence"] >= 0.6
    assert set(rec) == {"layout_version", "requested", "mode", "facecam", "facecam_frac", "fallback", "confidence"}
    st_ = probe_all(d / "final_en.mp4")
    assert (st_["video"]["width"], st_["video"]["height"]) == (1080, 1920)
    monkeypatch.setattr(facecam, "detect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-detected")))
    asyncio.run(postprocess.render_final(eng, cid, "en", notify=False))       # the stored decision is reused
    c2 = st.clip(cid)
    assert c2["files"]["final_version"]["en"] == 2 and c2["files"]["final_layout"]["en"]["1"] == rec
    assert c2["files"]["final_layout"]["en"]["2"]["facecam"] == rec["facecam"]


# ---------------------------------------------------------------- preview is provisional; the final decides
def test_the_cut_preview_never_probes_and_never_uses_an_unvalidated_area(monkeypatch):
    monkeypatch.setattr(facecam, "detect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")))
    for L in ({"mode": "auto"}, {"mode": "auto", "facecam": [0, 0, 0.29, 0.29]}):
        p = render.preview_layout(L)
        assert p["preview"]["mode"] == "fit" and p["preview"]["provisional"] and "resolved" not in p
        assert render.effective(p)["mode"] == "fit" and render.needs_detection(p, {"source": "s"})
    exp = {"mode": "split", "facecam": [0, 0, 0.29, 0.29]}
    assert render.preview_layout(exp) == exp                                   # explicit: honoured as is


@pytest.mark.parametrize("name,expect,rect", [
    ("cam", "split", None),                          # unchanged camera across the window
    ("gone", "fit", None),                           # camera before the trigger, gone after it
    ("moved", "fit", None),                          # camera moves to another corner after the trigger
    ("cam", "split", "cal"),                         # calibrated area, camera present throughout
    ("gone", "fit", "cal"),                          # calibrated area, camera gone after the trigger
    ("moved", "fit", "cal"),                         # calibrated area, camera moved after the trigger
    ("nocam", "fit", "cal"),                         # calibrated area on a stream without a camera
])
def test_the_first_final_reevaluates_the_whole_clip_window(fx, tmp_path, monkeypatch, name, expect, rect):
    from live_clipper import postprocess
    cal = [round(CAM[0] / 640, 4), round(CAM[1] / 360, 4), round(160 / 640, 4), round(120 / 360, 4)]
    L = {"mode": "auto", **({"facecam": cal} if rect else {})}
    L = render.preview_layout(L)
    eng, st, cid, d = _engine(tmp_path, fx / f"{name}.mp4", L)
    calls = _count_detect(monkeypatch)
    asyncio.run(postprocess.render_final(eng, cid, "en", notify=False))
    c = st.clip(cid)
    assert len(calls) == 1 and calls[0]["rect"] == (cal if rect else None)
    ts = [t for _, t in calls[0]["samples"]]         # samples span the whole window, after the trigger too
    assert len(ts) == facecam.MAX_FRAMES and min(ts) < D * 0.2 and max(ts) > D * 0.8
    res = c["layout"]["resolved"]
    assert res["mode"] == expect and c["files"]["final_layout"]["en"]["1"]["mode"] == expect
    if expect == "fit":
        assert res["fallback"] and c["files"]["final_layout"]["en"]["1"]["facecam"] is None


def test_the_window_decision_is_cached_across_languages_and_redone_for_a_new_window(fx, tmp_path, monkeypatch):
    from live_clipper import postprocess
    eng, st, cid, d = _engine(tmp_path, fx / "gone.mp4", render.preview_layout({"mode": "auto"}), langs=("en", "es"))
    calls = _count_detect(monkeypatch)
    asyncio.run(postprocess.render_final(eng, cid, "en", notify=False))
    asyncio.run(postprocess.render_final(eng, cid, "es", notify=False))
    asyncio.run(postprocess.render_final(eng, cid, "en", notify=False))
    c = st.clip(cid)
    assert len(calls) == 1 and c["layout"]["resolved"]["mode"] == "fit"
    assert c["files"]["final_layout"]["es"]["1"] == c["files"]["final_layout"]["en"]["1"]
    f = dict(c["files"], duration=D / 2 - 0.5)                 # operator trims to the part with the camera
    st.update("clips", cid, files=f)
    asyncio.run(postprocess.render_final(eng, cid, "en", notify=False))
    c = st.clip(cid)
    assert len(calls) == 2 and c["layout"]["resolved"]["window"]["duration"] == round(D / 2 - 0.5, 3)
    assert c["layout"]["resolved"]["mode"] == "split"           # the new window has a stable camera
    assert c["files"]["final_layout"]["en"]["1"]["mode"] == "fit"   # earlier records are immutable


def test_an_explicit_operator_split_is_never_redetected(fx, tmp_path, monkeypatch):
    from live_clipper import postprocess
    exp = {"mode": "split", "facecam": [0.03, 0.055, 0.25, 0.333]}
    eng, st, cid, d = _engine(tmp_path, fx / "nocam.mp4", exp)
    monkeypatch.setattr(facecam, "detect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-detected")))
    asyncio.run(postprocess.render_final(eng, cid, "en", notify=False))
    rec = st.clip(cid)["files"]["final_layout"]["en"]["1"]
    assert rec["mode"] == "split" and rec["requested"] == "split" and rec["facecam"] == exp["facecam"]


# ---------------------------------------------------------------- face-model validation (no colour rules)
def _frames(n=8, h=360, w=640, cam=True, live=True, seed=0):
    rng = np.random.default_rng(seed)
    F = np.zeros((n, h, w, 3), np.uint8)
    for i in range(n):
        F[i] = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)        # changing "gameplay"
        if cam:
            F[i, 18:142, 18:182] = 255                                   # inset border
            room = np.repeat(np.linspace(30, 160, 160, dtype=np.float32)[None, :, None], 120, 0)
            room = np.repeat(room, 3, 2) + np.linspace(0, 60, 120, dtype=np.float32)[:, None, None]
            room = room.astype(np.uint8)                                 # a static room with structure
            if live:
                room = (room + rng.integers(0, 12, room.shape)).astype(np.uint8)
            F[i, 20:140, 20:180] = room
    return F


RECT = [20 / 640, 20 / 360, 160 / 640, 120 / 360]


def _face(cx, cy, s=0.12, score=0.95):
    return {"x": cx - s / 2, "y": cy - s / 2, "w": s * 0.56, "h": s, "score": score}


def test_validate_accepts_a_face_inside_the_area_in_both_halves(monkeypatch):
    from live_clipper import faces
    monkeypatch.setattr(faces, "available", lambda: True)
    inside = [[_face(0.15, 0.2)] for _ in range(8)]
    assert faces.validate(_frames(), RECT, inside)["ok"]
    half = [[_face(0.15, 0.2)]] * 4 + [[]] * 4                            # gone after the trigger, no camera frame
    v = faces.validate(_frames(cam=False), RECT, half)
    assert not v["ok"] and v["signals"]["face_second_half"] == 0
    moved = [[_face(0.15, 0.2)]] * 3 + [[_face(0.85, 0.8, s=0.1)]] * 5    # the face is now elsewhere
    v = faces.validate(_frames(cam=False), RECT, moved)
    assert not v["ok"] and "outside" in v["reason"]


def test_validate_without_a_face_needs_borders_stability_and_live_video(monkeypatch):
    from live_clipper import faces
    for model in (True, False):
        monkeypatch.setattr(faces, "available", lambda m=model: m)
        none = [[] for _ in range(8)]
        assert faces.validate(_frames(), RECT, none)["ok"]                       # bordered, stable, live inset
        frozen = faces.validate(_frames(live=False), RECT, none)                 # a still card in the same place
        assert not frozen["ok"] and frozen["signals"]["live"] < 0.6
        assert not faces.validate(_frames(cam=False), RECT, none)["ok"]          # no camera at all


def test_proposals_need_a_stable_inset_sized_face_and_are_never_central(monkeypatch):
    from live_clipper import faces
    monkeypatch.setattr(faces, "available", lambda: True)
    F = _frames()
    p = faces.propose(F, [[_face(0.15, 0.2)] for _ in range(8)])
    assert p and p["rect"][0] <= 0.04 and p["rect"][1] <= 0.07                  # snapped to the inset's edges
    assert faces.propose(F, [[_face(0.5, 0.45)] for _ in range(8)]) is None      # central: the game, not a cam
    assert faces.propose(F, [[_face(0.15, 0.2)]] * 4 + [[]] * 4) is None         # not stable over the window
    jitter = [[_face(0.1 + 0.1 * (i % 2), 0.2 + 0.15 * (i % 3))] for i in range(8)]
    assert faces.propose(F, jitter) is None                                      # moving face: not an inset


@pytest.mark.parametrize("rect", [[0, 0, 0.01, 0.01], [0.5, 0.5, 0.04, 0.3], [0.9, 0.9, 0.1, 0.03]])
def test_a_tiny_accepted_calibration_keeps_the_whole_frame_instead_of_failing(fx, tmp_path, rect):
    from live_clipper import faces, postprocess
    accepted = {"mode": "auto", "facecam": rect}                             # a calibration the channel API accepts
    v = faces.validate(np.zeros((8, 360, 640, 3), np.uint8), rect, [[] for _ in range(8)])
    assert not v["ok"] and "too small" in v["reason"]
    r = detect_rect(fx / "cam.mp4", rect)                                    # normal detection entry point
    assert r["facecam"] is None and "too small" in r["reason"]
    eng, st, cid, d = _engine(tmp_path, fx / "cam.mp4", render.preview_layout(accepted))
    asyncio.run(postprocess.render_final(eng, cid, "en", notify=False))       # the render is not lost
    c = st.clip(cid)
    assert c["layout"]["resolved"]["mode"] == "fit" and "too small" in c["layout"]["resolved"]["fallback"]
    assert c["files"]["final_layout"]["en"]["1"]["mode"] == "fit" and (d / "final_en.mp4").exists()


def test_the_detector_never_raises_and_falls_back_to_the_whole_frame(fx, monkeypatch):
    from live_clipper import faces
    monkeypatch.setattr(faces, "validate", lambda *a, **k: 1 / 0)
    r = detect_rect(fx / "cam.mp4", [0.03, 0.055, 0.25, 0.333])
    assert r["facecam"] is None and "ZeroDivisionError" in r["reason"]


# ---------------------------------------------------------------- bounded frame reads: threads, deadline, coverage
def test_every_frame_read_has_an_explicit_one_thread_budget_and_is_niced(monkeypatch):
    seen = []

    def run(cmd, **k):
        seen.append(cmd)
        return SimpleNamespace(stdout=b"\0" * (64 * 36 * 3), stderr=b"[Parsed_showinfo_0] n:0 pts:0 pts_time:1.5 x")
    monkeypatch.setattr(facecam.subprocess, "run", run)
    raw, at = facecam._read_frame("x.mp4", 1.5, 64, 36, False, 5.0)
    assert raw and at == 1.5
    cmd = seen[0]
    ff = cmd[cmd.index("ffmpeg"):]
    i = ff.index("-i")
    assert ff[ff.index("-threads") + 1] == "1" and ff.index("-threads") < i            # decoder
    assert ff[ff.index("-filter_threads") + 1] == "1"                                   # scale / filters
    assert "-threads" in ff[i:] and ff[i:][ff[i:].index("-threads") + 1] == "1"          # rawvideo output
    assert "-skip_frame" in ff and "-noaccurate_seek" in ff                              # one decoded frame
    from live_clipper.proc import _NICE
    assert cmd[:3] == [_NICE, "-n", "8"] if _NICE else cmd[0] == "ffmpeg"       # Windows: below-normal priority
    facecam._read_frame("x.mp4", 1.5, 64, 36, True, 5.0)
    assert "-skip_frame" not in seen[1] and "-noaccurate_seek" not in seen[1]           # exact read


def _fake_reads(monkeypatch, have=lambda t: True, delay=0.0, key_shift=0.0):
    """Each fake read costs `delay` seconds on a fake clock, so deadline tests don't depend on runner speed."""
    import time as _time
    calls = []
    clock = [0.0]
    if delay:
        monkeypatch.setattr(_time, "monotonic", lambda: clock[0])

    def read(path, t, aw, ah, exact, timeout, offset=0.0):
        calls.append((round(t, 2), exact))
        clock[0] += delay
        if not have(t):
            return None, None
        return b"\x10" * (aw * ah * 3), (t if exact else t - key_shift)
    monkeypatch.setattr(facecam, "_read_frame", read)
    return calls


def test_reads_go_first_last_middle_and_a_deadline_never_leaves_only_early_evidence(monkeypatch):
    assert facecam.coverage_order(8) == [0, 7, 4, 2, 6, 1, 3, 5]
    _fake_reads(monkeypatch, delay=0.1)
    s = facecam.window_samples("x.mp4", 10.0, 30.0)
    F, times, rd = facecam.frames_timed(s, 1920, 1080, aw=64, deadline_s=0.45, window=(10.0, 40.0))
    assert rd["deadline_hit"] and 3 <= len(times) <= 5 and times == sorted(times)
    assert min(times) < 11 and max(times) > 39 and any(20 < t < 30 for t in times)     # first, last, middle
    early = [t for t in facecam.window_samples("x.mp4", 10.0, 30.0)[:4]]
    assert facecam.coverage_problem([t for _, t in early], 10.0, 30.0)                   # 4 early: not enough
    assert "last third" in facecam.coverage_problem([10.2, 12, 15, 20], 10.0, 30.0)
    assert facecam.coverage_problem([10.2, 20, 25, 39.7], 10.0, 30.0) is None


def test_missing_late_frames_keep_the_whole_frame_instead_of_deciding_from_the_early_part(monkeypatch):
    _fake_reads(monkeypatch, have=lambda t: t < 30.0)                # 5 frames, none from the last third
    info = {"w": 1920, "h": 1080, "fps": 60.0, "start": 0.0}
    r = facecam.detect(facecam.window_samples("x.mp4", 10.0, 30.0), info, rect=[0, 0, 0.29, 0.29])
    assert r["facecam"] is None and "last third" in r["reason"] and r["probe"]["missed"] == 3
    _fake_reads(monkeypatch, delay=0.06)
    monkeypatch.setattr(facecam, "PROBE_DEADLINE_S", 0.2)
    r = facecam.detect(facecam.window_samples("x.mp4", 10.0, 30.0), info, rect=[0, 0, 0.29, 0.29])
    assert r["facecam"] is None and "in time" in r["reason"] and r["probe"]["deadline_hit"]


def test_a_keyframe_from_the_pre_roll_or_another_slot_is_reread_exactly(monkeypatch):
    _fake_reads(monkeypatch, key_shift=1.9)                  # keyframes 1.9 s before each moment
    s = facecam.window_samples("x.mp4", 10.0, 6.0)                   # 0.8 s spacing: slot 0.4 s
    F, times, rd = facecam.frames_timed(s, 1920, 1080, aw=64, window=(10.0, 16.0))
    assert rd["exact_reads"] == 8 and min(times) >= 10.0                 # never a pre-roll frame
    assert len(set(round(t, 2) for t in times)) == 8
    _fake_reads(monkeypatch, key_shift=1.5)
    s = facecam.window_samples("x.mp4", 10.0, 30.0)                  # 4.2 s spacing: keyframes accepted ...
    F, times, rd = facecam.frames_timed(s, 1920, 1080, aw=64, window=(10.0, 40.0))
    assert rd["exact_reads"] == 1 and min(times) >= 10.0                 # ... except the first (before the window)


def test_the_bundled_face_model_is_the_official_file_and_runs_offline():
    import hashlib

    from live_clipper import faces
    assert faces.MODEL.name == "face_detection_yunet_2023mar.onnx"
    assert hashlib.sha256(faces.MODEL.read_bytes()).hexdigest() == \
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
    assert (faces.MODEL.parent / "LICENSE.face_detection_yunet").exists()
    pytest.importorskip("onnxruntime")
    assert faces.available()
    assert faces.detect_faces(np.zeros((360, 640, 3), np.uint8)) == []           # loads and decodes, no network


def test_edits_accept_the_new_modes_and_keep_explicit_choices_first(tmp_path):
    from live_clipper.postprocess import EditError, validate_edit
    eng = SimpleNamespace(settings=SimpleNamespace(max_clip_seconds=90), clip_dir=lambda c: tmp_path, cold=None)
    clip = {"id": 1, "layout": {"mode": "auto"}, "files": {}}
    ok = validate_edit.__wrapped__ if hasattr(validate_edit, "__wrapped__") else validate_edit
    with pytest.raises(EditError, match="split needs facecam"):
        _validate_layout(ok, eng, clip, {"mode": "split"})
    assert _validate_layout(ok, eng, clip, {"mode": "split", "facecam": [0, 0, 0.25, 0.3]})["mode"] == "split"
    assert _validate_layout(ok, eng, clip, {"mode": "auto"})["mode"] == "auto"
    assert _validate_layout(ok, eng, clip, {"mode": "fit", "pin": True})["pin"] is True


def _validate_layout(fn, eng, clip, layout):
    """validate_edit needs a source file for trims only; give it a tiny one."""
    src = Path(eng.clip_dir(1)) / "source.mp4"
    if not src.exists():
        _ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src))
    clip["files"] = {"source": "source.mp4", "trim_in": 0.0, "duration": 1.0}
    return fn(eng, clip, {"layout": layout})["layout"]
