"""Selection formats and rules: everything game- or format-specific about picking moments, as data.

A selection format (SelectionFormat) holds the keywords, topic matching, thresholds, sensitivity, clip window,
scoring, title lexicon/templates, hashtags and language policy that the generic selection core reads
(detector, editorial, gameplay, candidates, titles, language_policy). The shipped default is
`formats/gta-vi.json`; with it loaded every decision equals the original GTA VI implementation.

Rules (Rule) adjust the effective values per moment: conditions on context (platform, category, title,
language), scenario (trigger type, evidence present), duration and the streamers involved; actions
skip/hold/force-qualify or set the threshold, clip window, preset, title template, caption style,
destinations.

Precedence, lowest to highest (docs/CONTRACTS.md): built-in format < the customer's format (key by key) <
rules
in list order < per-clip override. resolve_effective() returns the effective values plus a trail naming the
layer
that set each value.

Storage: SQLite namespace `select` (select_formats, select_rules). Built-in formats are seeded on first run
and
never overwritten afterwards; reset_format() restores one explicitly.
"""
from __future__ import annotations

import copy
import functools
import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .db import register_migrations

BUILTIN_DIR = Path(__file__).resolve().parent / "formats"
DEFAULT_ID = "gta-vi"
ACTIVE_KEY = "select.active_format"
ID_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"

