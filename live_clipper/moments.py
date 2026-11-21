"""Creator moments, one delivery per moment per physical account, per-account cadence, and the rotation plan.

Owner rules:
  * A duplicate is the SAME creator's SAME source moment: overlapping cuts, alternate crops, captions,
    re-renders, retries, older deliveries, and simulcasts on linked identities (channels sharing
    settings.simulcast_group, e.g. one streamer on Twitch + Kick). Different creators' own perspectives are
    independent even when the gameplay or frames are identical; different moments of one creator are
    independent.
  * Each physical account gets at most one delivery per moment, whatever sends it (manual or native
  automatic).
    Only a failed/cancelled attempt frees it; an uncertain one keeps holding it.
  * Per canonical streamer (a channel, or every channel sharing its simulcast_group): DIFFERENT moments are at
    least CREATOR_GAP_S (exactly 2 h) apart across the whole rotation, on every account, past and future
    reservations alike. Repeats of the SAME moment on other accounts (an intentional cross-post) count once
    and
    never block each other. Different streamers are independent: they may post within 2 h of each other, even
    on
    the same account and in overlapping slots.
  * Per physical account: NO post limit (explicit "unlimited"; there is no hidden default or large integer).
    Genuine provider limits still apply where they are enforced by the publisher.

Accounts are physical, not destination rows: rows (pinned id, explicit account_handle, Instagram username)
that
name the same account are one account. Historical claims use what the row itself recorded
(deliveries.account_key),
so repinning, deleting or recreating a destination never frees or moves an old claim. No new tables; callers
run
the checks inside Store.tx() (SQLite BEGIN IMMEDIATE), which serializes separate processes too.

Also here: the live trigger merge rule (window_for, merge_target, dedupe_key, simulcast_channels) the engine
uses
before it creates a clip.
"""
from __future__ import annotations

import json
import time

from . import candidates, gameplay

NEAR_S = 15.0
OVERLAP = 0.5
SCAN_S = 600.0
DAY = 86400.0
# owner policy: unlimited posts per physical account (explicit, not a number)
ACCOUNT_LIMIT = None
CREATOR_GAP_S = 2 * 3600.0
SENT_DELIVERY = ("uploading", "processing", "uncertain", "needs_confirmation", "published", "private",
                 "draft")


# ---------------------------------------------------------------- live trigger merge (engine)
def window_for(action_time, pre, post):
    return action_time - pre, action_time + post


def merge_target(clips, kind, action_time, start, end):
    """Engine.on_trigger same-moment rule. clips: rows (dict with action_time, window_end) of the channel's
    simulcast group, in insertion order. Returns the clip the trigger merges into, or None (a manual trigger
    never
    merges)."""
    merge = min(end - action_time, action_time - start)
    for c in clips:
        if not c["action_time"] > action_time - 120:
            continue
        same = abs(c["action_time"] - action_time) < merge or \
            c["action_time"] <= action_time <= (c["window_end"] or c["action_time"]) + 2.0
        if same and kind != "manual":
            return c
    return None


def dedupe_key(channel_id, kind, action_time):
    return f"{channel_id}:{kind}:{int(action_time)}"


def simulcast_channels(store, channel_id: int) -> list[int]:
    """Channel ids sharing this channel's settings.simulcast_group (opt-in; a channel alone otherwise)."""
    group = (store.channel(channel_id) or {}).get("settings", {}).get("simulcast_group")
    if not group:
        return [channel_id]
    return sorted({channel_id} | {c["id"] for c in store.channels()
                                  if (c["settings"] or {}).get("simulcast_group") == group})


# ---------------------------------------------------------------- creators and moments
def creator_channels(store, channel_id: int) -> list[int]:
    ch = store.channel(channel_id) or {}
    group = (ch.get("settings") or {}).get("simulcast_group")
    if not group:
        return [channel_id]
    return sorted({channel_id} | {c["id"] for c in store.channels()
                                  if (c["settings"] or {}).get("simulcast_group") == group})


