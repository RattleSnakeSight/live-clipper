"""Face evidence for the split layout: YuNet (OpenCV Zoo, MIT licence, live_clipper/models) run with onnxruntime, which is
already installed with faster-whisper. No skin or colour rules: lighting, skin tone and colour grading do not decide.

validate(frames, rect)  is a known webcam area (channel calibration or a proposal) really showing the streamer in
                        THIS clip window? A real face must be inside the area in most samples and in both halves of
                        the window; a face found only outside it means the camera moved; no face means it is gone or
                        the evidence is too weak. Border persistence on the area's inner sides and appearance
                        stability inside it are reported as supporting signals (and decide only without the model).
propose(frames)         a face-anchored webcam area when no area is configured: one face in nearly every sample, at a
                        stable position away from the screen centre and at webcam-inset size; the area is snapped to
                        persistent edges around it (or to the nearest frame edge) and must then pass validate().
Everything here is read-only and works on frames already pulled by live_clipper.facecam (bounded, inside the media slot).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

MODEL = Path(os.environ.get("LIVE_CLIPPER_FACE_MODEL") or Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx")
INPUT = 640
MIN_SCORE = 0.7
_sess = None
_lock = threading.Lock()


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return MODEL.is_file()


def _session():
    global _sess
    with _lock:
        if _sess is None:
            import onnxruntime as ort
            o = ort.SessionOptions()
            o.intra_op_num_threads, o.inter_op_num_threads = 1, 1     # bounded: never competes with encodes
            _sess = ort.InferenceSession(str(MODEL), o, providers=["CPUExecutionProvider"])
        return _sess


def detect_faces(rgb, min_score: float = MIN_SCORE) -> list:
    """Faces in one RGB frame (h, w <= 640): [{"x", "y", "w", "h", "score"}] normalised to the frame."""
    import numpy as np
    h, w = rgb.shape[:2]
    if max(h, w) > INPUT:
        step = int(np.ceil(max(h, w) / INPUT))
        rgb = rgb[::step, ::step]
        h, w = rgb.shape[:2]
    canvas = np.zeros((INPUT, INPUT, 3), np.float32)
    canvas[:h, :w] = rgb[..., ::-1]                                # BGR, 0-255, top-left aligned (no rescale)
    out = dict(zip([o.name for o in _session().get_outputs()],
                   _session().run(None, {"input": canvas.transpose(2, 0, 1)[None]})))
    boxes = []
    for s in (8, 16, 32):
        cols = INPUT // s
        cls = np.clip(out[f"cls_{s}"][0, :, 0], 0, 1)
        obj = np.clip(out[f"obj_{s}"][0, :, 0], 0, 1)
        score = np.sqrt(cls * obj)
        idx = np.nonzero(score >= min_score)[0]
        for i in idx.tolist():
            r, c = divmod(i, cols)
            dx, dy, dw, dh = out[f"bbox_{s}"][0, i]
            cx, cy = (c + dx) * s, (r + dy) * s
            bw, bh = float(np.exp(dw)) * s, float(np.exp(dh)) * s
            boxes.append([cx - bw / 2, cy - bh / 2, bw, bh, float(score[i])])
    boxes.sort(key=lambda b: -b[4])
    keep = []
    for b in boxes:                                                  # NMS, IoU 0.3
        if all(_iou(b, k) < 0.3 for k in keep):
            keep.append(b)
    return [{"x": max(0.0, b[0] / w), "y": max(0.0, b[1] / h), "w": b[2] / w, "h": b[3] / h,
             "score": round(b[4], 3)} for b in keep if b[0] + b[2] / 2 < w and b[1] + b[3] / 2 < h]


def _iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------- supporting signals (no colour)
def _lum(F):
    import numpy as np
    return F.astype("float32") @ np.array([0.299, 0.587, 0.114], "float32")


def side_evidence(L, rect) -> dict:
    """Per inner side of the area: share of frames where a vertical/horizontal discontinuity sits on that side
    (within +-2 px), compared with the columns/rows next to it. Frame borders are not sides."""
    import numpy as np
    n, h, w = L.shape
    x, y, rw, rh = rect
    x0, x1, y0, y1 = int(x * w), int((x + rw) * w), int(y * h), int((y + rh) * h)
    out = {}

    def col_strength(fr, col, a, b):
        if col <= 1 or col >= w - 2:
            return 0.0
        return float(np.abs(fr[a:b, col + 1] - fr[a:b, col - 1]).mean())

    def row_strength(fr, row, a, b):
        if row <= 1 or row >= h - 2:
            return 0.0
        return float(np.abs(fr[row + 1, a:b] - fr[row - 1, a:b]).mean())
    for name, pos, vertical in (("left", x0, True), ("right", x1, True), ("top", y0, False), ("bottom", y1, False)):
        lim = w if vertical else h
        if pos <= 2 or pos >= lim - 3:
            continue
        hits = 0
        for fr in L:
            if vertical:
                at = max(col_strength(fr, c, y0, y1) for c in range(pos - 2, pos + 3))
                ref = np.median([col_strength(fr, c, y0, y1) for c in (pos - 8, pos - 6, pos + 6, pos + 8)])
            else:
                at = max(row_strength(fr, r, x0, x1) for r in range(pos - 2, pos + 3))
                ref = np.median([row_strength(fr, r, x0, x1) for r in (pos - 8, pos - 6, pos + 6, pos + 8)])
            hits += at >= max(4.0, 1.8 * ref)
        out[name] = round(hits / n, 3)
    return out


def stability(L, rect) -> float:
    """Share of frames whose area thumbnail correlates (>= 0.6) with the median thumbnail: a static room behind a
    moving person correlates; gameplay or a changed scene in the area does not."""
    import numpy as np
    n, h, w = L.shape
    x, y, rw, rh = rect
    a = L[:, int(y * h):int((y + rh) * h), int(x * w):int((x + rw) * w)]
    if a.shape[1] < 12 or a.shape[2] < 16:
        return 0.0                                                   # too small to compare: never "stable"
    sh, sw = a.shape[1] // 12, a.shape[2] // 16
    t = a[:, :sh * 12, :sw * 16].reshape(n, 12, sh, 16, sw).mean(axis=(2, 4)).reshape(n, -1)
    med = np.median(t, axis=0)
    good = 0
    for v in t:
        if v.std() < 1e-3 or med.std() < 1e-3:
            continue
        good += float(np.corrcoef(v, med)[0, 1]) >= 0.6
    return round(good / n, 3)


def liveness(L, rect) -> float:
    """Share of consecutive frame pairs whose area changed (mean abs luma difference >= 1.5): live video always has
    some motion or sensor noise; a frozen still (a "be right back" card, a static panel) has none."""
    import numpy as np
    n, h, w = L.shape
    x, y, rw, rh = rect
    a = L[:, int(y * h):int((y + rh) * h), int(x * w):int((x + rw) * w)]
    if n < 2 or a.size == 0:
        return 0.0
    d = np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))
    return round(float((d >= 1.5).mean()), 3)


MIN_ROI_FRAC = 0.05                    # a webcam area narrower/shorter than 5 % of the frame is not judged
MIN_ROI_PX = 24                        # ... nor one under 24 px on a side in the analysed frames


def roi_problem(shape, rect) -> Optional[str]:
    """Why an area cannot be judged in frames of this (n, h, w[, c]) shape, or None."""
    try:
        x, y, rw, rh = (float(v) for v in rect)
    except (TypeError, ValueError):
        return "the webcam area is not four numbers"
    h, w = shape[1], shape[2]
    if not (0 <= x < 1 and 0 <= y < 1 and rw > 0 and rh > 0 and x + rw <= 1.0001 and y + rh <= 1.0001):
        return "the webcam area is not inside the frame"
    if rw < MIN_ROI_FRAC or rh < MIN_ROI_FRAC or int(rw * w) < MIN_ROI_PX or int(rh * h) < MIN_ROI_PX:
        return (f"the webcam area is too small to judge ({rw:.3f} x {rh:.3f} of the frame; at least "
                f"{MIN_ROI_FRAC:.2f} and {MIN_ROI_PX} px per side)")
    return None


# ---------------------------------------------------------------- validation and proposals
def faces_per_frame(F) -> list:
    return [detect_faces(f) for f in F]


def validate(F, rect, faces: Optional[list] = None, times: Optional[list] = None) -> dict:
    """{"ok", "reason", "signals"} for a webcam area in these frames."""
    n = len(F)
    if why := roi_problem(F.shape, rect):
        return {"ok": False, "reason": f"{why}: the whole frame is kept", "signals": {"frames": n, "roi": why}}
    L = _lum(F)
    sig = {"frames": n, "sides": side_evidence(L, rect), "stability": stability(L, rect),
           "live": liveness(L, rect), "model": available()}
    x, y, rw, rh = rect
    strong_sides = sum(v >= 0.75 for v in sig["sides"].values())
    # without a face: borders and appearance hold over the whole window AND the area is live video, not a still
    frame_ok = strong_sides >= 2 and sig["stability"] >= 0.75 and sig["live"] >= 0.6
    if sig["model"]:
        faces = faces if faces is not None else faces_per_frame(F)
        inside, outside = [], 0
        for fl in faces:
            hit = False
            for f in fl:
                cx, cy = f["x"] + f["w"] / 2, f["y"] + f["h"] / 2
                if x - 0.01 <= cx <= x + rw + 0.01 and y - 0.01 <= cy <= y + rh + 0.01 and \
                        0.06 * rh <= f["h"] <= 0.9 * rh:
                    hit = True
                elif f["score"] >= 0.85 and f["h"] < 0.35:
                    outside += 1
            inside.append(hit)
        half = n // 2
        if times and len(times) == n:                     # halves by source time, not by index
            mid = (min(times) + max(times)) / 2
            order = sorted(range(n), key=lambda i: times[i])
            inside = [inside[i] for i in order]
            half = sum(1 for t in times if t < mid) or n // 2
        sig.update(face_ratio=round(sum(inside) / max(1, n), 3), face_first_half=sum(inside[:half]),
                   face_second_half=sum(inside[half:]), faces_outside=outside)
        if sum(inside) >= 0.6 * n and sum(inside[:half]) >= max(1, half // 2) and \
                sum(inside[half:]) >= max(1, (n - half) // 2):
            return {"ok": True, "reason": "a face is inside the webcam area across the clip window", "signals": sig}
        if outside >= 0.5 * n:
            return {"ok": False, "reason": "a face is present but outside the webcam area (the camera moved?): the "
                                           "whole frame is kept", "signals": sig}
        if frame_ok:
            return {"ok": True, "reason": "the area's borders and appearance are stable and it is live video "
                                          "across the window (no face detected, e.g. turned away or dim)", "signals": sig}
        return {"ok": False, "reason": "no face inside the webcam area and its borders/appearance/motion do not "
                                       "hold across the clip window (camera gone, moved, frozen or weak evidence): the "
                                       "whole frame is kept", "signals": sig}
    if frame_ok:
        return {"ok": True, "reason": "no face model: the area's border and appearance are stable across the window",
                "signals": sig}
    return {"ok": False, "reason": "no face model and the area's border/appearance are not stable enough: the whole "
                                   "frame is kept", "signals": sig}


def propose(F, faces: Optional[list] = None) -> Optional[dict]:
    """A face-anchored webcam area, or None. Never colour based."""
    import numpy as np
    if not available():
        return None
    n = len(F)
    faces = faces if faces is not None else faces_per_frame(F)
    pts = [(i, f) for i, fl in enumerate(faces) for f in fl if f["score"] >= 0.75 and f["h"] <= 0.35]
    best = None
    for _, f in pts:                                                # the face position seen in most frames
        cx, cy = f["x"] + f["w"] / 2, f["y"] + f["h"] / 2
        members = {}
        for j, g in pts:
            gx, gy = g["x"] + g["w"] / 2, g["y"] + g["h"] / 2
            if abs(gx - cx) <= 0.04 and abs(gy - cy) <= 0.05 and j not in members:
                members[j] = g
        if best is None or len(members) > len(best):
            best = members
    if not best or len(best) < max(3, int(np.ceil(0.85 * n))):
        return None
    cxs = np.array([g["x"] + g["w"] / 2 for g in best.values()])
    cys = np.array([g["y"] + g["h"] / 2 for g in best.values()])
    fw = float(np.median([g["w"] for g in best.values()]))
    fh = float(np.median([g["h"] for g in best.values()]))
    cx, cy = float(np.median(cxs)), float(np.median(cys))
    if cxs.std() > 0.02 or cys.std() > 0.025:
        return None                                                 # moves around: a game character, not a camera
    if 0.3 < cx < 0.7 and 0.3 < cy < 0.7:
        return None                                                 # centre of the screen: not an inset
    L = _lum(F)
    _, h, w = L.shape
    rect = _snap(L, cx, cy, fw, fh, w, h)
    if rect is None:
        return None
    return {"rect": rect, "face": [round(cx, 4), round(cy, 4), round(fw, 4), round(fh, 4)], "frames": len(best)}


def _snap(L, cx, cy, fw, fh, w, h) -> Optional[list]:
    """The webcam area around a stable face: each side at the strongest persistent edge in a plausible range, else
    at the frame edge when close, else a head-and-shoulders proportion of the face."""
    import numpy as np
    n = len(L)  # noqa: F841

    def best_col(lo, hi, a, b):
        cand = []
        for c in range(max(2, int(lo * w)), min(w - 3, int(hi * w)) + 1):
            s = [float(np.abs(fr[a:b, c + 1] - fr[a:b, c - 1]).mean()) for fr in L]
            cand.append((float(np.median(s)), c))
        if not cand:
            return None, 0.0
        vals = np.array([v for v, _ in cand])
        v, c = max(cand)
        return c / w, (v / max(1e-3, float(np.median(vals))))

    def best_row(lo, hi, a, b):
        cand = []
        for r in range(max(2, int(lo * h)), min(h - 3, int(hi * h)) + 1):
            s = [float(np.abs(fr[r + 1, a:b] - fr[r - 1, a:b]).mean()) for fr in L]
            cand.append((float(np.median(s)), r))
        if not cand:
            return None, 0.0
        vals = np.array([v for v, _ in cand])
        v, r = max(cand)
        return r / h, (v / max(1e-3, float(np.median(vals))))
    a, b = int(max(0.0, cy - 1.5 * fh) * h), int(min(1.0, cy + 1.5 * fh) * h)
    left = 0.0 if cx - 1.8 * fw <= 0.04 else best_col(max(0.0, cx - 3.0 * fw), cx - 1.0 * fw, a, b)[0]
    right = 1.0 if cx + 1.8 * fw >= 0.96 else None
    if right is None:
        r, strength = best_col(cx + 1.0 * fw, min(1.0, cx + 3.0 * fw), a, b)
        right = r if strength >= 2.0 else min(1.0, cx + 1.6 * fw)
    if left is None:
        left = max(0.0, cx - 1.6 * fw)
    xa, xb = int(left * w), int(right * w)
    top = 0.0 if cy - 1.6 * fh <= 0.05 else None
    if top is None:
        t, strength = best_row(max(0.0, cy - 2.5 * fh), cy - 0.9 * fh, xa, xb)
        top = t if strength >= 2.0 else max(0.0, cy - 1.3 * fh)
    bottom = 1.0 if cy + 2.0 * fh >= 0.95 else None
    if bottom is None:
        bt, strength = best_row(cy + 0.9 * fh, min(1.0, cy + 3.0 * fh), xa, xb)
        bottom = bt if strength >= 2.0 else min(1.0, cy + 1.9 * fh)
    rw, rh = right - left, bottom - top
    if not (0.08 <= rw <= 0.5 and 0.1 <= rh <= 0.6):
        return None                                                 # not an inset (a quadrant, a full screen...)
    return [round(left, 4), round(top, 4), round(rw, 4), round(rh, 4)]