register_migrations("select", ["""
CREATE TABLE IF NOT EXISTS select_formats(
    id TEXT PRIMARY KEY, data_json TEXT NOT NULL, builtin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS select_rules(
    id TEXT PRIMARY KEY, position INTEGER NOT NULL, data_json TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""])


# ====================================================================== models
class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_regex(v: str | None) -> str | None:
    if v is not None:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regular expression: {e}") from None
    return v


class Labels(_M):
    game: str = Field(min_length=1,
        description="Game name as written in reasons and messages, for example 'GTA VI'.")
    game_short: str = Field(min_length=1, description="Short game name, for example 'GTA'.")
    game_title: str = Field(
        min_length=1,
            description="Game name the way streamers write it in stream titles, for example 'GTA 6'.")


class Match(_M):
    platforms: list[str] = Field(
        [],
            description=(
                "Platforms this format applies to, one per line (twitch, kick, youtube). Empty means all."))
    title_regex: str = Field(
        description=(
            "Pattern (regular expression, any case) that the stream title must match to count as this game."))
    category_regex: str = Field(
        description=(
            "Pattern (regular expression, any case) that the stream category must match to count as this "
            "game."))
    category_evidence: bool = Field(
        True, description="Treat a matching platform category as proof that the game is being played.")
    gate_on_title: bool = Field(
        True,
            description=(
                "For channels that only record on topic, start only while the title matches, not just the "
                "category."))

    _rx = field_validator("title_regex", "category_regex")(_check_regex)


class TopicStates(_M):
    title: str = Field(description="Status name used when the stream title matches the game.")
    category: str = Field(description="Status name used when only the platform category matches the game.")
    archive: str = Field(description="Status name used when a past broadcast's saved details match the game.")
    off_topic: str = Field(description="Status name used when the stream is about something else.")
    unverified: str = Field("unverified",
        description="Status name used when the stream topic cannot be checked yet.")


class EvidenceKeys(_M):
    vod_metadata: str = Field(
        description="Name under which saved broadcast details are recorded as evidence.")
    mentions: str = Field(description="Name under which game mentions in speech are recorded as evidence.")


class Detector(_M):
    sensitivity: str = Field(
        "medium",
            description=(
                "Which preset below decides how big a chat spike must be. Channels can pick their own."))
    presets: dict[str, tuple[float, int, float]] = Field(
        description=(
            "Spike limits per sensitivity level: how many times above normal chat, how many different "
            "chatters, and the least messages per 10 seconds."))
    per_user_cap: int = Field(ge=1, description="Most messages from one chatter that count towards a spike.")
    window: float = Field(gt=0,
        description="Seconds of recent chat looked at when measuring the current pace.")
    reaction_delay: float = Field(
        ge=0,
            description=(
                "Seconds chat usually lags behind what happened on stream. The clip is centred earlier by "
                "this much."))
    baseline_seconds: float = Field(gt=0,
        description="Seconds of chat history used to learn the channel's normal pace.")
    baseline_floor: float = Field(
        ge=0,
            description=(
                "Lowest normal pace assumed, in messages per 10 seconds, so quiet channels do not fire on "
                "noise."))
    merge_gap: float = Field(
        ge=0,
            description=(
                "Seconds of calm chat before a spike counts as over. A spike that restarts sooner is the "
                "same moment."))
    distinct_gap: float = Field(
        ge=0, description="Seconds after a moment began before a new spike counts as a separate moment.")
    warmup: float = Field(ge=0,
        description="Seconds of chat to collect after joining before any moment can trigger.")
    max_burst: float = Field(
        gt=0,
            description=(
                "A spike lasting longer than this many seconds is treated as the channel's new normal, not "
                "a moment."))
    ratio: float | None = Field(
        None,
            description=(
                "Overrides the 'times above normal' limit of the sensitivity preset. Empty keeps the "
                "preset."))
    min_unique: int | None = Field(
        None,
            description=(
                "Overrides the 'different chatters' limit of the sensitivity preset. Empty keeps the "
                "preset."))
    min_abs: float | None = Field(
        None,
            description=(
                "Overrides the 'least messages per 10 seconds' limit of the sensitivity preset. Empty keeps "
                "the preset."))

    @model_validator(mode="after")
    def _known_sensitivity(self):
        if self.sensitivity not in self.presets:
            raise ValueError(f"sensitivity '{self.sensitivity}' is not one of the presets "
                             f"{sorted(self.presets)}")
        return self


class Window(_M):
    pre: float = Field(ge=0, le=120, description="Seconds of video kept before the moment.")
    post: float = Field(ge=0, le=120, description="Seconds of video kept after the moment.")


class Keywords(_M):
    strong: str = Field(
        description="Pattern for spoken words that clearly name the game. Matched on plain lower-case text.")
    game_terms: str = Field(
        description="Pattern for game vocabulary that hints at gameplay but does not prove it.")
    greet_speech: str = Field(
        description="Pattern for greetings and housekeeping talk in speech, which count against a clip.")
    greet_chat: str = Field(
        description="Pattern for chat messages that welcome arriving viewers, which count against a clip.")
    react_chat: str = Field(
        description=(
            "Pattern for chat messages that react to something that happened, which count for a clip."))

    _rx = field_validator("strong", "game_terms", "greet_speech", "greet_chat", "react_chat")(_check_regex)


class Visual(_M):
    cols: int = Field(ge=1,
        description="Number of columns the frame is split into when comparing two frames.")
    rows: int = Field(ge=1, description="Number of rows the frame is split into when comparing two frames.")
    flat_std: float = Field(description="Frame cells flatter than this (little detail) are ignored as blank.")
    ncc: float = Field(
        description="How alike two cells must be to count as unchanged. Higher means stricter.")
    active_frac: float = Field(
        description="Share of cells that must change between frames to call it moving gameplay.")
    static_frac: float = Field(
        description="Below this share of changing cells, the picture counts as static.")
    min_usable: float = Field(
        description="Least share of cells with enough detail for a frame pair to be judged.")


class Editorial(_M):
    opening_s: float = Field(
        description="Seconds after a stream starts during which greetings are expected, not gameplay.")
    min_setup_s: float = Field(description="Least seconds of lead-up a clip needs before the moment.")
    min_payoff_s: float = Field(description="Least seconds a clip needs after the moment.")
    gameplay_ratio: float = Field(
        description="Share of compared frames that must show moving gameplay to confirm gameplay.")
    camera_view_face_h: float = Field(
        description=(
            "A face taller than this share of the frame means the stream shows the camera, not the game."))
    opinion_words: int = Field(
        description="Words spoken near the moment that make it a statement worth clipping.")
    opinion_span_s: float = Field(description="Seconds around the moment in which spoken words are counted.")
    statement_max_greeting: float = Field(
        description="Most greeting share (0 to 1) of that speech for it to still count as a statement.")
    surge_min_messages: int = Field(
        description="Chat messages needed in a spike before it can be called a greeting surge.")
    surge_greeting_share: float = Field(
        description="Share of greetings (0 to 1) in a spike that makes it a greeting surge.")
    surge_max_reaction_share: float = Field(
        description="Share of reactions (0 to 1) below which a greeting-heavy spike is a surge.")
    reaction_share: float = Field(
        description="Share of reaction messages (0 to 1) needed for chat to count as reacting.")
    speech_greeting_share: float = Field(
        description="Share of greeting words (0 to 1) that makes the speech count as greeting talk.")
    face_dominant_ratio: float = Field(
        description="Share of frames dominated by a face that marks the clip as a camera view.")
    static_ratio: float = Field(
        description="Share of static frame pairs that marks the clip as a still picture.")
    min_pairs: int = Field(description="Least number of frame pairs needed before gameplay can be confirmed.")
    visual: Visual = Field(
        description="How frames are compared to tell moving gameplay from a still or camera picture.")


class Scoring(_M):
    fresh_half_life_h: float = Field(
        gt=0, description="Hours after which a moment counts half as much when ranking, so newer clips win.")
    neutral_reaction: float = Field(
        ge=0, description="Score given to a clip with no chat measurement. 1 means chat at its normal level.")
    min_score: float = Field(ge=0,
        description="Auto-post threshold: moments scoring below this chat spike ratio are not posted.")


class LanguagePolicy(_M):
    prefs: list[str] = Field(
        description=(
            "Caption languages to try, in order, one per line. Must include 'auto' (use the detected "
            "language)."))
    names: dict[str, str] = Field(description="Display name for each language code, for example en: English.")
    min_window_prob: float = Field(
        ge=0, le=1, description="How sure speech detection must be (0 to 1) before its language is trusted.")

    @model_validator(mode="after")
    def _auto(self):
        if "auto" not in self.prefs:
            raise ValueError("language.prefs must include 'auto'")
        bad = [p for p in self.prefs if p != "auto" and p not in self.names]
        if bad:
            raise ValueError(f"language.prefs {bad} have no entry in language.names")
        return self


class TitleAction(_M):
    key: str = Field(description="Short name of this kind of moment, used to pick a title template.")
    priority: int = Field(description="When several kinds match, the one with the highest number wins.")
    regex: str = Field(
        description="Pattern (regular expression) that chat or speech must match for this kind of moment.")
    _rx = field_validator("regex")(_check_regex)


class ContextRule(_M):
    action: str | None = Field(None,
        description="Only add this context for the named kind of moment. Empty means any.")
    stars: bool = Field(False, description="Add this context only when a star or wanted level was mentioned.")
    unless_primary: list[str] = Field([],
        description="Skip this context when the main kind of moment is one of these.")
    text: str = Field(description="Name of the context phrase to add, as listed under context text.")


class Place(_M):
    regex: str = Field(description="Pattern (regular expression) that finds this place in chat or speech.")
    names: dict[str, str] = Field(description="How the place is named in each title language.")
    _rx = field_validator("regex")(_check_regex)


class PlaceExclusion(_M):
    vehicles: list[str] = Field(description="Vehicle names this rule applies to.")
    place: str = Field(description="Place name that must not be paired with those vehicles in a title.")


class TitleReaction(_M):
    key: str = Field(description="Short name of this kind of chat reaction.")
    patterns: list[str] = Field(
        description="Patterns (regular expressions) for chat messages that show this reaction.")
    text: dict[str, str] = Field(description="Title wording for this reaction in each title language.")

    @field_validator("patterns")
    @classmethod
    def _rxs(cls, v):
        for p in v:
            _check_regex(p)
        return v


class TitleFallback(_M):
    clip: str = Field(description="Title used when nothing better can be said about the clip.")
    moment_suffix: str = Field(description="Words added after the game name when only the moment is known.")
    at: str = Field(description="Word placed before the time or place in a fallback title.")


class Titles(_M):
    max_len: int = Field(ge=10, description="Longest title allowed, in characters.")
    separator: str = Field(description="Text placed between the parts of a title.")
    phrase_max: int = Field(ge=4,
        description="Longest quoted phrase from chat or speech used in a title, in characters.")
    languages: list[str] = Field(min_length=1, description="Languages to write titles in, one per line.")
    actions: list[TitleAction] = Field(
        description="Kinds of moment a title can describe, with the words that find them.")
    context_actions: list[str] = Field([],
        description="Kinds of moment that only add context and never lead a title.")
    context_rules: list[ContextRule] = Field([],
        description="Extra phrases added to a title when their conditions hold.")
    stars_regex: str = Field(description="Pattern that reads a star or wanted level from text.")
    stars_fallback_regex: str = Field(
        description="Backup pattern for star levels when the first one finds nothing.")
    numbers: dict[str, int] = Field({}, description="Number words understood in text, such as five: 5.")
    vehicles: dict[str, str] = Field({},
        description="Vehicle words found in text and the name used in titles.")
    places: list[Place] = Field([], description="Places that can be named in titles.")
    place_exclusions: list[PlaceExclusion] = Field([],
        description="Vehicle and place pairs that never go together in a title.")
    templates: dict[str, dict[str, tuple[str, str, str, str]]] = Field(
        description=(
            "Title wording per language and kind of moment. Four variants each, so titles do not repeat."))
    context_text: dict[str, dict[str, str]] = Field(
        description="Wording of each context phrase per language, as referenced by the context rules.")
    reactions: list[TitleReaction] = Field([], description="Chat reactions that can be quoted in titles.")
    reaction_min: int = Field(2,
        description="Least number of chat messages that must show a reaction before it is used.")
    reaction_divisor: int = Field(
        8, ge=1, description="A reaction must also come from at least one in this many chat messages.")
    stop_words: list[str] = Field([], description="Words ignored when picking a phrase to quote.")
    fallback: dict[str, TitleFallback] = Field(description="Fallback title wording per language.")

    _rx = field_validator("stars_regex", "stars_fallback_regex")(_check_regex)

    @model_validator(mode="after")
    def _complete(self):
        keys = {a.key for a in self.actions}
        for lang in self.languages:
            for what, table in (("templates", self.templates), ("context_text", self.context_text),
                                ("fallback", self.fallback)):
                if lang not in table:
                    raise ValueError(f"titles.{what} has no '{lang}' entry")
            missing = keys - set(self.templates[lang])
            if missing:
                raise ValueError(f"titles.templates.{lang} lacks actions {sorted(missing)}")
            for r in self.context_rules:
                if r.text not in self.context_text[lang]:
                    raise ValueError(f"titles.context_text.{lang} lacks '{r.text}'")
        return self


class Posting(_M):
    caption_template: str = Field(
        description=(
            "Post description. Placeholders in braces are filled in: {title}, {creator}, {platform} and "
            "{game}."))
    hashtags: list[str] = Field(
        [], description="Hashtags added to every post unless the description already has them, one per line.")


Decision = Literal["skip", "hold", "force_qualify"]


class Output(_M):
    decision: Decision | None = Field(
        None,
            description=(
                "Post, hold for review or skip clips whatever the score says. Empty decides by score."))
    preset_id: str | None = Field(None,
        description="Preset (look) used for the clips. Empty uses the default preset.")
    title_template: str | None = Field(
        None,
            description=(
                "Title pattern, for example {title} | {channel}. Placeholders: {title}, {channel}, {game}, "
                "{platform}. Empty uses the generated title."))
    caption_style: str | None = Field(None,
        description="Legacy file field. Choose an editing preset to set the caption style.")
    destinations: list[str] | None = Field(
        None,
            description=(
                "Limit posting to these destinations, one per line. Empty keeps each channel's own choice."))


class SelectionFormat(_M):
    id: str = Field(pattern=ID_RE,
        description="Fixed ID used in files and rules. Duplicate the format to make a separate copy.")
    name: str = Field(min_length=1, max_length=120, description="The name shown in the app.")
    version: int = Field(1, description="Version number of this file layout. Leave it as it is.")
    description: str = Field("", description="One line about what this format is for.")
    labels: Labels = Field(description="Names for the game used in titles and messages.")
    match: Match = Field(description="When this format applies to a stream.")
    topic_states: TopicStates = Field(description="Status names for how well a stream matches the game.")
    evidence_keys: EvidenceKeys = Field(description="Names under which proof of the game is recorded.")
    detector: Detector = Field(description="How chat activity turns into a moment.")
    window: Window = Field(description="How much video goes around a moment.")
    keywords: Keywords = Field(description="Word patterns that tell game talk from greetings and reactions.")
    editorial: Editorial = Field(description="Checks that decide whether a moment is worth posting.")
    scoring: Scoring = Field(description="How moments are ranked and which ones post automatically.")
    language: LanguagePolicy = Field(
        description="Which spoken languages are expected and how captions pick one.")
    titles: Titles = Field(description="How clip titles are written.")
    posting: Posting = Field(description="Text added to every post.")
    output: Output = Field(Output(),
        description="Default decision, look and destinations for clips of this format.")


class RuleConditions(_M):
    platforms: list[str] = Field([],
        description="Only clips from these platforms, one per line. Empty means any.")
    categories: list[str] = Field(
        [], description="Patterns (regular expressions, any case) for the stream category. One must match.")
    title_regex: str | None = Field(
        None, description="Pattern (regular expression, any case) that the stream title must match.")
    languages: list[str] = Field([],
        description="Only these spoken languages, as codes like en or es. Empty means any.")
    _rx = field_validator("title_regex")(_check_regex)

    @field_validator("categories")
    @classmethod
    def _cats(cls, v):
        for p in v:
            _check_regex(p)
        return v


class RuleScenario(_M):
    trigger_types: list[str] = Field(
        [], description="Only clips found this way: chat_spike, audio_spike or manual. Empty means any.")
    evidence: list[str] = Field(
        [],
            description=(
                "Evidence that must all be present: chat, topic, speech, gameplay, statement or reaction."))


class RuleDuration(_M):
    min_s: float | None = Field(None, ge=0, description="Only clips at least this many seconds long.")
    max_s: float | None = Field(None, ge=0, description="Only clips at most this many seconds long.")


class RuleStreamers(_M):
    channels: list[str] = Field([],
        description="Only clips involving these streamers, one per line. Empty means any.")
    mode: Literal["any", "all"] = Field(
        "any",
            description="For clips with several streamers: any listed streamer matches, or all of them must.")
    simulcast_group: str | None = Field(None,
        description="Only clips from this named group of simulcasting channels.")
    min_count: int | None = Field(None, ge=1,
        description="Only clips with at least this many streamers in them.")
    max_count: int | None = Field(None, ge=1,
        description="Only clips with at most this many streamers in them.")


class RuleActions(_M):
    decision: Decision | None = Field(
        None, description="Post, hold for review or skip matching clips whatever the score says.")
    threshold: float | None = Field(
        None, ge=0, description="Least score a matching clip needs. Empty keeps the format's value.")
    window_pre: float | None = Field(
        None, ge=0, le=120,
            description=(
                "Seconds of video before the moment for matching clips. Empty keeps the format's value."))
    window_post: float | None = Field(
        None, ge=0, le=120,
            description=(
                "Seconds of video after the moment for matching clips. Empty keeps the format's value."))
    preset_id: str | None = Field(None, description="Switch matching clips to this preset (look).")
    title_template: str | None = Field(
        None,
            description=(
                "Title pattern for matching clips, for example {title} | {channel}. Placeholders: {title}, "
                "{channel}, {game}, {platform}. Empty keeps the format's value."))
    caption_style: str | None = Field(None,
        description="Legacy file field. Choose an editing preset to set the caption style.")
    destinations: list[str] | None = Field(
        None,
            description=(
                "Post matching clips only to these destinations, one per line. Empty keeps the channel's "
                "choice."))


class Rule(_M):
    id: str = Field(pattern=ID_RE,
        description="Short unique name of the rule: lower-case letters, digits, dashes.")
    name: str = Field(min_length=1, max_length=120,
        description="A short name so you recognise the rule in the list.")
    enabled: bool = Field(True, description="Turn the rule on or off without deleting it.")
    format_id: str | None = Field(
        None,
            description=(
                "Apply the rule only while this format is in effect. Empty applies it with any format."))
    conditions: RuleConditions = Field(RuleConditions(), description="Which streams the rule applies to.")
    scenario: RuleScenario = Field(RuleScenario(),
        description="How the moment was found and what proof exists for it.")
    duration: RuleDuration = Field(RuleDuration(), description="How long the clip must be.")
    streamers: RuleStreamers = Field(RuleStreamers(), description="Which streamers are in the clip.")
    actions: RuleActions = Field(RuleActions(), description="What the rule changes for the clips it matches.")


# rule action -> effective value key it sets
ACTION_KEYS = {"decision": "output.decision", "threshold": "scoring.min_score", "window_pre": "window.pre",
               "window_post": "window.post", "preset_id": "output.preset_id",
               "title_template": "output.title_template", "caption_style": "output.caption_style",
               "destinations": "output.destinations"}


# ====================================================================== errors
class FormatError(ValueError):
    """Invalid format/rule input. `errors` is a list of {"loc": "dotted.path", "msg": "..."}."""

    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []

    def to_dict(self) -> dict:
        return {"message": self.message, "errors": self.errors}


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


def _errors(e: ValidationError) -> list[dict]:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
        msg = err.get("msg", "invalid")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        out.append({"loc": loc, "msg": msg})
    return out


def validate_format(data: Any) -> SelectionFormat:
    if not isinstance(data, dict):
        raise FormatError("a selection format must be a JSON object")
    try:
        return SelectionFormat.model_validate(data)
    except ValidationError as e:
        errs = _errors(e)
        raise FormatError(f"invalid selection format: {len(errs)} problem(s); first: {errs[0]['loc']}: "
                          f"{errs[0]['msg']}", errs) from None


def validate_rule(data: Any) -> Rule:
    if not isinstance(data, dict):
        raise FormatError("a rule must be a JSON object")
    try:
        return Rule.model_validate(data)
    except ValidationError as e:
        errs = _errors(e)
        raise FormatError(f"invalid rule: {len(errs)} problem(s); first: {errs[0]['loc']}: {errs[0]['msg']}",
                          errs) from None


def parse_json(payload: Any) -> Any:
    """bytes/str -> JSON value with a clear message (line/column) on syntax errors; dicts pass through."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise FormatError("the file is not UTF-8 text") from None
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            raise FormatError(f"not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}",
                              [{"loc": f"line {e.lineno}", "msg": e.msg}]) from None
    return payload


# ====================================================================== built-ins and the runtime default
@functools.cache
def _builtin_text(fid: str) -> str:
    return (BUILTIN_DIR / f"{fid}.json").read_text(encoding="utf-8")


def builtin_ids() -> list[str]:
    return sorted(p.stem for p in BUILTIN_DIR.glob("*.json"))


def builtin_doc(fid: str = DEFAULT_ID) -> dict:
    if fid not in builtin_ids():
        raise NotFound(f"no built-in format '{fid}'")
    return json.loads(_builtin_text(fid))


@functools.cache
def _builtin_model(fid: str) -> SelectionFormat:
    return SelectionFormat.model_validate(json.loads(_builtin_text(fid)))


_ACTIVE: SelectionFormat | None = None


def active() -> SelectionFormat:
    """The format the core uses when a caller passes none (the service keeps it in sync with the database)."""
    return _ACTIVE or _builtin_model(DEFAULT_ID)


def set_active(fmt: SelectionFormat | dict | None) -> None:
    global _ACTIVE
    _ACTIVE = None if fmt is None else (fmt if isinstance(fmt, SelectionFormat) else validate_format(fmt))


@functools.lru_cache(maxsize=512)
def rx(pattern: str) -> re.Pattern:
    """Compiled pattern (cached); flags travel inside the pattern, e.g. '(?i)'."""
    return re.compile(pattern)


def title_matches(title: str | None, fmt: SelectionFormat | None = None) -> bool:
    """The stream title names the format's game (the capture gate uses the title only)."""
    return bool(rx((fmt or active()).match.title_regex).search(title or ""))


def category_matches(category: str | None, fmt: SelectionFormat | None = None) -> bool:
    return bool(rx((fmt or active()).match.category_regex).search(category or ""))


def topic_state(st: dict, category_counts: bool | None = None, fmt: SelectionFormat | None = None) -> str:
    """Evidence state of a live status answer: title match | category match | off topic | unverified. A live
    answer with neither a title nor a category is not evidence of anything: unverified."""
    fmt = fmt or active()
    ts = fmt.topic_states
    if category_counts is None:
        category_counts = fmt.match.category_evidence
    if (st or {}).get("state") != "live":
        return ts.unverified
    if not str(st.get("title") or "").strip() and not str(st.get("category") or "").strip():
        return ts.unverified
    if title_matches(st.get("title"), fmt):
        return ts.title
    if category_counts and category_matches(st.get("category"), fmt):
        return ts.category
    return ts.off_topic


# ====================================================================== merging and flattening
def deep_merge(base: dict, patch: dict, _prefix: str = "") -> dict:
    """Key-by-key merge; lists and the lexicon dicts in _LEAF_DICTS are replaced whole."""
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        key = f"{_prefix}{k}"
        if isinstance(v, dict) and isinstance(out.get(k), dict) and key not in _LEAF_DICTS:
            out[k] = deep_merge(out[k], v, key + ".")
        else:
            out[k] = copy.deepcopy(v)
    return out


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v and key not in _LEAF_DICTS:
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


# dict-valued settings that are replaced as a whole (a customer's lexicon is not merged word by word)
_LEAF_DICTS = {"detector.presets", "language.names", "titles.numbers", "titles.vehicles", "titles.templates",
               "titles.context_text", "titles.fallback"}


def unflatten(flat: dict) -> dict:
    out: dict = {}
    for key, v in flat.items():
        cur = out
        parts = key.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = copy.deepcopy(v)
    return out


def _dump(fmt: SelectionFormat) -> dict:
    return fmt.model_dump(mode="json")


def complete(data: dict, base_id: str = DEFAULT_ID) -> SelectionFormat:
    """A customer format (possibly partial) merged key by key over the built-in default, validated."""
    return validate_format(deep_merge(builtin_doc(base_id), data))


# ====================================================================== storage: formats
def _now() -> float:
    return time.time()


def seed(db) -> list[str]:
    """Insert built-in formats that are not in the database yet. Never overwrites a stored (edited) format."""
    added = []
    for fid in builtin_ids():
        doc = _dump(_builtin_model(fid))
        cur = db.execute("INSERT OR IGNORE INTO select_formats(id, data_json, builtin, created_at, "
                         "updated_at) "
                         "VALUES (?, ?, 1, ?, ?)", (fid, json.dumps(doc), _now(), _now()))
        if cur.rowcount:
            added.append(fid)
    return added


def _row(db, fid: str):
    return db.execute("SELECT id, data_json, builtin, created_at, updated_at FROM select_formats WHERE id=?",
                      (fid,)).fetchone()


def get_format(db, fid: str) -> dict:
    r = _row(db, fid)
    if r is None:
        raise NotFound(f"no selection format '{fid}'")
    return json.loads(r["data_json"])


def get_format_model(db, fid: str) -> SelectionFormat:
    return validate_format(get_format(db, fid))


def active_id(db, settings=None) -> str:
    fid = settings.get(ACTIVE_KEY) if settings is not None else _kv_get(db, ACTIVE_KEY)
    if not fid or _row(db, fid) is None:
        return DEFAULT_ID
    return fid


def _kv_get(db, key):
    r = db.execute("SELECT value_json FROM settings_kv WHERE key=?", (key,)).fetchone()
    return None if r is None else json.loads(r[0])


def set_active_id(db, fid: str) -> str:
    get_format(db, fid)
    db.execute("INSERT OR REPLACE INTO settings_kv VALUES (?, ?, ?)", (ACTIVE_KEY, json.dumps(fid), _now()))
    return fid


def _summary(db, r) -> dict:
    doc = json.loads(r["data_json"])
    modified = bool(r["builtin"]) and doc != _dump(_builtin_model(r["id"]))
    return {"id": r["id"], "name": doc.get("name"), "version": doc.get("version"),
            "description": doc.get("description", ""), "builtin": bool(r["builtin"]), "modified": modified,
            "active": r["id"] == active_id(db), "updated_at": r["updated_at"]}


def list_formats(db) -> list[dict]:
    rows = db.execute("SELECT id, data_json, builtin, created_at, updated_at FROM select_formats "
                      "ORDER BY builtin DESC, id").fetchall()
    return [_summary(db, r) for r in rows]


def create_format(db, data: Any) -> dict:
    data = parse_json(data)
    if not isinstance(data, dict):
        raise FormatError("a selection format must be a JSON object")
    fmt = complete(data)
    if _row(db, fmt.id) is not None:
        raise Conflict(f"a format with id '{fmt.id}' already exists")
    doc = _dump(fmt)
    db.execute("INSERT INTO select_formats(id, data_json, builtin, created_at, updated_at) "
               "VALUES (?, ?, 0, ?, ?)",
               (fmt.id, json.dumps(doc), _now(), _now()))
    return doc


def update_format(db, fid: str, data: Any, replace: bool = False) -> dict:
    """Edit a stored format. The body may be partial: it is merged key by key over the stored format
    (lists and lexicon tables replace whole). replace=True merges over the built-in default instead
    (a whole new document)."""
    data = parse_json(data)
    if not isinstance(data, dict):
        raise FormatError("a selection format must be a JSON object")
    r = _row(db, fid)
    if r is None:
        raise NotFound(f"no selection format '{fid}'")
    if data.get("id", fid) != fid:
        raise FormatError("the format id cannot be changed (duplicate it instead)",
                          [{"loc": "id", "msg": f"expected '{fid}'"}])
    patch = {**data, "id": fid}
    fmt = complete(patch) if replace else validate_format(deep_merge(json.loads(r["data_json"]), patch))
    doc = _dump(fmt)
    db.execute("UPDATE select_formats SET data_json=?, updated_at=? WHERE id=?", (json.dumps(doc), _now(),
                                                                                  fid))
    return doc


def delete_format(db, fid: str) -> None:
    r = _row(db, fid)
    if r is None:
        raise NotFound(f"no selection format '{fid}'")
    if r["builtin"]:
        raise Conflict(f"'{fid}' is built in and cannot be deleted; use reset to restore its defaults")
    in_use = [x["id"] for x in list_rules(db) if x.get("format_id") == fid]
    if in_use:
        raise Conflict(f"rules {in_use} are limited to '{fid}'; change or delete them first")
    if active_id(db) == fid:
        set_active_id(db, DEFAULT_ID)
    db.execute("DELETE FROM select_formats WHERE id=?", (fid,))


def duplicate_format(db, fid: str, new_id: str | None = None, name: str | None = None) -> dict:
    doc = get_format(db, fid)
    if new_id is None:
        n = 2
        while _row(db, f"{fid}-copy" + ("" if n == 2 else f"-{n - 1}")) is not None:
            n += 1
        new_id = f"{fid}-copy" + ("" if n == 2 else f"-{n - 1}")
    doc.update(id=new_id, name=name or f"{doc.get('name', fid)} (copy)")
    return create_format(db, doc)


def export_format(db, fid: str) -> dict:
    return get_format(db, fid)


def import_format(db, payload: Any, overwrite: bool = False) -> dict:
    """A format file (JSON text/bytes or an already parsed object). Validated; partial files fill in from the
    built-in default. An existing id is replaced only with overwrite=True (never silently)."""
    data = parse_json(payload)
    if not isinstance(data, dict):
        raise FormatError("a selection format file must contain one JSON object")
    if "id" not in data:
        raise FormatError("the format has no id", [{"loc": "id", "msg": "Field required"}])
    if _row(db, str(data["id"])) is not None:
        if not overwrite:
            raise Conflict(f"a format with id '{data['id']}' already exists; "
                           "import with overwrite to replace it")
        return update_format(db, str(data["id"]), data, replace=True)
    return create_format(db, data)


def reset_format(db, fid: str) -> dict:
    """Explicitly restore a built-in format to its shipped values (drops the customer's edits)."""
    if fid not in builtin_ids():
        raise Conflict(f"'{fid}' is not a built-in format; only built-in formats can be reset")
    doc = _dump(_builtin_model(fid))
    if _row(db, fid) is None:
        db.execute("INSERT INTO select_formats(id, data_json, builtin, created_at, updated_at) "
                   "VALUES (?, ?, 1, ?, ?)", (fid, json.dumps(doc), _now(), _now()))
    else:
        db.execute("UPDATE select_formats SET data_json=?, updated_at=? WHERE id=?", (json.dumps(doc),
                                                                                      _now(), fid))
    return doc


# ====================================================================== storage: rules
def list_rules(db) -> list[dict]:
    return [json.loads(r["data_json"]) for r in
            db.execute("SELECT data_json FROM select_rules ORDER BY position, id").fetchall()]


def get_rule(db, rid: str) -> dict:
    r = db.execute("SELECT data_json FROM select_rules WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise NotFound(f"no rule '{rid}'")
    return json.loads(r["data_json"])


def _check_rule_format(db, rule: Rule) -> None:
    if rule.format_id and _row(db, rule.format_id) is None:
        raise FormatError(f"rule refers to unknown format '{rule.format_id}'",
                          [{"loc": "format_id", "msg": f"no selection format '{rule.format_id}'"}])


def create_rule(db, data: Any, position: int | None = None) -> dict:
    rule = validate_rule(parse_json(data))
    _check_rule_format(db, rule)
    if db.execute("SELECT 1 FROM select_rules WHERE id=?", (rule.id,)).fetchone():
        raise Conflict(f"a rule with id '{rule.id}' already exists")
    doc = rule.model_dump(mode="json")
    n = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM select_rules").fetchone()[0]
    db.execute("INSERT INTO select_rules(id, position, data_json, created_at, updated_at) "
               "VALUES (?, ?, ?, ?, ?)",
               (rule.id, n, json.dumps(doc), _now(), _now()))
    if position is not None:
        ids = [r["id"] for r in list_rules(db) if r["id"] != rule.id]
        ids.insert(max(0, min(position, len(ids))), rule.id)
        reorder_rules(db, ids)
    return doc


def update_rule(db, rid: str, data: Any) -> dict:
    data = parse_json(data)
    get_rule(db, rid)
    if isinstance(data, dict) and data.get("id", rid) != rid:
        raise FormatError("the rule id cannot be changed", [{"loc": "id", "msg": f"expected '{rid}'"}])
    rule = validate_rule({**data, "id": rid} if isinstance(data, dict) else data)
    _check_rule_format(db, rule)
    doc = rule.model_dump(mode="json")
    db.execute("UPDATE select_rules SET data_json=?, updated_at=? WHERE id=?", (json.dumps(doc), _now(), rid))
    return doc


def delete_rule(db, rid: str) -> None:
    get_rule(db, rid)
    db.execute("DELETE FROM select_rules WHERE id=?", (rid,))


def reorder_rules(db, ids: list[str]) -> list[dict]:
    cur = [r["id"] for r in list_rules(db)]
    if sorted(ids) != sorted(cur) or len(set(ids)) != len(ids):
        raise FormatError("reorder needs every rule id exactly once",
                          [{"loc": "ids", "msg": f"expected a permutation of {cur}"}])
    with db.tx():
        for i, rid in enumerate(ids):
            db.execute("UPDATE select_rules SET position=? WHERE id=?", (i, rid))
    return list_rules(db)


# ====================================================================== effective values
def _ci(xs) -> set:
    return {str(x).lower() for x in xs}


def rule_matches(rule: Rule, channel: Any, context: dict, clip: dict) -> tuple[bool, str]:
    """(matches, why not). Unknown facts never match a condition that needs them."""
    c, s, d, st = rule.conditions, rule.scenario, rule.duration, rule.streamers
    if c.platforms and str(context.get("platform") or "").lower() not in _ci(c.platforms):
        return False, f"platform is not one of {c.platforms}"
    if c.categories and not any(re.search(p, str(context.get("category") or ""), re.I) for p in c.categories):
        return False, f"category does not match {c.categories}"
    if c.title_regex and not re.search(c.title_regex, str(context.get("title") or ""), re.I):
        return False, "title does not match"
    lang = context.get("language") or clip.get("language")
    if c.languages and str(lang or "").lower() not in _ci(c.languages):
        return False, f"language is not one of {c.languages}"
    kind = clip.get("trigger_type") or clip.get("kind")
    if s.trigger_types and kind not in s.trigger_types:
        return False, f"trigger type is not one of {s.trigger_types}"
    have = set(clip.get("evidence") or [])
    if s.evidence and not set(s.evidence) <= have:
        return False, f"missing evidence {sorted(set(s.evidence) - have)}"
    dur = clip.get("duration_s")
    if d.min_s is not None or d.max_s is not None:
        if dur is None:
            return False, "clip duration unknown"
        if d.min_s is not None and float(dur) < d.min_s:
            return False, f"shorter than {d.min_s:g} s"
        if d.max_s is not None and float(dur) > d.max_s:
            return False, f"longer than {d.max_s:g} s"
    involved = [str(x) for x in (context.get("channels") or ([channel] if channel is not None else []))]
    if st.channels:
        want = [str(x) for x in st.channels]
        ok = all(w in involved for w in want) if st.mode == "all" else any(w in involved for w in want)
        if not ok:
            return False, f"streamers involved do not include {st.mode} of {want}"
    if st.simulcast_group and context.get("simulcast_group") != st.simulcast_group:
        return False, f"not simulcast group '{st.simulcast_group}'"
    if st.min_count is not None and len(involved) < st.min_count:
        return False, f"fewer than {st.min_count} streamers involved"
    if st.max_count is not None and len(involved) > st.max_count:
        return False, f"more than {st.max_count} streamers involved"
    return True, ""


def resolve(format_doc: dict, rules: list, channel: Any = None, context: dict | None = None,
            clip: dict | None = None, builtin: dict | None = None, format_id: str | None = None) -> dict:
    """Pure precedence: built-in < customer format < rules in order < per-clip override."""
    context, clip = context or {}, clip or {}
    base = builtin if builtin is not None else _dump(_builtin_model(DEFAULT_ID))
    fid = format_id or format_doc.get("id")
    values, trail = {}, {}

    def put(key, value, layer, source, name=None):
        values[key] = copy.deepcopy(value)
        step = {"layer": layer, "source": source, "value": copy.deepcopy(value)}
        if name:
            step["name"] = name
        trail.setdefault(key, []).append(step)

    for k, v in flatten(base).items():
        put(k, v, "builtin", base.get("id", DEFAULT_ID))
    for k, v in flatten(_dump(complete(format_doc))).items():
        if k not in values or values[k] != v:
            put(k, v, "format", fid)
    matched, skipped = [], []
    for raw in rules:
        rule = raw if isinstance(raw, Rule) else validate_rule(raw)
        if not rule.enabled:
            skipped.append({"id": rule.id, "name": rule.name, "why": "disabled"})
            continue
        if rule.format_id and rule.format_id != fid:
            skipped.append({"id": rule.id, "name": rule.name, "why": f"limited to format '{rule.format_id}'"})
            continue
        ok, why = rule_matches(rule, channel, context, clip)
        if not ok:
            skipped.append({"id": rule.id, "name": rule.name, "why": why})
            continue
        matched.append({"id": rule.id, "name": rule.name})
        for action, key in ACTION_KEYS.items():
            v = getattr(rule.actions, action)
            if v is not None:
                put(key, v, "rule", rule.id, rule.name)
    override = clip.get("override") or {}
    unknown = [k for k in override if k not in values]
    if unknown:
        raise FormatError(f"unknown override key(s) {unknown}", [{"loc": f"override.{k}",
                                                                  "msg": "unknown setting"}
                                                                 for k in unknown])
    for k, v in override.items():
        put(k, v, "override", "clip")
    eff = validate_format(unflatten(values))
    return {"format_id": fid, "values": values, "effective": _dump(eff), "matched_rules": matched,
            "skipped_rules": skipped,
            "trail": {k: {"value": values[k], "set_by": steps[-1]["layer"], "source": steps[-1]["source"],
                          "layers": steps} for k, steps in trail.items()}}


def resolve_effective(db, channel: Any = None, context: dict | None = None, clip: dict | None = None,
                      format_id: str | None = None) -> dict:
    """Effective values for one moment plus the explanation trail. The format is context["format_id"], else
    `format_id`, else the active format."""
    context = context or {}
    fid = format_id or context.get("format_id") or active_id(db)
    return resolve(get_format(db, fid), list_rules(db), channel, context, clip, format_id=fid)


def effective_format(result: dict) -> SelectionFormat:
    """The SelectionFormat to hand to the core (detector, editorial, titles, ...) for this moment."""
    return SelectionFormat.model_validate(result["effective"])


def apply_decision(evidence: dict, effective: dict | SelectionFormat) -> dict:
    """A rule's skip/hold/force_qualify applied on top of gameplay.evidence(): skip rejects, hold keeps it for
    review, force_qualify approves. No decision: the evidence is returned unchanged."""
    fmt = effective if isinstance(effective, SelectionFormat) else effective_format(effective)
    dec = fmt.output.decision
    if dec is None:
        return evidence
    detail = f"rule decision '{dec}' (was {evidence.get('state')}: {evidence.get('detail', '')})"[:400]
    state = {"skip": "rejected", "hold": "unknown", "force_qualify": "approved"}[dec]
    return {"state": state, "source": "rule", "detail": detail}
