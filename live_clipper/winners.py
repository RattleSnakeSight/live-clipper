"""Which posted clips beat comparable posts on BOTH views and likes, what their content had in common, and a
bounded
preference for new moments that share an evidenced pattern.

Content descriptors (clip_descriptors, one small row per clip, kept after the clip's media is retired):
  everything is read from what the app itself produced for the clip, never guessed:
    opening      the first subtitle cue of the clip's original-language subtitles (first 3 s): its wording (at
    most
                 12 words), and whether it is a question or a spoken reaction;
    spoken       whether the subtitles contain a question / a spoken reaction anywhere, and words per second;
    duration     the final clip length (files.duration);
    chat spike   the detector's spike ratio and chatters for a chat-spike trigger (no measurement = "none");
    payoff       when the detected moment happens inside the clip (seconds from the clip's first frame), only
    when
                 the clip's own cut timing makes that computable.
  A feature whose source is not available is listed under "missing", never filled in. Only these short
  descriptors
  and labels are stored: no media, no full transcript.

Post data (anchors, readings, proof_state): the confirmed posts' identity and publication time and their
metric
readings belong to the publishing side. They come from the store when it provides post_anchors(),
post_readings(anchors) and post_proof(kind, ref); without them there are no posts and every preference is 1.0.

Comparable posts (evaluate):
  a post is compared only with posts on the SAME platform, SAME account and SAME language, read at the SAME
  snapshot
  age window (WINDOWS, by the post's REAL age when the reading was taken: 1-2 h, 2-4 h, 4-8 h, 8-16 h, 24 h,
  72 h,
  7 d, 30 d; one reading per post and window, the one closest to the window's centre). Its peers are the other
  posts in that group that have BOTH views and likes.
    win          the unique leader on both views and likes;
    tie          tied for first on either metric while leading or tied on the other;
    promising    above both medians without jointly leading both metrics;
    timing_unverified  public time is unknown or its bounds span more than one comparison window;
    not a win    anything else;
    missing      the post has no views or no likes reading at that window (an unavailable metric is never a
    zero);
    insufficient fewer than MIN_PEERS peers with both metrics: no verdict at all.
  One creator moment posted to several accounts is ONE moment: its per-platform verdicts are shown side by
  side and
  it counts once (a "winning moment" when most of its decisive verdicts are wins), never three independent
  wins.

Patterns (patterns): for each content label, winning moments with the label vs all decided moments with it,
smoothed toward the overall win rate. A label is "evidenced" only with enough decided moments, at least two
winning
moments from at least two different creators, and a lift above 1.1. This is an association in a small sample,
not
a cause.

Preference (factor): a ready clip that carries evidenced labels gets a bounded multiplier on its queue
priority (at
most +MAX_BOOST). It never adds a clip to the queue, never favors a moment that was already posted (re-cuts
and
re-edits of a posted moment get nothing), and one hour in every EXPLORE_EVERY uses no preference at all
(exploration). Every gate (freshness, rights, language, rotation, 2 h cooldown, duplicates, quota) is applied
as
before, after it.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import time
from pathlib import Path

from . import formats, moments

# comparable age windows (hours since publication): a reading belongs to the window that contains its REAL age
WINDOWS = (("1-2h", 1.0, 2.0), ("2-4h", 2.0, 4.0), ("4-8h", 4.0, 8.0), ("8-16h", 8.0, 16.0),
           ("24h", 18.0, 36.0), ("72h", 54.0, 108.0), ("7d", 126.0, 252.0), ("30d", 540.0, 900.0))
WINDOW_ORDER = {w[0]: i for i, w in enumerate(WINDOWS)}
AGE_SPREAD_WARN = 1.6
MIN_PEERS = 4                 # peers with both metrics needed before any verdict
MIN_LABEL_MOMENTS = 3         # decided moments carrying a label before it can be evidenced
MIN_DECIDED = 6               # decided moments in a scope before any pattern is evidenced
SMOOTH_K = 4.0
MIN_LIFT = 1.1
MAX_BOOST = 0.25
BOOST_WEIGHT = 0.5
EXPLORE_EVERY = 4
OPENING_S = 3.0
REACTION = {
    "en": ("oh my god", "omg", "no way", "what the", "wtf", "holy", "let's go", "lets go", "bro", "dude",
           "wow", "yo", "haha", "lol", "oh no", "are you kidding", "insane", "crazy", "bruh"),
    "es": ("dios mío", "dios mio", "no manches", "qué es eso", "que es eso", "wow", "jaja", "no puede ser",
           "madre mía", "madre mia", "increíble", "increible", "vamos", "hermano", "bro", "güey", "wey",
           "loco", "ostia", "hostia"),
}
QUESTION_START = {
    "en": ("what", "why", "how", "who", "where", "when", "is ", "are ", "did ", "do ", "does ", "can ",
           "was ", "were ", "should ", "would "),
    "es": ("qué", "que ", "por qué", "porque ", "cómo", "como ", "quién", "quien", "dónde", "donde", "cuándo",
           "cuando ", "es ", "está", "esta "),
}
LABEL_TEXT = {
    "opening:question": "opens with a spoken question", "opening:reaction": "opens on a spoken reaction",
    "opening:statement": "opens with a plain spoken line", "opening:silent": "no speech in the first 3 s",
    "spoken:question": "a question is spoken in the clip", "spoken:reaction": "a spoken reaction in the clip",
    "duration:short": "under 20 s", "duration:medium": "20-35 s", "duration:long": "over 35 s",
    "spike:high": "chat spike 8x+ baseline", "spike:medium": "chat spike 3-8x",
    "spike:low": "chat spike under 3x",
    "spike:none": "no chat measurement", "payoff:early": "moment within the first 5 s",
    "payoff:middle": "moment at 5-12 s", "payoff:late": "moment after 12 s",
    "density:fast": "fast speech (2.5+ words/s)", "density:normal": "normal speech density",
    "density:slow": "sparse speech (under 1.2 words/s)",
}
DECIDED = ("winner", "promising", "tie", "not_win")
WEAK_LIKES = ("youtube_feed",)   # sources whose like counts are not a verified likeCount: likes are ignored


# ---------------------------------------------------------------- subtitles (same parser as the captions SRT
# reader)
_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_ARROW = re.compile(_TS + r"\s*-->\s*" + _TS)


def _secs(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _parse_srt(text: str) -> list[dict]:
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


# ---------------------------------------------------------------- content descriptors
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def _is_question(text: str, lang: str) -> bool:
    t = text.lower().lstrip("¿¡\"' ")
    return "?" in text or "¿" in text or any(t.startswith(w)
                                              for w in QUESTION_START.get(lang, QUESTION_START["en"]))


def _is_reaction(text: str, lang: str) -> bool:
    t = " " + re.sub(r"[^\w' ]+", " ", text.lower()) + " "
    return "!" in text or any(f" {w} " in t for w in REACTION.get(lang, REACTION["en"]))


def _srt(settings, clip: dict, lang: str) -> Path | None:
    rel = ((clip.get("captions") or {}).get(lang) or {}).get("srt")
    if not rel or settings is None:
        return None
    base = (Path(settings.clips_dir) / str(clip["id"])).resolve()
    p = (base / str(rel)).resolve()
    if base not in p.parents or not p.is_file():          # confined to the clip's own folder
        return None
    return p


def features(settings, clip: dict) -> tuple[dict, list, list]:
    """(features, labels, missing) from the clip's own subtitles, cut timing and trigger. Pure read."""
    from .candidates import original_language
    lang = original_language(clip) or clip.get("source_lang") or ""
    files, trig = clip.get("files") or {}, clip.get("trigger") or {}
    f: dict = {"language": lang}
    labels: list = []
    missing: list = []
    dur = files.get("duration")
    f["duration_s"] = round(float(dur), 2) if dur else None
    if f["duration_s"]:
        labels.append("duration:short" if dur < 20 else "duration:medium" if dur <= 35 else "duration:long")
    else:
        missing.append("duration (no final cut length recorded)")
    p = _srt(settings, clip, lang)
    cues = []
    if p is not None:
        try:
            cues = [c for c in _parse_srt(p.read_text(encoding="utf-8")) if _norm(c["text"])]
        except (OSError, ValueError):
            cues = None
    if p is None or cues is None:
        missing.append("subtitles (no original-language subtitle file)")
        f.update(opening_text=None, opening_question=None, opening_reaction=None, spoken_question=None,
                 spoken_reaction=None, words_per_s=None)
    else:
        first = [c for c in cues if float(c["start"]) < OPENING_S]
        opening = _norm(" ".join(c["text"] for c in first))
        words = opening.split()
        f["opening_text"] = " ".join(words[:12]) or ""
        f["opening_question"] = bool(opening) and _is_question(opening, lang)
        f["opening_reaction"] = bool(opening) and _is_reaction(opening, lang)
        allt = " ".join(_norm(c["text"]) for c in cues)
        f["spoken_question"] = bool(allt) and any(_is_question(_norm(c["text"]), lang) for c in cues)
        f["spoken_reaction"] = bool(allt) and _is_reaction(allt, lang)
        n = len(allt.split())
        f["words_per_s"] = round(n / float(dur), 2) if dur else None
        labels.append("opening:silent" if not opening else "opening:question" if f["opening_question"] else
                      "opening:reaction" if f["opening_reaction"] else "opening:statement")
        if f["spoken_question"]:
            labels.append("spoken:question")
        if f["spoken_reaction"]:
            labels.append("spoken:reaction")
        if f["words_per_s"] is not None:
            labels.append("density:fast" if f["words_per_s"] >= 2.5 else
                          "density:slow" if f["words_per_s"] < 1.2 else "density:normal")
    if (trig.get("kind") or clip.get("kind")) == "chat_spike" and trig.get("score") is not None:
        r = float(trig["score"])
        f["chat_spike_ratio"], f["chatters"] = round(r, 2), trig.get("unique_users")
        labels.append("spike:high" if r >= 8 else "spike:medium" if r >= 3 else "spike:low")
    else:
        f["chat_spike_ratio"], f["chatters"] = None, None
        labels.append("spike:none")
    f["payoff_at_s"] = None
    at = clip.get("action_time")
    if at is not None and dur:
        start = None
        if files.get("source_t0") is not None and files.get("trim_in") is not None:
            start = float(files["source_t0"]) + float(files["trim_in"])
            if not -1 <= float(at) - start <= float(dur) + 1:
                start = None
        if start is None and clip.get("window_start") is not None and \
                -1 <= float(at) - float(clip["window_start"]) <= float(dur) + 1:
            start = float(clip["window_start"])
        if start is not None:
            f["payoff_at_s"] = round(max(0.0, float(at) - start), 1)
            labels.append("payoff:early" if f["payoff_at_s"] < 5 else
                          "payoff:middle" if f["payoff_at_s"] <= 12 else "payoff:late")
    if f["payoff_at_s"] is None:
        missing.append("payoff timing (the clip's cut timing does not place the detected moment)")
    return f, labels, missing


