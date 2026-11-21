"""Posting candidates: unsent finished clips, ranked for the posting queue (separate from deliveries in
progress).

Inputs are all persisted per clip, so the queue survives restarts and is recomputed the same way every time:
  reaction    the detector's chat-spike ratio for the clip's trigger: peak messages per 10 s divided by that
              channel's own 300 s baseline (floor 3/10 s), stored as trigger.score with peak_rate and
              unique_users. Ranking uses log2(1 + ratio) so an outlier spike cannot dominate for days. Clips
              without a chat measurement (manual "Clip now", audio or tab-share triggers) get the neutral
              value
              (selection format scoring.neutral_reaction, 1.0 = a ratio of 1, baseline level): they are
              neither
              boosted nor buried.
  freshness   0.5 ** (age / scoring.fresh_half_life_h), age measured from the moment's action time.
  priority    reaction x freshness. A strong fresh moment overtakes unsent older ones; an old high spike sinks
              steadily but stays a candidate until retention removes its media.
Editorial eligibility (any failure = held, with the reason; nothing is marked publishable implicitly):
  the original-language final exists; game evidence AND a qualified editorial content verdict for THIS clip
  (or
  an operator approval; live_clipper.gameplay, live_clipper.editorial); a native account also needs an enabled
  auto-post rule the clip passes (pub.rule_reason, the worker's own check); the persisted
  speech/original-language
  check allows unattended posting (pub.auto_reason); at least one destination of that language accepts it
  (channel routing / exclusive accounts, one delivery per moment per account).
Clips with any delivery that is not failed/cancelled are in the delivery queue, not candidates. Creator
rotation, the 2 h creator cooldown, rights/dedup and provider limits are applied by the scheduler
(moments.plan,
the claim) on top of this order; existing reservations are never moved.
"""
from __future__ import annotations

import math
import time

from . import formats

FRESH_HALF_LIFE_H = 12.0
NEUTRAL_REACTION = 1.0


def reaction(clip: dict, fmt=None) -> tuple[float, str]:
    fmt = fmt or formats.active()
    t = clip.get("trigger") or {}
    kind = t.get("kind") or clip.get("kind") or "manual"
    if kind == "chat_spike":
        r = max(0.0, float(t.get("score") or 0))
        users = t.get("unique_users")
        return math.log2(1 + r), (f"chat spike {r:.1f}x the channel baseline"
                                  + (f", {users} chatters" if users else ""))
    return fmt.scoring.neutral_reaction, f"no chat measurement ({kind} clip): neutral"


def freshness(clip: dict, now: float, fmt=None) -> tuple[float, float]:
    fmt = fmt or formats.active()
    at = clip.get("action_time") or clip.get("created_at") or now
    age_h = max(0.0, (now - float(at)) / 3600)
    return 0.5 ** (age_h / fmt.scoring.fresh_half_life_h), age_h


def priority(clip: dict, now: float, fmt=None) -> float:
    return reaction(clip, fmt)[0] * freshness(clip, now, fmt)[0]


def below_threshold(clip: dict, min_score: float) -> str | None:
    """The auto-post rule's trigger-score threshold, shared by the worker and the dashboard (None = passes).
    Live
    clips keep the rule exactly as before (a manual or audio clip without a chat ratio scores 0 and is not
    auto-posted). VOD scan candidates found from audio/speech/visual evidence have no chat ratio: the
    editorial
    content verdict alone decides those (a chat-replay VOD trigger keeps the threshold)."""
    t = (clip or {}).get("trigger") or {}
    if t.get("input") == "vod_import" and (t.get("kind") or "") != "chat_spike":
        return None                                # a VOD scan candidate: the editorial verdict decides
    score = float(t.get("score") or 0)
    if score >= float(min_score):
        return None
    return f"chat spike {score:.1f}x is below this auto-post rule's threshold ({float(min_score):g}x)"


def original_language(clip: dict) -> str:
    return (((clip.get("captions") or {}).get("policy") or {}).get("render_default")
            or clip.get("source_lang") or "")


def started_on(store, clip_id: int) -> list[str]:
    """Labels of the real destinations that already hold this clip (a delivery that is not failed/cancelled).
    The local sandbox is not a destination: a sandbox test never counts."""
    rows = store.q("SELECT t.label FROM deliveries d JOIN destinations t ON t.id=d.destination_id "
                   "WHERE d.clip_id=? AND d.status NOT IN ('failed','cancelled') AND t.provider<>'sandbox'",
                   clip_id)
    return sorted({r["label"] for r in rows})


