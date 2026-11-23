# Subsystem contracts

Modules are flat under `live_clipper/` and keep the names and public functions of the original design
(`detector`, `candidates`, `moments`, `editorial`, `gameplay`, `winners`, `titles`, `language_policy`,
`buffer`, `timeline`, `capture`, `clipper`, `render`, `postprocess`, `facecam`, `faces`, `loudness`,
`captions/`, `chat/`, `source_status`, `engine`, `publish/`). Within the pipeline, modules call each other
directly with those signatures. The event bus is for the UI, notifications, storage bookkeeping and publishing.

## Ownership (branch → files)

The branch names are from the first build (see the history in PROGRESS.md); the table still says which
subsystem owns which files.

| Branch | Owns |
|---|---|
| `select` | detector, candidates, moments, editorial, gameplay, winners, titles, language_policy, freshness, formats (selection formats + rules), `formats/*.json` |
| `edit` | buffer, timeline, clipper, render, postprocess, facecam, faces, loudness, captions/, presets (editing presets), edit service, `models/` |
| `monitor` | sources/ (twitch, kick, youtube live status + OAuth), chat/, chat_tokens, capture, source_status, engine, kick_webhook, monitor service + `/api/channels*`, `/api/sources*` |
| `publish` | publish/ (worker, base, sandbox, youtube, tiktok, instagram), destination OAuth, `/api/publish*`, `/api/destinations*` |
| `ui` | `live_clipper/web/**` (wizard, tour, guide, dashboard, editors) |
| `ops` | install scripts, SKILL.md, AGENTS.md, docs/guide, CI, maintenance/storage (disk cap, retention, cleanup), notifications |
| integration | `events`, `config`, `store` (shared by every subsystem), `pipeline` (selection decisions inside the live pipeline) |

## The live pipeline

The call graph is the original engine's; the public pieces it goes through:

```
capture (ffmpeg/streamlink) -> buffer.BufferManager / timeline.SegmentIndex (ring, stream timeline)
chat adapter -> Monitor.on_chat -> detector.SpikeDetector.feed/tick -> Trigger
Engine.on_trigger:
    maintenance admission (engine thresholds + ops Maintenance.admit_clip)
    pipeline.Selection.resolve(channel, kind, topic evidence)      -> effective values
    moments.window_for(action, window.pre, window.post)             -> cut window
    moments.merge_target / dedupe_key / simulcast_channels          -> merge or new clip
    EditEngine.create_clip(preset_id = output.preset_id, overrides = {window})
    moments.moment_id, capture-time topic evidence, timings
EditEngine.process (port of Engine._process):
    clipper.cut_clip -> preview / audio / source.ts
    postprocess.post_cut: captions (ASR, original-language check, translation), titles (effective format +
        output.title_template), language_policy.decide (effective format), editorial gate (effective format),
        render_final with the clip's preset values
    publisher.on_language_ready -> Selection.gate -> select.moment; qualified -> edit.clip_ready
PublishService (edit.clip_ready) -> deliveries (auto: queued, manual: held until Post)
```

Restart: the engine re-arms armed channels, `EditEngine.recover()` finishes interrupted edits, unfinished clips
with `preview.mp4` + `source.ts` resume their post-cut work (`EditEngine.resume`), the rest fail; the dedupe key
and publish's idempotency key keep a moment from becoming a second clip or a second post.

## Shared store (`live_clipper.store.Store`)

One store over the app database serves the engine, edit and the selection functions, with the original `Store`
API: `q, q1, x, update, merge_json, tx, add_channel, channel, channels, create_clip(channel_id, dedupe_key,
kind, trigger, **fields) -> id | None, clip, clips(channel_id?, limit, offset), mark, log`. Code uses the
original table names; they are rewritten to the `monitor` namespace:

