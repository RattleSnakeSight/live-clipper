"""Selection formats and rules: storage, validation, precedence and the effect of effective values on
decisions."""
from __future__ import annotations

import copy
import json

import pytest

from live_clipper import candidates, editorial, formats, gameplay, titles
from live_clipper.db import Database
from live_clipper.detector import DetectorConfig, SpikeDetector
from live_clipper.events import ChatEvent

T0 = 1_800_000_000.0


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "select.sqlite3")
    d.migrate()
    formats.seed(d)
    yield d
    formats.set_active(None)


def clip_at(act_rel=18.0, topic=None, score=2.2, sample=("lmao", "kekw", "no way", "lol")):
    """A cut [act-18, act+12] like the engine makes, with a chat-spike trigger."""
    act = T0 + 96
    ws = act - act_rel
    trig = {"kind": "chat_spike", "score": score, "unique_users": 16, "reason": "Chat spike",
            "action_time": act,
            "sample_messages": list(sample), "topic": topic or {"state": "live_gta", "title": "GTA 6 day 3"}}
    return {"id": 1, "channel_id": 1, "status": "ready", "kind": "chat_spike", "action_time": act,
            "window_start": ws, "window_end": ws + 30, "created_at": act,
            "files": {"source": "source.ts", "source_t0": ws, "trim_in": 0.0,
                      "duration": 30.0}, "trigger": trig}


GAMEPLAY = {"frames": 8, "pairs": 7, "gameplay_pairs": 7, "static_pairs": 0, "face_dominant_frames": 0,
            "gameplay_ratio": 1.0, "static_ratio": 0.0, "face_dominant_ratio": 0.0, "first_gameplay_t": 3.8,
            "camera_view_t": None}


def verdict(clip, fmt=None, visual=GAMEPLAY, transcript=None):
    sp = editorial.speech_evidence(transcript, fmt)
    ch = editorial.chat_evidence(clip["trigger"], fmt)
    return editorial.decide(clip, visual, sp, ch, window=editorial.window_of(clip), now=T0 + 3600, fmt=fmt)


# ---------------------------------------------------------------- storage
def test_builtin_is_seeded_once_and_customer_edits_survive_reseeding(db):
    assert [f["id"] for f in formats.list_formats(db)] == ["gta-vi"]
    doc = formats.get_format(db, "gta-vi")
    assert doc == json.loads(json.dumps(formats.builtin_doc("gta-vi")))
    formats.update_format(db, "gta-vi", {"scoring": {"min_score": 2.0}})
    assert formats.seed(db) == []                                       # never overwrites customer edits
    assert formats.get_format(db, "gta-vi")["scoring"]["min_score"] == 2.0
    row = formats.list_formats(db)[0]
    assert row["builtin"] and row["modified"] and row["active"]
    formats.reset_format(db, "gta-vi")                                  # explicit reset restores the defaults
    assert formats.get_format(db, "gta-vi")["scoring"]["min_score"] == 3.0
    assert not formats.list_formats(db)[0]["modified"]


def test_crud_duplicate_and_active(db):
    dup = formats.duplicate_format(db, "gta-vi")
    assert dup["id"] == "gta-vi-copy" and dup["name"] == "GTA VI (copy)"
    assert formats.duplicate_format(db, "gta-vi")["id"] == "gta-vi-copy-2"
    formats.set_active_id(db, "gta-vi-copy")
    assert formats.active_id(db) == "gta-vi-copy"
    formats.delete_format(db, "gta-vi-copy")                            # deleting the active one falls back
    assert formats.active_id(db) == "gta-vi"
    with pytest.raises(formats.Conflict, match="built in"):
        formats.delete_format(db, "gta-vi")
    with pytest.raises(formats.NotFound):
        formats.get_format(db, "nope")
    with pytest.raises(formats.Conflict, match="only built-in"):
        formats.reset_format(db, "gta-vi-copy-2")


