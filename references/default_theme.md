# Default theme: scholar-default

Used only when no template or starting deck is provided. The builder also
uses these rules as a fallback when a provided template lacks a needed
layout. All values are deterministic and every font is resolved against
installed fonts.

## Canvas

- 16:9 widescreen, 13.333 × 7.5 inches (960 × 540 pt).
- Safe area: left 0.62 in, right 12.72 in, top 0.52 in, bottom 6.92 in.
  No content crosses the safe area; the QA stage verifies this against
  rendered previews.

## Palette (deliberately restrained, print-friendly)

| Token | Hex | Use |
|---|---|---|
| ink | `#1F2A37` | body text, titles |
| primary | `#173F5F` | deep navy; headers, section bands, title page |
| accent | `#2E86AB` | steel blue; emphasis, chart key, rules |
| accent2 | `#E07A5F` | terracotta; sparing highlight for key numbers |
| soft | `#F4F6F8` | panel/table header fill |
| rule | `#D9DEE4` | hairlines, table borders |
| muted | `#6B7785` | captions, source labels, footers |

White background. No gradients, shadows or decorative imagery. Color never
alone encodes meaning; labels accompany every highlighted element.

## Typography

- Latin: `Liberation Sans` for titles and UI, `Liberation Serif` for long
  prose and formulas; CJK: `Noto Sans CJK SC` / `Noto Serif CJK SC`.
- Sizes (pt): slide title 28; section/divider 34; body 16–18; table 11–13;
  captions/source labels 10–11; footer 9.
- Line spacing 1.15–1.25; generous whitespace over dense packing.
- Fonts are selected through `resolve_font` so an unavailable family is
  silently replaced by a real installed one and the substitution is
  logged.

## Slide anatomy

- **Title slide:** primary band or primary-colored title; paper title,
  authors, venue/audience line; no body bullets.
- **Content slide:** claim title top-left in ink; a 1.5pt primary rule
  under the title; content region split into text and figure/table zones;
  source label in muted at the bottom-right of the evidence zone.
- **Divider:** large role label in primary on white; optional section
  number.
- **Conclusion:** numbered takeaways in primary; no new evidence.
- **Backup:** identical anatomy plus a small "Backup" tag in muted.

## Tables and figures

- Native PowerPoint tables (real rows/columns, editable), header row on
  soft fill with ink text, 0.75pt rule borders; no merged decorative
  cells.
- Figures are placed as real images, fit (not stretched) into the zone,
  and the source label cites the bound figure id and page.
- Long tables are truncated to the rows the claim needs with an explicit
  "full table in source, p. X" note.

## Template precedence

1. Starting deck: preserve its master, theme and size; edit in place.
2. Template file: use its layouts and theme; fall back to safe-area
   geometry only where a layout is missing.
3. No deck: `scholar-default` as defined here.
