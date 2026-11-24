# UI design

The web UI is plain HTML, CSS and ES modules in `live_clipper/web/`. No build step, no framework, no CDN,
no web fonts and nothing vendored. The server serves it at `/`; every API call uses the auth cookie.

## Skills used and what they decided

| Skill | Decision |
|---|---|
| `ui-ux-pro-max` (design system search: "local utility tool dashboard calm trustworthy") | Minimal, Swiss-style app look; navy/teal family for trust; WCAG AA contrast, visible focus, reduced motion, large click targets. Its suggested web fonts were dropped: the app works offline, so it uses the system font stack. |
| `taste-skill:design-taste-frontend` (app-screen run, dealt style `accent-ladder`) | One hue at full strength only on small marks (primary button, active nav text, switches, progress); its pale tints carry areas (active nav row, info notes, precedence panel). Flat surfaces with 1 px borders, no shadows at rest, no hero header, KPIs as plain text, sentence case. The dealt raspberry was replaced by the product's own calm teal `#0e6b6f` (no violet or indigo). |
| `antislop:antislop-ui` | No gradients, glass, glow, decorative dots or icon tiles; a status dot only where it marks a real state; no invented numbers (counts come from the API, zeros print as zeros); empty states say why and give the one next action. |
| `antislop:antislop-copywriting` | All UI text: plain verbs, named actions ("Start watching", "Post to YouTube Shorts"), no buzzwords, no em dashes in our copy, one plain line per explanation. |
| `fixing-accessibility` | Native controls first; every input has a label and an `aria-describedby` help line; errors sit next to the field with `aria-invalid`; native `<dialog>` for confirmations; the tour is `role="dialog"` with a focus trap and Escape; `aria-live` for status text; skip link. |
| `baseline-ui` | Tailwind/React rules did not apply (vanilla stack); kept the transferable ones: `text-wrap: balance/pretty`, tabular numbers, `dvh`, a fixed z-index scale, only transform/opacity transitions under 200 ms, destructive actions behind a confirm dialog. |

## Direction

A calm, grouped look in the manner of a phone's settings app (clear hierarchy, grouped rows, familiar
switches, readable status), balanced with what a media workstation needs on a wide screen (channel health,
the review queue and the clip editor side by side). Reading: an app screen for people who clip streams as a
side project; energy low, section rhythm varied by content, motion limited to hover and focus feedback.

Each screen is built around the one decision made there:

| Screen | Decision | What leads |
|---|---|---|
| Dashboard | What needs me now? | One row per thing that waits on you (clips to post, failed posts, parts that did not start). Below it, monitoring on the left (channels, problems first) and clip work on the right (recent clips, posting) |
| Clips | Which clip do I post or fix? | The review queue, then the library |
| One clip | Is it good, and where does it go? | The video, the decision, where it was posted; the editor beside them with Render again always in reach |
| Sources | How do I want to watch this platform? | What already works without an account, then one chosen way to connect: its portal steps, only its fields, the address to copy, one button. Limits and uncommon errors one click away |
| Destinations | What is left before this account can post? | Who posts here, then four numbered steps (keys, sign-in, check the account and rights, on) with only the next one open. The account the provider reported is shown before the switch |
| Settings | Which answer do I change? | One row per setting, the control on the right when it is short |

## Tokens

Ground `#f1f4f4`, surface `#ffffff`, raised `#e9eeed`, hairlines `#dde4e3`, text `#14201f`, muted `#56615f`
(5.8:1 on the ground), accent `#0e6b6f` (white text 6.27:1), accent ink on tint `#08484b` on `#e3f1f1`
(8.89:1), field borders `#7a8684` (3.77:1). The accent is kept for the one main action of a screen, switches
and progress. Every other button is a quiet fill, `#e2e9e8` with accent ink text (8.36:1; hover `#d5dedd`,
7.51:1), and so is the selected navigation row. The top bar and the sidebar share the ground colour, so the
white groups are the only surfaces. Status pills: positive `#1d6b40`/`#e2f5e9`, warning
`#8a5a00`/`#fdf3dc`, negative `#9b2226`/`#fdecec`, neutral `#3a4543`/`#e9eeed`; all at least 5.3:1 (checked
with the antislop contrast tool). Three radii: 12 px groups and panels, 10 px controls, pills fully round.
Lists are grouped: a small label, then one white group whose rows are split by hairlines.
No shadows at rest; dialogs, toasts and the tour card are the only raised surfaces. Light theme only.

Type: system font stack. Titles 28 px, sections 17 px, body 15 px with a mouse and 16 px on a phone (a
16 px field keeps phone browsers from zooming in on focus), small text 13 px and 14 px.

Control heights come from four tokens (`--tap`, `--tap-small`, `--field`, `--row`): 36, 32, 40 and 40 px
with a mouse, and 44, 44, 44 and 48 px below 761 px wide or on a touch screen. Every button, field and
stand-alone link is at least 24 px in both sizes; a link inside a sentence is as tall as its line.

## Behaviour

- Help: every control carries `data-help`. Form fields show it as visible helper text; buttons and links get
  a screen-reader description and a tooltip on hover or keyboard focus (Escape hides it).
  `tests/ui/test_help_coverage.py` fails on any visible control without it.
- Tour: `js/tour.js`. Starts after the wizard on first use, or on the first open when `tour.state` is null.
  Steps cover every screen; each step lists the controls in the highlighted area using their own help text,
  so it cannot drift from the UI. Missing targets (no clips yet, no channel needing action) fall back to a
  centred card that still explains the screen. Result goes to `tour.state` (`completed`/`skipped`) and
  `tour.version` in the app database; Settings > Replay tutorial runs it again.
- Wizard: answers are saved on every Next, so a reload resumes at the first unanswered question.
  `show_if` is evaluated client-side with the same rules as the server.
- Navigation: the sidebar groups the screens by job (Watch, Clip, Post, App). Below 761 px a bar at the
  bottom holds Dashboard, Channels, Clips and Menu; Menu opens the same grouped list as a sheet.
- Responsive down to 360 px: grids collapse to one column; the clip library keeps its title and status
  columns and moves channel, age and length under the title; each row's buttons share the row's width.
- `[hidden]` always wins: components set their own `display`, so the stylesheet restores `display: none` for
  hidden elements. Conditional fields (the webhook questions, the title-check interval) rely on it.
- The tour card sits below its target, else above, else beside it, so it does not cover what it explains.

## Review of 2026-11

Changes from a review of the rendered app at 1280 px and 390 px, with the same skills as above plus `chisle`
(prefer a native feature or CSS over new script).

| Finding | Change |
|---|---|
| Conditional fields were visible while hidden (`.field` set `display: grid`) | `[hidden] { display: none !important; }` |
| Phone: clip titles broke one syllable per line and the status column was cut off | Two-column library on a phone |
| Phone: the link to the clips waiting for approval was last on the page | A review row with one button directly under the title |
| The dashboard was a grid of tall cards with the same sentence on each | One grouped list; a line that is true for every channel by default is left out |
| Almost every control was under 44 px on a phone | Height tokens, switch and rows as above |
| The tour card covered the channel cards it described | Placement beside the target; one line per kind of control |
| The same capture notice on every source card | Shown once above the cards |
| Settings showed wizard-sized fields for short answers | Rows with the control on the right; yes/no as a switch |
