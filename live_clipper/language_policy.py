"""Per-channel subtitle language policy (pure functions, shared by rendering, auto-post rules and upload
claims).

Channel setting `subtitle_language`: "auto" or one of the selection format's languages (built-in: "en" |
"es").

decide(asr, pref) -> {"status": "ok"|"hold", "lang": <code>|None, "reason": str, "detected": ..., "pref": ...}
  ok    the clip has coherent speech in one confident language that matches the preference ("auto" accepts
  every
        language of the format): only the original-language final is rendered by default and only that
        language
        may be auto-posted.
  hold  no speech, low confidence, mixed languages, or a language that does not match the channel preference:
        the clip stays a playable draft for review; nothing is auto-posted.

Language detection says which language is heard, NOT who is speaking. The accepted languages, their display
names
and the window confidence come from the selection format (format.language); the constants below are the
built-in
format's values.
"""
from __future__ import annotations

from . import formats

PREFS = ("auto", "en", "es")
NAMES = {"en": "English", "es": "Spanish"}
MIN_WINDOW_PROB = 0.5


def _lang(fmt):
    return (fmt or formats.active()).language


def channel_pref(settings: dict | None, fmt: formats.SelectionFormat | None = None) -> str:
    p = (settings or {}).get("subtitle_language") or "auto"
    return p if p in _lang(fmt).prefs else "auto"


def original_language_check(global_lang: str | None, windows: list[dict] | None,
                            fmt: formats.SelectionFormat | None = None) -> dict:
    """Positive evidence that the clip's speech is coherently in ONE language, the one detected globally.
    windows: per-~10 s detections [{"start","end","lang","prob"}] over windows with enough voiced speech.
    pass only if: at least one usable window, no window detection failed, at least one window is confident
    and every confident window agrees with the global language. Low-confidence windows are not evidence."""
    min_prob = _lang(fmt).min_window_prob
    wins = windows or []
    if not wins:
        return {"status": "fail", "reason": "no usable language windows (too little speech per 10 s window)"}
    if any(not w.get("lang") for w in wins):
        return {"status": "fail", "reason": "language detection failed for at least one speech window"}
    conf = [w for w in wins if (w.get("prob") or 0) >= min_prob]
    if not conf:
        return {"status": "fail", "reason": "no speech window has a confident language"}
    other = [w for w in conf if w["lang"] != global_lang]
    if other:
        desc = ", ".join(f"{w['lang']} {w['start']:.0f}-{w['end']:.0f}s ({w['prob']:.2f})" for w in other)
        return {"status": "fail", "reason": f"window languages contradict the overall {global_lang}: {desc}"}
    return {"status": "pass",
            "reason": f"{len(conf)} of {len(wins)} speech windows confidently {global_lang}"}


def decide(asr: dict | None, pref: str = "auto", fmt: formats.SelectionFormat | None = None) -> dict:
    """Autonomous decision. "ok" requires coherent speech AND a persisted passing original-language check AND
    a
    matching channel setting. Anything else is review-only."""
    lp = _lang(fmt)
    names = lp.names
    pref = pref if pref in lp.prefs else "auto"
    asr = asr or {}
    st, lang = asr.get("speech_status"), asr.get("language")
    out = {"pref": pref, "detected": lang, "speech_status": st, "lang": None, "status": "hold"}
    if not st:
        return {**out, "reason": "speech not analysed yet"}
    if st == "no_speech":
        return {**out, "reason": "no coherent speech detected (music, game audio or silence)"}
    if st != "speech" or lang not in names:
        return {**out, "reason": f"low-confidence or mixed speech: {asr.get('speech_note') or 'uncertain'}"}
    chk = asr.get("original_check") or {}
    if chk.get("status") != "pass":
        why = chk.get("reason") or ("original-language check still running" if asr.get("check_pending")
                                    else "no original-language evidence (analysed before this check existed)")
        return {**out, "reason": f"original language not confirmed: {why}"}
    if pref != "auto" and lang != pref:
        return {**out, "reason": f"channel subtitles are set to {names[pref]} but the audio is {names[lang]} "
                                 f"(could be game dialogue, radio or a guest; not verified)"}
    return {**out, "status": "ok", "lang": lang,
            "reason": f"original language {names[lang]}"
            + (" (auto)" if pref == "auto" else " (channel setting)")}


def auto_ineligible(asr: dict | None, pref: str, lang: str,
                    fmt: formats.SelectionFormat | None = None) -> str | None:
    """Why `lang` may NOT be posted automatically for this clip (None = eligible)."""
    names = _lang(fmt).names
    d = decide(asr, pref, fmt)
    if d["status"] != "ok":
        return f"held for review: {d['reason']}"
    if lang != d["lang"]:
        return (f"{names.get(lang, lang)} is a translation of this {names[d['lang']]} clip; "
                "translations are manual-only")
    return None
