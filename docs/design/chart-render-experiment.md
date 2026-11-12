# Charts stage 1: renderer experiment

November 11, 2026. The thresholds set in the [stage 1 plan](charts-stage-1-plan.md)
were **not all met**. Speed, size and validity pass for every chart type except
one case: Qt takes 385 ms to load and paint a pie with 10,000 slices, against a
150 ms limit. Text widths pass only when the viewer's font is the measured font
or a narrower one; a wide substitute font overflows the estimate by up to 18%.
By the plan's rule the recorded result is `fallback_qcustomplot`. The author's
reading is that neither failure is cured by the fallback, and that two smaller
design changes are the better answer. That is a decision for the owner; see
[Result and what it means](#result-and-what-it-means).

Everything here is isolated experiment code in `tests/`. Nothing is installed,
stored in workbooks or exposed through REST, and no file under `service/` or
`desktop/` changed.

## Design and contract

`tests/chart_render_experiment.py` turns a resolved chart (plain values, no
workbook access) into SVG plus a list of mark rectangles, using only the Python
standard library. It supports column, bar, line, area, pie and scatter, with
clustered and stacked grouping where they apply, a title, axis titles and a
legend at the right or bottom. Gaps (`null`, text, errors, non-finite numbers)
draw nothing and have no mark. It refuses, with a structured code, more than
10,000 points per series, more than 64 series, a canvas outside 1 to 16,384
pixels, an unknown type, and a negative value in a pie.

Layout is computed from estimated text widths, because the service has no font
metrics. The default estimate uses six character classes. Category labels are
thinned and truncated so they do not overlap by that estimate.

The SVG stays inside a static SVG Tiny 1.2 subset: `svg`, `g`, `rect`, `line`,
`path`, `circle` and `text`, with presentation attributes only. A test enforces
the element list for every fixture.

`tests/chart_svg_probe.cpp` is a small Qt program, built only when Qt SVG is
found. It loads each SVG with `QSvgRenderer`, rasterises it, times loading and
painting, measures real label widths with `QFontMetricsF`, and can write a
per-character advance table for a font. `tests/chart_render_benchmark.py` runs
both, applies the thresholds and writes the evidence.

23 `unittest` cases cover ticks, text estimates, every chart type, the five
review-focus inputs from the plan, and the threshold evaluation. One CTest,
`chart_svg_probe_smoke`, checks that Qt SVG draws a known document and rejects
a malformed one.

## Measurement

Reference Mac (arm64, macOS 26.6.2), Qt 6.12.0 on the native `cocoa` platform,
Python 3.14.6. Medians of 20 runs. Qt resolved the SVG's `sans-serif` to
`.AppleSystemUIFont`. Evidence:
[chart-render-experiment.json](evidence/chart-render-experiment.json) (Arial as
the substitute font) and
[chart-render-experiment-verdana.json](evidence/chart-render-experiment-verdana.json)
(Verdana), with rendered charts in [evidence/chart-render/](evidence/chart-render/).

One series of 10,000 points at 800×500:

| Type | Python render (≤ 250 ms) | SVG size (≤ 4 MiB) | Qt load + paint (≤ 150 ms) | Met |
| --- | --- | --- | --- | --- |
| Column | 33 ms | 0.69 MiB | 61 ms | yes |
| Bar | 33 ms | 0.67 MiB | 54 ms | yes |
| Line | 24 ms | 0.14 MiB | 96 ms | yes |
| Area | 30 ms | 0.39 MiB | 116 ms | yes |
| Pie | 45 ms | 1.37 MiB | **385 ms** | **no** |
| Scatter | 23 ms | 0.51 MiB | 31 ms | yes |

Three series of 12 points (6 slices for the pie), render plus Qt load plus
paint: 0.6 to 1.3 ms for every type, against a 50 ms limit. Every one of the 22
SVG files was valid in Qt SVG.

Text widths, 36 labels (every label in the fixtures plus five stress strings),
measured in the default font and the substitute:

| Estimate | Substitute font | Worst measured ÷ estimate (≤ 1.05) | Median estimate ÷ measured (≤ 1.35) | Met |
| --- | --- | --- | --- | --- |
| Character classes | Arial | 1.075 | 1.09 | no |
| Character classes | Verdana | 1.098 | 1.04 | no |
| Metrics table for the default font | Arial | 1.029 | 1.02 | yes |
| Metrics table for the default font | Verdana | 1.176 | 1.00 | no |

The metrics table is 5.5 KiB of JSON for U+0020–U+007E and U+00A0–U+00FF.

## Findings

- **The 10,000-slice pie is slow in Qt, not in Python.** Removing the slice
  outline only brought painting from 348 ms to 286 ms, so the cost is filling
  10,000 antialiased arc paths. Such a pie is not a readable chart.
- **A metrics table works only for the font it was measured from.** With the
  table, the default font and the narrower Arial stay within 3%. Verdana, which
  is wide in the way DejaVu Sans on Linux is, overflows by 18%. Estimating
  cannot make layout safe when the viewer picks the font.
- **A table must be measured at label size.** The first run measured advances
  at 1000 px, as the plan said. The macOS system font tracks tighter at display
  sizes, and that table understated 12 px text by up to 19%. The probe now
  measures at 12 px.
- **The character classes are slightly narrow for this font.** Digits and `W`
  run 5 to 8% wider than estimated.
- **`sans-serif` is not a family Qt knows on macOS.** Qt logged a 57 ms
  one-time cost for populating font aliases. A real family name avoids it.
- **Visual check.** All eight fixture images were inspected. Titles, tick
  labels, category labels and legends are legible and inside the canvas, with
  no overlap. Three things a threshold cannot see: a gap in a stacked area
  leaves a white hole down to the series below; a scatter point on the lowest
  tick is half covered by the axis line; column, line and area charts draw no
  vertical axis line.

## Limits of this experiment

- One machine and macOS only. Nothing here is evidence for Windows or Linux.
- Resize latency is estimated as render plus load plus paint. There is no REST
  endpoint in the experiment, so a real round trip was not timed.
- The substitute fonts are stand-ins. Verdana approximates a wide Linux font;
  no Linux or Windows font was measured.
- Pie marks are bounding boxes with start and end angles; a client must use the
  angles to hit-test a slice.
- Only Latin text was measured. Characters outside the table fall back to the
  class estimate of one em.
- The palette is a placeholder.

## Result and what it means

By the plan's rule the result is `fallback_qcustomplot`: one speed threshold
failed, and the text thresholds fail for a wide substitute font with or without
a metrics table.

The author does not recommend taking the fallback on this evidence, for two
reasons. QCustomPlot has no pie chart, so it does not address the only speed
failure. And the text failure is about a viewer choosing a different font from
the one layout assumed, which a second renderer in the desktop would fix for
the desktop only while leaving the REST image as it is.

What the evidence supports instead, each a change to the approved design and so
the owner's decision:

1. **A lower point limit for pies.** Every other type passes at 10,000 points.
   A pie limit in the hundreds would pass with a wide margin and loses nothing
   readable. The exact value should be measured in stage 2.
2. **One font, shipped with the product.** The service lays out against a
   metrics table for a bundled, freely licensed font, and the SVG names that
   family. The desktop loads the same font, so its text matches the layout
   exactly. A REST consumer that views the SVG without that font may still see
   text a little wider or narrower than laid out; a PNG rasterised by the
   desktop does not have that problem. This is the remedy section 6 of the
   design anticipated, and at 5.5 KiB the table itself is cheap. The font file
   and its license are the real cost.

If the owner accepts both, stage 2 proceeds on the service SVG route with those
two changes. If not, the plan's rule stands and stage 2 starts from the
fallback: the service resolves data and computes scales, the desktop paints
with QCustomPlot, and pies need a separate answer.
