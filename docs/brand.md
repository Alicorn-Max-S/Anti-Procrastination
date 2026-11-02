# warmstart brand

Decided Nov 2, 2026. Read this before touching branding, copy, colors, icons or page metadata. Everything here is final unless Max changes it.

warmstart is a study tool for students. It pulls assignments from Canvas, plans when to do them, and preps each one in a template the student picks (document, checklist, dashboard, focus mode, Cornell notes). It pings the student at start time, and a missed start gets a check-in and a re-plan.

## Name and copy

Use these strings exactly, including casing and punctuation.

| Item | Exact text | Where it goes |
| --- | --- | --- |
| Name | warmstart | One word, always lowercase, also at the start of a sentence ("warmstart couldn't read…"). Never "Warm Start", "WarmStart" or "Warmstart". |
| Slogan | Less staring. More starting. | Under the wordmark on the hero or title screen, title slide, `<title>` suffix, social cards. |
| Descriptor | We do the warm-up and wake-up, so you can get to work. | Directly under the slogan, meta description, one-line pitch. |

"Warm-up" is the prep and "wake-up" is the start-time reminder. The descriptor has one "the" on purpose: "the warm-up and wake-up" reads as one pair.

Optional lines for onboarding or empty states: "Prepped before you sit down." and "Every assignment, warmed up and ready." Suggested but not confirmed, for the template picker: "Warmed up, so you can shape it."

Copy voice:

- Plain, everyday words. No stock app-copy words like "nudge" or "dive in"; they read as AI-written.
- No "cold" ("never start cold") and no "fresh start" framing, which implies starting from scratch.
- The student does the work. Copy never implies the app does homework for them.

Web metadata (`static/index.html`; `app.js` keeps the same title when it adds a check-in count):

```html
<title>warmstart · Less staring. More starting.</title>
<meta name="description" content="We do the warm-up and wake-up, so you can get to work.">
```

## Logo mark

A play button inside a progress ring that's already a third full, so you never start from zero.

Geometry on a 48 × 48 viewBox:

| Part | SVG | Notes |
| --- | --- | --- |
| Track | `<circle cx="24" cy="24" r="16" fill="none" stroke-width="5"/>` | Full ring |
| Progress arc | `<path d="M24 8 A16 16 0 0 1 37.86 32" fill="none" stroke-width="5" stroke-linecap="round"/>` | Starts at 12 o'clock, runs clockwise 120°, round caps |
| Play triangle | `<path d="M20.5 17.5 L31 24 L20.5 30.5 Z" stroke-width="2.5" stroke-linejoin="round"/>` | Fill and stroke in the same color; the stroke rounds the corners |

