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

### edit (proposed)

- **Clips live in the edit namespace** (`edit_clips`, `edit_presets`, `edit_log`; one `register_migrations("edit")`
  in `live_clipper/presets.py`). The engine (monitor) creates and processes clips through `EditService.engine`:
  `create_clip(channel_id, window_start, window_end, action_time, *, kind, trigger, layout, preset_id, overrides)
  -> clip_id`, then `await process(buffers, channel_id, clip_id, start, end, action)` (cut -> preview -> captions ->
  finals, a port of the original `Engine._process`). Window: `engine.settings_for(values).window_for(action_time)`
  (pre 18 / post 12 from the preset; chat action = onset - reaction delay stays with the detector).
- **Effective values from select:** pass the resolver's merged dict as `overrides` (partial preset values: `window`,
  `encode`, `layout`, `facecam`, `captions`, `title`, `languages`); they are merged onto the clip's preset.
  `trigger` may carry `description`, `hashtags`, `format_id` for `edit.clip_ready`.
- `ClipStore.channel_lookup = fn(channel_id) -> {"settings": {...}}` lets monitor feed per-channel glossary /
  subtitle language preference into `post_cut`.
- `live_clipper.events.Trigger` (used by `loudness`) and `live_clipper.language_policy` / `titles` / `editorial`
  (used by `postprocess.post_cut`) are expected from select; they are imported lazily.
- `buffer.RING_MARKER = ".live-clipper-ring"` for whoever places a transient ring directory.
- `edit.clip` (UI refresh, `{id}`) is published in addition to `edit.clip_ready`.
