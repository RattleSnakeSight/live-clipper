# Formats and presets

A format decides which moments become clips and how they are titled. A preset decides how a clip looks:
layout, captions and length.

Live Clipper ships one of each: the GTA VI format and the Default vertical clip preset. You can edit them,
copy them, import one from a file or export yours to share. Rules can then change a format or preset for a
particular streamer, platform, game or title. A setting you change on a single clip always wins. Which channels use a format, and the watch modes, are on
[Monitoring](monitoring.md); changing one clip is on [Editing](editing.md).

## Which setting wins

From weakest to strongest:

1. The built-in format.
2. Your format. Only the values you changed count, key by key.
3. Rules, in the order they are listed. A later rule overrides an earlier one.
4. A change you make on one clip.

For any clip, the app can show which layer set each value, so you can see why it behaved that way.

## The built-in GTA VI format

It looks for gameplay, spoken opinions and chat reactions in Grand Theft Auto VI streams. It is built in and
cannot be deleted, but you can edit it, and Reset puts it back exactly as shipped. Your edits are never
overwritten by an update.

Each part of a format, in plain words:

| Part | What it controls |
|---|---|
| Labels | The game's name as it appears in reasons and titles (GTA VI, GTA, GTA 6). |
| Match | Which streams count as the topic: a pattern for the title and one for the category, which platforms it applies to (none listed means all), whether a matching category counts as evidence, and whether a gated channel records only while the title matches. |
| Topic states | The names the app uses for how well a stream matches. |
| Evidence keys | The names of the two pieces of proof the app can attach to a clip. |
| Detector | How a chat burst is spotted. Sensitivity picks low, medium or high. Each is three numbers: how much chat must grow compared with normal, how many different people must take part, and a minimum count. Other values: how many messages one person can add (2), the window length in seconds (10), the delay between an event and the chat reaction (4), how long the "normal chat" baseline is (300 seconds), the smallest baseline (3), how close two bursts can be to merge (8 seconds) or count as different (15 seconds), a warm-up at the start (20 seconds) and the longest burst (90 seconds). Three optional overrides replace the sensitivity numbers. |
| Window | How many seconds before (18) and after (12) the moment the clip runs. |
| Keywords | Word patterns for what streamers say and what chat types: game terms, greetings, and reactions. Greetings are filtered out so "hello chat" is not a moment. |
| Editorial | The quality checks: how long the stream's opening is ignored (600 seconds), the shortest setup (5 seconds) and payoff (3 seconds), how much of the picture must be gameplay, how long and wordy a spoken opinion must be, and how much greeting or reaction chat is too much. It also has settings for telling gameplay from a static or face-dominated picture. |
| Scoring | How quickly clips from old moments lose priority (half-life 12 hours), and the minimum chat spike ratio (3.0) that qualifies a clip for an automatic post. |
| Language | Which languages are allowed (automatic, English, Spanish) and how sure the app must be about a stretch of speech. |
| Titles | How titles are written: at most 70 characters, a list of actions (explode, crash, heist, snipe, jump and so on) with word patterns and priorities, places and vehicles it can name, chat reactions it can mention, and sentence templates in English and Spanish. |
| Posting | The caption template (title, creator, platform, hashtags) and the hashtags. |
| Output | Rules can set a decision, a preset, a title template and the destinations. Defaults are empty. |

Pattern values are regular expressions. If you do not write them, you do not need to understand them: leave
them alone and change the numbers.

## Rules

A rule has conditions and actions. It runs in list order. You can switch a rule off, limit it to one format,
and reorder rules.

Conditions (all that you fill in must match):

- Context: platforms, categories (patterns, ignoring capitals), a pattern for the title, and languages.
- Scenario: what triggered the clip (a chat spike, a manual clip, or an audio spike), and evidence that must be
  present on the clip.
- Duration: a shortest and a longest clip length.
- Streamers: which channels, whether any or all of them must be involved, a group of channels that stream the
  same event at once, and a minimum or maximum count of streamers.

Actions (any that you fill in):

- Decision: skip the clip, hold it for your review, or force it to qualify.
- Threshold: the chat spike ratio needed.
- Clip window: seconds before and after the moment.
- Preset to use, title template and destinations. To change the caption style for a rule, choose an editing preset with the desired caption settings.

## Presets

The Default vertical clip preset makes 1080x1920 clips with captions burned in and a 2.8 second title at the
start. Previews are 720x1280.

The preset's Captions settings control the rendered text. A format or rule can contain a legacy
`caption_style` value. Live Clipper preserves it when you save other edits.

| Part | What it controls |
|---|---|
| Window | Seconds before and after the moment (18 and 12), and the longest clip you may trim to (60 seconds). |
| Encode | Preview and final height, encoder speed, quality numbers (lower is better), frame rate cap (30), and whether cutting is frame-exact. |
| Layout | Auto, fit, crop, split or stack, with optional zoom, vertical shift, horizontal centre, areas for the face and gameplay, and the facecam share of the height. |
| Facecam | Whether the app looks for a webcam inset, and how much of the height it gets in a split layout (32%). |
| Captions | Font, bold, size, outline, shadow, margins, position, and the three colours. |
| Title | How long the title shows (2.8 seconds) and how many characters per line (20). |
| Languages | Render the original language only, or English and Spanish both. |

## What each field means

Every field in the format, rule and preset editors carries its own one-line explanation under its name. The
text is the same one the app serves at `/api/formats/schema`, `/api/rules/schema` and `/api/presets/schema`
(a JSON Schema), so it never drifts from what the app checks. Fields that hold lists or nested tables
(for example title templates) are edited as JSON and show the explanation of the whole table.

## Make your own format

The GTA VI format is the starting point for any game or topic. To clip something else:

1. Open **Formats**, find the GTA VI format and press **Duplicate**. The copy is yours to change.
2. Press **Edit** on the copy. Give it a name, and change at least these parts: Labels (the name used in
   titles), Match (the title and category patterns that mean "this stream is on topic") and Keywords (game
   terms streamers say).
3. Press **Make active** to use it for every channel that does not pick a format, or pick it for one channel
   only on that channel's page.

A channel set to "Only when the title matches" records only while its title matches the format it uses.

## Import, export, replace

You never need to write code.

- **Export** downloads a format or preset as a `.json` file named after its ID, to share or keep as a backup.
- **Import** reads such a file. A format file with only some parts is fine: the missing parts come from the
  built-in default. If a format with the same ID already exists, the app asks before it replaces it.
- **Duplicate** makes an editable copy. **Reset** puts a built-in one back as shipped. **Delete** removes one of
  yours; channels that used it fall back to the built-in one.
- The app checks every imported file and says which part is wrong and where, for example a bad pattern or a
  missing language.
- The Rules list has its own Import and Export, for the whole list at once. Importing rules replaces all of
  them, after you confirm.