def creator_key(store, channel_id: int) -> str:
    ch = store.channel(channel_id) or {}
    group = (ch.get("settings") or {}).get("simulcast_group")
    return f"group:{group}" if group else f"channel:{channel_id}"


def _same(a: dict, b: dict) -> bool:
    ta, tb = a.get("action_time"), b.get("action_time")
    if ta is not None and tb is not None and abs(ta - tb) < NEAR_S:
        return True
    a0, a1, b0, b1 = a.get("window_start"), a.get("window_end"), b.get("window_start"), b.get("window_end")
    if None in (a0, a1, b0, b1):
        return False
    inter = min(a1, b1) - max(a0, b0)
    return inter / max(1e-6, min(a1 - a0, b1 - b0)) >= OVERLAP


def _ledger(store, clip_id: int) -> dict | None:
    try:
        return store.q1("SELECT * FROM clip_ledger WHERE clip_id=?", clip_id)
    except Exception:                                 # read-only store of a database from before this version
        return None


def _from_ledger(r: dict) -> dict:
    return {"id": r["clip_id"], "channel_id": r["channel_id"], "action_time": r["action_time"],
            "window_start": r["window_start"], "window_end": r["window_end"], "retired": True,
            "trigger": {"moment_id": r["moment_id"], "moment_creator": r["moment_creator"]}}


def clip_meta(store, clip_id: int) -> dict | None:
    """The clip row, or - after queue-only removal - its ledger identifiers (channel, moment, times)."""
    c = store.clip(clip_id)
    if c:
        return c
    r = _ledger(store, clip_id)
    return _from_ledger(r) if r else None


def _neighbours(store, c: dict) -> list[dict]:
    ids = creator_channels(store, c["channel_id"])
    ph, lo, hi = ",".join("?" * len(ids)), c["action_time"] - SCAN_S, c["action_time"] + SCAN_S
    rows = store.q(f"SELECT id, channel_id, trigger, action_time, window_start, window_end FROM clips WHERE "
                   f"channel_id IN ({ph}) AND action_time BETWEEN ? AND ? ORDER BY id", *ids, lo, hi)
    try:                                              # moments already posted and removed still cluster
        rows += [_from_ledger(r) for r in store.q(f"SELECT * FROM clip_ledger WHERE channel_id IN ({ph}) AND "
                                                  f"action_time BETWEEN ? AND ?", *ids, lo, hi)]
    except Exception:
        pass
    return sorted(rows, key=lambda r: r["id"])


def cluster_id(store, c: dict, ck: str) -> str:
    """Resolve a creator's temporal cluster read-only, including ledger rows for retired clips."""
    clip_id = c["id"]
    mid = f"m{clip_id}"
    if c.get("action_time") is not None:
        cluster = [r for r in _neighbours(store, c) if r["id"] != clip_id and _same(c, r)]
        valid = [r["trigger"].get("moment_id") for r in cluster
                 if (r.get("trigger") or {}).get("moment_id") and r["trigger"].get("moment_creator") == ck]
        mid = min(valid, key=lambda m: int(m[1:]) if m[1:].isdigit() else 0) if valid else \
            f"m{min([clip_id] + [r['id'] for r in cluster])}"
    return mid


def moment_id(store, clip_id: int) -> str:
    """Stable id of the clip's creator moment, stored with the creator key it was computed under. When that
    key
    changes (two channels linked as a simulcast after both halves were tagged) it is recomputed, so stale ids
    never
    keep a simulcast's halves apart. A removed (posted) clip keeps the moment it was posted under."""
    c = clip_meta(store, clip_id)
    if not c:
        return f"m{clip_id}"
    if c.get("retired"):
        return c["trigger"]["moment_id"]
    ck = creator_key(store, c["channel_id"])
    trig = c.get("trigger") or {}
    if trig.get("moment_id") and trig.get("moment_creator") == ck:
        return trig["moment_id"]
    mid = cluster_id(store, c, ck)
    store.merge_json("clips", clip_id, "trigger", {"moment_id": mid, "moment_creator": ck})
    return mid