# ---------------------------------------------------------------- custom format round trip changes decisions
def test_export_modify_import_changes_decisions(db):
    exported = json.dumps(formats.export_format(db, "gta-vi"))
    custom = json.loads(exported)
    custom.update(id="gta-vi-tight-cuts", name="GTA VI, tight cuts")
    custom["editorial"]["min_setup_s"] = 0.5                            # accept a trigger 1 s into the cut
    custom["scoring"]["min_score"] = 2.0  # live first-crossing scores (2.2) pass
    formats.import_format(db, json.dumps(custom).encode())
    edge = clip_at(act_rel=1.0)
    builtin = formats.effective_format(formats.resolve_effective(db, 1))
    assert verdict(edge, builtin)["state"] == "rejected"
    assert candidates.below_threshold(edge, builtin.scoring.min_score)
    eff = formats.resolve_effective(db, 1, {"format_id": "gta-vi-tight-cuts"})
    fmt = formats.effective_format(eff)
    assert verdict(edge, fmt)["state"] == "qualified"
    assert candidates.below_threshold(edge, eff["values"]["scoring.min_score"]) is None
    assert eff["trail"]["editorial.min_setup_s"]["set_by"] == "format"
    assert eff["trail"]["editorial.min_setup_s"]["source"] == "gta-vi-tight-cuts"
    assert eff["trail"]["detector.window"]["set_by"] == "builtin"
    with pytest.raises(formats.Conflict, match="overwrite"):
        formats.import_format(db, json.dumps(custom))
    custom["scoring"]["min_score"] = 2.5
    assert formats.import_format(db, json.dumps(custom), overwrite=True)["scoring"]["min_score"] == 2.5


def test_partial_import_fills_from_builtin(db):
    doc = formats.import_format(db, {"id": "lenient", "name": "Lenient", "scoring": {"min_score": 0}})
    assert doc["scoring"] == {"fresh_half_life_h": 12.0, "neutral_reaction": 1.0, "min_score": 0.0}
    assert doc["keywords"] == formats.builtin_doc()["keywords"]


# ---------------------------------------------------------------- a completely different game, no code
# changes
KART = {
    "id": "kart-racing", "name": "Kart racing", "version": 1, "description": "Synthetic kart racing format",
    "labels": {"game": "Kart Racer", "game_short": "kart", "game_title": "Kart Racer"},
    "match": {"platforms": [], "title_regex": r"(?i)\bkart\s*racer\b", "category_regex": r"(?i)\bkart",
              "category_evidence": True, "gate_on_title": True},
    "topic_states": {"title": "live_on_topic", "category": "live_on_topic_category",
                     "archive": "archive_on_topic",
                     "off_topic": "live_off_topic", "unverified": "unverified"},
    "evidence_keys": {"vod_metadata": "game_metadata", "mentions": "game_mentions"},
    "detector": {"sensitivity": "chill", "presets": {"chill": [4.0, 10, 12.0], "medium": [2.2, 5, 6.0]},
                 "per_user_cap": 2, "window": 10.0, "reaction_delay": 2.0, "baseline_seconds": 300.0,
                 "baseline_floor": 3.0, "merge_gap": 8.0, "distinct_gap": 15.0, "warmup": 20.0,
                 "max_burst": 90.0,
                 "ratio": None, "min_unique": None, "min_abs": None},
    "window": {"pre": 10.0, "post": 8.0},
    "keywords": {"strong": r"\b(kart racer|blue shell|rainbow road)\b",
                 "game_terms": r"\b(lap|drift|shell)\b",
                 "greet_speech": r"\b(hello|welcome)\b", "greet_chat": r"^(hi|hello)\b",
                 "react_chat": r"\b(lol|omg|no way)\b"},
    "language": {"prefs": ["auto", "en"], "names": {"en": "English"}, "min_window_prob": 0.6},
    "titles": {
        "max_len": 60, "separator": " | ", "phrase_max": 40, "languages": ["en"],
        "actions": [{"key": "shell", "priority": 9, "regex": r"\bblue shell\b"},
                    {"key": "drift", "priority": 8, "regex": r"\bdrift(?:ed|s|ing)?\b"}],
        "context_actions": [], "context_rules": [{"stars": True, "text": "laps"}],
        "stars_regex": r"\b(\d)[- ]laps? left\b", "stars_fallback_regex": r"(\d)(?!)",
        "numbers": {}, "vehicles": {"bolt": "Bolt kart"},
        "places": [{"regex": r"rainbow road", "names": {"en": "Rainbow Road"}}], "place_exclusions": [],
        "templates": {"en": {"shell": ["{S} gets shelled at {O}", "{S} gets shelled", "Blue shell at {O}",
                                       "Blue shell out of nowhere"],
                             "drift": ["{S} drifts past {O}", "{S} nails the drift", "Perfect drift at {O}",
                                       "Perfect drift"]}},
        "context_text": {"en": {"laps": "with {n} laps left"}},
        "reactions": [{"key": "laugh", "patterns": [r"(?i)\blol\b"], "text": {"en": "chat loses it"}}],
        "reaction_min": 2, "reaction_divisor": 8, "stop_words": ["the", "a"],
        "fallback": {"en": {"clip": "Clip from the race", "moment_suffix": " moment", "at": "at "}}},
    "posting": {"caption_template": "{title} #kart #racing", "hashtags": ["#kart", "#racing"]},
}


