"""Positive game + content evidence per clip, required before any UNATTENDED send to a real platform.

Two things must hold for THIS clip (never a channel's present title):
  1. the game is the selection format's game: capture-time title/category watch (trigger.topic.state), the
     platform's official archive of the same broadcast, VOD metadata, or the game named in the clip's own
     speech;
  2. the content is a worthwhile moment: the editorial check (live_clipper.editorial, trigger.editorial)
  qualified it
     as gameplay, a substantive comment or a hype/reaction moment with setup and payoff, for the clip's
     CURRENT cut
     and the current check version. A title, category, greeting or viewer-arrival surge alone never qualifies.
An operator review overrides both (trigger.review.gameplay: approved / rejected). Explicit off-topic
evidence, an
editorial rejection or an operator rejection blocks; anything else is "unknown" and is held. Every consumer
reads
this one function, so the dashboard, scheduler and publisher cannot disagree.
"""
from __future__ import annotations

import time

from . import formats


def context(clip: dict | None, fmt: formats.SelectionFormat | None = None) -> dict:
    """Is the game the format's game for this clip? {"state": gta|off_topic|unknown, "source", "detail"}."""
    fmt = fmt or formats.active()
    ts, lb = fmt.topic_states, fmt.labels
    t = (clip or {}).get("trigger") or {}
    topic = t.get("topic") or {}
    if topic.get("state") == ts.off_topic:
        return {"state": "off_topic", "source": "title watch",
                "detail": f"the live title at trigger time was not {lb.game_short} "
                          f"('{topic.get('title', '')}')"}
    if topic.get("state") == ts.title:
        return {"state": "gta", "source": "title watch",
                "detail": f"live {lb.game_title} title at trigger time ('{topic.get('title', '')}')"}
    if topic.get("state") == ts.category:
        return {"state": "gta", "source": "category watch",
                "detail": f"platform category at trigger time was '{topic.get('category', '')}' "
                          f"(title: '{topic.get('title', '')}')"}
    if topic.get("state") == ts.archive:
        return {"state": "gta", "source": "official archive",
                "detail": str(topic.get("basis") or "official archive of the same broadcast")[:400]}
    vk = fmt.evidence_keys.vod_metadata
    if (t.get("vod") or {}).get(vk):
        return {"state": "gta", "source": "VOD metadata", "detail": str(t["vod"][vk])[:200]}
    why = str(topic.get("why") or "")[:200]
    return {"state": "unknown", "source": "",
            "detail": f"no capture-time {lb.game_short} evidence" + (f" ({why})" if why else "")}


def evidence(clip: dict | None, fmt: formats.SelectionFormat | None = None) -> dict:
    from . import editorial
    fmt = fmt or formats.active()
    game = fmt.labels.game
    t = (clip or {}).get("trigger") or {}
    rv = ((t.get("review") or {}).get("gameplay") or {})
    if rv.get("state") == "rejected":
        return {"state": "rejected", "source": "operator",
                "detail": rv.get("note") or "rejected by the operator"}
    if rv.get("state") == "approved":
        return {"state": "approved", "source": "operator",
                "detail": rv.get("note") or "approved by the operator"}
    ctx = context(clip, fmt)
    if ctx["state"] == "off_topic":
        return {"state": "rejected", "source": ctx["source"], "detail": ctx["detail"]}
    ed = editorial.current(clip)
    if ed is None:
        stale = isinstance(t.get("editorial"), dict)
        if t.get("input") == "vod_import" and ctx["state"] != "gta" and not stale:
            return {"state": "unknown", "source": "",
                    "detail": "VOD import: awaiting the editorial content check "
                              "(or an operator gameplay review)"}
        return {"state": "unknown", "source": "editorial",
                "detail": ("the editorial check belongs to an older cut or version and is being redone"
                           if stale else
                           f"awaiting the editorial content check (gameplay / {game} talk / reaction)")
                          + (f"; game: {ctx['detail']}" if ctx["state"] == "gta" else f"; {ctx['detail']}")}
    at = f" [evidence at {ed['evidence_s']:.1f} s]" if ed.get("evidence_s") is not None else ""
    if ed["state"] == "rejected":
        return {"state": "rejected", "source": "editorial", "detail": ed["reason"] + at}
    if ed["state"] != "qualified":
        return {"state": "unknown", "source": "editorial", "detail": ed["reason"]}
    sig_ctx = (ed.get("signals") or {}).get("game_context")
    if ctx["state"] != "gta" and not sig_ctx:
        return {"state": "unknown", "source": "editorial",
                "detail": f"{ed['reason']}; but {game} is not identified "
                                                                       f"for this clip ({ctx['detail']})"}
    content = f"; content: {ed['reason']}{at}" if ed.get("reason") else ""
    if ctx["state"] == "gta":                 # the game evidence's own source, plus what the clip shows
        return {"state": "approved", "source": ctx["source"], "detail": ctx["detail"] + content}
    return {"state": "approved", "source": f"editorial ({ed.get('kind')})", "detail": (ed["reason"] + at)}


def hold_reason(clip: dict | None, fmt: formats.SelectionFormat | None = None) -> str | None:
    fmt = fmt or formats.active()
    e = evidence(clip, fmt)
    if e["state"] == "approved":
        return None
    if e["state"] == "rejected":
        return f"not a {fmt.labels.game_short} gameplay moment: {e['detail']}"
    return (f"awaiting gameplay approval: {e['detail']}; it is not sent unattended until it qualifies. "
            f"An operator "
            f"can review it under Streamer schedule > Gameplay review (or publish it manually)")


def review(store, clip_id: int, approved: bool, note: str = "") -> dict:
    store.merge_json("clips", clip_id, "trigger", {"review": {"gameplay": {
        "state": "approved" if approved else "rejected", "at": time.time(), "note": (note or "")[:200]}}})
    return evidence(store.clip(clip_id))
