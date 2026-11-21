"""9:16 layouts as pure FFmpeg filtergraphs (no per-frame Python), suitable for gaming footage.

layout dict:
  mode: "auto"  (new clips) a detected or configured webcam inset -> "split"; otherwise "fit" (live_clipper/facecam.py).
                The decision is stored once per clip in layout["resolved"] and reused by every later render.
        "split" facecam on top (facecam_frac, default 0.32 of the height), the WHOLE gameplay frame below, aspect
                kept, centred in its panel over a blurred backdrop of itself; the inset is smoothed out of the
                gameplay panel so the face is not shown twice. Nothing of the gameplay or HUD is cropped. Captions
                sit in the padding just below the seam (never over the gameplay); the hook just above the seam.
        "fit"   full gameplay scaled to width over a blurred fill (nothing is cut off)
        "crop"  9:16 crop of the gameplay; cx (0..1) horizontal centre, zoom >= 1
        "stack" facecam region on top, gameplay crop below (older preset, kept as it was)
  zoom: float        fit/crop zoom (fit: >1 crops the sides a bit for a bigger picture)
  dy: float          fit: vertical offset of the gameplay as a fraction of output height
  cx: float          crop centre x (0..1)
  facecam: [x,y,w,h] normalized source rect (stack)
  gameplay: [x,y,w,h] normalized source rect (stack; default centre region)
  facecam_frac: float  share of output height for the facecam (stack, default 0.36)
"""
from __future__ import annotations

import time

DEFAULT_LAYOUT = {"mode": "fit", "zoom": 1.0, "dy": -0.06, "cx": 0.5,
                  "facecam": [0.0, 0.0, 0.25, 0.3], "gameplay": [0.1, 0.0, 0.8, 1.0], "facecam_frac": 0.36}
LAYOUT_VERSION = 1
SPLIT_FRAC = 0.32                       # measured reference: 32 % facecam, 68 % gameplay, full-width seam
SPLIT_CAPTION_GAP = 0.015               # split: captions sit just below the seam, in the padding above the gameplay


def is_legacy_default(L) -> bool:
    """A channel layout that only carries the old built-in default (fit): treated as "auto" for NEW clips.
    {"mode": "fit", "pin": true} keeps fit deliberately."""
    if not isinstance(L, dict) or not L:
        return True
    if L.get("pin") or set(L) - set(DEFAULT_LAYOUT):
        return False
    return L.get("mode", "fit") == "fit" and all(L[k] == DEFAULT_LAYOUT[k] for k in L if k != "mode")


def channel_layout(L) -> dict:
    """The layout a NEW clip of this channel starts with. Explicit presets and coordinates are kept as they are."""
    return {"mode": "auto"} if is_legacy_default(L) else dict(L)


def window_key(source: str, size: int, trim_in: float, duration: float) -> dict:
    """The exact media a decision was made on: a later render reuses it only for the same window."""
    return {"source": str(source), "size": int(size), "trim_in": round(float(trim_in), 3),
            "duration": round(float(duration), 3)}


def needs_detection(L, window: dict | None = None) -> bool:
    """An auto layout needs a (new) decision for this window. A channel calibration (auto + facecam) is validated
    per window too; an explicit split with coordinates (operator edit or channel preset) never is."""
    if not isinstance(L, dict) or L.get("mode") != "auto":
        return False
    r = L.get("resolved")
    return not r or (window is not None and r.get("window") != window)


def resolve(L: dict, det: dict | None = None, now: float | None = None, window: dict | None = None) -> dict:
    """The clip layout with the decision for this window stored under "resolved" (reused by every later render of
    the same window). A calibrated area is used only when det validated it in this window."""
    if not isinstance(L, dict) or L.get("mode") != "auto":
        return L
    frac = float(L.get("facecam_frac", SPLIT_FRAC))
    if det and det.get("facecam"):
        r = {"mode": "split", "facecam": det["facecam"], "facecam_frac": frac, "fallback": None, "detector": det}
    else:
        why = (det or {}).get("reason") or ("the calibrated webcam area was not validated in this window"
                                            if L.get("facecam") else "no detection ran")
        r = {"mode": "fit", "fallback": why, "detector": det or {}}
    r.update(layout_version=LAYOUT_VERSION, resolved_at=round(now or time.time(), 3),
             source=(det or {}).get("source"), window=window)
    return {**L, "resolved": r}


def preview_layout(L: dict) -> dict:
    """Provisional decision for the cut-time preview only, without probing any frames: always the whole frame (a
    calibrated area is never used before it is validated). The first final decides on the full clip window."""
    if not isinstance(L, dict) or L.get("mode") != "auto" or L.get("resolved"):
        return L
    return {**L, "preview": {"mode": "fit", "provisional": True,
                             "fallback": "preview: the layout is decided at the first final render"}}


