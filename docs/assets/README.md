# Brand assets — provenance and usage rules

This directory holds Kestrel's brand assets. It mirrors the brand kit
(“Kestrel — brand kit v1.0”, November 2026) so anyone touching visuals can
follow the same rules without the original kit.

## Assets

| File | What it is | Provenance |
|------|------------|------------|
| `kestrel-wordmark.png` | Primary lockup: kestrel mark + wordmark, horizontal. Use wherever width allows — README header, portal login, documentation title pages. | Generated as art for the Kestrel fork identity (brand kit v1.0, November 2026); the wordmark is drawn once as art — never re-typeset in a substitute font. |
| `kestrel-mark.png` | Primary mark: the kestrel hover alone, square. Use the mark alone in squares — favicon, app icon, avatar; it reads down to 16 px. | Same provenance as above. |

The brand kit also defines dark-surface and single-color monochrome
variants (dark variant for Charcoal and other deep surfaces; monochrome
when only one ink is available). They are not yet committed here; add
them to this table when they land.

## Palette

| Color | Hex | Role |
|-------|-----|------|
| Rufous | `#D2622A` | Primary — actions, accents, the interactive voice of the brand. Never for long body text. |
| Slate | `#5B7185` | Secondary — structure, secondary buttons, inactive states, captions. |
| Cream | `#F5EFE3` | Surface — default light background; warmer and calmer than white. |
| Charcoal | `#22272B` | Ink — body text, terminal surfaces, the monochrome mark. Never pure black. |

Supporting tones: Rufous Deep `#B44F1E` (hover/pressed), Slate Light
`#8FA6B8` (borders, disabled), Field `#4C7A5C` (success, score-positive),
Alarm `#B3402E` (errors, blocked actions). Typography: Outfit SemiBold
(display), Inter Regular (body), JetBrains Mono (code & CLI) — all free
Google Fonts with system fallbacks so nothing breaks offline.

## Usage rules

Do:

- Give the mark clear space of at least the height of the kestrel's head
  on all sides.
- Use the dark variant on Charcoal and other deep surfaces; the
  monochrome when only one ink is available.
- Pair Rufous with Cream and Charcoal — the palette is designed as a set.
- Use the mark alone in squares: favicon, app icon, avatar — it reads at
  16 px.

Don't:

- Don't stretch, skew, rotate, or add gradients, shadows, or outlines to
  the mark.
- Don't recolor the mark outside the four core colors.
- Don't set the wordmark in any font other than the drawn lockup art.
- Don't place the full-color mark on busy photos or low-contrast
  backgrounds.

## Attribution

Kestrel is an AGPL-3.0 fork of Pickle-Pixel's ApplyPilot. The license file
and upstream copyright notices travel with every distribution; carry the
attribution line “Forked from Pickle-Pixel/ApplyPilot” in the README
wherever the logo appears publicly. See [NOTICE](../../NOTICE) for the
full fork-lineage record.

The kestrel imagery references no third-party trademarks. Any third-party
names appearing in product documentation (Playwright, Composio, Gmail,
etc.) are the property of their owners and do not indicate endorsement.