def moment_clips(store, clip_id: int) -> list[int]:
    c = clip_meta(store, clip_id)
    mid = moment_id(store, clip_id)
    if not c or c.get("action_time") is None:
        return [clip_id]
    return [r["id"] for r in _neighbours(store, c) if moment_id(store, r["id"]) == mid]


# ---------------------------------------------------------------- physical accounts
def dest_platform(dest: dict) -> str:
    if dest.get("provider") == "external":
        return str((dest.get("settings") or {}).get("platform") or "")
    return str(dest.get("provider") or "")


def norm(platform: str, acct) -> str:
    a = str(acct or "").strip()
    return a if platform == "youtube" else a.lstrip("@").lower()


def account_keys(dest: dict) -> set:
    """Identities of the physical account a destination row points at: its pinned id, an explicit
    account_handle
    (maps a handle to a numeric id / TikTok open_id), the verified id, and for Instagram the verified
    username."""
    p, s = dest_platform(dest), dest.get("settings") or {}
    out = {f"{p}:{norm(p, s[k])}" for k in ("expected_account_id", "account_handle") if s.get(k)}
    info = s.get("account_info") or {}
    if info.get("account_id"):
        out.add(f"{p}:{norm(p, info['account_id'])}")
    if p == "instagram" and dest.get("account"):
        out.add(f"{p}:{norm(p, dest['account'])}")
    return out


def primary_key(dest: dict) -> str:
    p, s = dest_platform(dest), dest.get("settings") or {}
    if s.get("expected_account_id"):
        return f"{p}:{norm(p, s['expected_account_id'])}"
    keys = sorted(account_keys(dest))
    return keys[0] if keys else f"dest:{dest.get('id')}"


def same_account_dests(store, keys: set) -> list:
    return [d["id"] for d in store.q("SELECT * FROM destinations") if account_keys(d) & keys]


def sent_evidence(r: dict) -> bool:
    """A delivery occupies time when it reached (or may have reached) the provider: it is in flight or done,
    or it
    was moved to blocked/paused/held/queued (auth loss, route toggle, social switch) while durable evidence of
    an
    earlier send remains (reconcile flag or a stored provider id). Never-sent rows don't occupy time."""
    if r["status"] in SENT_DELIVERY:
        return True
    if r["status"] in ("failed", "cancelled"):
        return False
    if r.get("reconcile"):
        return True
    ref = r.get("provider_ref") or ""
    try:
        ref = json.loads(ref) if isinstance(ref, str) and ref.startswith("{") else ref
    except ValueError:
        ref = {}
    return bool(ref.get("id")) if isinstance(ref, dict) else bool(ref)


FINAL_DELIVERY = ("published", "private", "draft")
AWAITING = ("uploading", "processing", "uncertain")


def delivery_time(r: dict) -> tuple:
    """(time, basis, provisional) a native delivery occupies on its streamer's timeline.

    Confirmed post: its ACTUAL public time: the provider's own public time when it reported one, else the
    moment
    this host first saw the provider confirm it. Never earlier than the upload claim.
    Not confirmed yet: a provisional reservation. While the provider is still being asked (uploading,
    processing,
    reconciling) the post cannot have become public before the last answer, so the reservation follows that;
    any
    other open row keeps its claim time. A late confirmation replaces the reservation by the verified time."""
    claim = float(r.get("claimed_at") or r.get("created_at") or 0)
    if r["status"] in FINAL_DELIVERY:
        seen = next((float(h["t"]) for h in r.get("history") or [] if isinstance(h, dict) and h.get("t")
                     and h.get("to") in FINAL_DELIVERY and h.get("from") not in FINAL_DELIVERY), None)
        pa = float(r.get("public_at") or 0)
        if pa and (seen is None or pa <= seen + 600):
            return max(claim, pa), "public time reported by the provider", False
        if seen is not None:
            return max(claim, seen), "confirmed by the provider at this time", False
        return claim, "upload claim (no confirmation time recorded)", False
    # the last recorded provider answer (the publisher's clock)
    if r["status"] in AWAITING:
        last = max([float(h.get("t") or 0) for h in r.get("history") or [] if isinstance(h, dict)] or [0.0])
        return max(claim, last), "not confirmed public yet (provisional)", True
    return claim, "upload claim (provisional, outcome open)", True