| Surface | Track | Arc | Play |
| --- | --- | --- | --- |
| Brand tile (gradient or solid accent) | #FFFFFF at 40% opacity | #FFFFFF | #FFFFFF |
| White or light background | --ws-tint | --ws-accent | --ws-accent |
| Dark background (#18181B) | --ws-deep | --ws-light | #FFFFFF |

Set the 40% with `stroke-opacity`, not by layering shapes, so the ring doesn't get a darker inner band.

## Files

| File | Used for |
| --- | --- |
| `static/brand/app-icon.svg` | The 512 icon tile: SVG favicon and the hero lockup's tile |
| `static/brand/mark-on-brand.svg`, `mark-light.svg`, `mark-dark.svg` | The bare mark for slides and docs. The page header draws the mark inline instead, so `style.css` can switch it for dark mode |
| `static/icons/icon-192.png`, `icon-512.png` | PWA icons and the notification icon (rounded tile, transparent corners) |
| `static/icons/icon-maskable-512.png` | Android adaptive icon: full-bleed square, the OS cuts the shape. The ring sits well inside the 80% safe zone |
| `static/icons/apple-touch-icon.png` | iPhone home screen, 180 px, full-bleed (iOS rounds it) |
| `static/icons/favicon-32.png` | PNG favicon for browsers without SVG favicons |
| `static/icons/badge-96.png` | Android status-bar badge: the white mark on transparent, since Android draws only the alpha |
| `extension/icons/icon.png` | Chrome extension icon |

The PNGs were rendered from the SVG geometry in headless Chromium.

## Icon, wordmark and lockups

- App icon and favicon: square tile, corner radius 23.8% of the side, fill `linear-gradient(135deg, stop1, stop2)`, mark centered at 66.7% of the side. Use the tile for the favicon, not the bare mark.
- Wordmark: "warmstart" as live text in Plus Jakarta Sans 800, line-height 1, letter-spacing −0.035em at 40 px and up, −0.03em around 20 px. --ws-ink on light, #FFFFFF on dark or brand backgrounds. Never outline it into SVG in the app.
- Small horizontal lockup (page header): 28 px mark, 8 px gap, 20 px wordmark.
- Hero lockup (empty Assignments page): 168 px icon tile, 32 px gap, then wordmark 60 px / 800 / −0.035em; 12 px gap; slogan 20 px / 700 / line-height 1.35 / ink; 4 px gap; descriptor 16 px / 500 / line-height 1.45 / muted. Under 640 px it stacks, with a 96 px tile and a 44 px wordmark.

## Design tokens

One typeface, zinc greys with no blue tint, and Clay with its gradient. `style.css` holds these as `--ws-*` and `theme.js` holds the Clay values as `BRAND`.

Plus Jakarta Sans at 500, 700 and 800 from Google Fonts; fallback `'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif`. Only those weights load, so 400 draws as 500 and 600 as 700.

| Token | Value | Usage |
| --- | --- | --- |
| --ws-ink | #18181B | Text, wordmark on light, dark surfaces (the dark-mode page) |
| --ws-muted | #52525B | Secondary text (7.7:1 on white, 7.0:1 on --ws-surface) |
| --ws-border | #E4E4E7 | Hairline borders |
| --ws-surface | #F4F4F5 | Flat tiles and cards |
| --ws-white | #FFFFFF | Page background |
| --ws-stop1 / --ws-accent | #B9624A | Palette stop 1; solid accent |
| --ws-stop2 | #C97E45 | Palette stop 2 |
| --ws-brand | linear-gradient(135deg, #B9624A, #C97E45) | Brand surfaces only |
| --ws-tint | #F4E6E2 | Accent mixed with white, 84% white |
| --ws-light | #CE9180 | Accent mixed with white, 30% white |
| --ws-deep | #55342D | Accent mixed with --ws-ink, 62% ink |

## Brand color and class colors

Clay is warmstart's own color. A class color only says which class something belongs to. Red is only for warnings.

| Clay | Class color | Red |
| --- | --- | --- |
| Header mark, icons, the empty-page hero | That class's prep doc and Focus mode | Watch-out lines |
| The start-time check-in ("Time to start") and the week block that's on now | The dot next to the class name, and the type tag on its row in the Assignments list | Missed starts |
| Focus rings, progress bars, today's column in Week | The Dashboard's "where you lose marks" grid for that course | |
| | The color picker swatches | |

- A missed check-in sits on grey, so Clay is never the background of a red warning. Its Start button stays Clay: Clay means start.
- The class colors are quieter than the Nov 1 set (OKLCH chroma about 0.05, down from 0.05 to 0.10, and tints at 18% instead of 22%). Red and orange moved off Clay's hue, and red's distance from Clay doubled. The stored colors (`app/colors.py`) didn't change, only the muted ones the page draws (`SUBJECT_COLORS[].muted` in `theme.js`).
- Every class dot is at least 3:1 on white and on tiles, and every derived text color is pushed to 4.5:1 (`ensure()` in `theme.js`). Re-check contrast after changing any color there.

## Usage rules and accessibility

- The gradient goes only on the icon tile, a colored lockup background, and title or hero panels. Marks on white or dark stay solid; never put a gradient inside the mark.
- White text on the brand color must be at least 24 px regular or about 19 px bold (white on stop 2 is only 3.2:1). So small primary buttons are ink. A Clay button with white text uses the Clay that `theme.js` darkens toward --ws-deep until it passes 4.5:1 (#9B5441 on light), not --ws-accent itself.
- Clay as text on white is 4.28:1, so it's for shapes (rings, bars, dots), not small text. Text in Clay uses the same darkened step.
- Watch-out lines are always red (`WATCH_OUT_RED`), whatever the class color. Never use the brand color for warnings. In dark mode the red is lifted toward a light red, not toward white, so it stays vivid next to Clay light.
- A standalone mark or icon gets `role="img"` and `aria-label="warmstart"`. A mark next to the visible wordmark gets `aria-hidden="true"` (or `alt=""` on an `<img>`).

## Rejected, don't bring back

- Other names: Unblank, Cue, Mise, Toehold, Preheat, and "Warm Start" as two words.
- Blue as the brand color.
- Taglines built on "cold" or "fresh start", and copy with "nudge" or "dive in".
- A mark with steam rising off a page. The ring-and-play mark is the one in use.
- The other calm palettes (Sage, Lagoon, Dusk, Plum). Clay is locked.
