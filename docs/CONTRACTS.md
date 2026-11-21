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

## HTTP API surface (all under `/api`, token-protected)

| Owner | Endpoints |
|---|---|
| core | `GET /health`, `GET /status`, `GET/PUT /app-state/{key}` (`setup.completed`, `tour.state` = `completed`/`skipped`/`null`, `tour.version`) |
| ops | `GET /setup/hardware`, `GET /setup/questions`, `PUT /setup/answers`, `GET /setup/state`, `POST /setup/complete`, `GET /storage`, `POST /storage/cleanup`, `GET /notifications`, `GET /diagnostics`, `GET /guide` + `GET /guide/{page}` (Markdown from `live_clipper/guide/`) |
| monitor | `GET/POST /channels` (body `{url, watch_mode, format_id?, publish_platforms?, enabled}`), `GET/PUT/DELETE /channels/{id}`, `POST /channels/{id}/arm`, `POST /channels/{id}/disarm`, `GET /channels/{id}/events`; `GET /sources` (per platform: connected?, what it enables: live status / chat / capture), `POST /sources/{platform}/connect` (credentials form or OAuth/device start), `GET /oauth/source/{platform}/callback`, `DELETE /sources/{platform}` |
| select | `/formats*`, `/rules*`, `POST /formats/preview-effective` |
| edit | `/presets*`, `GET /clips`, `GET /clips/{id}`, `GET /clips/{id}/media/{kind}`, `POST /clips/{id}/render` |
| publish | `/destinations*`, `/oauth/{dest}/start|callback`, `/destinations/instagram/token`, `/publish/deliveries*`, `GET/PUT /publish/mode` |

Channel status values shown in the UI: `offline`, `waiting`, `recording`, `reconnecting`, `needs_action`
(plus `disabled`). `needs_action` always carries a human-readable `detail` and a fix link.