def _occupancy(store, keys: set, dest_id: int | None) -> tuple:
    """Durable rows on this physical account: deliveries by their recorded account (legacy rows without one by
    their destination's identity). (deliveries, []): the second slot held external handoff receipts in the
    reference and is always empty here."""
    dids = set(same_account_dests(store, keys)) | ({dest_id} if dest_id is not None else set())
    dels = [r for r in store.q("SELECT id, clip_id, destination_id, status, account_key, claimed_at, "
                               "created_at, reconcile, provider_ref FROM deliveries WHERE status NOT IN "
                               "('failed','cancelled')")
            if (r["account_key"] and r["account_key"] in keys)
            or (not r["account_key"] and r["destination_id"] in dids)]
    return dels, []


def keys_for(dest: dict) -> set:
    return account_keys(dest) or {f"dest:{dest.get('id')}"}


def conflict(store, clip_id: int, dest: dict, *, receipt_id: int | None = None,
             delivery_id: int | None = None) -> str | None:
    """Why this clip may NOT go to this physical account: any row of the same creator moment on it (this
    clip's own
    other rows included: re-renders and reposts are duplicates) that is not failed/cancelled. Only the exact
    row
    being updated or polled is exempt. Call inside Store.tx(). receipt_id is accepted for signature
    compatibility
    (there are no external handoff receipts here)."""
    mids = set(moment_clips(store, clip_id))
    dels, _ = _occupancy(store, keys_for(dest), dest.get("id"))
    for r in dels:
        if r["clip_id"] in mids and r["id"] != delivery_id:
            return (f"moment already held on this account by clip {r['clip_id']} (delivery {r['id']}, "
                    f"{r['status']}); one delivery per creator moment per account")
    return None


# ---------------------------------------------------------------- per-account occupancy (no limit)
def account_cap(dest: dict) -> None:
    """Posts allowed per physical account: unlimited (None)."""
    return ACCOUNT_LIMIT


def _policy(dest: dict) -> tuple:                     # (limit, gap): neither exists per account any more
    return ACCOUNT_LIMIT, 0.0


def post_times(store, dest: dict, exclude: tuple = ()) -> list:
    """Times occupying this account: native posts in flight or done (upload start). exclude: (("d", id), ...)
    by
    row ID only."""
    dels, _ = _occupancy(store, keys_for(dest), dest.get("id"))
    out = [float(r["claimed_at"] or r["created_at"]) for r in dels
           if sent_evidence(r) and ("d", r["id"]) not in exclude]
    return sorted(out)


def _cap_violation(times: list, at: float, cap: int | None) -> str | None:
    if cap is None:
        return None
    allt = sorted(times + [at])
    for w in allt:                                # every rolling window that would contain the new post
        if w <= at < w + DAY and sum(1 for t in allt if w <= t < w + DAY) > cap:
            return f"more than {cap} posts in a rolling 24 h on this account"
    return None


def account_problem(store, dest: dict, at: float, exclude: tuple = (), extra=()) -> str | None:
    return _cap_violation(post_times(store, dest, exclude) + list(extra), at, account_cap(dest))


def _acct_earliest(times: list, after: float, cap: int | None) -> float:
    if cap is None:
        return after
    for c in sorted({after} | {t + DAY for t in times if t + DAY >= after}):
        if not _cap_violation(times, c, cap):
            return c
    return max([after] + list(times)) + DAY


def account_next(store, dest: dict, after: float, extra=(), exclude: tuple = ()) -> float:
    return _acct_earliest(sorted(post_times(store, dest, exclude) + list(extra)), after, account_cap(dest))