def effective(L) -> dict:
    """What a render uses: an auto layout's final decision, else its provisional preview decision, else the whole
    frame; any other layout as it is."""
    L = L or {}
    if L.get("mode") == "auto":
        if L.get("resolved"):
            return dict(L["resolved"])
        if L.get("preview"):
            return dict(L["preview"])
        return {"mode": "fit"}
    return L


def snapshot(L) -> dict:
    """The per-render record (files.final_layout[lang][media_version]): what this exact render used."""
    e = effective(L)
    mode = e.get("mode", "fit")
    return {"layout_version": LAYOUT_VERSION, "requested": (L or {}).get("mode", "fit"), "mode": mode,
            "facecam": e.get("facecam") if mode in ("split", "stack") else None,
            "facecam_frac": (float(e.get("facecam_frac", SPLIT_FRAC)) if mode == "split" else
                             float(e.get("facecam_frac", DEFAULT_LAYOUT["facecam_frac"])) if mode == "stack" else None),
            "fallback": e.get("fallback"), "confidence": (e.get("detector") or {}).get("confidence")}


def split_ass(src, dst, layout) -> str:
    """For a split render: a copy of the ASS file whose styles sit top-centred just below the seam, in the padding
    above the centred gameplay (never over it). Returns dst; any other layout returns src unchanged."""
    from pathlib import Path
    e = effective(layout)
    if e.get("mode") != "split" or not e.get("facecam"):
        return str(src)
    text = Path(src).read_text(encoding="utf-8")
    play_h = 1280
    for line in text.splitlines():
        if line.startswith("PlayResY:"):
            try:
                play_h = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    top = int(round((float(e.get("facecam_frac", SPLIT_FRAC)) + SPLIT_CAPTION_GAP) * play_h))
    fmt, out = None, []
    for line in text.splitlines():
        if line.startswith("Format:") and fmt is None and "Alignment" in line:
            fmt = [f.strip() for f in line.split(":", 1)[1].split(",")]
        if line.startswith("Style:") and fmt:
            vals = [v.strip() for v in line.split(":", 1)[1].split(",")]
            if len(vals) == len(fmt):
                vals[fmt.index("Alignment")] = "8"              # top centre
                vals[fmt.index("MarginV")] = str(top)
                line = "Style: " + ",".join(vals)
        out.append(line)
    Path(dst).write_text("\n".join(out) + "\n", encoding="utf-8")
    return str(dst)


