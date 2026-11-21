# Subsystem contracts

Modules are flat under `live_clipper/` and keep the names and public functions of the original design
(`detector`, `candidates`, `moments`, `editorial`, `gameplay`, `winners`, `titles`, `language_policy`,
`buffer`, `timeline`, `capture`, `clipper`, `render`, `postprocess`, `facecam`, `faces`, `loudness`,
`captions/`, `chat/`, `source_status`, `recovery`, `engine`, `publish/`). Within the pipeline, modules call each
other directly with those signatures. The event bus is for the UI, notifications and publishing.

## Ownership (branch → files)

| Branch | Owns |
|---|---|
| `select` | detector, candidates, moments, editorial, gameplay, winners, titles, language_policy, formats (selection formats + rules), `formats/*.json` |
| `edit` | buffer, timeline, clipper, render, postprocess, facecam, faces, loudness, captions/, presets (editing presets), `models/` |
| `monitor` | sources/ (twitch, kick, youtube live status + OAuth), chat/, capture, source_status, recovery, engine, kick_webhook, monitor service + `/api/channels*` |
| `publish` | publish/ (worker, base, sandbox, youtube, tiktok, instagram), destination OAuth, `/api/publish*`, `/api/destinations*` |
| `ui` | `live_clipper/web/**` (wizard, tour, guide, dashboard, editors) |
| `ops` | install scripts, SKILL.md, AGENTS.md, docs/guide, CI, maintenance/storage (disk cap, retention, cleanup), notifications |

## Bus topics (payloads are JSON dicts)

| Topic | Publisher | Payload |
|---|---|---|
| `monitor.channel_state` | monitor | `{channel_id, platform, login, state, detail}`; `state` ∈ `offline, waiting, live, recording, reconnecting, needs_action, disabled` |
| `select.moment` | select/engine | `{moment_id, channel_id, decision, score, reasons}`; `decision` ∈ `qualified, rejected, held` |
| `edit.clip_ready` | edit/engine | `{clip_id, channel_id, path, language, title, description, hashtags, duration_s, width, height, format_id, preset_id}` |
| `publish.delivery` | publish | `{delivery_id, clip_id, destination, state, url?, error?}` |
| `app.notify` | any | `{title, body}` |

## Format, rules and override precedence (lowest → highest)

1. Built-in default format (`formats/gta-vi.json`, the shipped GTA VI clip-selection format) and default editing preset.
2. The customer's edited or imported format/preset (replaces built-in values key by key).
3. Customer rules, evaluated in list order; each matching rule's actions overwrite earlier values.
   Conditions may use context (platform, category/game, title, language), scenario (trigger type, evidence),
   duration, and the streamers involved (single channel, simulcast group, any/all of a list).
4. Per-clip manual override (from the clip editor), always wins.

Formats, presets and rules are JSON, validated with pydantic, stored in the database, and importable/exportable
as files from the UI and `/api/formats*`, `/api/presets*`, `/api/rules*`.

## Proposed changes

(Branches append here; the integrator resolves.)

### From `select`

1. **Selection format API** (`live_clipper.formats`). `formats.active()` is the format the core uses by default;
   `formats.resolve_effective(db, channel, context, clip)` returns `{"format_id", "values" (dotted keys),
   "effective" (full format), "matched_rules", "skipped_rules", "trail"}`; `formats.effective_format(result)` gives
   the `SelectionFormat` to pass as `fmt=` to `DetectorConfig.from_format`, `editorial.*`, `gameplay.*`,
   `language_policy.*`, `titles.make_title`, `candidates.*`. Context keys: `platform, category, title, language,
   channels (streamers involved), simulcast_group, format_id`; clip keys: `kind|trigger_type, evidence (list),
   duration_s, language, override {dotted key: value}`.
   Values other subsystems read: `window.pre`/`window.post` (engine cut window), `scoring.min_score` (auto-post
   threshold), `output.decision` (skip/hold/force_qualify; apply with `formats.apply_decision(gameplay.evidence(clip),
   result)`), `output.preset_id`, `output.title_template`, `output.caption_style`, `output.destinations`,
   `posting.caption_template`, `posting.hashtags`, `labels.*`.
2. **monitor**: the topic gate and topic evidence use `formats.title_matches`, `formats.category_matches` and
   `formats.topic_state(st, category_counts, fmt)` instead of a hard-coded GTA regex (`source_status.is_gta6*` can
   delegate); topic state names come from `fmt.topic_states`. The engine builds detectors with
   `DetectorConfig.from_format(fmt, sensitivity=channel, reaction_delay=channel)` and applies the live merge/dedupe
   through `moments.window_for`, `moments.merge_target`, `moments.dedupe_key`, `moments.simulcast_channels` and
   `moments.moment_id` (ports of `Engine.on_trigger`'s rule).
3. **events**: `live_clipper/events.py` (`ChatEvent`, `ChatHealth`, `Trigger`, unchanged from the original) ships with
   the select branch because the detector needs it; chat adapters import it from there.
4. **publish**: the auto-post threshold check is `candidates.below_threshold(clip, min_score)` with `min_score` from
   the effective values (`scoring.min_score`, rule action `threshold`).
5. **Store adapter**: selection's store-backed functions keep the original duck-typed `store` (`q, q1, x, update,
   merge_json, tx, channel, channels, clip, log`) and table names (`clips, channels, destinations, deliveries,
   clip_ledger, clip_descriptors, vod_imports, recovery_evidence`, view `clip_channels`): `moments.*`,
   `candidates.editorial/candidates/started_on`, `freshness.*`, `winners.describe/evaluate/patterns/factor`,
   `editorial.record/check/sweep`, `gameplay.review`. The integrator provides an adapter over the engine's clip
   store. Post metrics for winners come from optional `store.post_anchors()`, `store.post_readings(anchors)`,
   `store.post_proof(kind, ref)` supplied by publish (absent: no pattern preference, factor 1.0).
6. **Bus**: topic `select.formats_changed` `{what: format|rule|rules|active, id}` after any format/rule change.
7. **HTTP** (`SelectService`, prefix `/api`): `GET/POST /api/formats`, `GET/PUT/DELETE /api/formats/{id}`,
   `GET /api/formats/{id}/export`, `POST /api/formats/import[?overwrite=true]` (raw JSON body),
   `POST /api/formats/{id}/reset|duplicate|activate`, `POST /api/formats/preview-effective`
   (`{channel, context, clip, format_id?}`), `GET/POST /api/rules`, `GET/PUT/DELETE /api/rules/{id}`,
   `POST /api/rules/reorder` (`{ids}`). Errors: 422 `{"detail": {"message", "errors": [{"loc", "msg"}]}}`, 404, 409.