# ---------------------------------------------------------------- per-streamer spacing (whole rotation)
def creator_events(store, clip_id: int, exclude: tuple = ()) -> list:
    """Every occupying post of the clip's canonical streamer on any real account: native deliveries that
    started
    (upload claimed) or finished. Each with its moment id. Removed (posted) clips count through their ledger
    identifiers."""
    c = clip_meta(store, clip_id)
    if not c:
        return []
    ids = creator_channels(store, c["channel_id"])
    ph = ",".join("?" * len(ids))
    out, mids = [], {}

    def mid(cid):
        if cid not in mids:
            mids[cid] = moment_id(store, cid)
        return mids[cid]
    for r in store.q(f"SELECT d.id, d.clip_id, d.status, d.claimed_at, d.created_at, d.reconcile, "
                     f"d.provider_ref, d.public_at, d.history, t.label FROM deliveries d "
                     f"JOIN clip_channels c ON c.id=d.clip_id LEFT JOIN destinations t "
                     f"ON t.id=d.destination_id WHERE c.channel_id IN ({ph}) AND "
                     f"d.status NOT IN ('failed','cancelled') AND COALESCE(t.provider,'') <> 'sandbox'",
                     *ids):
        if ("d", r["id"]) not in exclude and sent_evidence(r):
            t, basis, prov = delivery_time(r)
            out.append({"t": t, "kind": "native", "row": ("d", r["id"]), "basis": basis, "provisional": prov,
                        "clip_id": r["clip_id"], "state": r["status"],
                        "label": r["label"] or "deleted destination", "moment": mid(r["clip_id"])})
    # recovered history placements (posts the database missed)
    try:
        gaps = store.q(f"SELECT id, platform, at FROM recovery_evidence WHERE kind='gap' AND imported=1 AND "
                       f"at IS NOT NULL AND channel_id IN ({ph})", *ids)
    except Exception:                                  # noqa: BLE001  (a database from before this table)
        gaps = []
    for g in gaps:
        if ("g", g["id"]) not in exclude:
            out.append({"t": float(g["at"]), "kind": "recovered", "row": ("g", g["id"]), "clip_id": None,
                        "state": "recovered post", "label": f"{g['platform']} (recovered history)",
                        "moment": f"recovered:{g['id']}"})
    return sorted(out, key=lambda e: e["t"])


def _creator_clash(events: list, moment: str, at: float) -> dict | None:
    return next((e for e in events if e["moment"] != moment and abs(e["t"] - at) < CREATOR_GAP_S), None)


def creator_problem(store, clip_id: int, at: float, exclude: tuple = (), extra=()) -> str | None:
    """extra: planned (time, moment_id) pairs of the same streamer (the advisory plan)."""
    c = clip_meta(store, clip_id)
    if not c:
        return None
    m = moment_id(store, clip_id)
    planned = [{"t": t, "moment": pm, "clip_id": None, "kind": "planned", "state": "planned", "label": "plan"}
               for t, pm in extra]
    events = creator_events(store, clip_id, exclude) + planned
    e = _creator_clash(events, m, at)
    if not e:
        return None
    when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(e["t"]))
    return (f"streamer {creator_key(store, c['channel_id'])} has another moment "
            f"(clip {e['clip_id']}, {e['state']} on {e['label']}) at {when}"
            + (f" [{e['basis']}]" if e.get("basis") else "") + "; at least 2 h between different moments of "
            "the same streamer (two full hours between public posts)")


def creator_next(store, clip_id: int, after: float, exclude: tuple = (), extra=()) -> float:
    c = clip_meta(store, clip_id)
    if not c:
        return after
    m = moment_id(store, clip_id)
    events = creator_events(store, clip_id, exclude) + [{"t": t, "moment": pm} for t, pm in extra]
    return _creator_earliest(events, m, after)


def _creator_earliest(events: list, moment: str, after: float) -> float:
    others = [e for e in events if e["moment"] != moment]
    for cand in sorted({after} | {e["t"] + CREATOR_GAP_S for e in others if e["t"] + CREATOR_GAP_S >= after}):
        if not _creator_clash(others, moment, cand):
            return cand
    return max([after] + [e["t"] for e in others]) + CREATOR_GAP_S


def _earliest(times: list, cap: int | None, events: list, moment: str, after: float) -> float:
    t = after
    for _ in range(64):
        t1 = _acct_earliest(times, t, cap)
        t2 = _creator_earliest(events, moment, t1)
        if t2 == t1:
            return t1
        t = t2
    return t