def _render_layouts(files: dict) -> dict:
    """Keep small render facts by final language and media version. Old or unknown snapshot schemas stay
    unknown."""
    out = {}
    manifest = files.get("final_layout")
    if not isinstance(manifest, dict):
        return out
    for lang, versions in manifest.items():
        if not isinstance(versions, dict):
            continue
        for version, snap in versions.items():
            if not isinstance(snap, dict) or type(snap.get("layout_version")) is not int or \
                    snap["layout_version"] != 1:
                continue
            if snap.get("mode") not in ("split", "stack", "fit", "crop"):
                continue
            fact = {"mode": snap["mode"], "layout_version": 1}
            if snap.get("requested") in ("auto", "split", "stack", "fit", "crop"):
                fact["requested"] = snap["requested"]
            out.setdefault(lang, {})[str(version)] = fact
    return out


def _fp(settings, clip: dict) -> str:
    from .candidates import original_language
    p = _srt(settings, clip, original_language(clip) or "")
    st = p.stat() if p is not None else None
    files = clip.get("files") or {}
    inputs = [st.st_size if st else 0, st.st_mtime_ns if st else 0, files.get("duration"),
              files.get("trim_in"), (clip.get("trigger") or {}).get("score"), clip.get("action_time")]
    layouts = _render_layouts(files)
    if layouts:
        inputs.append(layouts)
    raw = json.dumps(inputs, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def descriptor(store, clip_id: int) -> dict | None:
    r = store.q1("SELECT * FROM clip_descriptors WHERE clip_id=?", clip_id)
    if not r:
        return None
    for k in ("features", "labels", "missing"):
        r[k] = json.loads(r[k] or ("{}" if k == "features" else "[]"))
    return r


def describe(store, settings, clip_id: int) -> dict | None:
    """Create or refresh the clip's descriptor from its live row; a retired clip keeps the stored one.
    Idempotent:
    nothing is written when the sources did not change."""
    clip = store.clip(clip_id)
    if not clip or clip["status"] in ("retiring", "discarded") or settings is None:
        return descriptor(store, clip_id)
    cur = descriptor(store, clip_id)
    fp = _fp(settings, clip)
    if cur and cur["source_fp"] == fp:
        return cur
    layouts = _render_layouts(clip.get("files") or {})
    if cur and _srt(settings, clip, cur["language"] or "") is None and \
            cur["features"].get("opening_text") is not None:
        if not layouts:
            return cur
        # keep the previously read subtitles
        f, labels, missing = cur["features"], cur["labels"], cur["missing"]
    else:
        f, labels, missing = features(settings, clip)
    saved = dict((cur or {}).get("features", {}).get("render_layouts") or {})
    for lang, versions in layouts.items():
        saved[lang] = {**versions, **saved.get(lang, {})}  # a final's recorded frame never changes
    if saved:
        f["render_layouts"] = saved
    now = time.time()
    mid = moments.moment_id(store, clip_id)
    ck = moments.creator_key(store, clip["channel_id"])
    store.x("INSERT INTO clip_descriptors(clip_id, moment_id, creator_key, language, source_fp, features, "
            "labels, missing, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(clip_id) DO UPDATE SET "
            "moment_id=excluded.moment_id, creator_key=excluded.creator_key, language=excluded.language, "
            "source_fp=excluded.source_fp, features=excluded.features, labels=excluded.labels, "
            "missing=excluded.missing, updated_at=excluded.updated_at", clip_id, mid, ck, f["language"], fp,
            json.dumps(f), json.dumps(labels), json.dumps(missing), now, now)
    return descriptor(store, clip_id)


def describe_pending(store, settings, limit: int = 30) -> int:
    """Bounded: ready clips with subtitles and every posted clip still on disk that has no current
    descriptor."""
    n = 0
    posted = {a["clip_id"] for a in anchors(store).values()}
    for r in store.q("SELECT id FROM clips WHERE status IN ('ready','expired') ORDER BY id DESC LIMIT 2000"):
        if n >= limit:
            break
        cur = store.q1("SELECT source_fp FROM clip_descriptors WHERE clip_id=?", r["id"])
        clip = store.clip(r["id"])
        if not clip or (clip["status"] == "expired" and r["id"] not in posted):
            continue
        if cur and cur["source_fp"] == _fp(settings, clip):
            continue
        describe(store, settings, r["id"])
        n += 1
    return n


# ---------------------------------------------------------------- post data (from the publishing side)
def anchors(store) -> dict:
    """{(kind, ref_id): {"kind", "ref_id", "clip_id", "platform", "account", "language", "url", "post_id",
    "media_version", "published_at", "public_bounds", "time_source"}} of every confirmed post. Read-only."""
    fn = getattr(store, "post_anchors", None)
    return fn() if fn is not None else {}


def proof_state(store, kind: str, ref: int) -> tuple[str | None, str]:
    """("confirmed" | "provisional" | None, why) of a post. Read-only."""
    fn = getattr(store, "post_proof", None)
    return fn(kind, ref) if fn is not None else (None, "no post data")


# ---------------------------------------------------------------- readings, comparable groups and verdicts
def window_of(age_h: float) -> str | None:
    for key, lo, hi in WINDOWS:
        if age_h >= lo and (hi is None or age_h < hi):
            return key
    return None


def _proven_window(anchor: dict, observed_at: float, window: str) -> bool:
    bounds = anchor["public_bounds"]
    return bool(bounds and window_of((observed_at - bounds[0]) / 3600) == window
                and window_of((observed_at - bounds[1]) / 3600) == window)


def _center(key: str) -> float:
    _, lo, hi = next(w for w in WINDOWS if w[0] == key)
    return (lo * hi) ** 0.5 if hi else lo


def readings(store, anc: dict | None = None) -> list[dict]:
    """Every reading of every post with its REAL age: [{"kind", "ref_id", "age_h", "views", "likes" (None from
    a
    weak source), "source", "observed_at", "label", "provenance", "by"}]."""
    anc = anchors(store) if anc is None else anc
    fn = getattr(store, "post_readings", None)
    return fn(anc) if fn is not None else []


def complete(rd: dict) -> bool:
    return rd["views"] is not None and rd["likes"] is not None


def _moment_of(store, clip_id: int) -> tuple[str, str]:
    """(moment id, creator key) without writing: the descriptor, else the id stored at capture / in the
    ledger."""
    d = store.q1("SELECT moment_id, creator_key FROM clip_descriptors WHERE clip_id=?", clip_id)
    meta = moments.clip_meta(store, clip_id)
    if not meta:
        return (d["moment_id"], d["creator_key"]) if d and d["moment_id"] else (f"m{clip_id}", "")
    creator = moments.creator_key(store, meta["channel_id"])
    trig = meta.get("trigger") or {}
    if trig.get("moment_id") and trig.get("moment_creator") == creator:
        return trig["moment_id"], creator
    if d and d["moment_id"] and d["creator_key"] == creator:
        return d["moment_id"], creator
    # Old descriptors and ledgers may predate linking two channels as one streamer. Reuse the capture-time
    # matching rules without rewriting a posted receipt, its descriptor, or its ownership.
    return moments.cluster_id(store, meta, creator), creator


def _comparison_ids(store, anc):
    """Collapse historical rows for the same physical post or creator's edit/simulcast on one destination."""
    parent = {}
    aliases = {}
    moment_by_clip = {}

    def root(key):
        while key != parent[key]:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for key, a in anc.items():
        parent[key] = key
        cid = a["clip_id"]
        if cid not in moment_by_clip:
            moment_by_clip[cid] = _moment_of(store, cid)
        moment, creator = moment_by_clip[cid]
        identifiers = [("moment", creator, moment)]
        post_id = a.get("post_id")
        if post_id:
            identifiers.append(("post", post_id))
        for identifier in identifiers:
            alias = (a["platform"], a["account"], a["language"], identifier)
            previous = aliases.setdefault(alias, key)
            parent[root(key)] = root(previous)
    return {key: root(key) for key in anc}


def quality(store, clip_id: int, cache: dict | None = None) -> str:
    """The clip's editorial standing (gameplay.evidence): approved | rejected | unknown. Only an approved
    (qualified or operator-approved) post may teach a content pattern; an editorially rejected post is never a
    winner, a peer or a teacher, however many views it got."""
    if cache is not None and clip_id in cache:
        return cache[clip_id]
    from . import gameplay
    clip = store.clip(clip_id)
    q = gameplay.evidence(clip)["state"] if clip else "unknown"
    if cache is not None:
        cache[clip_id] = q
    return q


def evaluate(store, fmt=None) -> dict:
    """Every comparable group with each post's verdict, and the moments linked across platforms. Read-only."""
    fmt = fmt or formats.active()
    anc = anchors(store)
    qual: dict = {}
    identities = _comparison_ids(store, anc)
    chosen: dict = {}
    render_layouts = {}
    off_window = []
    for rd in readings(store, anc):
        w = window_of(rd["age_h"])
        if w is None:
            off_window.append({"kind": rd["kind"], "ref_id": rd["ref_id"], "age_h": rd["age_h"],
                               "source": rd["source"],
                               "why": "age outside every comparable window: kept, never compared"})
            continue
        k = (identities[(rd["kind"], rd["ref_id"])], w)
        # one reading per post and window: a complete one (views AND likes) first, then the closest to the
        # centre
        rd["age_verified"] = _proven_window(anc[(rd["kind"], rd["ref_id"])], rd["observed_at"], w)
        key = (not (complete(rd) and rd["age_verified"]), not complete(rd),
               abs(math.log(max(rd["age_h"], 0.01) / _center(w))), rd["observed_at"])
        if k not in chosen or key < chosen[k][0]:
            chosen[k] = (key, rd)
    groups: dict = {}
    for (_, w), (_, rd) in chosen.items():
        f = anc[(rd["kind"], rd["ref_id"])]
        groups.setdefault((f["platform"], f["account"], f["language"], w), []).append((f, rd))
    out_groups, verdicts = [], []
    for (platform, account, lang, w), rs in sorted(groups.items(),
                                                   key=lambda kv: (kv[0][:3], WINDOW_ORDER[kv[0][3]])):
        # peers are eligible posts only: an editorially rejected post is never a yardstick
        full = [(f, rd) for f, rd in rs if complete(rd) and rd["age_verified"]
                and quality(store, f["clip_id"], qual) != "rejected"]
        ages = [rd["age_h"] for _, rd in full]
        warn = []
        if len(ages) >= 2 and min(ages) > 0 and max(ages) / min(ages) > AGE_SPREAD_WARN:
            warn.append(f"reading ages in this window differ up to {max(ages) / min(ages):.1f}x "
                        f"({min(ages):g}-{max(ages):g} h): early counts grow fast, read with care")
        posts = []
        for f, rd in sorted(rs, key=lambda x: x[0]["ref_id"]):
            mid, ck = _moment_of(store, f["clip_id"])
            cid = f["clip_id"]
            if cid not in render_layouts:
                d = descriptor(store, cid)
                render_layouts[cid] = (d or {}).get("features", {}).get("render_layouts", {})
            render = render_layouts[cid].get(lang, {}).get(str(f["media_version"]))
            v = {"kind": f["kind"], "ref_id": f["ref_id"], "clip_id": f["clip_id"], "moment_id": mid,
                 "creator": ck, "platform": platform, "account": account, "language": lang, "window": w,
                 "url": f["url"], "age_h": round(rd["age_h"], 2), "age_verified": rd["age_verified"],
                 "time_source": f["time_source"],
                 "render_layout": render, "media_version": f["media_version"],
                 "observed_at": rd["observed_at"], "views": rd["views"], "likes": rd["likes"],
                 "source": rd["source"], "provenance": rd["provenance"]}
            peers = [rd2 for f2, rd2 in full if not (f2["kind"] == f["kind"] and f2["ref_id"] == f["ref_id"])]
            v["editorial"] = quality(store, cid, qual)
            if v["editorial"] == "rejected":
                v.update(verdict="excluded",
                         why=f"editorially rejected (not a worthwhile {fmt.labels.game} moment): its counts "
                         "stay visible but it is never a winner, a peer or a pattern teacher")
            elif not complete(rd):
                v.update(verdict="missing", why="no " + " and no ".join(
                    k for k in ("views", "likes") if rd[k] is None) + f" in the {w} reading"
                    + (" (the public feed has no verified like count)" if rd["source"] in WEAK_LIKES else ""))
            elif not rd["age_verified"]:
                v.update(verdict="timing_unverified",
                         why="publication time is unverified or its possible range crosses a comparison "
                         "window; counts remain visible and do not supply peer evidence")
            elif len(peers) < MIN_PEERS:
                v.update(verdict="insufficient", peers=len(peers),
                         why=f"only {len(peers)} comparable post(s) with both metrics in {w} "
                         f"(need {MIN_PEERS})")
            else:
                mv = statistics.median(p["views"] for p in peers)
                ml = statistics.median(p["likes"] for p in peers)
                v.update(peers=len(peers), peer_median_views=round(mv, 1), peer_median_likes=round(ml, 1),
                         views_x=round(rd["views"] / mv, 2) if mv else None,
                         likes_x=round(rd["likes"] / ml, 2) if ml else None)
                xv, xl = max(p["views"] for p in peers), max(p["likes"] for p in peers)
                v.update(peer_max_views=xv, peer_max_likes=xl)
                if rd["views"] > xv and rd["likes"] > xl:
                    v.update(verdict="winner",
                             why=f"the unique top post on BOTH metrics: views {rd['views']:g} > every other "
                             f"({xv:g} next) and likes {rd['likes']:g} > every other ({xl:g} next) among "
                             f"{len(peers)} comparable posts ({w})")
                elif rd["views"] >= xv and rd["likes"] >= xl:
                    v.update(verdict="tie", why="tied for the top: no definitive winner")
                elif rd["views"] > mv and rd["likes"] > ml:
                    v.update(verdict="promising",
                             why=f"above the comparable medians on both (views {rd['views']:g} > {mv:g}, "
                             f"likes {rd['likes']:g} > {ml:g}) but not the unique top on both "
                             f"({'tied' if rd['views'] == xv or rd['likes'] == xl else 'beaten'} on "
                             f"{'views' if rd['views'] <= xv else 'likes'}): not a verified winner")
                else:
                    v.update(verdict="not_win",
                             why=f"views {rd['views']:g} (median {mv:g}, top {xv:g}), likes "
                             f"{rd['likes']:g} (median {ml:g}, top {xl:g})")
            posts.append(v)
            verdicts.append(v)
        out_groups.append({"platform": platform, "account": account, "language": lang, "window": w,
                           "posts_with_both_metrics": len(full), "warnings": warn, "posts": posts})
    by_moment: dict = {}
    for v in verdicts:
        by_moment.setdefault(v["moment_id"], []).append(v)
    out_moments = []
    for mid, vs in by_moment.items():
        per_acct: dict = {}
        for v in sorted(vs, key=lambda x: (x["verdict"] in DECIDED, WINDOW_ORDER[x["window"]])):
            per_acct[(v["platform"], v["account"])] = v  # last wins: a decided verdict, the latest window
        dec = [v for v in per_acct.values() if v["verdict"] in DECIDED]
        wins = sum(v["verdict"] == "winner" for v in dec)
        good = sum(v["verdict"] in ("winner", "promising") for v in dec)
        outcome = ("undecided" if not dec else "winner" if wins * 2 > len(dec) else "promising" if good
                   else "not_winner")
        teaches = all(v.get("editorial") == "approved" for v in vs)
        out_moments.append({"moment_id": mid, "creator": vs[0]["creator"],
                            "clip_ids": sorted({v["clip_id"] for v in vs}),
                            "outcome": outcome, "decisive_results": len(dec), "wins": wins,
                            "editorial": sorted({v.get("editorial") for v in vs}), "teaches": teaches,
                            "platforms": [{"platform": v["platform"], "account": v["account"],
                                           "language": v["language"], "window": v["window"],
                                           "age_h": v["age_h"], "views": v["views"], "likes": v["likes"],
                                           "verdict": v["verdict"], "why": v["why"], "source": v["source"],
                                           "url": v["url"], "render_layout": v["render_layout"],
                                           "media_version": v["media_version"]}
                                          for v in sorted(per_acct.values(),
                                                          key=lambda x: (x["platform"], x["account"]))]})
    rank = {"winner": 0, "promising": 1, "not_winner": 2, "undecided": 3}
    out_moments.sort(key=lambda m: (rank[m["outcome"]], m["moment_id"]))
    # One current verdict per destination post, even if another platform lost for the same creator moment.
    # Keep the latest decided observation when a later window has too few peers to judge the post again.
    latest = {}
    for v in sorted(verdicts, key=lambda x: (x["verdict"] in DECIDED, WINDOW_ORDER[x["window"]])):
        latest[identities[(v["kind"], v["ref_id"])]] = v
    coverage = {}
    for key, a in anc.items():
        if key != identities[key]:
            continue
        route = (a["language"], a["platform"], a["account"])
        row = coverage.setdefault(route, {"language": a["language"], "platform": a["platform"],
                                          "account": a["account"], "posts": 0, "no_window_reading": 0,
                                          "missing": 0, "insufficient": 0, "timing_unverified": 0,
                                          "excluded": 0,
                                          "decided": 0, "winners": 0})
        row["posts"] += 1
        v = latest.get(key)
        state = v["verdict"] if v else "no_window_reading"
        if state in DECIDED:
            row["decided"] += 1
            row["winners"] += int(state == "winner")
        else:
            row[state] += 1
    winning_posts = sorted((v for v in latest.values() if v["verdict"] == "winner"),
                           key=lambda x: (-x["observed_at"], x["kind"], x["ref_id"]))
    return {"groups": out_groups, "moments": out_moments, "off_window": off_window,
            "coverage": [coverage[k] for k in sorted(coverage)], "winning_posts": winning_posts}


# ---------------------------------------------------------------- content patterns
def patterns(store, ev: dict | None = None) -> dict:
    """Per language (and pooled), which labels are over-represented among winning moments."""
    ev = ev or evaluate(store)
    decided = []
    not_teaching = {"rejected": 0, "unverified": 0}
    for m in ev["moments"]:
        if m["outcome"] not in ("winner", "not_winner", "promising"):
            continue
        if not m.get("teaches", True):                 # only editorially approved moments teach a pattern
            not_teaching["rejected" if "rejected" in (m.get("editorial") or []) else "unverified"] += 1
            continue
        d = None
        for cid in m["clip_ids"]:
            d = descriptor(store, cid)
            if d:
                break
        row = {"moment_id": m["moment_id"], "creator": m["creator"],
               "labels": set((d or {}).get("labels") or []), "described": d is not None}
        decided.append({**row, "scope": "all", "win": m["outcome"] == "winner"})
        destinations = {}
        for placement in m["platforms"]:
            if placement["verdict"] in DECIDED and placement["language"]:
                destinations.setdefault(placement["language"], []).append(placement["verdict"])
        for language, verdicts in destinations.items():
            decided.append({**row, "scope": language, "win": verdicts.count("winner") * 2 > len(verdicts)})
    scopes = {}
    for scope in sorted({"all"} | {x["scope"] for x in decided}):
        xs = [x for x in decided if x["scope"] == scope]
        desc = [x for x in xs if x["described"]]
        wins = [x for x in desc if x["win"]]
        base = len(wins) / len(desc) if desc else 0.0
        labels = []
        for lab in sorted({lab for x in desc for lab in x["labels"]}):
            have = [x for x in desc if lab in x["labels"]]
            hw = [x for x in have if x["win"]]
            rate = (len(hw) + SMOOTH_K * base) / (len(have) + SMOOTH_K) if desc else 0.0
            lift = rate / base if base else 0.0
            why = None
            if len(desc) < MIN_DECIDED:
                why = (f"only {len(desc)} decided moment(s) with content descriptors in this scope "
                       f"(need {MIN_DECIDED})")
            elif len(wins) < 2:
                why = "fewer than 2 winning moments in this scope"
            elif len(have) < MIN_LABEL_MOMENTS:
                why = f"only {len(have)} decided moment(s) carry it (need {MIN_LABEL_MOMENTS})"
            elif len(hw) < 2 or len({x["creator"] for x in hw}) < 2:
                why = "its wins come from fewer than 2 moments of 2 different creators"
            elif lift < MIN_LIFT:
                why = f"no clear lift ({lift:.2f}x the scope's win rate)"
            labels.append({"label": lab, "meaning": LABEL_TEXT.get(lab, lab), "moments": len(have),
                           "winning_moments": len(hw), "winning_creators": len({x["creator"] for x in hw}),
                           "smoothed_win_rate": round(rate, 3), "lift": round(lift, 2),
                           "evidenced": why is None, "why_not": why})
        labels.sort(key=lambda x: (not x["evidenced"], -x["lift"], x["label"]))
        scopes[scope] = {"decided_moments": len(xs), "described": len(desc),
                         "winning_moments": sum(x["win"] for x in xs), "described_winners": len(wins),
                         "base_win_rate": round(base, 3), "labels": labels,
                         "undescribed": len(xs) - len(desc)}
    return {"scopes": scopes, "not_teaching": not_teaching,
            "caution": "associations in a small sample of the account's own posts, not causes; a label is "
            "used for ranking only when evidenced, and only as a bounded nudge. Only editorially approved "
            "moments teach: "
            f"{not_teaching['rejected']} decided moment(s) were editorially rejected and "
            f"{not_teaching['unverified']} have no editorial verdict (e.g. posted before the check existed)"}


# ---------------------------------------------------------------- preference in candidate selection
_CACHE: dict = {}


def _post_signature(store) -> str:
    """Digest of the post data the model is computed from (anchors and readings, as the store reports
    them)."""
    anc = anchors(store)
    raw = json.dumps([[[list(k), v] for k, v in sorted(anc.items(), key=lambda kv: repr(kv[0]))],
                      readings(store, anc)], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()


def _model(store) -> dict:
    sig = _post_signature(store)
    sig2 = store.q1("SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), 0) AS t FROM clip_descriptors")
    key = (id(store), sig, sig2["n"], sig2["t"])
    hit = _CACHE.get(id(store))
    if hit and hit[0] == key and time.time() - hit[2] < 600:
        return hit[1]
    ev = evaluate(store)
    pat = patterns(store, ev)
    posted = {r["clip_id"] for r in store.q("SELECT clip_id FROM deliveries d LEFT JOIN destinations t "
                                             "ON t.id=d.destination_id WHERE d.status NOT IN "
                                             "('failed','cancelled') AND COALESCE(t.provider,'')<>'sandbox'")}
    posted_moments = {_moment_of(store, c)[0] for c in posted}
    m = {"patterns": pat, "posted_moments": posted_moments, "evaluation": ev}
    _CACHE[id(store)] = (key, m, time.time())
    return m


def exploring(now: float) -> bool:
    return int(now // 3600) % EXPLORE_EVERY == EXPLORE_EVERY - 1


def factor(store, settings, clip: dict, now: float | None = None, prefer: bool = True) -> tuple[float, list]:
    """(multiplier >= 1, reasons) for a candidate clip. Read-only: uses the stored descriptor (written by
    housekeeping), never computes or writes one."""
    now = now or time.time()
    if not prefer or exploring(now):
        return 1.0, ["exploration: no pattern preference in this hour" if prefer else
                     "pattern preference off"]
    try:
        model = _model(store)
    except Exception as e:                              # a broken report never blocks the queue
        return 1.0, [f"pattern model unavailable ({type(e).__name__})"]
    if _moment_of(store, clip["id"])[0] in model["posted_moments"]:
        return 1.0, ["this moment was already posted: never favored again"]
    d = descriptor(store, clip["id"])
    if not d:
        return 1.0, ["no content descriptor yet"]
    from .candidates import original_language
    lang = original_language(clip) or d["language"]
    scopes = model["patterns"]["scopes"]
    # A language with no qualifying evidence stays neutral. Pooled results are descriptive only.
    scope = lang if lang and lang != "all" else ""
    ev = [x for x in (scopes.get(scope) or {}).get("labels", [])
          if x["evidenced"] and x["label"] in d["labels"]]
    if not ev:
        return 1.0, ["shares no pattern evidenced by verified winners"]
    boost = min(MAX_BOOST, sum((x["lift"] - 1.0) * BOOST_WEIGHT for x in ev))
    return round(1.0 + boost, 4), [f"{x['meaning']} ({x['winning_moments']} of {x['moments']} decided "
                                   f"moments were verified winners, {x['lift']:.2f}x, {scope})" for x in ev]
