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

### publish

- **OAuth callback must bypass the token check.** The browser reaches `GET /api/oauth/{provider}/callback` from the
  provider's site, and the `SameSite=Strict` cookie is not sent on cross-site navigations, so `Guard` answers 401.
  Please exempt `GET /api/oauth/*/callback` in `live_clipper/server.py` (`Guard.refuse`). The route is protected by a
  single-use, provider-bound, 15-minute `state` plus a server-side PKCE verifier. Tests send the bearer header.
- Router prefix is `/api` (not `/api/publish`): it serves `/api/destinations*`, `/api/oauth/*` and `/api/publish/*`.
- Settings keys used: `publish.mode` (`auto`|`manual`), `publish.test_mode`, `publish.notifications` (default true),
  `publish.channel_platforms`. The monitor/UI may write `publish.channel_platforms` or call
  `PUT /api/publish/channel-platforms/{channel_id}`.
- `publish.delivery` payload additionally carries `destination_id` and `test` (true for the test destination).
- Storage cleanup should call `Publisher.holds_provider_bytes(clip_id)` before deleting a clip file; an upload session
  may still need it.
- `edit.clip_ready.path` must stay readable until every delivery for the clip is finished.
