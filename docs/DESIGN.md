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

## Tokens

Ground `#f4f7f7`, surface `#ffffff`, raised `#eaeeee`, text `#14201f`, muted `#56615f`, accent `#0e6b6f`
(white text 6.27:1), accent ink on tint `#08484b` on `#e3f1f1` (8.89:1), control borders `#7a8684` (3.77:1).
Status pills: positive `#1d6b40`/`#e2f5e9`, warning `#8a5a00`/`#fdf3dc`, negative `#9b2226`/`#fdecec`,
neutral `#3a4543`/`#eaeeee`; all at least 5.3:1 (checked with the antislop contrast tool). Radius 8 px cards,
6 px controls, pills fully round. Body 15 px, titles 26 px, sections 18 px. Light theme only.

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
- Responsive down to 360 px: the sidebar becomes a menu sheet, grids collapse to one column, tables scroll
  inside their panel.
