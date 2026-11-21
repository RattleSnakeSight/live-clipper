"""Caption cues + SRT/VTT/ASS writers and parsers.

Cue = {"start": float, "end": float, "text": "line1\\nline2"}
"""
from __future__ import annotations

import re

from .translate import distribute_words

PUNCT_END = ".!?…"
PUNCT_SOFT = ",;:—-"
PAD = 0.12          # seconds a cue lingers after the last word (kept inside speech gap)
GAP_BREAK = 0.6     # pause between words that forces a cue break
MIN_GAP = 0.02


# words a cue should not end on (articles, prepositions, conjunctions) - en + es
NO_END = set("""a an the of to in on at for with and or but that this my your his her its our their by from
into as is are was be el la los las un una unos unas de del al en con por para y o pero que se mi tu su
sus lo le les me te a""".split())


def _split_point(cur: list[dict]) -> int:
    """Where to break an overfull cue: after punctuation, else not after a function word."""
    n = len(cur)
    lo = max(1, n // 2)
    for k in range(n, lo - 1, -1):
        if cur[k - 1]["word"][-1] in PUNCT_END + PUNCT_SOFT:
            return k
    for k in range(n, lo - 1, -1):
        if cur[k - 1]["word"].lower().strip(",.;:!?¡¿\"'") not in NO_END:
            return k
    return n


def _words(segments) -> list[dict]:
    out = []
    for s in segments:
        ws = s.get("words") or distribute_words(s["text"], s["start"], s["end"])
        for w in ws:
            t = w["word"].strip()
            if t:
                out.append({"start": float(w["start"]), "end": float(w["end"]), "word": t,
                            "seg_end": False})
        if out and ws:
            out[-1]["seg_end"] = True
    return out


def _balance(words: list[str], max_chars: int, max_lines: int) -> list[str]:
    """Split words into <= max_lines lines, each <= max_chars if possible, minimising imbalance."""
    text = " ".join(words)
    if len(text) <= max_chars or max_lines == 1 or len(words) == 1:
        return [text]
    best, best_cost = None, None
    # max_lines == 2 is the product case; for >2 do a greedy fill
    if max_lines == 2:
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            over = max(0, len(a) - max_chars) + max(0, len(b) - max_chars)
            cost = over * 100 + abs(len(a) - len(b))
            # prefer breaking after punctuation, and don't leave a 1-short-word orphan
            if words[i - 1][-1] in PUNCT_SOFT + PUNCT_END:
                cost -= 6
            if len(b) < 6 or len(a) < 6:
                cost += 8
            if best_cost is None or cost < best_cost:
                best, best_cost = [a, b], cost
        return best
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    return lines


def build_cues(segments, max_chars: int = 32, max_lines: int = 2, min_dur: float = 0.8,
               max_dur: float = 3.5) -> list[dict]:
    words = _words(segments)
    if not words:
        return []
    cap = max_chars * max_lines

    def fits(ws) -> bool:
        ls = _balance([x["word"] for x in ws], max_chars, max_lines)
        return len(ls) <= max_lines and all(len(l) <= max_chars for l in ls)
    groups: list[list[dict]] = []
    cur: list[dict] = []

    def flush():
        nonlocal cur
        if cur:
            groups.append(cur)
            cur = []

    for i, w in enumerate(words):
        if cur:
            dur = w["end"] - cur[0]["start"]
            if not fits(cur + [w]) or dur > max_dur:
                k = _split_point(cur)
                rest = cur[k:]
                cur = cur[:k]
                flush()
                cur = rest
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        chars = len(" ".join(x["word"] for x in cur))
        last = w["word"][-1]
        gap = (nxt["start"] - w["end"]) if nxt else 99
        if (last in PUNCT_END and chars >= 12) or gap >= GAP_BREAK or w["seg_end"] \
                or (last in PUNCT_SOFT and chars >= cap * 0.6):
            flush()
        elif nxt and not fits(cur + [nxt]):
            k = _split_point(cur)   # don't strand "the"/"de" at the end of a cue
            rest = cur[k:]
            cur = cur[:k]
            flush()
            cur = rest
    flush()

    # merge fragments that are too short in time with a neighbour when they fit
    merged: list[list[dict]] = []
    for g in groups:
        if merged:
            p = merged[-1]
            comb = len(" ".join(x["word"] for x in p + g))  # noqa: F841
            dur_p = p[-1]["end"] - p[0]["start"]
            gap = g[0]["start"] - p[-1]["end"]
            tail = len(g) <= 2 or len(" ".join(x["word"] for x in g)) < 12
            orphan = tail and p[-1]["word"][-1] not in PUNCT_END     # e.g. "...into the gas" + "station."
            if fits(p + g) and gap < GAP_BREAK and (
                    (dur_p < min_dur and g[-1]["end"] - p[0]["start"] <= max_dur)
                    or (orphan and g[-1]["end"] - p[0]["start"] <= max_dur + 1.5)):
                merged[-1] = p + g
                continue
        merged.append(g)

    cues = []
    for g in merged:
        lines = _balance([x["word"] for x in g], max_chars, max_lines)
        cues.append({"start": g[0]["start"], "end": g[-1]["end"] + PAD, "text": "\n".join(lines),
                     "_last_word_end": g[-1]["end"]})
    # timing: min duration, hard max, no overlap
    for i, c in enumerate(cues):
        nxt_start = cues[i + 1]["start"] if i + 1 < len(cues) else None
        limit = (nxt_start - MIN_GAP) if nxt_start is not None else float("inf")
        c["end"] = min(c["end"], limit)
        if c["end"] - c["start"] < min_dur:
            c["end"] = min(c["start"] + min_dur, limit)
        if c["end"] - c["start"] > max_dur:
            c["end"] = c["start"] + max_dur
        if c["end"] <= c["start"]:
            c["end"] = c["start"] + 0.2
    for c in cues:
        c.pop("_last_word_end", None)
        c["start"] = round(c["start"], 3)
        c["end"] = round(c["end"], 3)
    return cues


# ---------------------------------------------------------------- writers
def _ts(t: float, sep: str) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_ts(c['start'], ',')} --> {_ts(c['end'], ',')}\n{c['text']}\n")
    return "\n".join(out)