def editorial(pub, clip: dict, dests: list | None = None, fmt=None) -> dict:
    """{"state": eligible|held_rule|needs_review|not_eligible, "reason", "destinations": [labels that accept
    it],
    "auto_destinations": [labels that will post it unattended]}. eligible = at least one account will actually
    send it (the worker's own rule check for native accounts, the plan for external ones); held_rule = it
    would be
    accepted but no rule sends it (e.g. below the rule's threshold): exactly what the worker does, never
    "eligible"."""
    from . import gameplay, moments
    fmt = fmt or formats.active()
    lang = original_language(clip)
    if not lang or not pub.video_path(clip, lang):
        return {"state": "not_eligible",
                "reason": pub.media_missing_reason(clip, lang) or "no final render yet", "destinations": []}
    from . import freshness as cutoff
    if why := cutoff.reason(pub.store, pub.settings, clip, pub.clock()):
        return {"state": "not_eligible", "reason": why, "destinations": []}
    g = gameplay.evidence(clip, fmt)
    if g["state"] == "rejected":
        return {"state": "not_eligible", "reason": f"not {fmt.labels.game_short} gameplay: {g['detail']}",
                "destinations": []}
    if why := pub.auto_reason(clip, lang):
        return {"state": "not_eligible", "reason": why, "destinations": []}
    store = pub.store
    ok, refused, auto_ok, rule_why = [], [], [], []
    for d in dests if dests is not None else store.q("SELECT * FROM destinations ORDER BY id"):
        s = d["settings"] or {}
        if d["provider"] == "sandbox" or s.get("account_language") != lang:
            continue
        if d["provider"] == "external" and s.get("enabled") is False:
            continue
        why = pub.platform_reason(clip, d) or moments.conflict(store, clip["id"], d)
        (refused if why else ok).append(d["label"] if not why else f"{d['label']}: {why}")
        if not why:
            # will something actually send it there unattended? native: the worker's own auto-post rule check
            # (the
            # same function the auto-post rules run); external: the scheduler's plan
            native = d["provider"] != "external" and hasattr(pub, "rule_reason")
            rw = pub.rule_reason(clip, d, lang) if native else None
            (rule_why.append(f"{d['label']}: {rw}") if rw else auto_ok.append(d["label"]))
    if not ok:
        return {"state": "not_eligible",
                "reason": "no destination accepts it" + (f" ({refused[0]})" if refused else
                                                         f" (no {lang.upper()} account)"),
                "destinations": []}
    if g["state"] != "approved":
        return {"state": "needs_review", "reason": f"needs review: {g['detail']}", "destinations": ok}
    if not auto_ok:
        return {"state": "held_rule",
                "reason": "no account will post it unattended: " + "; ".join(rule_why)[:300],
                "destinations": ok, "auto_destinations": []}
    return {"state": "eligible", "reason": f"gameplay: {g['detail']}", "destinations": ok,
            "auto_destinations": auto_ok}


def candidates(pub, now: float | None = None, language: str | None = None, limit: int = 200,
               prefer: bool = True, fmt=None) -> list[dict]:
    """Ranked candidates (eligible first by priority, then held ones by priority), with the reason for
    rank/hold.
    priority = reaction x freshness x the bounded content-pattern preference (winners.factor; 1.0 = none)."""
    from . import freshness as cutoff
    from . import winners
    fmt = fmt or formats.active()
    store, now = pub.store, now or time.time()
    s = pub.settings
    dests = store.q("SELECT * FROM destinations ORDER BY id")
    rows = []
    # rank all, truncate after
    for c in store.q("SELECT id FROM clips WHERE status='ready' ORDER BY id DESC"):
        clip = store.clip(c["id"])
        lang = original_language(clip)
        if language and lang != language:
            continue
        ed = editorial(pub, clip, dests, fmt)
        on = started_on(store, clip["id"])
        if on and not ed["destinations"]:
            # every account that accepts it already holds it: nothing left
            continue
        rx, rx_why = reaction(clip, fmt)
        fr, age = freshness(clip, now, fmt)
        pf, pf_why = winners.factor(store, s, clip, now, prefer) if ed["state"] == "eligible" else (1.0, [])
        ch = store.channel(clip["channel_id"]) or {}
        rows.append({"clip_id": clip["id"], "title": clip.get("title"), "language": lang,
                     "channel": f"{ch.get('platform')}/{ch.get('name')}",
                     "action_time": clip.get("action_time"),
                     "age_h": round(age, 2), "reaction": round(rx, 3), "freshness": round(fr, 3),
                     "priority": round(rx * fr * pf, 4), "base_priority": round(rx * fr, 4),
                     "pattern_boost": pf, "pattern_reasons": pf_why,
                     "editorial": ed["state"], "reason": ed["reason"],
                     "destinations": ed["destinations"],     # accounts that still accept it (per destination)
                     "auto_destinations": ed.get("auto_destinations", []),   # ... and will post it unattended
                     # accounts that already hold it (never a reason to hide)
                     "already_on": on,
                     "rank_reason": f"{rx_why}; {age:.1f} h old (freshness x{fr:.2f})"
                                    + (f"; content pattern x{pf:.2f}" if pf > 1 else "")
                                    + f"; priority {rx * fr * pf:.2f}",
                     # the hard freshness cutoff of the moment (None: its time is not trusted, the clip is
                     # held)
                     "expires_at": cutoff.expires_at(store, s, clip)})
    order = {"eligible": 0, "held_rule": 1, "needs_review": 2, "not_eligible": 3}
    rows.sort(key=lambda r: (order[r["editorial"]], -r["priority"], r["clip_id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows[:max(1, limit)]
