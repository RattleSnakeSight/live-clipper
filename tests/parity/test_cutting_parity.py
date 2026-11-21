"""Cut/render parity against results recorded with the original implementation (data/cutting_ref.json).

Generates the same synthetic sources as the original harness (testsrc2 + sine with a frame-number marker,
MPEG-TS),
segments them with the capture command, runs the public cut/render code through PublicAdapter and compares:
cut boundaries and stored reasons, epochs, ffprobe properties (codec, size, fps, pix_fmt, audio rate/channels,
durations, start offsets incl. the late video start of input-side seeks), ffmpeg command lines, filtergraphs,
caption files and pure render facts. Durations: +-1 output frame + 1 AAC frame."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from adapter import PublicAdapter  # noqa: E402
from normalize import norm_args, norm_layout, props, summarize  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)
INP = json.loads((HERE / "data" / "cutting_inputs.json").read_text(encoding="utf-8"))
REF = json.loads((HERE / "data" / "cutting_ref.json").read_text(encoding="utf-8"))
TOL = 0.034 + 0.022 + 0.005  # one output frame @30 + one AAC frame (+ rounding)
T0, LAT = INP["timeline"]["T0"], INP["timeline"]["latency_s"]


def make_source(spec: dict, out: Path) -> None:
    vf, dur = spec["video"], spec["duration_s"]
    rate = int(vf.split("r=")[1].split(":")[0])
    exprs = [
        f"16+40*floor(floor(N/{rate})/10)",
        f"16+20*mod(floor(N/{rate}),10)",
        f"16+30*floor(mod(N,{rate})/10)",
        f"16+20*mod(mod(N,{rate}),10)",
    ]
    graph = f"{vf}:d={dur}[bg];"
    for i, e in enumerate(exprs):
        graph += f"color=c=black:s=64x64:r={rate}:d={dur},format=yuv420p,geq=lum='{e}':cb=128:cr=128[p{i}];"
    graph += (
        "[bg][p0]overlay=0:0[o0];[o0][p1]overlay=64:0[o1];[o1][p2]overlay=128:0[o2];[o2][p3]overlay=192:0,"
        "format=yuv420p[v]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"{spec['audio']}:d={dur}",
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-map",
            "0:a",
            "-t",
            str(dur),
            *spec["encode"],
            str(out),
        ],
        check=True,
        timeout=600,
    )


def segment(src: Path, run_dir: Path, seconds: float) -> list:
    """The capture command (stream copy into 2 s MPEG-TS segments + a live CSV list)."""
    run_dir.mkdir(parents=True)
    csv = run_dir / "segments.csv"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-fflags",
            "+genpts",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(seconds),
            "-segment_format",
            "mpegts",
            "-segment_list",
            str(csv),
            "-segment_list_type",
            "csv",
            "-segment_list_flags",
            "live",
            "-reset_timestamps",
            "0",
            str(run_dir / "s%06d.ts"),
        ],
        check=True,
        timeout=300,
    )
    segs = []
    for ln in csv.read_text().splitlines():
        p = ln.strip().split(",")
        if len(p) >= 3:
            segs.append(
                {"name": p[0], "path": str(run_dir / p[0]), "pts_start": float(p[1]), "pts_end": float(p[2])}
            )
    return segs


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("parity")
    segs = {}
    for k in ("src60", "src30"):
        spec = INP["sources"][k]
        make_source(spec, d / spec["file"])
        segs[k] = segment(d / spec["file"], d / f"seg_{k}", INP["segmenting"]["segment_seconds"])
    return d, segs


@pytest.fixture(scope="module")
def adapter():
    return PublicAdapter()


def close(got, exp, tol=0.02):
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and isinstance(got, (int, float)):
        return abs(got - exp) <= tol
    return got == exp


def assert_props(got: dict, exp: dict, where: str, keyframes: bool = True):
    assert got["exists"] == exp["exists"], where
    if not exp["exists"]:
        return
    assert close(got["format_duration"], exp["format_duration"], TOL), (where, got, exp)
    for kind in ("video", "audio"):
        assert (kind in got) == (kind in exp), (where, kind)
        for k, v in (exp.get(kind) or {}).items():
            tol = TOL if k == "duration" else 0.02
            assert close(got[kind][k], v, tol), (where, kind, k, got[kind][k], v)
    if "faststart" in exp:
        assert got["faststart"] == exp["faststart"], where
    if keyframes and "keyframes" in exp:
        assert got["keyframes"] == pytest.approx(exp["keyframes"], abs=0.02), where


def cut_record(res, rel0: float) -> dict:
    return {
        "ok": res.ok,
        "start_rel": round(res.start - rel0, 4),
        "end_rel": round(res.end - rel0, 4),
        "source_t0_rel": round(res.source_t0 - rel0, 4),
        "trim_in": round(res.trim_in, 4),
        "duration": round(res.duration, 4),
        "truncated": res.truncated,
        "epoch": res.epoch,
        "files": res.files,
        "layout": res.layout,
        "error": res.error,
    }


def test_segments_match_the_recorded_capture_split(media):
    _, segs = media
    for k, ss in segs.items():
        exp = REF["segments"][k]
        assert [s["name"] for s in ss] == [s["name"] for s in exp]
        for a, b in zip(ss, exp, strict=True):
            assert close(a["pts_start"], b["pts_start"], 0.001) and close(a["pts_end"], b["pts_end"], 0.001)


@pytest.mark.parametrize("case", INP["live_cuts"], ids=lambda c: c["id"])
def test_live_cut(media, adapter, tmp_path, case):
    d0, segsets = media
    segs = segsets[case["source"]]
    rel0 = segs[0]["pts_start"] + T0 + LAT
    pre, post = INP["roll"]["pre_roll_s"], INP["roll"]["post_roll_s"]
    (ws, we), _ = adapter.window_for(rel0 + case["action_rel"], pre, post)
    prefer = rel0 + case.get("prefer_rel", case["action_rel"])
    out = tmp_path / case["id"]
    res, events, epochs, _ = asyncio.run(
        adapter.cut(
            segs,
            case.get("index") or {},
            T0,
            LAT,
            ws,
            we,
            out,
            json.loads(json.dumps(case["layout"])),
            1280,
            "veryfast",
            30,
            prefer,
            case.get("segment_timeout_s", 20.0),
        )
    )
    rec, exp = cut_record(res, rel0), REF["live_cuts"][case["id"]]
    assert [round(ws - rel0, 4), round(we - rel0, 4)] == exp["window_rel"]
    for k in (
        "ok",
        "start_rel",
        "end_rel",
        "source_t0_rel",
        "trim_in",
        "duration",
        "truncated",
        "epoch",
        "files",
        "layout",
        "error",
    ):
        assert close(rec[k], exp[k]), (k, rec[k], exp[k])
    for k, v in case["expect"].items():  # the implementation-neutral expectations too
        if k != "note":
            assert close(rec[k], v), (k, rec[k], v)
    assert [{"id": e["id"], "reason": e["reason"], "n": e["n"]} for e in epochs] == exp["epochs"]
    for name in ("preview.mp4", "audio.wav", "source.ts"):
        assert_props(props(summarize(out / name)), exp["outputs"][name], f"{case['id']}/{name}")


@pytest.mark.parametrize("name", list(INP["layout_previews"]["layouts"]))
def test_layout_preview(media, adapter, tmp_path, name):
    _, segsets = media
    lp = INP["layout_previews"]
    lay = lp["layouts"][name]
    segs = segsets[lp["source"]]
    rel0 = segs[0]["pts_start"] + T0 + LAT
    exp = REF["layout_previews"][name]
    assert adapter.render.layout_filter(json.loads(json.dumps(lay)), 1280, fps=30) == exp["filter_complex"]
    res, _, _, _ = asyncio.run(
        adapter.cut(
            segs,
            {},
            T0,
            LAT,
            rel0 + lp["start_rel"],
            rel0 + lp["end_rel"],
            tmp_path,
            json.loads(json.dumps(lay)),
            1280,
            "veryfast",
            30,
            rel0 + lp["start_rel"],
            20.0,
        )
    )
    rec = cut_record(res, rel0)
    for k, v in exp["cut"].items():
        if k not in ("window_rel", "prefer_rel", "first_frame_error_s"):
            assert close(rec[k], v), (k, rec[k], v)
    assert_props(props(summarize(tmp_path / "preview.mp4", kf=False)), exp["preview"], name)


@pytest.fixture(scope="module")
def base_cut(media, adapter, tmp_path_factory):
    """The mid_unaligned live cut whose source.ts every final render starts from."""
    _, segsets = media
    case = next(c for c in INP["live_cuts"] if c["id"] == INP["final_renders"]["from_live_cut"])
    segs = segsets[case["source"]]
    rel0 = segs[0]["pts_start"] + T0 + LAT
    (ws, we), _ = adapter.window_for(
        rel0 + case["action_rel"], INP["roll"]["pre_roll_s"], INP["roll"]["post_roll_s"]
    )
    out = tmp_path_factory.mktemp("base")
    res, _, _, _ = asyncio.run(
        adapter.cut(
            segs,
            {},
            T0,
            LAT,
            ws,
            we,
            out,
            {"mode": "fit"},
            1280,
            "veryfast",
            30,
            rel0 + case["action_rel"],
            20.0,
        )
    )
    return out, res


def words_uniform(seg: dict) -> dict:
    ws = seg["text"].split()
    step = (seg["end"] - seg["start"]) / max(1, len(ws))
    return {
        **seg,
        "words": [
            {"start": seg["start"] + i * step, "end": seg["start"] + (i + 1) * step - 0.02, "word": w}
            for i, w in enumerate(ws)
        ],
    }


def render_case(adapter, base_cut, tmp_path, case, settings=None):
    base_dir, res = base_cut
    fr = INP["final_renders"]
    shutil.copy2(base_dir / "source.ts", tmp_path / "source.ts")
    files = {"source": "source.ts", "trim_in": round(res.trim_in, 4), "duration": round(res.duration, 4)}
    if case.get("trim"):
        a, b = case["trim"]
        files = {**files, "trim_in": files["trim_in"] + a, "duration": b - a}
    caps = {}
    if case.get("captions") and case.get("lang"):
        tr = INP["caption_transcript"]
        adapter.captions_files(
            tmp_path,
            case["lang"],
            adapter.build_cues([words_uniform(s) for s in tr["segments"]]),
            tr["play_res_final"],
        )
        caps[case["lang"]] = {"ass": f"captions/{case['lang']}.ass", "status": "ready"}
    clip = {
        "id": 1,
        "files": files,
        "captions": caps,
        "layout": json.loads(json.dumps(case["layout"])),
        "title": "",
    }
    height = fr["settings"][case.get("height_key", "final_height")]
    out = tmp_path / (f"final_{case['lang']}.mp4" if case.get("lang") else "preview.mp4")
    rc, err, cmds, _, lay_after = asyncio.run(
        adapter.render_clip(
            tmp_path, clip, out, height, case.get("lang"), case.get("title"), settings or fr["settings"], None
        )
    )
    return rc, err, cmds, lay_after, out


@pytest.mark.parametrize("case", INP["final_renders"]["cases"], ids=lambda c: c["id"])
def test_final_render(adapter, base_cut, tmp_path, case):
    exp = REF["final_renders"][case["id"]]
    rc, err, cmds, lay_after, out = render_case(adapter, base_cut, tmp_path, case)
    assert rc == exp["rc"] == 0, err
    assert [norm_args(c["args"]) for c in cmds] == exp["ffmpeg_args"]  # same command line
    got_lay, exp_lay = norm_layout(lay_after), exp["layout_after"]
    assert got_lay == exp_lay
    # the late video start of the input-side seek is part of the recorded behaviour (docs/DEVIATIONS.md)
    assert_props(props(summarize(out)), exp["output"], case["id"], keyframes=False)


def test_accurate_seek_opt_in_starts_the_video_at_the_trim_point(adapter, base_cut, tmp_path):
    case = INP["final_renders"]["cases"][0]
    settings = {**INP["final_renders"]["settings"], "accurate_seek": True}
    rc, err, cmds, _, out = render_case(adapter, base_cut, tmp_path, case, settings)
    assert rc == 0, err
    args = cmds[0]["args"]
    assert args.index("-i") < args.index("-ss")
    p = props(summarize(out, kf=False))
    assert p["video"]["start_time"] <= 0.05 and abs(p["video"]["duration"] - 30.0) <= TOL


def test_captions_and_render_facts(adapter, tmp_path):
    tr = INP["caption_transcript"]
    cues = adapter.build_cues([words_uniform(s) for s in tr["segments"]])
    exp = REF["captions"]
    assert cues == exp["cues"]
    fp = adapter.captions_files(tmp_path / "preview", "en", cues, tr["play_res_preview"])
    ff = adapter.captions_files(tmp_path / "final", "en", cues, tr["play_res_final"])
    assert Path(fp["srt"]).read_text(encoding="utf-8") == exp["srt"]
    assert Path(fp["vtt"]).read_text(encoding="utf-8") == exp["vtt"]
    assert Path(fp["ass"]).read_text(encoding="utf-8") == exp["ass_720x1280"]
    assert Path(ff["ass"]).read_text(encoding="utf-8") == exp["ass_1080x1920"]
    sp = adapter.split_ass(
        Path(ff["ass"]), tmp_path / "split_en.ass", {"mode": "split", "facecam": [0, 0, 0.25, 0.3]}
    )
    assert Path(sp).read_text(encoding="utf-8") == exp["split_ass_1080x1920"]
    R, facts = adapter.render, REF["render_facts"]
    assert {str(h): list(R.size_for(h)) for h in (1280, 1920, 640)} == facts["size_for"]
    assert {
        f"{c}/{s}": R.fps_cap(c, s)
        for c, s in ((30, 60.0), (30, 30.0), (30, 29.97), (60, 30.0), (30, None), (0, 60.0), (30, 30.02))
    } == facts["fps_cap"]
    assert {
        m: R.title_y({"mode": m, "facecam": [0, 0, 0.25, 0.3]} if m == "split" else {"mode": m}, 1920)
        for m in ("fit", "split", "crop", "stack")
    } == facts["title_y"]
    assert (
        R.wrap_title("Zentorno slams into the gas station — chat loses it badly today", 20)
        == facts["wrap_title_20"]
    )
    assert {
        "none": R.channel_layout(None),
        "legacy_fit": R.channel_layout({"mode": "fit"}),
        "pinned_fit": R.channel_layout({"mode": "fit", "pin": True}),
        "crop": R.channel_layout({"mode": "crop", "cx": 0.3}),
    } == facts["channel_layout"]
    g = R.layout_filter(
        {"mode": "fit"},
        1920,
        ass="captions/en.ass",
        title_file="hook_en_1_2.txt",
        font="/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
        fps=30,
    )
    assert norm_args([g])[0] == facts["filter_final_fit_ass_title"]