def to_vtt(cues) -> str:
    out = ["WEBVTT\n"]
    for c in cues:
        out.append(f"{_ts(c['start'], '.')} --> {_ts(c['end'], '.')}\n{c['text']}\n")
    return "\n".join(out)


def default_style(play_res=(720, 1280)) -> dict:
    """Phone-readable style, scaled to the output resolution (pixel-accurate)."""
    w, h = play_res
    return {
        "font": "Inter", "bold": True,
        "size": round(w * 0.068),            # 49px @720 wide: 32 chars fit the safe width
        "outline": max(2.0, round(w * 0.0075, 1)),   # thick black outline
        "shadow": max(1.0, round(w * 0.002, 1)),
        "margin_v": round(h * 0.27),         # above bottom platform UI (Shorts/TikTok ~20-25%)
        "margin_l": round(w * 0.06), "margin_r": round(w * 0.06),
        "alignment": 2,                      # bottom-centre
        "primary": "#FFFFFF", "outline_color": "#000000", "back_color": "#000000",
    }


def _ass_color(c: str, alpha: int = 0) -> str:
    c = c.lstrip("#")
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def to_ass(cues, style: dict | None = None, play_res=(720, 1280)) -> str:
    st = {**default_style(play_res), **(style or {})}
    w, h = play_res
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: None

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{st['font']},{st['size']},{_ass_color(st['primary'])},{_ass_color(st['primary'])},{_ass_color(st['outline_color'])},{_ass_color(st['back_color'], 0x80)},{-1 if st['bold'] else 0},0,0,0,100,100,0,0,1,{st['outline']},{st['shadow']},{st['alignment']},{st['margin_l']},{st['margin_r']},{st['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for c in cues:
        text = c["text"].replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{_ass_ts(c['start'])},{_ass_ts(c['end'])},Default,,0,0,0,,{text}")
    return head + "\n".join(lines) + "\n"


def _ass_ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ---------------------------------------------------------------- parsers
_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_ARROW = re.compile(_TS + r"\s*-->\s*" + _TS)


def _secs(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _parse(text: str) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        for i, ln in enumerate(lines):
            m = _ARROW.search(ln)
            if m:
                g = m.groups()
                body = "\n".join(lines[i + 1:]).strip()
                body = re.sub(r"<[^>]+>", "", body)
                cues.append({"start": round(_secs(*g[:4]), 3), "end": round(_secs(*g[4:]), 3),
                             "text": body})
                break
    return cues


def parse_srt(text: str) -> list[dict]:
    return _parse(text)


def parse_vtt(text: str) -> list[dict]:
    return _parse(text)