def test_completely_replaced_kart_format_works_without_code_changes(db):
    formats.create_format(db, KART)
    formats.set_active_id(db, "kart-racing")
    fmt = formats.effective_format(formats.resolve_effective(db, "streamer_alpha"))
    assert fmt.id == "kart-racing"
    # topic matching and game context come from the format
    assert formats.topic_state({"state": "live", "title": "Kart Racer cup", "category": ""}, fmt=fmt) == \
        "live_on_topic"
    assert formats.topic_state({"state": "live", "title": "GTA 6 day 3", "category": "GTA VI"}, fmt=fmt) == \
        "live_off_topic"
    on = clip_at(topic={"state": "live_on_topic", "title": "Kart Racer cup"})
    assert gameplay.context(on, fmt)["detail"] == "live Kart Racer title at trigger time ('Kart Racer cup')"
    v = verdict(on, fmt)
    assert v["state"] == "qualified" and v["reason"] == \
        "chat reaction over Kart Racer gameplay (capture-time Kart Racer title)"
    # a GTA title is not evidence for the kart format; GTA talk is not a kart statement, kart talk is
    gta = clip_at(topic={"state": "live_gta", "title": "GTA 6"})
    assert verdict(gta, fmt)["state"] == "unknown"
    talk = {"segments": [{"start": 10.0, "end": 20.0,
                          "text": "that blue shell on rainbow road was the most unfair "
                                                              "thing I have seen in this whole cup "
                                                              "today honestly"}]}
    sp = editorial.speech_evidence(talk, fmt)
    assert sp["game_mentions"][0]["term"] == "blue shell" and sp["statement"]
    assert verdict(gta, fmt, visual=None, transcript=talk)["kind"] == "opinion"
    gta_talk = {"segments": [{"start": 10.0, "end": 20.0, "text": "GTA 6 Vice City Leonida Rockstar " * 4}]}
    assert editorial.speech_evidence(gta_talk, fmt)["game_mentions"] == []
    # titles use the kart lexicon
    t = titles.make_title({"segments": [{"text": "I drifted the bolt past rainbow road"}]}, ["lol", "lol"],
                          "",
                          fmt=fmt)
    assert t == "Bolt kart drifts past Rainbow Road | chat loses it"
    assert titles.make_title({"segments": []}, [], "", lang="es", fmt=fmt) == "Clip from the race"
    # detector presets and window come from the format
    cfg = DetectorConfig.from_format(fmt)
    assert cfg.sensitivity == "chill" and cfg.params() == (4.0, 10, 12.0) and cfg.reaction_delay == 2.0
    det = SpikeDetector(1, cfg)
    for i in range(300):
        det.feed(ChatEvent("replay", "c", f"m{i}", f"u{i % 14}", "u", "hi", received_at=T0 + i + 0.2,
                           sent_at=T0 + i))
    assert det.threshold(det.baseline(det.now)) == 40.0              # chill: max(10 x 4.0, 10 + 12.0)
    # language policy: English only
    from live_clipper import language_policy
    asr = {"speech_status": "speech", "language": "es", "original_check": {"status": "pass"}}
    assert language_policy.decide(asr, "auto", fmt)["status"] == "hold"


# ---------------------------------------------------------------- rules and precedence
def rule(rid, **kw):
    return {"id": rid, "name": rid.replace("-", " "), **kw}