# ---------------------------------------------------------------- combined checks (claim, plan)
def slot_problem(store, dest: dict, at: float, exclude: tuple = (), clip_id: int | None = None) -> str | None:
    return account_problem(store, dest, at, exclude) or \
        (creator_problem(store, clip_id, at, exclude) if clip_id is not None else None)


def next_slot(store, dest: dict, after: float, extra=(), exclude: tuple = (), clip_id: int | None = None,
              creator_extra=()) -> float:
    """Earliest time >= after for a clip: its streamer's 2 h spacing between different moments (past AND
    future
    reservations). There is no per-account limit."""
    t = after
    for _ in range(64):
        t1 = account_next(store, dest, t, extra, exclude)
        t2 = creator_next(store, clip_id, t1, exclude, creator_extra) if clip_id is not None else t1
        if t2 == t1:
            return t1
        t = t2
    return t


# ---------------------------------------------------------------- rotation
def recent_by_creator(store, keys: set, now: float) -> dict:
    dels, recs = _occupancy(store, keys, None)
    out: dict = {}
    for r in dels + recs:
        if r["created_at"] > now - DAY:
            c = clip_meta(store, r["clip_id"])
            if c:
                k = creator_key(store, c["channel_id"])
                out[k] = out.get(k, 0) + 1
    return out


def rotation_key(store, clip_id: int, recent: dict, now: float | None = None) -> tuple:
    """Native automatic send order on one account: least recently posted creator first, then the candidate
    priority
    (reaction x freshness, candidates), then the clip id."""
    from . import winners
    c = store.clip(clip_id) or {}
    now = now or time.time()
    return (recent.get(creator_key(store, c.get("channel_id", 0)), 0),
            -candidates.priority(c, now) * winners.factor(store, None, c, now)[0] if c else 0, clip_id)