def fps_cap(cap: int, src_fps: float | None) -> int:
    """The fps filter value: the configured cap only when the source is faster (never duplicate frames)."""
    if not cap or cap <= 0:
        return 0
    if src_fps and src_fps <= cap + 0.01:
        return 0
    return int(cap)


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _esc(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def title_y(layout: dict | None, out_h: int) -> str:
    """Vertical position for the hook title: centred in the top fill for 'fit', else near the top."""
    raw = effective(layout)
    L = {**DEFAULT_LAYOUT, **raw}
    H = _even(out_h)
    if L["mode"] == "split":
        fh = _even(H * float(raw.get("facecam_frac", SPLIT_FRAC)))
        return f"max(h*0.03\\,{fh}-text_h-h*0.025)"         # just above the seam, as in the reference
    if L["mode"] == "fit":
        W = _even(H * 9 / 16)
        top = (H - W * max(1.0, float(L["zoom"])) * 9 / 16) / 2 + float(L["dy"]) * H
        return f"max(h*0.03\\,({int(top)}-text_h)/2)"
    return "h*0.06"


SAFE_NAME = __import__("re").compile(r"^[A-Za-z0-9_.-]{1,64}$")


def layout_filter(layout: dict | None, out_h: int, *, ass: str | None = None,
                  title_file: str | None = None, title_secs: float = 2.8, font: str | None = None, fps: int = 0) -> str:
    """Return a -filter_complex graph from [0:v] to [v]. fps > 0 caps the output frame rate first.
    title_file: a plain file NAME (run ffmpeg with the clip folder as working directory) holding the UTF-8 hook text.
    The text itself never enters the filtergraph (quotes, commas, colons, percent signs or newlines in a hook used to
    break drawtext parsing); expansion=none keeps '%' literal."""
    raw = effective(layout)
    L = {**DEFAULT_LAYOUT, **raw}
    H = _even(out_h)
    W = _even(H * 9 / 16)
    mode = L["mode"]
    if mode == "split" and raw.get("facecam"):
        fh = _even(H * float(raw.get("facecam_frac", SPLIT_FRAC)))
        gh = H - fh
        fx, fy, fw, fhh = (float(v) for v in raw["facecam"])
        src = raw.get("source") or []
        if len(src) == 2 and src[0] and src[1]:     # smooth the inset out of the gameplay (delogo needs pixels)
            sw, sh = int(src[0]), int(src[1])
            mx, my = max(2, int(sw * 0.008)), max(2, int(sh * 0.008))        # the inset's own frame/border too
            x0, y0 = max(1, int(fx * sw) - mx), max(1, int(fy * sh) - my)
            x1, y1 = min(sw - 2, int((fx + fw) * sw) + mx), min(sh - 2, int((fy + fhh) * sh) + my)
            hide = f"delogo=x={x0}:y={y0}:w={max(1, x1 - x0)}:h={max(1, y1 - y0)}," if x1 > x0 and y1 > y0 else ""
        else:
            hide = ""
        g = (f"[0:v]split=3[fc][gp][bk];"
             f"[fc]crop=iw*{fw}:ih*{fhh}:iw*{fx}:ih*{fy},"
             f"scale={W}:{fh}:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop={W}:{fh}:(iw-ow)/2:(ih-oh)*0.3[top];"                       # head and shoulders, headroom kept
             f"[bk]scale={W // 8}:{gh // 8}:force_original_aspect_ratio=increase,crop={W // 8}:{gh // 8},"
             f"boxblur=6:2,eq=brightness=-0.15:saturation=1.1,scale={W}:{gh}[bg];"
             f"[gp]{hide}scale={W}:{gh}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
             f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1[bot];"            # whole frame, centred in its panel
             f"[top][bot]vstack=2,setsar=1[base]")
    elif mode == "split":                           # no coordinates: never invent a camera
        L["mode"] = mode = "fit"
    if mode == "split":
        pass                                        # built above
    elif mode == "crop":
        z = max(1.0, float(L["zoom"]))
        cx = min(1.0, max(0.0, float(L["cx"])))
        # crop a 9:16 window of height ih/z, centred at cx
        g = (f"[0:v]crop=w='min(iw,ih/{z}*9/16)':h='ih/{z}':"
             f"x='max(0,min(iw-ow,{cx}*iw-ow/2))':y='(ih-oh)/2',scale={W}:{H},setsar=1[base]")
    elif mode == "stack":
        fh = _even(H * float(L["facecam_frac"]))
        gh = H - fh
        fx, fy, fw, fhh = L["facecam"]
        gx, gy, gw, ghh = L["gameplay"]
        g = (f"[0:v]split=2[fc][gp];"
             f"[fc]crop=iw*{fw}:ih*{fhh}:iw*{fx}:ih*{fy},scale={W}:{fh}:force_original_aspect_ratio=increase,crop={W}:{fh}[top];"
             f"[gp]crop=iw*{gw}:ih*{ghh}:iw*{gx}:ih*{gy},scale={W}:{gh}:force_original_aspect_ratio=increase,crop={W}:{gh}[bot];"
             f"[top][bot]vstack=2,setsar=1[base]")
    else:  # fit
        z = max(1.0, float(L["zoom"]))
        fw = _even(W * z)
        dy = int(float(L["dy"]) * H)
        g = (f"[0:v]split=2[a][b];"
             f"[a]scale={W // 8}:{H // 8}:force_original_aspect_ratio=increase,crop={W // 8}:{H // 8},"
             f"boxblur=6:2,eq=brightness=-0.12:saturation=1.2,scale={W}:{H}[bg];"
             f"[b]scale={fw}:-2[fg];"
             f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2+{dy}:shortest=1,setsar=1[base]")
    tail = "[base]"
    if ass:
        g += f";{tail}ass='{_esc(ass)}'[subs]"         # split: pass split_ass() output (captions below the seam)
        tail = "[subs]"
    if title_file:
        if not SAFE_NAME.match(title_file):
            raise ValueError("title_file must be a plain safe file name")
        fontfile = f":fontfile='{_esc(font)}'" if font else ""
        size = int(H * 0.038)
        g += (f";{tail}drawtext=textfile={title_file}:expansion=none{fontfile}:fontsize={size}:fontcolor=white:"
              f"line_spacing={size // 4}:"
              f"box=1:boxcolor=black@0.62:boxborderw={size // 2}:x=(w-text_w)/2:y={title_y(layout, out_h)}:"
              f"enable='lt(t,{title_secs})'[titled]")
        tail = "[titled]"
    if fps and fps > 0:
        g = g.replace("[0:v]", f"[0:v]fps={int(fps)},", 1)
    return g + f";{tail}null[v]"


def wrap_title(text: str, width: int) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines[:3])


def size_for(out_h: int) -> tuple[int, int]:
    H = _even(out_h)
    return _even(H * 9 / 16), H