def test_rules_apply_in_order_and_per_clip_override_wins(db):
    formats.create_rule(db, rule("kick-strict", conditions={"platforms": ["kick"]},
                                 actions={"threshold": 5.0}))
    formats.create_rule(db, rule("beta-long", streamers={"channels": ["streamer_beta"]},
                                 actions={"threshold": 7.0, "window_pre": 25.0, "preset_id": "wide"}))
    ctx = {"platform": "kick", "title": "GTA 6 day 3", "language": "en"}
    eff = formats.resolve_effective(db, "streamer_beta", ctx)
    assert eff["values"]["scoring.min_score"] == 7.0 and eff["values"]["window.pre"] == 25.0
    layers = [s["source"] for s in eff["trail"]["scoring.min_score"]["layers"]]
    assert layers == ["gta-vi", "kick-strict", "beta-long"]
    assert eff["trail"]["output.preset_id"]["set_by"] == "rule"
    assert [m["id"] for m in eff["matched_rules"]] == ["kick-strict", "beta-long"]
    formats.reorder_rules(db, ["beta-long", "kick-strict"])              # later rules overwrite earlier ones
    eff = formats.resolve_effective(db, "streamer_beta", ctx)
    assert eff["values"]["scoring.min_score"] == 5.0 and eff["values"]["window.pre"] == 25.0
    eff = formats.resolve_effective(db, "streamer_beta", ctx, {"override": {"scoring.min_score": 1.0,
                                                                            "window.post": 20.0}})
    assert eff["values"]["scoring.min_score"] == 1.0 and eff["trail"]["window.post"]["set_by"] == "override"
    assert formats.effective_format(eff).window.post == 20.0
    other = formats.resolve_effective(db, "streamer_alpha", {"platform": "twitch"})
    assert other["values"]["scoring.min_score"] == 3.0 and other["matched_rules"] == []
    assert {s["id"] for s in other["skipped_rules"]} == {"kick-strict", "beta-long"}


@pytest.mark.parametrize("cond,channel,context,clip,match", [
    ({"conditions": {"categories": ["grand theft"]}}, "a", {"category": "Grand Theft Auto VI"}, {}, True),
    ({"conditions": {"categories": ["minecraft"]}}, "a", {"category": "Grand Theft Auto VI"}, {}, False),
    ({"conditions": {"title_regex": r"\brp\b"}}, "a", {"title": "GTA 6 RP night"}, {}, True),
    ({"conditions": {"languages": ["es"]}}, "a", {}, {"language": "es"}, True),
    ({"conditions": {"languages": ["es"]}}, "a", {"language": "en"}, {}, False),
    ({"scenario": {"trigger_types": ["manual"]}}, "a", {}, {"kind": "manual"}, True),
    ({"scenario": {"trigger_types": ["manual"]}}, "a", {}, {"trigger_type": "chat_spike"}, False),
    ({"scenario": {"evidence": ["speech", "gameplay"]}}, "a", {}, {"evidence": ["gameplay", "speech",
                                                                                "chat"]}, True),
    ({"scenario": {"evidence": ["speech", "gameplay"]}}, "a", {}, {"evidence": ["gameplay"]}, False),
    ({"duration": {"min_s": 20, "max_s": 40}}, "a", {}, {"duration_s": 30}, True),
    ({"duration": {"min_s": 20, "max_s": 40}}, "a", {}, {"duration_s": 45}, False),
    ({"duration": {"max_s": 40}}, "a", {}, {}, False),
    ({"streamers": {"channels": ["a", "b"], "mode": "all"}}, "a", {"channels": ["a", "b", "c"]}, {}, True),
    ({"streamers": {"channels": ["a", "b"], "mode": "all"}}, "a", {"channels": ["a"]}, {}, False),
    ({"streamers": {"channels": ["b", "z"], "mode": "any"}}, "a", {"channels": ["a", "b"]}, {}, True),
    ({"streamers": {"simulcast_group": "grp1"}}, "a", {"simulcast_group": "grp1"}, {}, True),
    ({"streamers": {"min_count": 2}}, "a", {}, {}, False),
    ({"streamers": {"min_count": 2, "max_count": 3}}, "a", {"channels": ["a", "b"]}, {}, True),
    ({"enabled": False}, "a", {}, {}, False),
    ({"format_id": "gta-vi"}, "a", {}, {}, True),
])
def test_rule_conditions(db, cond, channel, context, clip, match):
    formats.create_rule(db, rule("r1", actions={"threshold": 9.0}, **cond))
    eff = formats.resolve_effective(db, channel, context, clip)
    assert (eff["values"]["scoring.min_score"] == 9.0) is match


def test_rule_limited_to_another_format_is_skipped(db):
    formats.create_format(db, {**copy.deepcopy(KART)})
    formats.create_rule(db, rule("kart-only", format_id="kart-racing", actions={"threshold": 9.0}))
    eff = formats.resolve_effective(db, "a")
    assert eff["values"]["scoring.min_score"] == 3.0
    assert eff["skipped_rules"][0]["why"] == "limited to format 'kart-racing'"
    assert formats.resolve_effective(db, "a",
                                     {"format_id": "kart-racing"})["values"]["scoring.min_score"] == 9.0
    with pytest.raises(formats.Conflict, match="kart-only"):
        formats.delete_format(db, "kart-racing")