def plan(pub, language: str, now: float | None = None, horizon_h: float = 24.0,
         per_creator: int | None = None) -> dict:
    """Advisory, read-only: the next posts for each sendable account of a language (editorially eligible
    candidates
    only: reviewed gameplay + speech check; ranked by candidates priority = reaction x freshness). Default
    ROTATE:
    distinct eligible moments (a moment held on any same-language account is not planned again), creators in
    fair
    rotation, then priority. Respects each streamer's 2 h spacing between different moments (past and future
    reservations, and the plan's own earlier picks); no per-account limit. Different streamers may share a
    slot.
    Enforcement happens at claim time, not here."""
    from . import freshness, winners
    store = pub.store
    now = now or time.time()
    end = now + horizon_h * 3600
    dests = []
    for d in store.q("SELECT * FROM destinations ORDER BY id"):
        s = d["settings"] or {}
        if s.get("account_language") != language or d["provider"] == "sandbox":
            continue
        if d["provider"] == "external":
            if s.get("enabled") is False:
                continue
        elif pub.block_reason(d) or pub.identity_reason(d):
            continue
        dests.append(d)
    lang_keys = set().union(*[keys_for(d) for d in dests]) if dests else set()
    held, recent = set(), {}
    if lang_keys:
        dels, recs = _occupancy(store, lang_keys, None)
        for r in dels + recs:
            if clip_meta(store, r["clip_id"]):
                held.add(moment_id(store, r["clip_id"]))
        recent = recent_by_creator(store, lang_keys, now)
    cands = []
    # every ready clip is ranked (no newest-N truncation before the language filter); a creator can fill at
    # most
    # one slot per CREATOR_GAP_S, so keeping horizon/gap + 1 moments per creator never hides a schedulable one
    per_creator = per_creator or int(horizon_h * 3600 // CREATOR_GAP_S) + 1
    for c in store.q("SELECT id, channel_id, captions, files, action_time, trigger FROM clips "
                     "WHERE status='ready' ORDER BY action_time DESC"):
        caps, files = c["captions"] or {}, c["files"] or {}
        if (caps.get("policy") or {}).get("render_default") != language:
            continue
        f = (files.get("final") or {}).get(language)
        if not f or not (pub.settings.clips_dir / str(c["id"]) / f).exists():
            continue
        mid = moment_id(store, c["id"])
        if mid in held:
            continue
        clip = store.clip(c["id"])
        if gameplay.evidence(clip)["state"] != "approved" or pub.auto_reason(clip, language):
            # editorial eligibility: reviewed gameplay + speech check
            continue
        # hard cutoff: an expired or untimed moment is never
        if freshness.reason(store, pub.settings, clip, now):
            continue                                           # planned
        fresh_until = freshness.expires_at(store, pub.settings, clip)
        rx, rx_why = candidates.reaction(clip)
        fr, age = candidates.freshness(clip, now)
        pf = winners.factor(store, pub.settings, clip, now)[0]
        cands.append({"clip_id": c["id"], "moment_id": mid, "creator": creator_key(store, c["channel_id"]),
                      "action_time": c["action_time"], "score": float((c["trigger"] or {}).get("score") or 0),
                      "priority": round(rx * fr * pf, 4), "fresh_until": fresh_until,
                      "rank_reason": f"{rx_why}; {age:.1f} h old (freshness x{fr:.2f})"
                                     + (f"; content pattern x{pf:.2f}" if pf > 1 else "")
                                     + f"; priority {rx * fr * pf:.2f}"})
    best, per = [], {}
    by_moment: dict = {}
    for x in cands:                                   # one clip per moment: the highest priority one
        if x["moment_id"] not in by_moment or (-x["priority"], x["clip_id"]) < \
                (-by_moment[x["moment_id"]]["priority"], by_moment[x["moment_id"]]["clip_id"]):
            by_moment[x["moment_id"]] = x
    for x in sorted(by_moment.values(), key=lambda y: (-y["priority"], y["clip_id"])):
        if per.get(x["creator"], 0) < per_creator:    # top moments per creator by priority
            per[x["creator"]] = per.get(x["creator"], 0) + 1
            best.append(x)
    times = {d["id"]: post_times(store, d) for d in dests}
    caps = {d["id"]: account_cap(d) for d in dests}
    events: dict = {}
    for x in best:
        if x["creator"] not in events:
            events[x["creator"]] = [{"t": e["t"], "moment": e["moment"]}
                                    for e in creator_events(store, x["clip_id"])]
    ok: dict = {}

    def allowed(x, d):
        k = (x["clip_id"], d["id"])
        if k not in ok:
            ok[k] = (not pub.platform_reason(store.clip(x["clip_id"]), d)
                     and not conflict(store, x["clip_id"], d))
        return ok[k]
    used, out, posted = set(), [], dict(recent)
    for _ in range(len(best)):                    # each pick uses one distinct moment
        pick = None
        for d in dests:
            for x in best:
                if x["moment_id"] in used or not allowed(x, d):
                    continue
                t = _earliest(times[d["id"]], caps[d["id"]], events[x["creator"]], x["moment_id"], now)
                key = (t, posted.get(x["creator"], 0), -x["priority"], x["clip_id"], d["id"])
                late = x["fresh_until"] - (freshness.margin_s(pub.settings) if d["provider"] == "external"
                                           else 0)
                if t >= late:                         # the slot would fall at or past the moment's cutoff
                    continue
                if t <= end and (pick is None or key < pick[0]):
                    pick = (key, t, d, x)
        if not pick:
            break
        _, t, d, x = pick
        used.add(x["moment_id"])
        posted[x["creator"]] = posted.get(x["creator"], 0) + 1
        times[d["id"]] = sorted(times[d["id"]] + [t])
        events[x["creator"]].append({"t": t, "moment": x["moment_id"]})
        out.append({"slot_at": t, "destination_id": d["id"], "destination": d["label"],
                    "account": primary_key(d),
                    "sender": "external" if d["provider"] == "external" else "native", **x})
    out.sort(key=lambda r: (r["slot_at"], r["destination_id"]))
    return {"language": language, "generated_at": now, "accounts": sorted(lang_keys), "slots": out,
            "candidates": len(best)}
