# AI Gateway Live Operations Dashboard — Design System

## Purpose

A read-only operational dashboard served by the sticky router at
`/dashboard`. It must feel like a developer-tool console: dense, dark,
scannable, and immediately honest about system health and spend.

## Source Style

Translated from **Sentry** (`design-md/sentry/DESIGN.md`) into an internal
ops surface. The dashboard keeps Sentry's developer-observability DNA
(violet midnight, electric lime signature, uppercase tracked labels,
hairline-bordered dark cards) but strips the marketing layer: no mascots,
no starfield, no hero display type, no light-canvas polarity flip.

## Atmosphere

A single dark workspace. The canvas absorbs visual noise so that panels of
counters, tables, and status chips read as a unified console. Color is used
semantically and sparingly: lime means healthy/success, pink means degraded
or failed, violet is structural. White is used only for primary labels and
pressed buttons.

## Color Tokens

| Token | Hex | Role |
| --- | --- | --- |
| `--canvas` | `#1f1633` | Page background |
| `--panel` | `#150f23` | Cards, panels, code surfaces |
| `--ink` | `#ffffff` | Primary text and active buttons |
| `--ink-muted` | `rgba(255,255,255,0.72)` | Secondary text, table headers |
| `--ink-faint` | `rgba(255,255,255,0.18)` | Ghost surfaces |
| `--hairline` | `#362d59` | Panel borders, table dividers |
| `--hairline-strong` | `rgba(255,255,255,0.14)` | Stronger separators |
| `--lime` | `#c2ef4e` | Healthy status, success badges, bar fills |
| `--pink` | `#fa7faa` | Degraded/failed status, error badges |
| `--violet` | `#6a5fc1` | Structural accent |
| `--violet-mid` | `#79628c` | Neutral badges |
| `--violet-deep` | `#422082` | Bar backgrounds |

## Typography Tokens

| Role | Font | Size | Weight | Tracking | Casing |
| --- | --- | --- | --- | --- | --- |
| Page title | Rubik | 24px | 600 | 0 | Sentence |
| Status chip | Rubik | 12px | 700 | 0.2px | Uppercase |
| Panel heading | Rubik | 12px | 700 | 0.2px | Uppercase |
| Metric | Rubik | 30px | 700 | 0 | — |
| Body / table | Rubik | 14–16px | 400–500 | 0 | Sentence |
| Mono data | Monaco / Menlo / Ubuntu Mono | 13px | 400 | 0 | — |

## Shape Tokens

| Token | Value | Use |
| --- | --- | --- |
| `--radius-sm` | 4px | Badges, status chips |
| `--radius-md` | 8px | Buttons, code blocks |
| `--radius-lg` | 12px | Panels/cards |
| `--radius-xl` | 18px | Large feature containers (unused here) |

## Layout

- Single centered container, max-width `1180px`, padding `24px`.
- Header separates brand/title/status from the window-selector button group.
- 12-column CSS grid for panels.
- Top metric cards span 3 columns each; data tables span 6 columns each.
- On narrow viewports (`≤900px`) all panels collapse to full width.

## Components

### Status Chip

Inline badge at the top of the page showing system readiness.
- `ready` → lime fill, dark text.
- `not ready` → pink fill, dark text.
- other → violet-mid fill, white text.

### Window Selector Buttons

Ghost buttons for `24h / 7d / 30d`. The active state inverts to white fill
with dark text. Uppercase, tracked, small radius.

### Panels (`window`)

Dark cards with violet hairline border, 12px radius, 16px padding. Panel
headings are uppercase micro-labels in muted ink.

### Metric

Large value (30px, weight 700). Optional small muted sub-line for health
breakdown.

### Tables

Full-width, hairline row separators, uppercase tracked headers in muted ink,
body cells in white. Model names and status codes render in monospace.

### Bars

Horizontal token bars: deep-violet track, lime fill, 999px radius.

### Badges

Small uppercase pills with 4px radius.
- `ok` → lime wash with lime text.
- `error` → pink wash with pink text.
- neutral → violet-mid fill with white text.

## Interaction

- Auto-refresh every 30 seconds.
- Window buttons swap data immediately.
- `prefers-reduced-motion` disables transitions.
- Keyboard focus ring preserved via browser defaults.

## Constraints

- No build step, no frontend package manager, no React.
- All styles are inline in the single HTML response.
- Google Fonts load Rubik from the network; system fallbacks apply if
  offline.
- No prompt bodies, responses, raw bearer tokens, or raw session IDs are
  displayed.
