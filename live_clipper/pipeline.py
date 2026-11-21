"""Selection decisions inside the live pipeline (the glue between the engine, edit and select).

`Selection` resolves the effective values for one moment (built-in format < the customer's format < rules in
order < the per-clip override, `formats.resolve_effective`) and applies them where the original engine and
publisher applied the matching constants:

- at the trigger: the cut window (`window.pre/post`), the editing preset (`output.preset_id`) and the detector;
- while post-processing: the title (`titles.make_title` + `output.title_template`), the editorial check and the
  original-language decision use the effective format;
- when a final is ready (`gate`): the auto-post threshold (`candidates.below_threshold` with `scoring.min_score`),
  the game/content evidence (`gameplay.evidence`, overridden by a rule `decision`) and the language eligibility
  (`language_policy.auto_ineligible`) decide qualified / held / rejected. Only a qualified clip is handed to
  publishing (`edit.clip_ready`); every decision is published as `select.moment`.

Facts a rule can test: context `platform, category, title, channels, simulcast_group, format_id`; clip
`kind, evidence, duration_s, language, override`. Evidence names: chat, topic, speech, gameplay, statement,
reaction. At the trigger only the context and the trigger type are known, so window and preset rules are
evaluated with those facts; the gate re-evaluates with everything known by then.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import formats

log = logging.getLogger(__name__)
PLATFORM_NAMES = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}
TITLE_MAX = 100


class _Safe(dict):
    def __missing__(self, key):
        return ""


def channel_key(ch: dict | None) -> str:
    """How a rule names a streamer: the platform login, or the display name of a creator-provided feed."""
    ch = ch or {}
    if ch.get("platform") in PLATFORM_NAMES:
        return str(ch.get("name") or "")
    return str(ch.get("display_name") or ch.get("name") or "")


def evidence(clip: dict | None, fmt: formats.SelectionFormat | None = None) -> list[str]:
    """Evidence names present on a clip (rule scenario `evidence`)."""
    from . import editorial

    fmt = fmt or formats.active()
    t = (clip or {}).get("trigger") or {}
    out = []
    if t.get("kind") == "chat_spike" or t.get("sample_messages"):
        out.append("chat")
    ts = fmt.topic_states
    if (t.get("topic") or {}).get("state") in (ts.title, ts.category, ts.archive):
        out.append("topic")
    ed = editorial.current(clip) or {}
    sig = ed.get("signals") or {}
    if (sig.get("speech") or {}).get(fmt.evidence_keys.mentions):
        out.append("speech")
    vis = sig.get("visual") or {}
    if vis.get("pairs", 0) >= fmt.editorial.min_pairs and vis.get("gameplay_ratio", 0) >= fmt.editorial.gameplay_ratio:
        out.append("gameplay")
    kind = {"opinion": "statement", "reaction": "reaction"}.get(ed.get("kind") or "")
    if kind and ed.get("state") == "qualified":
        out.append(kind)
    return out


class Selection:
    def __init__(self, db, store, bus=None):
        self.db, self.store, self.bus = db, store, bus
        try:
            formats.seed(db)
        except Exception:  # noqa: BLE001 - the select tables may be missing in a partial install
            log.exception("could not seed the built-in selection formats")

    # ---- effective values ---------------------------------------------------------------------------------
    def context(self, channel_id: int, topic: dict | None = None) -> tuple[str, dict]:
        from . import moments

        ch = self.store.channel(channel_id) or {}
        ids = moments.simulcast_channels(self.store, channel_id) if ch else [channel_id]
        topic = topic or {}
        ctx = {
            "platform": ch.get("platform") or "",
            "category": topic.get("category") or "",
            "title": topic.get("title") or "",
            "channels": [channel_key(self.store.channel(i)) for i in ids],
            "simulcast_group": (ch.get("settings") or {}).get("simulcast_group"),
            "format_id": ch.get("format_id") or None,
        }
        return channel_key(ch), ctx

    def resolve(self, channel_id: int, clip: dict | None = None, *, kind: str | None = None,
                topic: dict | None = None, language: str | None = None) -> dict:
        t = (clip or {}).get("trigger") or {}
        key, ctx = self.context(channel_id, topic if topic is not None else t.get("topic"))
        facts: dict[str, Any] = {"kind": kind or (clip or {}).get("kind") or t.get("kind")}
        if clip:
            files = clip.get("files") or {}
            facts.update(evidence=evidence(clip), language=language or clip.get("source_lang") or None,
                         override=t.get("override") or {})
            if files.get("duration") is not None:
                facts["duration_s"] = float(files["duration"])
        try:
            return formats.resolve_effective(self.db, key, ctx, facts)
        except formats.NotFound:  # the channel names a format that was deleted: the active one applies
            log.warning("channel %s names unknown format %r; using the active format", channel_id,
                        ctx.get("format_id"))
            return formats.resolve_effective(self.db, key, {**ctx, "format_id": None}, facts)

    def fmt(self, clip: dict | None) -> formats.SelectionFormat:
        if not clip:
            return formats.active()
        return formats.effective_format(self.resolve(clip["channel_id"], clip))

    def preset_id(self, values: dict, presets) -> str:
        from .presets import DEFAULT_ID

        pid = values.get("output.preset_id") or DEFAULT_ID
        if presets.get(pid) is None:
            log.warning("selection names unknown editing preset %r; using %r", pid, DEFAULT_ID)
            return DEFAULT_ID
        return pid

    # ---- titles -------------------------------------------------------------------------------------------
    def title(self, clip: dict, transcript: dict, msgs: list, reason: str, lang: str) -> tuple[str, str]:
        """(generated title, title after the effective `output.title_template`)."""
        from .titles import make_title

        res = self.resolve(clip["channel_id"], clip, language=lang)
        base = make_title(transcript, msgs, reason, lang=lang, fmt=formats.effective_format(res))
        return base, self.apply_template(res, clip, base)

    def apply_template(self, res: dict, clip: dict, title: str) -> str:
        tpl = res["values"].get("output.title_template")
        if not tpl:
            return title
        ch = self.store.channel(clip["channel_id"]) or {}
        eff = formats.effective_format(res)
        out = tpl.format_map(_Safe(title=title, channel=ch.get("display_name") or ch.get("name") or "",
                                   game=eff.labels.game, platform=PLATFORM_NAMES.get(ch.get("platform"), "")))
        return " ".join(out.split())[:TITLE_MAX]

    # ---- the decision when a final is ready ---------------------------------------------------------------
    def gate(self, clip_id: int, lang: str) -> dict:
        from . import candidates, gameplay, language_policy, moments

        c = self.store.clip(clip_id)
        res = self.resolve(c["channel_id"], c, language=lang)
        fmt, v = formats.effective_format(res), res["values"]
        reasons = []
        if below := candidates.below_threshold(c, v["scoring.min_score"]):
            reasons.append(below)
        ev = formats.apply_decision(gameplay.evidence(c, fmt), res)
        if ev["state"] == "rejected":
            reasons.append(f"not a {fmt.labels.game_short} gameplay moment: {ev['detail']}")
        elif ev["state"] != "approved":
            reasons.append(f"awaiting gameplay approval: {ev['detail']}")
        ch = self.store.channel(c["channel_id"]) or {}
        pref = language_policy.channel_pref(ch.get("settings"), fmt)
        asr = (c.get("captions") or {}).get("asr")
        if why := language_policy.auto_ineligible(asr, pref, lang, fmt):
            reasons.append(why)
        decision = "rejected" if ev["state"] == "rejected" else "held" if reasons else "qualified"
        self.record(c, decision, reasons, res, lang, moments.moment_id(self.store, clip_id))
        return {"decision": decision, "reasons": reasons, "result": res, "evidence": ev}

    def rejected(self, clip_id: int, verdict: dict) -> None:
        """The editorial check rejected the moment before its final (nothing is rendered or published)."""
        from . import moments

        c = self.store.clip(clip_id)
        if c:
            res = self.resolve(c["channel_id"], c)
            self.record(c, "rejected", [verdict.get("reason") or "editorial check rejected the moment"], res,
                        None, moments.moment_id(self.store, clip_id))

    def record(self, c: dict, decision: str, reasons: list, res: dict, lang: str | None, moment_id: str) -> None:
        entry = {"decision": decision, "reasons": reasons, "format_id": res["format_id"],
                 "matched_rules": [r["id"] for r in res["matched_rules"]], "at": round(time.time(), 3)}
        self.store.merge_json("clips", c["id"], "trigger", {"selection": {lang or "any": entry}})
        self.store.log(f"clip {c['id']}{f' [{lang}]' if lang else ''}: {decision}"
                       + (f" ({'; '.join(reasons)})" if reasons else ""), channel_id=c["channel_id"],
                       clip_id=c["id"])
        if self.bus is not None:
            self.bus.publish("select.moment", {
                "moment_id": moment_id, "channel_id": c["channel_id"], "decision": decision,
                "score": float((c.get("trigger") or {}).get("score") or 0), "reasons": reasons,
                "clip_id": c["id"], "language": lang, "format_id": res["format_id"]})

    def publish_fields(self, clip: dict, lang: str, res: dict | None = None) -> dict:
        """Description, hashtags, format and allowed destinations for `edit.clip_ready`."""
        res = res or self.resolve(clip["channel_id"], clip, language=lang)
        v, eff = res["values"], formats.effective_format(res)
        ch = self.store.channel(clip["channel_id"]) or {}
        title = ((clip.get("captions") or {}).get(lang) or {}).get("title") or clip.get("title") or ""
        desc = eff.posting.caption_template.format_map(_Safe(
            title=title, creator=ch.get("display_name") or ch.get("name") or "",
            platform=PLATFORM_NAMES.get(ch.get("platform"), ""), game=eff.labels.game))
        tags = [h for h in eff.posting.hashtags if h.lower() not in desc.lower()]
        dests = v.get("output.destinations")
        chan = ch.get("publish_platforms") or []
        if chan:
            dests = [d for d in (dests if dests is not None else chan) if d in chan]
        return {"description": " ".join(desc.split()), "hashtags": tags, "format_id": res["format_id"],
                "destinations": dests}
