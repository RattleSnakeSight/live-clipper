# Deviations from the original implementation

Each subsystem appends its own section. A deviation is any place where the public code does not reproduce the original
behavior, or reproduces it through a different structure, with the reason and the evidence.

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
| `editorial.assess`, `editorial.sweep` | Unchanged, but their frame sampling (`facecam`, `faces`) and final render (`postprocess.render_final`) come from the edit subsystem. | Ownership; not runnable until edit lands. |

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
|---|---|
| Live loudness trigger, speech classification (`captions.asr.classify_speech`), VOD import detection/scan/plan | Owned by the edit subsystem / VOD import; their parity sections are not compared by this branch. |

### New behavior (only when configured)

| Where | What |
|---|---|
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
