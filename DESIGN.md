# Design — Upgrade Chamber

Durable visual decisions for the browser frontend. Recorded from the committed world of the
`web/` build (direction "Controller faceplate", direction-seed key `773f50be`, mode
`operate`, assigned candidate 5 of the grounded list; no image generation available, so the
build took the code-led path — recorded in neither `.impeccable/config.json` nor PRODUCT.md).

## Mode and world

- **Mode: Operate.** The visitor completes a task (submit a run, watch it, inspect
  evidence). Scanability and honest status legibility outrank decoration; expression never
  obscures the task, state, or a familiar affordance.
- **World: controller faceplate.** The UI is the painted-steel front panel of the
  chamber's test-rig controller, viewed in daylight: engraved silkscreen labels, recessed
  LCD readout cells for recorded values, small status lamps, hairline-ruled instrument
  sections. It refuses the two category defaults — the airy SaaS card dashboard and the
  near-black neon "hacker console".
- **Physical scene (drives light):** a maintainer checks an automated server-side run from
  a bright office; a judge audits the evidence on a laptop in daylight. The panel is light;
  the recorded values sit in backlit-style LCD cells.

## Color strategy: restrained (neutrals + one accent)

| Token | Value | Role |
| --- | --- | --- |
| `--ground` | `#cfd1cc` | Painted-steel page ground |
| `--plate` | `#dedfd9` | Instrument section plates (raised, hairline rule) |
| `--plate-edge` | `#a8aba5` | 1px hairline rules on plates and inputs |
| `--plate-rule` | `#9b9f98` | Stronger internal rules; table hairlines |
| `--cell` | `#eef0e6` | LCD readout cells (values, patch, JSON) |
| `--cell-edge` | `#c2c6ba` | LCD cell border |
| `--ink` | `#24282c` | Primary text |
| `--ink-soft` | `#4d5358` | Secondary text, silkscreen labels |
| `--accent` | `#a13c17` | Safety orange: primary action only |
| `--ok` / `--fail` / `--warn` / `--lamp-off` | `#1f7a37` / `#bf3333` / `#b97700` / `#787d76` | Status lamps only |

- Green, red, amber are **functional state lamps** (see State coding); they never decorate.
- State text is always ink; hue lives in the lamp dot and cell tint, so color is never the
  only carrier.
- The accent appears on the primary button, focus rings, and the current phase indicator.
  Nothing else.

## Type

- **Display/labels:** Bahnschrift (Windows DIN; the industrial-lettering standard) with
  Franklin Gothic/Segoe fallbacks — `--font-display`. Used for headings and silkscreen
  micro-labels (small caps, `letter-spacing: .08em`, 600).
- **Body:** `--font-body` — Segoe UI/system sans, 1rem / 1.55.
- **Data:** `--font-mono` — Cascadia Mono/Consolas, reserved strictly for measured values:
  timestamps, counts, hashes, container ids, versions, patches, JSON. `tabular-nums` in
  tables.
- Scale: h1 ~2rem, h2 ~1.15rem, body 1rem, data 0.875rem, micro-labels 0.6875rem.
- No eyebrow/kicker above any heading; silkscreen labels belong to fields and cells.

## Space and rhythm

- 8px base; panel padding 20–24px; 24px between plates; more space above headings than
  below (28px vs 12px).
- Evidence tables are allowed to be genuinely dense (hairline rows, compact type,
  right-aligned numerals) — density is an instrument virtue here, borrowed from the
  high-density-web challenger.

## Depth and materials

- Plates: 1px hairline, 6px radius, layered real shadow (`0 1px 2px` + soft ambient).
- LCD cells: recessed (`inset` shadow), pale green-gray ground, mono ink.
- Placard (containment panel): double-rule engraved plate — inner and outer 1px rules.
- No glass, no gradients-as-decoration, no colored side-borders above 1px, no hard offset
  block shadows.

## Components

- **Status lamp + badge:** a small round lamp (`ok`/`fail`/`warn`/neutral) plus ink text of
  the exact state string. Every state renders its name.
- **Phase strip (signature interaction):** one unbroken horizontal row of run phases
  (PREPARE · BASELINE · SELECT · UPGRADE · VERIFY) with chase lamps — completed phases
  hold a dim solid lamp, the current phase lamp blinks, a failed/interrupted phase shows
  red/amber, future phases stay hollow. Never wraps; scales down (donated by the
  drum-machine step row; motion is the chase, locked to state).
- **Faceplate plate:** section container with silkscreen title and hairline internals.
- **Placard:** the containment panel; header carries "configuration, not proof" wording.
- **Buttons:** engraved plates. Primary = safety-orange plate, white text; secondary =
  steel plate, ink text. Pressed state insets. All keyboard-activatable with visible focus.
- **Inputs:** recessed LCD cells with silkscreen labels bound via `for`/`id`.
- **Timeline:** vertical hairline rail; mono UTC timestamps; silkscreen kind tag; human
  label; lamp per event status.
- **Collapsible evidence:** `<details>` sections expand in place to the complete content
  (never truncated summaries — donated by the Miura fold); stepped deployment on small
  screens is the same native element.

## State coding (functional, from the spec)

- Neutral (running states): `--lamp-off` lamp.
- `completed` / `passed`: green lamp.
- Failures (`upgrade_failed`, `baseline_failed`, `unsupported`, and attempt
  `install_failed`/`collection_failed`/`test_failed`/`failed`): red.
- `cancelled` / `timed_out` / `infrastructure_failed`: amber.

## Motion

One authored moment: the phase strip's chase lamps step as state changes (soft color
transition, current lamp's slow pulse). Timeline items appear once with a small
fade-rise, exponential ease-out. `prefers-reduced-motion` disables the pulse and entrance.
No scattered hover effects beyond pressed/hover plate states.

## Iconography and browser surfaces

- No emoji/unicode-as-icon; the only glyphs are authored lamp dots and the `<details>`
  chevron (authored CSS triangle).
- Themed browser chrome: `::selection` tinted from the accent, `caret-color` accent,
  custom `scrollbar-color` on scrollable cells, focus-visible rings everywhere, underline
  offset on links.

## Responsive

Single instrument column, `max-width: 62rem`. Attempt/containment grids collapse to one
column under 720px; comparison table scrolls horizontally inside its plate rather than
clipping. The phase strip scales, never wraps.

## Honesty constraints binding this world

- No decorative security badges; the containment panel is engraved "configuration, not
  proof" wording plus the run's recorded `cleanup_state` and image identity.
- Every rendered datum comes from the API; absent values render as "—".
- Failures render prominently with recorded `status_detail`; advisory "unavailable" shows
  "Advisory check unavailable" exactly; the phrase "target advisory no longer reported for
  this installed version" appears only when the target snapshot has zero vulnerabilities;
  never "secure".
- The start page claims execution only for the validated profile; research candidates
  render as explicitly not runnable.

## Anti-calibration record

Deliberate divergences: no warm-cream/serif/terracotta rendition (my measured prior);
no near-black + neon glow; no broadsheet hairline-italic-editorial. The one-bit HyperCard,
ticket-wallet, and cloud-world challengers declined on both weighing axes; the drum-machine
step row was competitive and donated the phase strip; the high-density web and Miura sheet
donated density courage and expand-in-place completeness respectively.
