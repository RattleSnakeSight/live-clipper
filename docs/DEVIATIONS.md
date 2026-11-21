# Deviations from the original design

Each section is owned by one branch. Format: what changed, why, evidence.

## ops: storage, retention and cleanup (`live_clipper/maintenance.py`)

- **Google Drive cold tier dropped.** The original archived `source.ts` to Drive, verified it, then released
  the local copy. Drive access needs a private OAuth client and a hosted redirect, which a local-only public
  app does not have. Instead, when the disk cap is hit, originals (`source.ts`) of fully posted clips are
  deleted locally, oldest first (`storage.clean_first = "originals"`). They are not archived anywhere.
- **Thresholds kept**: `min_free_mb` 4096, `critical_free_mb` 1536, `max_active_clips` 6, disk cap default
  20 GB (the original `clips_ceiling_mb` 20480), trash keep 168 h clamped to 24-720 h, trash name pattern
  `<id>-<epoch>`, temp patterns `*.part.mp4`, `hook_*.txt`, `audio_pre.wav`, `audio_tail.wav`.
- **Admission order kept**: low free space, captures paused, over the clips ceiling, too many active clips.
- **Cap now triggers cleanup of posted clips.** The original never deleted ready clips by size ("nothing is
  deleted automatically"). Here the wizard's cleanup rules (`expire_posted`, `clean_when_full`) allow it, but
  only for clips whose deliveries are all finished with at least one `published`. Clips with no delivery,
  or any queued/held/uploading/other non-terminal delivery, are never touched. Protection is re-checked at
  delete time. Turn both rules off to get the original behavior.
- **Posted trash** is driven by `storage.retention_days` (original: `posted_trash_hours`).
- **Freshness trash (24 h unposted)** is not ported: unposted clips are always kept.
- **Queue-only retirement / clip ledger** is not ported (no historical retirement API).
- **Clip/delivery knowledge** comes from bus topics `edit.clip_ready` and `publish.delivery` into
  `maintenance_clip` / `maintenance_delivery`, not from the clip tables of other subsystems.
- **Ring buffer sweeping** belongs to the `edit` branch (`buffer`).

## publish

Ported unchanged: the delivery state machine, idempotency (unique key + unique clip/destination/language/version),
backoff `min(900, 10*2^(n-1)) * U(0.5, 1.0)`, 6 attempts, timeout -> uncertain -> reconcile before any re-upload,
8 reconcile attempts -> needs_confirmation, a known provider id never becomes `failed`, restart turns
uploading/processing into uncertain, 429/401/refresh/revoked handling, the adapters' request shapes.

Public-product changes:

| Change | Reason |
|---|---|
| Dropped the `operator-runtime.json` allow-list, `native_publishing` flag, pinned `expected_account_id`, fresh-start cutoff / history gate, one-sender-per-account rule, Metricool egress and receipts, and the global social-publishing environment switch. | Owner-specific deployment gates; a public app has one user posting to their own accounts. |
| New gates: per-destination connected + enabled, a one-time rights/permission acknowledgement per destination, global `publish.mode` (`auto` default, `manual` holds finished clips as `held` until the user clicks Post). Disabling a destination moves unfinished deliveries to `blocked` (no provider call), enabling resumes them. | Product requirement. Without a connected, enabled, acknowledged destination nothing is posted. |
| Dropped freshness hard cutoff, creator spacing / moments conflict, auto-post rules and min score, language-policy and gameplay holds, `vod_only` and `reserved_for_channels`. | Owned by the select/monitor subsystems or specific to the owner's channels. Selection decides what becomes a clip; publish posts every finished clip to every eligible destination. |
| Kept the per-channel `publish_platforms` allowlist, stored as settings key `publish.channel_platforms` (`{channel_id: [provider]}`), API `PUT /api/publish/channel-platforms/{channel_id}`. | The channel table belongs to the monitor branch; this avoids a cross-branch schema dependency. |
| `token_expired` is now `needs_reconnect`; the destination notifies via `ctx.notify`. | Spec wording. |
| Clips are not read from a clips table: `edit.clip_ready` payloads are cached in `publish_clips` (one row per clip and language); `idempotency key = sha256("clip:<id>\|<destination>\|<language>")`. Language is the payload's `language`; there is no `waiting_language` flow (the file exists when the event fires). | The public edit contract carries path/title/description/hashtags. |
| Marker prefix `lc-` instead of `lsc-` (tags / caption / description marker used by reconcile). | Neutral naming. |
| Sandbox provider is the `test` destination, label "Test destination — nothing is posted", outcome `test`, never notifies, `publish.delivery` carries `test: true`, API deliveries carry `real: false`. | Must never be mistaken for a real post. State lives under `<data>/sandbox/`. |
| Added daily quota tracking (`publish_quota`): YouTube 100/day (midnight Pacific; fixed UTC-8 if no tz database), TikTok 15/day direct or 5/day inbox (UTC day), override with setting `daily_limit`. A delivery over the limit waits for the reset instead of attempting. Instagram keeps reading the live `content_publishing_limit`. | Research notes: YouTube insert bucket and TikTok caps. |
| `retry_after` also understands HTTP-date and `X-RateLimit-Reset`. | Rate-limit headers respected. |
| YouTube: `privacyStatus` always sent (default `public`, setting `privacy`), `paidProductPlacementDetails.hasPaidProductPlacement: false` always sent (`part=snippet,status,paidProductPlacementDetails`), no legacy `audited` flag, client secret optional (Desktop OAuth client). | Research: 2026-11-16 revision-history entry; the 2026-11-02 private-lock reversal conflicts with the Help page, so the result is labelled from the real `privacyStatus`. |
| TikTok: Login Kit for desktop with PKCE S256 and a HEX challenge (`Adapter.challenge()` hook), scopes `user.info.basic,video.upload,video.publish`, `code_verifier` in the token exchange. Default mode stays `inbox` (drafts); unaudited direct posts stay SELF_ONLY. | Research notes. |
| Instagram: `target_account_id` (optional, picks one account when a Facebook sign-in manages several) replaces the pin. Instagram Login cannot upload local files, so it uses `video_url` built from setting `video_base_url` (a public https folder the user serves); Facebook Login keeps the resumable `rupload` path. Redirect URI configurable (`redirect_uri` setting) and a paste-a-token form `POST /api/destinations/instagram/token` stores the token in the vault. | Meta loopback redirects are unverified; research notes. |
| OAuth state + PKCE verifier live in the vault (`publish/oauth/<state>`), single use, 900 s TTL, provider bound. Vault names: `publish/app/<provider>`, `publish/dest/<id>`, `publish/ref/<delivery>`. The callback registers the returned code as a secret before the token exchange. | Spec; credential hygiene. |
| Dropped `probe()`, `sweep_unsent`, `receipts`, `render_hook`, `orphan_deliveries`; added `cancel`, `confirm_posted`, `post`, `post_clip`, `disconnect`. | Owner-specific or replaced by the new API. |
| ruff: `E501` ignored for `live_clipper/publish/*.py` and `tests/*publish*.py` (ported code keeps the reference's line layout). | Avoid risky reformatting of ported code. |


## Select (moment selection, formats and rules)

Evidence: `tests/parity/test_selection_parity.py` replays the synthetic parity inputs
(`tests/parity/data/selection_inputs.json`) and compares every covered section with the original implementation's
output (`tests/parity/data/selection_ref.json`): detector triggers (as emitted and as mutated later), live merge and
dedupe, creator moment ids, editorial verdicts, game/content gate, language gate, rule threshold, reaction, freshness,
priority, winners labels, topic matching, chat/speech evidence, creator spacing, titles, camera faces and exploration
hours. All are byte-identical with the built-in format. A randomized differential check of titles, speech/chat
evidence, visual evidence (numpy), language decisions and the detector against the original also matched.

### Structure (same decisions, different shape)

| Where | What | Why |
|---|---|---|
| `formats/gta-vi.json`, `formats.py` | Every GTA-VI-specific value moved out of the code into the built-in selection format: title/category regex, topic state names, evidence key names, speech/chat keyword regexes, editorial thresholds, detector presets, clip window, scoring, language list, the whole title lexicon (actions, vehicles, places, templates, context phrases, chat reactions, stop words), caption template and hashtags. | The core is generic; a format is data. With the built-in format the values equal the original constants (generated from them). |
| `editorial`, `gameplay`, `language_policy`, `titles`, `candidates` | Public functions take an optional trailing `fmt=None` (default: the active format). Reason strings name the game through `fmt.labels` (`game` "GTA VI", `game_short` "GTA", `game_title` "GTA 6"). | Same strings with the built-in format; other formats read naturally. |
| `gameplay.context` | The on-topic state value stays `"gta"` (an enum token consumers compare against), for every format. | Parity of stored/compared values; it means "on topic". |
| `detector.DetectorConfig` | Gains `per_user_cap` (was the module constant `PER_USER_CAP`) and `presets` (was `SENSITIVITY`), both defaulting to the original values, plus `from_format()`. | Format-driven sensitivity; defaults unchanged. |
| `titles` | The special counting regexes of the original `_reaction` (hype: extra case-sensitive `\bW\b`; rip: its own pattern) are written as data in `titles.reactions[].patterns`. The unused `WANTED_RX` and a no-op string replace are not carried over. | Same counts and titles (differential check). |
| `formats.topic_state`, `title_matches`, `category_matches` | Port of `source_status.is_gta6`, `is_gta6_category`, `topic_state` driven by the format. | `source_status` belongs to the monitor subsystem; the regex is format data. See CONTRACTS "Proposed changes". |
| `moments.window_for`, `merge_target`, `dedupe_key`, `simulcast_channels` | The live same-moment/dedupe rule of `Engine.on_trigger` / `window_for` / `simulcast_channels` as pure functions. | The engine belongs to the monitor subsystem; selection owns the decision. |
| `candidates.below_threshold` | Port of `Publisher.below_threshold`. | The rule threshold is a selection decision (format `scoring.min_score`, rule action `threshold`). |
| `events.py` | `ChatEvent`/`Trigger` shipped with the detector. | The detector needs them; unchanged copy. |
| `editorial.assess`, `editorial.sweep` | Unchanged, but their frame sampling (`facecam`, `faces`) and final render (`postprocess.render_final`) come from the edit subsystem. `editorial.check` takes the moment's effective format (`fmt=`). | Ownership; formats. |

### Dropped hosted/private integrations

| Where | What | Why |
|---|---|---|
| `moments`, `candidates`, `winners` | External publisher handoffs (Metricool receipts, `external_receipts`, the routine) are not read: occupancy, conflicts, creator spacing, `started_on` and the posted-moment set use native `deliveries` only. | Hosted third-party integration; not part of the free local app. |
| `winners` | Metric collection is not ported: YouTube feed/API collection, Metricool analytics ingest, observation import, receipts check, report, cycle. Post anchors, readings and proof come from optional `store.post_anchors()`, `store.post_readings(anchors)` and `store.post_proof(kind, ref)` (publish side). Without them there is no pattern preference (factor 1.0, as on a fresh install). `_comparison_ids` uses the anchor's `post_id` instead of parsing the URL. The verdict, pattern and factor math is unchanged. | Post identity and metrics belong to publishing; parts of it were hosted services. |
| `winners.features` | The SRT parser is a private copy in `winners.py`, not an import of `captions.subs`. | `captions/` belongs to the edit subsystem. |
| `freshness` | Only `max_age_s, margin_s, moment_time, expires_at, reason, native_reason, expired` are ported. The fresh-start `recovery` gate in `reason`/`expired` and the expiry cycle/gc/tombstone (media removal) are not. | Fresh-start recovery and retention belong to ops. |
| Google Drive cold tier, calendar | Not ported. | Hosted/private integrations, not selection. |

### Ported tests

`test_detector` (16/16), `test_titles` (8/8), `test_moments` and `test_streamer_policy` (all, with external-handoff
cases replaced by native deliveries), `test_candidates`, `test_winners` (verdict, pattern and preference tests on a
store that supplies post data; tests of metric collection, imports and receipts dropped with those features),
`test_editorial_gate` (verdict fixtures with numpy frames; the persistence/dispatch tests need the publisher),
the pure parts of `test_language_policy`. `test_gameplay_gate` is API/publisher based: its decisions are covered
by the parity pipeline cases and `test_editorial_gate`. Real streamer names in the original tests were replaced
with synthetic ones.

### Not covered here

| Area | Why |
| Live loudness trigger, speech classification (`captions.asr.classify_speech`), VOD import detection/scan/plan | Owned by the edit subsystem / VOD import; their parity sections are not compared by this branch. |

### New behavior (only when configured)

| Where | What |
| `formats.apply_decision` | A rule action `decision` (skip / hold / force_qualify) overrides `gameplay.evidence` for that moment. With no rule decision (the default) evidence is returned unchanged, so original decisions stand. |

### Preserved original quirks (deliberately not fixed)

- A live chat clip stores the ratio at the FIRST hot evaluation as its score (about 2.2 at a baseline of 10), not the
  peak; the detector keeps mutating the emitted trigger in memory only (parity D02/D04/D12-D14).
- The chat classifier normalizes text to ASCII, which drops the detector's `×`, so the "top: text ×n" repeat weights
  never apply to real detector reasons (parity C01 vs C02).
- `stats["merged"]` also counts the first onset of every burst.
- A bare "gta" (so "gta 5" too), "rockstar" and "lucia" count as naming the game in speech (built-in keywords).
- `titles.make_title` raises `TypeError` for an English title with an action but no place (operator precedence in the
  original; the differential check compares the exception too).
Each subsystem appends its own section. Everything not listed here behaves as the original.

## Edit (cutting, rendering, captions)

Evidence: `tests/parity/test_cutting_parity.py` reruns the recorded synthetic cases (`tests/parity/data/`) and
compares cut boundaries, stored reasons, epochs, ffmpeg command lines, filtergraphs, ffprobe properties and caption
files. With the same FFmpeg 9.1 build, every live preview and every final except the titled one is byte-for-byte
the same size as the original's output.

### Kept on purpose (known quirks of the original)

- **Late video start on re-renders.** Finals, re-rendered previews and trims seek on the input side
  (`-ss <trim_in> -i source.ts`). On MPEG-TS the video only starts at the next keyframe after the target: in the
  recorded case the first video frame is at 0.767 s of a 30 s final (video 29.233 s, audio 30.000 s, still in sync).
  This is the default and the parity test checks it. Opt in to the fix with preset value `encode.accurate_seek: true`,
  which moves `-ss` after `-i`. The video then starts at 0 (tested), and decoding takes a little longer.
- Only `buffer_start` / `gap_before` from `resolve()` are stored as `truncated`; the cut loop's `gap_after`,
  `stream_ended` and `segment_evicted` overwrite them, as before.

### Platform and packaging differences

- **Title font.** The original hard-coded the Debian path `/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf`.
  `postprocess.TITLE_FONT` now resolves to the bundled `live_clipper/fonts/Inter-ExtraBold.otf` (Inter 3.19, SIL OFL),
  then that Debian path, then none (drawtext's default font). Hosts without Inter now render the same glyphs as the
  original's Debian host. The original failed outright on Windows/macOS (ffmpeg could not open the font).
  The path is passed with forward slashes. The only change in the command line is the `fontfile=` value.
- **Caption font.** The ASS style still names `Inter`, and the `ass=` filter is unchanged (no `fontsdir`), so the
  command line stays the same. libass uses an installed Inter if there is one. Otherwise it picks the system
  fallback (for example Arial on Windows), and the glyph metrics differ slightly. Install Inter for an identical look.
- **`nice`.** `proc.niced` prefixes `nice -n N` on Linux/macOS as before. On Windows, `nice` is never used, even an
  MSYS/Cygwin one on PATH, because it would rewrite path-like ffmpeg arguments. Instead, every media process (cut,
  render, frame read, loudness probe) starts with `BELOW_NORMAL_PRIORITY_CLASS` (`proc.LOW_PRIO`). No `/proc`
  access exists in these modules.
- **FFmpeg location.** Commands still call `ffmpeg`/`ffprobe`. When the setting `ffmpeg_path` (a folder or the
  binary) is set, the edit service puts that folder first on `PATH` at start.
- **Renamed identifiers.** The transient-ring marker file is now `.live-clipper-ring` (`buffer.RING_MARKER`), the
  face-model override env var is `LIVE_CLIPPER_FACE_MODEL`, and the sweeper thread is `live-clipper-buffer-sweeper`.
- **Lazy cross-subsystem imports.** `postprocess.post_cut` imports `language_policy` (from the selection side) when it
  runs, not at module import. `loudness.LoudnessTrigger.feed` imports `events.Trigger` when it runs. Without this,
  the render and edit paths would need modules this branch does not own.

### Parameters that are now data (defaults = original constants)

`live_clipper/presets/default.json` holds the original values: pre/post roll 18/12 s, max trim 60 s, preview 1280
(crf 23, `-g 60`), final 1920 (crf 21), x264 `veryfast`, fps cap 30, layout `auto`, split 0.32, caption style (Inter
bold, 0.068 W, ...), title 2.8 s / wrap 20, render the original language only. `cut_clip` gained a `crf` keyword
(default 23). `render_video` reads `render_crf`, `accurate_seek`, `title_secs` and `title_wrap` from
`engine.settings`, falling back to the original constants when they are missing. `post_cut` and `apply_edit` pass
the preset's caption style, and `render_languages: "both"` also renders the other language automatically. With the
shipped preset, every output is unchanged.

### Additions (do not change any output)

- `GET /api/clips/{id}/media/thumbnail` makes `thumbnail.jpg` (360 px wide, frame at 1 s of the preview) on first
  request. The original produced no thumbnails.
- ASR model and device come from settings `asr.model` (default `small`), `asr.device` (`auto`: CUDA if ctranslate2
  sees a GPU, else CPU) and `asr.compute_type` (default float16 on CUDA, int8 on CPU). The model downloads into
  `<data dir>/models/whisper`. `GET /api/status` → `services.edit.asr` reports whether faster-whisper is installed
  and whether the model is downloaded.

### Not ported here

- VOD import (`vod.py`) is not part of this branch. The parity adapter reports `vod_cut` as skipped.
- The auto-layout decision in the recorded reference fell back to `fit` because numpy was missing on that host.
  Here numpy and onnxruntime are installed and the real detector decides; for the synthetic source it also picks
  `fit`. The parity comparison ignores the fallback text and detector details.

## Monitoring and recording (`monitor` branch)

Parity evidence: `tests/parity/test_monitoring_parity.py` replays the original characterization sequences (capture
state machine and backoffs, gated watch, passive observation, topic evidence, stream-probe parsing, chat
normalization, three-channel engine, crash restart, disk producers) and compares every value with
`tests/parity/data/monitoring_ref.json`. Everything not listed below is unchanged.

| Area / file | Change | Reason |
|---|---|---|
| `sources/`, `engine.py` (`Monitor._official`), `source_status.py` (`hub=`) | When the user connects an official API for Twitch (Helix Get Streams), Kick (`/public/v1/livestreams`) or YouTube (Data API `search.list` + `videos.list`), live status comes from that API. Offline: capture is not started (no stream-probe retries); live: the original capture loop runs unchanged. Topic-gated channels read the same official answer. Without an official API the original stream probe is used and labelled "stream probe (no official API connected)". Rejected credentials are `needs_action` with the exact fix; capture is not started on a guess. | Public product requirement: official status where the user has credentials. |
| `sources/kick.py` | Uses the documented v1 livestreams endpoint (marked deprecated); the v2 cursor endpoint's parameters could not be verified. Broadcast id = `started_at`. | Only verifiable endpoint with multi-broadcaster lookup. |
| `sources/youtube.py` | `search.list` limited to 100 calls per 24 h and spaced `86400 * channels / 100` s per channel; quota exhaustion is shown as unavailable for an hour. | YouTube search quota bucket. |
| `engine.py` | Recording-slot limit `processing.max_concurrent_recordings` (wizard): extra channels wait (`waiting`, "waiting for a free recording slot") and start when a slot frees. In ungated mode without an official API the original loop runs continuously, so each such channel holds a slot. | Wizard limit. |
| `engine.py` `public_state()` | Internal states kept; mapped to UI states offline / waiting / recording / reconnecting / needs_action / disabled. | UI contract. |
| `engine.py` | Not ported: tab share, VOD import, cold tier, publisher/routine/editorial/winners/freshness/reach sweeps, prune, readiness heartbeat/outage file, `discard`/`reserve_edit`/`request_render`/failed-final backfill. Clip-storage ceiling admission and storage cleanup belong to the ops maintenance service. | Owned by other subsystems or not in the public product. The core supervisor records worker restarts in `events`. |
| `engine.py` | Cutting and post-cut work go through the edit engine (`EditEngine.process` / `resume`, a port of `Engine._process`); the merge/dedupe rule through `moments.*`; topic matching, topic state names, the detector and the cut window through the channel's effective selection format (built-in format = original values). | One implementation per step; formats. |
| `chat/kick.py` `ACT_URL`, `chat/twitch.py` `ACT_401_SIGNIN`, `chat_tokens.py` `ACT_REVOKED` | Fix texts point at the public app's Sources page instead of environment variables / the original settings page. | Public UI. Excluded from the parity comparison by path. |
| `chat_tokens.py` | Google refresh endpoint added (YouTube sign-in). | YouTube OAuth sign-in. |
| `capture.py` | FFmpeg binary from setting `ffmpeg_path` (default `ffmpeg` on PATH); Streamlink found as `streamlink.exe` next to the interpreter on Windows. | Cross-platform. |
| `config.py`, task names | Ring marker `.live-clipper-ring`, asyncio task names `lc-*`, status User-Agent `live-clipper (status check)`. | Public naming. |
| `store.py` | Original `Store` API over the app database, shared by monitor, edit and select; logical tables live in `monitor_*` tables (SQL table names are rewritten); `destinations`/`deliveries` are read-only views over the publishing tables. See "Integration" below. | Namespaced tables; one clip store. |
| `monitor_service.py` | Only `ungated` and `topic_gated` watch modes are offered; Rumble is not offered (no official status API; it still works as a creator-provided feed URL). VOD-only / retired channels stay refused. | Public scope. |

Recording note shown in the API and UI: monitoring permission (live status or chat) does not grant the right to
record or redistribute a stream; Streamlink and FFmpeg are third-party tools.

## Integration (one pipeline from the subsystems)

Evidence: `tests/test_e2e_pipeline.py` and `tests/test_e2e_crash.py` (real FFmpeg on a synthetic local HLS stream
and chat: cut window, sizes, publication, manual hold, two channels, custom format/rule/per-clip override in the
rendered files, worker kill and restart), `tests/test_pipeline.py`; the selection, monitoring and cutting parity
harnesses give the same results as on their branches.

| Where | What | Why |
|---|---|---|
| `store.py` | One clip store for the engine, edit and the selection functions (the original single `clips` table). Clip rows gain `preset_id` and `overrides`; the trigger JSON gains `format_id`, `override` and `selection`. `deliveries`/`destinations` are read-only views over publish's tables (`account_key` empty; the test destination reads as the original's `sandbox`), so selection code that excludes the sandbox excludes it. Clip ids are never reused while a delivery references them. | The branches had three clip stores; publishing owns its tables. |
| `config.py` | `Settings` keeps capture, ring, disk and topic values only. The clip window, reaction delay and detector values come from the selection format; preview/final height, x264 preset, fps and CRF from the editing preset (defaults = the original constants). `RING_MARKER` lives here (`buffer` imports it, as in the original). | Formats and presets are data. |
| `engine.py` | A channel's own `pre_roll` / `post_roll` settings are not read any more; the window is `window.pre/post` of the effective format (a rule with a streamer condition sets it per channel). A channel's `sensitivity` / `reaction_delay` still override the format's detector. | One precedence chain (format < rules < per-clip override). |
| `pipeline.py`, `edit_service.py` | The original publisher's auto-post checks (rule threshold `below_threshold`, gameplay `hold_reason`, language `auto_ineligible`) run once per finished final, before publishing, for every destination including the test destination (the original skipped the gameplay check for its sandbox). Only a qualified final becomes `edit.clip_ready`; a held one waits for the user's review (`POST /clips/{id}/review`) or explicit publish (`POST /clips/{id}/publish`), as the original's "publish manually". A rule `decision` overrides the gameplay evidence. | Publishing posts every clip it receives; selection decides which clips it receives. |
| `pipeline.py` | Titles get the effective `output.title_template`; the description comes from `posting.caption_template` and hashtags already in it are not repeated. Rules about duration, evidence or language cannot change the window or preset (they are decided at the trigger, before those facts exist); they apply to the title and the gate. | Facts known at each step. |
| `source_status.py` | `is_gta6`, `is_gta6_category`, `topic_state` delegate to the format (`match.title_regex`, `match.category_regex`, `topic_states`); with the built-in format the patterns and state names are the original ones. | Formats. |
| `maintenance.py` | Cleanup also keeps a clip whose file a provider may still need (`Publisher.holds_provider_bytes`). The engine also honours the storage service's admission check and disk pause. | Contract between ops, monitor and publish. |
| `presets.py` | The `edit` namespace only holds presets (clips moved to the shared store). | One clip store. |