| Logical name | Table / view |
|---|---|
| `channels`, `clips`, `log`, `clip_ledger`, `clip_descriptors`, `vod_imports`, `recovery_evidence` | `monitor_<name>` |
| `clip_channels` | view over clips + clip_ledger |
| `destinations`, `deliveries` | read-only views over `publish_destinations` / `publish_deliveries` with the original columns (`account_key` = `''`; the test destination's provider reads as `sandbox`) |

Clip rows carry the original columns plus `preset_id` (editing preset) and `overrides` (partial preset values,
merged onto the preset). The clip `trigger` JSON additionally holds `format_id` (selection format at the trigger),
`override` (per-clip selection values by dotted key) and `selection` (`{language: {decision, reasons, format_id,
matched_rules, at}}`). A clip id is never reused while a ledger row or a delivery references it. Optional post
metrics for `winners` (`post_anchors`, `post_readings`, `post_proof`) are not provided: no pattern preference.

## Engine ↔ edit

`EditEngine` (the edit service's `engine`, or a standalone one the engine builds) is the media code's `engine`
duck type (`store, settings, clip_dir, render_lock, emit, publisher, captioner, asr_pool, early_asr,
media_slot, selection`):

- `create_clip(channel_id, window_start, window_end, action_time, *, kind, trigger, layout, preset_id, overrides,
  dedupe_key, delay) -> clip_id | None`; `await process(buffers, channel_id, clip_id, start, end, action)`;
  `await resume(clip_id)`; `rerender(...)`; `publish(clip_id, lang?)` (the user's explicit publish).
- `for_clip(clip)` gives a view whose `settings` (`presets.EditSettings`) come from the clip's preset + overrides.
- Clip folders: `<storage.dir or media>/clips/<clip_id>/` with `preview.mp4`, `source.ts`, `final_<lang>.mp4`.
- `engine.settings` (`config.Settings`) holds capture, ring, disk and topic-observation values only: keys
  `monitor.<field>`, `processing.max_concurrent_recordings`, `storage.dir`, `ffmpeg_path`. The ring marker is
  `config.RING_MARKER` (`.live-clipper-ring`).

## Selection inside the pipeline (`live_clipper.pipeline.Selection`)

- `resolve(channel_id, clip?, kind?, topic?, language?)` calls `formats.resolve_effective(db, channel, context,
  clip)`. Context: `platform, category, title` (capture-time topic evidence), `channels` (streamers involved:
  the channel's simulcast group; a streamer is named by its platform login, or by the display name of a
  creator-provided feed), `simulcast_group`, `format_id` (the channel's format, else the active one). Clip facts:
  `kind`, `evidence` (names: `chat, topic, speech, gameplay, statement, reaction`), `duration_s`, `language`,
  `override` (`trigger.override`).
- Values consumed: `window.pre/post` (cut window at the trigger), `output.preset_id` (the clip's editing preset;
  unknown id → `default`), the format's `detector`, `match` and `topic_states` (detector, topic gate and topic
  evidence of the channel), `output.title_template` (`{title} {channel} {game} {platform}`), editorial/language
  values (editorial gate, language decision), `scoring.min_score`, `output.decision`, `posting.caption_template`
  + `posting.hashtags` (description and hashtags), `output.destinations`.
- Rules are evaluated with the facts known at each step: window/preset/detector at the trigger (context and
  trigger type), title when the first captions are ready, the gate when a final is ready (everything).
- `gate(clip_id, lang)`: `candidates.below_threshold(clip, scoring.min_score)`, `formats.apply_decision(
  gameplay.evidence(clip, fmt), result)`, `language_policy.auto_ineligible(asr, channel_pref, lang, fmt)`.
  `rejected` if the evidence is rejected, `held` if any reason remains, else `qualified`. An editorial rejection
  before the final is also published as `rejected`. Only `qualified` finals become `edit.clip_ready`; the user can
  approve (`POST /clips/{id}/review`) or publish anyway (`POST /clips/{id}/publish`).
- The detector and topic matching of an armed channel use the format in effect when it was armed (re-arm to apply
  a changed format); windows, presets, titles and decisions are resolved per moment.

## Bus topics (payloads are JSON dicts)

| Topic | Publisher | Payload |
|---|---|---|
| `monitor.channel_state` | monitor | `{channel_id, platform, login, state, detail}`; `state` ∈ `offline, waiting, recording, reconnecting, needs_action, disabled` |
| `select.moment` | pipeline | `{moment_id, channel_id, decision, score, reasons, clip_id, language, format_id}`; `decision` ∈ `qualified, rejected, held` |
| `select.formats_changed` | select | `{what: format\|rule\|rules\|active, id}` |
| `edit.clip` | edit | `{id}` (a clip row changed; UI refresh) |
| `edit.clip_ready` | edit | `{clip_id, channel_id, path, language, title, description, hashtags, duration_s, width, height, format_id, preset_id, destinations}`; `destinations` = providers allowed for this clip (`null` = every eligible one) |
| `publish.delivery` | publish | `{delivery_id, destination_id, clip_id, destination, state, test, url?, error?}` on every state change |
| `app.notify` | any | `{title, body}` |

`edit.clip_ready.path` is inside the clip's folder and stays readable until every delivery for the clip is
finished. `destinations` is the format/rule `output.destinations` within the channel's `publish_platforms`
(an empty channel list allows every provider); publish skips other providers, never the test destination.

## Format, rules and override precedence (lowest → highest)

1. Built-in default format (`formats/gta-vi.json`, the shipped GTA VI clip-selection format) and default editing preset.
2. The customer's edited or imported format/preset (replaces built-in values key by key).
3. Customer rules, evaluated in list order; each matching rule's actions overwrite earlier values.
   Conditions may use context (platform, category/game, title, language), scenario (trigger type, evidence),
   duration, and the streamers involved (single channel, simulcast group, any/all of a list).
4. Per-clip manual override (from the clip editor), always wins: selection values by dotted key
   (`POST /clips/{id}/render {"override": {...}}`, stored as `trigger.override`) and preset values
   (`{"overrides": {...}}`, stored as the clip's `overrides`).

Formats, presets and rules are JSON, validated with pydantic, stored in the database, and importable/exportable
as files from the UI and `/api/formats*`, `/api/presets*`, `/api/rules*`. Rule actions map to values:
`decision → output.decision`, `threshold → scoring.min_score`, `window_pre/post → window.pre/post`,
`preset_id`, `title_template`, `caption_style`, `destinations → output.*`. `output.caption_style` is accepted
but not used yet (caption styles live in the editing preset).

## Storage maintenance (ops) and publishing

- The engine asks `maintenance.Maintenance(ctx).admit_clip(active_clips)` (after its own disk/active-clip checks)
  before every clip and pauses captures while `Maintenance.captures_paused` is true (MaintenanceService
  `status()["paused"]`); they resume when space is back and ops no longer pauses.
- Storage cleanup asks `Publisher.holds_provider_bytes(clip_id)` before deleting or trashing a clip or its
  original; a clip an upload may still need is kept. Clip and delivery knowledge comes from `edit.clip_ready`
  and `publish.delivery`. Terminal delivery states: `published`, `failed`, `cancelled`, `skipped`.
- Temporary files `*.part.mp4`, `hook_*.txt`, `audio_pre.wav`, `audio_tail.wav` are swept after an hour.

## Settings keys and vault entries

- ops (wizard, `GET /api/setup/questions` is authoritative): `setup.usage`, `setup.permission_confirmed`,
  `platforms.enabled`, `storage.dir`, `storage.cap_gb`, `storage.retention_days`, `storage.clean_first`,
  `storage.cleanup_rules`, `processing.asr_device|asr_model|asr_compute_type`,
  `processing.max_concurrent_recordings`, `posting.destinations`, `posting.mode`, `notify.method`,
  `notify.webhook_format`, `service.start_at_login`; `setup.completed` is in `ctx.app_state`; the webhook URL is
  in the vault under `notifications`. Answers are stored under their question id, except where the consumer
  already has a key (`setup_service.STORED_AS`): `processing.asr_device|asr_model|asr_compute_type` are edit's
  `asr.device|model|compute_type` and `posting.mode` is publish's `publish.mode` (default `auto`: nothing posts
  until a destination is connected, acknowledged and enabled). Consumers: `platforms.enabled` and
  `posting.destinations` order the Sources and Destinations pages; `notify.method` also gates browser
  notifications in the UI; `setup.usage` decides whether `setup.permission_confirmed` is asked.
- monitor: `monitor.<config.Settings field>`, `ffmpeg_path` (binary or folder); vault `source.<platform>`; the
  optional Kick chat webhook listener binds `127.0.0.1:<monitor.kick_webhook_port>` (default 8788) only when
  `monitor.kick_webhook_url` is set.
- select: `select.active_format`.
- edit: `asr.model`, `asr.device`, `asr.compute_type`, `ffmpeg_path`.
- publish: `publish.mode` (`auto`|`manual`), `publish.test_mode`, `publish.notifications`,
  `publish.channel_platforms` (`{channel_id: [provider]}`); vault `publish/app/<provider>`, `publish/dest/<id>`,
  `publish/ref/<delivery>`, `publish/oauth/<state>`.
- OAuth callbacks (`GET /api/oauth/*/callback`) skip the token check; a single-use, provider-bound `state` (plus
  PKCE where the provider supports it) protects them.
- The CLI has a global `--home PATH` (same as `LIVE_CLIPPER_HOME`).

## HTTP API surface (all under `/api`, token-protected)

| Owner | Endpoints |
|---|---|
| core | `GET /health`, `GET /status`, `GET/PUT /app-state/{key}` (`setup.completed`, `tour.state` = `completed`/`skipped`/`null`, `tour.version`) |
| ops | `GET /setup/hardware`, `GET /setup/questions`, `PUT /setup/answers`, `GET /setup/state`, `POST /setup/complete`, `GET /storage`, `POST /storage/cleanup {dry_run}` (default true), `GET /notifications?since=`, `GET /diagnostics`, `GET /guide` + `GET /guide/{slug}` |
| monitor | `GET/POST /channels` (body `{url, watch_mode: ungated|topic_gated, poll_seconds?, format_id?, publish_platforms?, enabled, display_name?}`), `GET/PUT/DELETE /channels/{id}`, `POST /channels/{id}/arm`, `POST /channels/{id}/disarm`, `GET /channels/{id}/events`; `GET /sources`, `POST /sources/{platform}/connect`, `GET /oauth/source/{platform}/callback`, `DELETE /sources/{platform}` |
| select | `GET/POST /formats`, `GET/PUT/DELETE /formats/{id}`, `GET /formats/{id}/export`, `POST /formats/import[?overwrite=true]`, `POST /formats/{id}/reset\|duplicate\|activate`, `POST /formats/preview-effective` (`{channel, context, clip, format_id?}`), `GET/POST /rules`, `GET/PUT/DELETE /rules/{id}`, `POST /rules/reorder` (`{ids}`); errors 422 `{"detail": {"message", "errors": [{"loc", "msg"}]}}`, 404, 409 |
| edit | `/presets*`, `GET /clips`, `GET /clips/{id}`, `GET /clips/{id}/media/{preview\|final\|thumbnail}`, `POST /clips/{id}/render` (`trim, layout, srt, title, preset_id, overrides, override, lang`), `POST /clips/{id}/review` (`{approved, note?}`), `POST /clips/{id}/publish` (`{lang?}`) |
| publish | `/destinations*`, `/oauth/{dest}/start\|callback`, `/destinations/instagram/token`, `GET/PUT /publish/mode`, `GET/PUT /publish/test-mode`, `GET /publish/channel-platforms`, `PUT /publish/channel-platforms/{channel_id}`, `GET /publish/deliveries`, `GET /publish/deliveries/{id}`, `POST /publish/deliveries/{id}/post\|cancel\|retry\|confirm-posted\|confirm-not-posted\|recheck`, `POST /publish/post` |

Channel status values shown in the UI: `offline`, `waiting`, `recording`, `reconnecting`, `needs_action`
(plus `disabled`). `needs_action` always carries a human-readable `detail` and a fix link.
