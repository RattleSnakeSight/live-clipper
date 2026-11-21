"""Rule-based clip titles (no LLM): the selection format's action lexicon + objects + chat reaction.

make_title(transcript, chat_messages, trigger_reason, lang="en") -> str  (<= format.titles.max_len chars)
Evidence-driven; when evidence is thin it falls back to the most informative transcript phrase.

The lexicon (actions, vehicles, places, templates per language, context phrases, chat reactions, stop words)
is data
in the selection format (format.titles); the built-in GTA VI format reproduces the original titles exactly.
"""
from __future__ import annotations

import re
from collections import Counter

from . import formats


def _t(fmt):
    return (fmt or formats.active()).titles


def _text(transcript: dict) -> str:
    return " ".join(s.get("text", "") for s in transcript.get("segments", []))


def _find_actions(low: str, fmt=None) -> list[tuple[str, int, int]]:
    out = []
    for a in _t(fmt).actions:
        ms = list(formats.rx(a.regex).finditer(low))
        if ms:
            out.append((a.key, a.priority * 10 + len(ms) * 3 + (ms[-1].start() * 5) // max(1, len(low)),
                        ms[0].start()))
    out.sort(key=lambda x: -x[1])
    return out


def _find_vehicle(low: str, fmt=None) -> str | None:
    vehicles = _t(fmt).vehicles
    pos = -1
    cnt = Counter()
    for k in vehicles:
        for m in re.finditer(r"\b" + re.escape(k) + r"\b", low):
            cnt[k] += 1
            pos = max(pos, m.start())
    if not cnt:
        return None
    k = max(cnt, key=lambda x: (cnt[x], low.rfind(x)))
    return vehicles[k]


def _find_place(low: str, exclude_vehicle: str | None, fmt=None) -> tuple[str, ...] | None:
    tt = _t(fmt)
    first = tt.languages[0]
    best = None
    for idx, place in enumerate(tt.places):
        ms = list(formats.rx(place.regex).finditer(low))
        if not ms:
            continue
        if exclude_vehicle and any(exclude_vehicle.lower() in [v.lower() for v in ex.vehicles]
                                   and place.names.get(first) == ex.place for ex in tt.place_exclusions):
            continue
        score = (-idx,)  # earlier places are more specific
        if best is None or score > best[0]:
            best = (score, tuple(place.names.get(lang, "") for lang in tt.languages))
    return best[1] if best else None


def _stars(low: str, fmt=None) -> int | None:
    tt = _t(fmt)
    for m in formats.rx(tt.stars_regex).finditer(low):
        v = m.group(1)
        return int(v) if v.isdigit() else tt.numbers[v]
    m = formats.rx(tt.stars_fallback_regex).search(low)
    return int(m.group(1)) if m else None


def _reaction(chat: list[str], lang: str, fmt=None) -> str | None:
    tt = _t(fmt)
    if not chat:
        return None
    blob = "\n".join(chat)
    best = None
    for r in tt.reactions:
        n = sum(len(formats.rx(p).findall(blob)) for p in r.patterns)
        if n and (best is None or n > best[0]):
            best = (n, r.text.get(lang, ""))
    return best[1] if best and best[0] >= max(tt.reaction_min, len(chat) // tt.reaction_divisor) else None


def _best_phrase(transcript: dict, lang_hint: str, fmt=None) -> str | None:
    """Most informative transcript sentence: names, exclamations, action words, mid length."""
    tt = _t(fmt)
    stop = set(tt.stop_words)
    cands = []
    for seg in transcript.get("segments", []):
        for sent in re.split(r"(?<=[.!?¡¿])\s+", seg.get("text", "")):
            sent = sent.strip()
            if len(sent) < 12:
                continue
            words = re.findall(r"[\w'’@]+", sent)
            content = [w for w in words if w.lower() not in stop]
            caps = sum(1 for w in words[1:] if w[:1].isupper())
            act = sum(1 for a in tt.actions if formats.rx(a.regex).search(sent.lower()))
            score = len(content) + caps * 3 + act * 4 + sent.count("!") * 2 - abs(len(sent) - 40) / 20
            cands.append((score, sent))
    if not cands:
        return None
    return max(cands)[1]


def _clip(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[: n - 1].rsplit(" ", 1)[0].rstrip(",;:-— ")
    return cut + "…"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def make_title(transcript: dict, chat_messages: list[str], trigger_reason: str, lang: str = "en",
               fmt: formats.SelectionFormat | None = None) -> str:
    tt = _t(fmt)
    first = tt.languages[0]
    lang = lang if lang in tt.languages else first
    li = tt.languages.index(lang)
    text = _text(transcript)
    low = text.lower() + " " + (trigger_reason or "").lower().replace("_", " ")
    acts = _find_actions(low, fmt)
    veh = _find_vehicle(low, fmt)
    place = _find_place(low, veh, fmt)
    react = _reaction(chat_messages or [], lang, fmt)
    T = tt.templates[lang]
    ctxs = tt.context_text[lang]
    fb = tt.fallback[lang]

    # primary action: skip pure-context actions (e.g. police/chase) if a stronger one exists
    primary = acts[0][0] if acts else None
    ctx_only = set(tt.context_actions)
    if primary in ctx_only and any(a[0] not in ctx_only for a in acts):
        strong = [a for a in acts if a[0] not in ctx_only]
        primary = strong[0][0]
    ctx = None
    n_stars = _stars(low, fmt)
    keys = {a[0] for a in acts}
    for rule in tt.context_rules:
        if rule.stars:
            if n_stars:
                ctx = ctxs[rule.text].format(n=n_stars)
                break
        elif primary not in rule.unless_primary and rule.action in keys:
            ctx = ctxs[rule.text]
            break

    if primary is None:
        phrase = _best_phrase(transcript, lang, fmt)
        if phrase:
            base = "“" + _clip(phrase.strip(" .!¡¿?"), tt.phrase_max) + "”"
        elif veh or place:
            base = (f"{veh} " if veh else "") + (fb.at + place[li]) if place else veh
            base = _cap(base.strip())
        else:
            base = _cap(trigger_reason.replace("_", " ").strip()) if trigger_reason else fb.clip
            base = base + (fb.moment_suffix if trigger_reason else "")
        return _assemble(base, None, react, lang, fmt)

    tmpl = T[primary]
    # the original's precedence: the first language's object is read even without a place (raises like it did)
    obj = place[li] if lang == first else place[li] if place else None
    if veh and obj:
        base = tmpl[0].format(S=veh, O=obj)
    elif veh:
        base = tmpl[1].format(S=veh)
    elif obj:
        base = tmpl[2].format(O=obj)
    else:
        base = tmpl[3]
    return _assemble(_cap(base), ctx, react, lang, fmt)


def _assemble(base: str, ctx: str | None, react: str | None, lang: str, fmt=None) -> str:
    tt = _t(fmt)
    sep = tt.separator
    for parts in ((base, ctx, react), (base, ctx), (base, react), (base,)):
        pieces = [p for p in parts if p]
        if len(pieces) == 3:
            title = f"{pieces[0]} {pieces[1]}{sep}{pieces[2]}"
        elif len(pieces) == 2:
            title = f"{pieces[0]}{sep}{pieces[1]}" if pieces[1] == react else f"{pieces[0]} {pieces[1]}"
        else:
            title = pieces[0]
        if len(title) <= tt.max_len:
            return title
    return _clip(base, tt.max_len)
