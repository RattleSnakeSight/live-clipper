"""Machine-independent views of parity results: media properties and ffmpeg arguments
without local paths.
Used to sanitize the recorded reference results and to compare the public code's results against them."""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
from pathlib import Path

V_KEYS = ("codec_name", "profile", "width", "height", "pix_fmt", "sample_aspect_ratio", "avg_frame_rate")
A_KEYS = ("codec_name", "profile", "sample_rate", "channels")


def _f(v):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def props(summary: dict) -> dict:
    """From a harness-style summary {"format", "streams", "faststart"?, "keyframes_first_12s"?}."""
    if not summary or not summary.get("exists", True) or not summary.get("streams"):
        return {"exists": False}
    out: dict = {"exists": True, "format_duration": _f((summary.get("format") or {}).get("duration"))}
    for s in summary["streams"]:
        kind = s.get("codec_type")
        if kind in ("video", "audio") and kind not in out:
            out[kind] = {
                **{k: s.get(k) for k in (V_KEYS if kind == "video" else A_KEYS)},
                "start_time": _f(s.get("start_time")),
                "duration": _f(s.get("duration")),
            }
    if "faststart" in summary:
        out["faststart"] = summary["faststart"]
    if "keyframes_first_12s" in summary:
        out["keyframes"] = summary["keyframes_first_12s"]
    return out


def _top_atoms(path: Path) -> list:
    out = []
    with open(path, "rb") as f:
        while True:
            h = f.read(8)
            if len(h) < 8:
                break
            size, typ = struct.unpack(">I4s", h)
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                f.seek(size - 16, 1)
            elif size == 0:
                out.append(typ.decode("latin-1"))
                break
            else:
                f.seek(size - 8, 1)
            out.append(typ.decode("latin-1"))
    return out


def summarize(path: Path, kf: bool = True) -> dict:
    """The harness's ffprobe summary of one produced file."""
    path = Path(path)
    if not path.exists():
        return {"exists": False}
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,profile,width,height,pix_fmt,sample_aspect_ratio,"
            "avg_frame_rate,r_frame_rate,sample_rate,channels,duration,start_time,nb_frames:"
            "format=duration,start_time",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    j = json.loads(r.stdout or "{}")
    d = {"exists": True, "format": j.get("format"), "streams": j.get("streams")}
    if path.suffix == ".mp4":
        atoms = _top_atoms(path)
        d["faststart"] = "moov" in atoms and "mdat" in atoms and atoms.index("moov") < atoms.index("mdat")
    if kf and any(s.get("codec_type") == "video" for s in d["streams"] or []):
        k = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-skip_frame",
                "nokey",
                "-show_entries",
                "frame=pts_time",
                "-of",
                "csv=p=0",
                "-read_intervals",
                "%+12",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        d["keyframes_first_12s"] = [round(float(x.strip(",")), 3) for x in k.stdout.split() if x.strip(",")]
    return d


_HOOK = re.compile(r"hook_[A-Za-z]+_\d+_\d+\.txt")
_SPLIT = re.compile(r"split_[A-Za-z]+_\d+_\d+\.ass")
_PART = re.compile(r"\.\d+\.\d+\.part\.mp4$")


def norm_args(args: list[str]) -> list[str]:
    """ffmpeg arguments with local paths, pids and timestamps replaced by placeholders."""
    out = []
    for a in args:
        if os.path.isabs(a) or re.match(r"^[A-Za-z]:[\\/]", a):
            a = "<clip>/" + _PART.sub(".<tmp>.part.mp4", re.split(r"[\\/]", a)[-1])
        a = _SPLIT.sub("split_<n>.ass", a)
        a = re.sub(r"ass='[^']*?([\w<>]+\.ass)'", lambda m: f"ass='<clip>/{m.group(1)}'", a)
        a = re.sub(r"fontfile='[^']*'", "fontfile='<font>'", a)
        out.append(_HOOK.sub("<hook>", a))
    return out


def norm_layout(L):
    """A stored layout without run-specific values (decision time, file size, detector details)."""
    if not isinstance(L, dict):
        return L
    L = json.loads(json.dumps(L))
    r = L.get("resolved")
    if isinstance(r, dict):
        for k in ("resolved_at", "detector", "fallback"):
            r.pop(k, None)
        if isinstance(r.get("window"), dict):
            r["window"].pop("size", None)
    return L