@pytest.mark.parametrize("decision,state", [("skip", "rejected"), ("hold", "unknown"), ("force_qualify",
                                                                                        "approved")])
def test_rule_decisions_override_gameplay_evidence(db, decision, state):
    formats.create_rule(db, rule("d", conditions={"platforms": ["twitch"]}, actions={"decision": decision}))
    eff = formats.resolve_effective(db, "a", {"platform": "twitch"})
    clip = clip_at(act_rel=1.0)  # editorially rejected (trigger at the edge)
    clip["trigger"]["editorial"] = verdict(clip)
    ev = gameplay.evidence(clip)
    assert ev["state"] == "rejected"
    out = formats.apply_decision(ev, eff)
    assert out["state"] == state and out["source"] == "rule" and "was rejected" in out["detail"]
    assert formats.apply_decision(ev, formats.resolve_effective(db, "a", {"platform": "kick"})) == ev


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("patch,loc,msg", [
    ({"keywords": {"strong": "(unclosed"}}, "keywords.strong", "invalid regular expression"),
    ({"scoring": {"min_score": -1}}, "scoring.min_score", "greater than or equal"),
    ({"window": {"pre": "long"}}, "window.pre", "valid number"),
    ({"surprise": 1}, "surprise", "Extra inputs"),
    ({"detector": {"sensitivity": "extreme"}}, "detector", "not one of the presets"),
    ({"id": "Bad Id!"}, "id", "pattern"),
    ({"language": {"prefs": ["en"]}}, "language", "must include 'auto'"),
])
def test_validation_errors_are_located_and_readable(db, patch, loc, msg):
    data = {"id": "x1", "name": "x", **patch}
    with pytest.raises(formats.FormatError) as e:
        formats.create_format(db, data)
    errs = e.value.errors
    assert any(x["loc"] == loc and msg in x["msg"] for x in errs), errs
    assert e.value.message.startswith("invalid selection format")


def test_title_templates_must_cover_every_action(db):
    bad = copy.deepcopy(KART)
    del bad["titles"]["templates"]["en"]["drift"]
    with pytest.raises(formats.FormatError, match="lacks actions"):
        formats.create_format(db, bad)


def test_import_errors(db):
    with pytest.raises(formats.FormatError, match="line 1, column"):
        formats.import_format(db, "{not json")
    with pytest.raises(formats.FormatError, match="one JSON object"):
        formats.import_format(db, "[1, 2]")
    with pytest.raises(formats.FormatError, match="no id"):
        formats.import_format(db, {"name": "x"})
    with pytest.raises(formats.FormatError, match="not UTF-8"):
        formats.import_format(db, b"\xff\xfe\x00")


def test_rule_validation(db):
    with pytest.raises(formats.FormatError) as e:
        formats.create_rule(db, {"id": "r", "name": "r", "actions": {"window_pre": 500, "decision": "maybe"}})
    assert {x["loc"] for x in e.value.errors} == {"actions.window_pre", "actions.decision"}
    with pytest.raises(formats.FormatError, match="unknown format"):
        formats.create_rule(db, rule("r", format_id="missing"))
    formats.create_rule(db, rule("r"))
    with pytest.raises(formats.Conflict):
        formats.create_rule(db, rule("r"))
    with pytest.raises(formats.FormatError, match="exactly once"):
        formats.reorder_rules(db, ["r", "r"])
    with pytest.raises(formats.FormatError, match="unknown override"):
        formats.resolve_effective(db, "a", clip={"override": {"no.such": 1}})
    with pytest.raises(formats.FormatError, match="window.pre"):
        formats.resolve_effective(db, "a", clip={"override": {"window.pre": 999}})


def test_partial_update_patches_the_stored_format_not_the_builtin(db):
    formats.create_format(db, copy.deepcopy(KART))
    doc = formats.update_format(db, "kart-racing", {"scoring": {"min_score": 1.0}})
    assert doc["scoring"]["min_score"] == 1.0
    assert doc["labels"]["game"] == "Kart Racer" and doc["window"] == {"pre": 10.0, "post": 8.0}
    assert list(doc["titles"]["vehicles"]) == ["bolt"]                   # lexicon tables are never mixed
    replaced = formats.import_format(db, {"id": "kart-racing", "name": "Kart, reset"}, overwrite=True)
    assert replaced["labels"]["game"] == "GTA VI"  # an import replaces the whole document
