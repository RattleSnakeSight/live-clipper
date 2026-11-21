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
