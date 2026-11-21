"""Conservative webcam-inset detection for the 9:16 split layout (streamer facecam on top, full gameplay below).

Lightweight and bounded: at most MAX_FRAMES small frames (ANALYSIS_W pixels wide) are pulled with ffmpeg from the
clip's own media (first / middle / last moments), then analysed with numpy only (no model, no new dependency).

A webcam inset is accepted only when ALL of these hold:
  * persistent borders: its inner edges are straight lines that stay at the same place in (nearly) every sampled
    frame, while the content inside changes;
  * plausible size and shape: 10-45 % of the frame width, 12-50 % of its height, aspect 0.7-2.4 (no quadrant or
    half-screen splits), at least two sides on persistent lines;
  * a person: a clearly larger share of skin-tone pixels inside than in the gameplay around it (a minimap, a HUD
    panel or a game scene does not qualify);
  * one compact face-sized skin region (the largest connected skin blob holds most of the skin pixels and covers at
    least 3 % of the box) in most frames: scattered skin-coloured pixels (map art, fire, sand, UI) do not count;
  * a camera, not artwork: real camera pictures have texture and sensor noise; a box whose pixels are mostly
    perfectly flat (cartoon or game character portraits, UI art) is rejected even when it moves and has skin tones;
  * stable: the same box is found independently in the first and in the second half of the samples (a camera that
    moves, appears or disappears fails this).
Anything else (no camera, weak evidence, an already portrait or stacked source, changing layouts) returns no camera,
and the renderer keeps the whole source frame. Explicit per-channel coordinates always win over detection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Optional

log = logging.getLogger(__name__)

VERSION = 1
ANALYSIS_W = 256
MAX_FRAMES = 8
MIN_CONF = 0.6
MAX_FLAT = 0.75                 # share of perfectly flat pixels above which a box is artwork, not a camera


# ---------------------------------------------------------------- media access (bounded)
def probe(path) -> Optional[dict]:
    """{"w", "h", "fps"} of the first video stream, or None."""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,avg_frame_rate,r_frame_rate,start_time", "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=15)
        st = (json.loads(p.stdout or "{}").get("streams") or [{}])[0]
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    if not w or not h:
        return None
    fps = 0.0
    for k in ("avg_frame_rate", "r_frame_rate"):
        try:
            n, d = str(st.get(k) or "0/0").split("/")
            if float(d):
                fps = float(n) / float(d)
                if fps > 0:
                    break
        except ValueError:
            continue
    try:
        start = float(st.get("start_time") or 0.0)
    except (TypeError, ValueError):
        start = 0.0
    return {"w": w, "h": h, "fps": round(fps, 3), "start": round(start, 3)}


PROBE_THREADS = "1"            # explicit thread budget for decoder, filters and output of every frame read
PROBE_DEADLINE_S = 20.0        # total wall-clock budget for one window's frame reads
SAMPLE_TIMEOUT_S = 8.0
MIN_FRAMES = 4


def coverage_order(n: int) -> list:
    """Sample indices in the order they are read: first, last, middle, then the quarters, then the rest; a deadline
    therefore leaves evenly spread evidence, never only the early part of the window."""
    if n <= 0:
        return []
    order = [0, n - 1, n // 2, n // 4, (3 * n) // 4]
    order += list(range(n))
    seen, out = set(), []
    for i in order:
        if 0 <= i < n and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _read_frame(path, t: float, aw: int, ah: int, exact: bool, timeout: float, offset: float = 0.0):
    """One frame at (keyframe at or before | exactly at) t -> (rgb bytes, actual source time) or (None, None).
    Keyframe mode decodes a single frame; exact mode decodes from the previous keyframe. One thread everywhere."""
    from .proc import LOW_PRIO, niced
    cmd =["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-threads", PROBE_THREADS,
           "-filter_threads", PROBE_THREADS]
    if not exact:
        cmd += ["-skip_frame", "nokey", "-noaccurate_seek"]
    cmd += ["-ss", f"{max(0.0, t):.3f}", "-copyts", "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
            "-vf", f"showinfo,scale={aw}:{ah}:flags=area", "-threads", PROBE_THREADS,
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    try:
        p = subprocess.run(niced(cmd, 8), capture_output=True, timeout=max(0.5, timeout), **LOW_PRIO)
    except (subprocess.SubprocessError, OSError):
        return None, None
    if len(p.stdout) != aw * ah * 3:
        return None, None
    at = None
    for line in p.stderr.decode("utf-8", "replace").splitlines():
        if "pts_time:" in line:
            try:
                at = float(line.split("pts_time:", 1)[1].split()[0]) - offset     # stream start -> 0
            except (IndexError, ValueError):
                at = None
            break
    return p.stdout, at


def frames_timed(samples: list, w: int, h: int, aw: int = ANALYSIS_W, deadline_s: Optional[float] = None,
                 window: Optional[tuple] = None, offset: float = 0.0):
    """samples: [(path, seconds)] -> (frames (n, ah, aw, 3) uint8 sorted by time, their source times, info).
    Reads in coverage order under a total deadline. A keyframe read whose frame lies before the window (pre-roll),
    or outside its own slot (half the sample spacing) is re-read exactly. Callers hold the shared media slot (bounded with encodes)."""
    import time as _t

    import numpy as np
    ah = max(2, int(round(aw * h / w / 2)) * 2)
    samples = samples[:MAX_FRAMES]
    start_w = window[0] if window else (min(t for _, t in samples) - 0.15 if samples else 0.0)
    deadline_s = PROBE_DEADLINE_S if deadline_s is None else float(deadline_s)
    ts = sorted(t for _, t in samples)
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    slot = min(2.5, 0.5 * min(gaps)) if gaps else 2.5
    t0 = _t.monotonic()
    got, missed, exact_reads, dupes = {}, 0, 0, 0
    for i in coverage_order(len(samples)):
        path, t = samples[i]
        left = deadline_s - (_t.monotonic() - t0)
        if left <= 0.2:
            break
        raw, at = _read_frame(path, t, aw, ah, False, min(SAMPLE_TIMEOUT_S, left), offset)
        # a keyframe stands in for its moment only inside its own slot (half the sample spacing, at most 2.5 s):
        # never pre-roll, never a frame that belongs to a neighbouring sample
        if raw is None or at is None or at < start_w - 0.05 or abs(at - t) > slot:
            left = deadline_s - (_t.monotonic() - t0)
            if left > 0.2:
                exact_reads += 1
                raw, at = _read_frame(path, t, aw, ah, True, min(SAMPLE_TIMEOUT_S, left), offset)
                if raw is not None and at is None:
                    at = t
        if raw is None:
            missed += 1
            continue
        if any(abs(at - g[0]) < 0.05 for g in got.values()):
            # the same frame again (an MPEG-TS seek lands on the next keyframe; sparse keyframes): one frame is one
            # piece of evidence, never two
            dupes += 1
            missed += 1
            continue
        got[i] = (at, np.frombuffer(raw, np.uint8).reshape(ah, aw, 3))
    keys = sorted(got, key=lambda k: got[k][0])
    F = np.stack([got[k][1] for k in keys]) if keys else np.zeros((0, ah, aw, 3), np.uint8)
    info = {"requested": len(samples), "read": len(keys), "missed": missed, "exact_reads": exact_reads,
            "duplicates": dupes,
            "seconds": round(_t.monotonic() - t0, 2),
            "deadline_hit": len(keys) + missed < len(samples)}
    return F, [got[k][0] for k in keys], info


def coverage_problem(times: list, start: float, duration: float) -> Optional[str]:
    """Whole-window evidence needs >= MIN_FRAMES frames and at least one in each third of the window."""
    if len(times) < MIN_FRAMES:
        return f"only {len(times)} of the window's frames could be read in time (need {MIN_FRAMES})"
    third = max(1e-6, duration / 3)
    have = {min(2, max(0, int((t - start) // third))) for t in times}
    names = {0: "first", 1: "middle", 2: "last"}
    if len(have) < 3:
        return "no frame from the " + " and ".join(names[k] for k in range(3) if k not in have) + \
            " third of the clip window could be read in time"
    return None


def frames(samples: list, w: int, h: int, aw: int = ANALYSIS_W):
    """samples: [(path, seconds)] -> numpy array (n, ah, aw, 3) uint8, sorted by time (missing frames skipped)."""
    return frames_timed(samples, w, h, aw)[0]


# ---------------------------------------------------------------- analysis
def _runs(mask) -> tuple:
    """Longest True run per column of a 2-D boolean array -> (start, length) arrays."""
    import numpy as np
    n, m = mask.shape
    best_len = np.zeros(m, int)
    best_start = np.zeros(m, int)
    cur = np.zeros(m, int)
    for i in range(n):
        cur = np.where(mask[i], cur + 1, 0)
        better = cur > best_len
        best_len = np.where(better, cur, best_len)
        best_start = np.where(better, i - cur + 1, best_start)
    return best_start, best_len


def _lines(P, min_len: int, k: int = 12) -> list:
    """Persistent straight lines: (position, start, end, strength) along axis 0 of P (rows run along axis 0)."""
    import numpy as np
    thr = max(10.0, 3.0 * float(np.median(P)))
    start, length = _runs(P > thr)
    cand = []
    for x in range(P.shape[1]):
        if length[x] >= min_len:
            seg = P[start[x]:start[x] + length[x], x]
            cand.append((x, int(start[x]), int(start[x] + length[x]), float(length[x]) * float(seg.mean())))
    cand.sort(key=lambda c: -c[3])
    kept = []
    for c in cand:                                   # non-maximum suppression: one line per 3 px
        if all(abs(c[0] - q[0]) > 2 for q in kept):
            kept.append(c)
        if len(kept) >= k:
            break
    return kept


def _skin(rgb):
    """Boolean skin-tone mask (YCbCr box, Chai & Ngan)."""
    r, g, b = (rgb[..., i].astype("float32") for i in range(3))
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    y = 0.299 * r + 0.587 * g + 0.114 * b
    return (cb >= 77) & (cb <= 127) & (cr >= 133) & (cr <= 173) & (y > 40)


def _largest_blob(mask) -> int:
    """Size of the largest 4-connected True region (small boxes only: analysis resolution)."""
    import numpy as np
    h, w = mask.shape
    seen = np.zeros_like(mask, bool)
    best = 0
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if seen[y0, x0]:
            continue
        seen[y0, x0] = True
        stack, n = [(y0, x0)], 0
        while stack:
            y, x = stack.pop()
            n += 1
            for yy, xx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True
                    stack.append((yy, xx))
        best = max(best, n)
    return best


def _compact_skin(skin_box) -> bool:
    """Most frames show one compact face-sized skin region in the box."""
    n, h, w = skin_box.shape
    good = 0
    for m in skin_box:
        total = int(m.sum())
        if not total:
            continue
        big = _largest_blob(m)
        good += big >= 0.4 * total and big >= 0.03 * h * w
    return good >= 0.6 * n


def _find(F) -> Optional[dict]:
    """Best webcam box in frames F (n, h, w, 3), in analysis pixels, or None."""
    import numpy as np
    n, h, w, _ = F.shape
    if n < 2:
        return None
    L = F.astype("float32") @ np.array([0.299, 0.587, 0.114], "float32")
    gv = np.abs(L[:, :, 1:] - L[:, :, :-1])          # vertical edges between columns x and x+1
    gh = np.abs(L[:, 1:, :] - L[:, :-1, :])          # horizontal edges between rows y and y+1
    q = 20 if n >= 5 else 0
    Pv = np.percentile(gv, q, axis=0)                # (h, w-1): present in (nearly) every frame
    Ph = np.percentile(gh, q, axis=0)                # (h-1, w)
    vl = _lines(Pv, int(0.10 * h))                   # columns
    hl = _lines(Ph.T, int(0.10 * w))                 # rows (transpose: run along x)
    vmask = Pv > max(10.0, 3.0 * float(np.median(Pv)))
    hmask = Ph > max(10.0, 3.0 * float(np.median(Ph)))
    xs = sorted({0, w} | {c[0] + 1 for c in vl})
    ys = sorted({0, h} | {c[0] + 1 for c in hl})
    skin = _skin(F)
    skin_all = skin.mean()
    motion = np.abs(np.diff(L, axis=0)).mean(axis=0)  # (h, w)
    grad = np.abs(np.diff(L, axis=2))[:, :-1, :] + np.abs(np.diff(L, axis=1))[:, :, :-1]
    flat_map = (grad < 1.0).mean(axis=0)              # (h-1, w-1): flat in the sampled frames
    cands = []

    def cover_v(x, y0, y1):                          # share of rows y0..y1 with a persistent edge at column x-1
        if x <= 0 or x >= w:
            return 1.0, False
        col = vmask[y0:y1, x - 1]
        return (float(col.mean()) if col.size else 0.0), True

    def cover_h(y, x0, x1):
        if y <= 0 or y >= h:
            return 1.0, False
        row = hmask[y - 1, x0:x1]
        return (float(row.mean()) if row.size else 0.0), True
    for xa in xs:
        for xb in xs:
            bw = xb - xa
            if not 0.10 * w <= bw <= 0.45 * w:
                continue
            for ya in ys:
                for yb in ys:
                    bh = yb - ya
                    if not 0.12 * h <= bh <= 0.50 * h:
                        continue
                    ar = (bw / w * 16) / (bh / h * 9)    # in source-pixel terms for a 16:9 frame
                    if not 0.7 <= ar <= 2.4:
                        continue
                    sides = [cover_v(xa, ya, yb), cover_v(xb, ya, yb), cover_h(ya, xa, xb), cover_h(yb, xa, xb)]
                    real = [c for c, r in sides if r]
                    if len(real) < 2 or min(real) < 0.8:
                        continue
                    inside = skin[:, ya:yb, xa:xb].mean()
                    outside = (skin_all * w * h - inside * bw * bh) / max(1, w * h - bw * bh)
                    mov = float(motion[ya:yb, xa:xb].mean())
                    std = float(L[:, ya:yb, xa:xb].std())
                    if inside < 0.05 or inside < 1.8 * outside + 0.01 or mov < 0.8 or std < 8:
                        continue
                    flat = float(flat_map[ya + 1:yb - 2, xa + 1:xb - 2].mean())
                    if flat > MAX_FLAT:                  # cartoon / game portrait / UI art: never a streamer
                        continue
                    conf = (0.45 * (sum(real) / len(real)) + 0.35 * min(1.0, inside / 0.15)
                            + 0.20 * min(1.0, (inside - outside) / 0.10))
                    cands.append({"box": (xa, ya, xb, yb), "conf": round(conf, 3), "area": bw * bh,
                                  "flat": round(flat, 3), "skin_in": round(float(inside), 3),
                                  "skin_out": round(float(outside), 3)})
    cands.sort(key=lambda c: (-c["conf"], c["area"]))
    for c in cands[:12]:                             # the structural face check only for the strongest boxes
        xa, ya, xb, yb = c["box"]
        if _compact_skin(skin[:, ya:yb, xa:xb]):
            return c
    return None


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def analyze(F, src_w: int, src_h: int) -> dict:
    """{"facecam": [x, y, w, h] normalised or None, "confidence", "reason", ...}. Pure function of the frames."""
    base = {"version": VERSION, "frames": int(len(F)), "source": [src_w, src_h]}
    if src_w <= src_h * 1.2:
        return {**base, "facecam": None, "confidence": 0.0,
                "reason": "portrait or already stacked source: the whole frame is kept"}
    if len(F) < 4:
        return {**base, "facecam": None, "confidence": 0.0, "reason": "too few frames to judge: whole frame kept"}
    whole = _find(F)
    if whole is None or whole["conf"] < MIN_CONF:
        return {**base, "facecam": None, "confidence": whole["conf"] if whole else 0.0,
                "reason": "no reliable webcam inset found: the whole frame is kept"}
    half = len(F) // 2
    a, b = _find(F[:half]), _find(F[half:])
    if not a or not b or _iou(a["box"], whole["box"]) < 0.8 or _iou(b["box"], whole["box"]) < 0.8:
        return {**base, "facecam": None, "confidence": whole["conf"],
                "reason": "the webcam inset moves, appears or disappears within the clip: the whole frame is kept"}
    xa, ya, xb, yb = whole["box"]
    fh, fw = F.shape[1], F.shape[2]
    rect = [round(xa / fw, 4), round(ya / fh, 4), round((xb - xa) / fw, 4), round((yb - ya) / fh, 4)]
    return {**base, "facecam": rect, "confidence": whole["conf"], "skin_in": whole["skin_in"],
            "skin_out": whole["skin_out"], "reason": "webcam inset found in every sampled frame"}


def _shrink(F, aw: int):
    """Point-sample frames down to width aw for the strict 256-px detector. Point sampling (not averaging) keeps flat
    artwork perfectly flat, so the cartoon/UI-art guard sees what it was tuned on."""
    import numpy as np
    n, h, w, c = F.shape
    if w <= aw:
        return F
    ah = max(2, int(round(aw * h / w / 2)) * 2)
    xs = ((np.arange(aw) + 0.5) * w / aw).astype(int)
    ys = ((np.arange(ah) + 0.5) * h / ah).astype(int)
    return F[:, ys][:, :, xs]


def detect(samples: list, info: Optional[dict], rect: Optional[list] = None) -> dict:
    """The layout decision for these samples. Never raises: any unexpected failure keeps the whole frame (with the
    reason), so a render is never lost to the layout check."""
    try:
        return _detect(samples, info, rect)
    except Exception as e:                                   # noqa: BLE001 - fail safe to the whole frame
        log.warning("layout check failed, keeping the whole frame: %s", e)
        return {"version": VERSION, "facecam": None, "confidence": 0.0, "frames": 0,
                "source": [info["w"], info["h"]] if info else None,
                "reason": f"the layout check failed ({type(e).__name__}): the whole frame is kept"}


def _detect(samples: list, info: Optional[dict], rect: Optional[list] = None) -> dict:
    """Pull the frames once (640 px wide) and decide.
    rect given (a calibrated webcam area): validate that area in THIS window (faces.validate).
    No rect: the strict generic detector, then a face-anchored proposal that must also validate."""
    from . import faces
    base = {"version": VERSION, "facecam": None, "confidence": 0.0, "frames": 0}
    if not info:
        return {**base, "reason": "the source could not be probed: the whole frame is kept"}
    w, h = info["w"], info["h"]
    base["source"] = [w, h]
    if w <= h * 1.2:
        return {**base, "reason": "portrait or already stacked source: the whole frame is kept"}
    start = min(t for _, t in samples) - 0.15 if samples else 0.0
    end = max(t for _, t in samples) + 0.25 if samples else 0.0
    F, times, rd = frames_timed(samples, w, h, aw=640, window=(start, end), offset=float(info.get("start") or 0))
    base.update(frames=int(len(F)), probe=rd)
    if why := coverage_problem(times, start, max(0.5, end - start)):
        return {**base, "reason": f"{why}: the whole frame is kept"}
    if rect:
        v = faces.validate(F, rect, times=times)
        return {**base, "method": "calibrated area", "signals": v["signals"], "reason": v["reason"],
                "facecam": list(rect) if v["ok"] else None, "confidence": 1.0 if v["ok"] else 0.0}
    strict = analyze(_shrink(F, ANALYSIS_W), w, h)
    if strict.get("facecam"):
        return {**strict, "method": "persistent border + skin"}
    if faces.available():
        fl = faces.faces_per_frame(F)
        p = faces.propose(F, fl)
        if p:
            v = faces.validate(F, p["rect"], fl, times=times)
            if v["ok"]:
                return {**base, "method": "face-anchored proposal", "facecam": p["rect"], "face": p["face"],
                        "confidence": round(min(1.0, p["frames"] / len(F)), 3), "signals": v["signals"],
                        "reason": "a stable face at webcam-inset size; area snapped to its edges and validated"}
            return {**base, "method": "face-anchored proposal", "proposal": p["rect"], "signals": v["signals"],
                    "reason": f"proposal rejected: {v['reason']}"}
        return {**strict, "method": "persistent border + skin, then faces",
                "reason": "no stable inset-sized face and no reliable bordered inset: the whole frame is kept"}
    return {**strict, "method": "persistent border + skin (no face model)"}


async def detect_async(samples: list, info: Optional[dict], rect: Optional[list] = None) -> dict:
    return await asyncio.to_thread(detect, samples, info, rect)


def window_samples(path, start: float, duration: float, n: int = MAX_FRAMES) -> list:
    """n moments spread over [start, start + duration] of one file (first, middle and last included)."""
    duration = max(0.5, float(duration))
    return [(path, start + 0.15 + (duration - 0.4) * i / max(1, n - 1)) for i in range(n)]


def segment_samples(segments: list) -> list:
    """Live buffer: two moments in each of the first, a middle and the last segment of the clip window."""
    if not segments:
        return []
    picks = [segments[0], segments[len(segments) // 2], segments[-1]]
    out = []
    for s in picks:
        d = max(0.4, float(s.t_end) - float(s.t_start))
        out += [(s.path, 0.1), (s.path, d * 0.6)]
    return out
