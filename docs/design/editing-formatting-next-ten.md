# Editing and formatting usability: next ten increments

Authorized November 7, 2026 after PR #74. Each increment is locally built/tested,
committed, pushed, opened as a PR, author-reviewed on its published final head,
and merged before the next. Hosted Actions credits are exhausted.

1. F4 cycles relative/absolute references in source and cell editors.
2. Cross-sheet formula reference picking.
3. Navigation from precedent/dependent trace results.
4. Unified Format Cells dialog for supported styles.
5. Configurable decimal places and toolbar controls.
6. Thousands-separator number formats.
7. Explicit currency display formats.
8. Font family, size and text color.
9. Individual border edges and line styles.
10. Format painter for copying styles between ranges.

Preserve exact inputs/types, shared REST/engine/transactional-hook behavior,
source-state/draft/late-reply fences and bounded work. Each style stage includes
applicable native persistence and XLSX/ODS mapping/reporting; formatting does not
retype values. This remains a local development preview, with cross-platform,
manual accessibility/input, runtime and distribution gates open.

## F4 reference cycling

In a writable automatic/formula draft, F4 cycles A1 → $A$1 → A$1 → $A1 → A1.
The caret targets one cell endpoint; a selection changes wholly selected cell
endpoints independently, including ranges/qualified references. A partial-cell
selection rejects the whole toggle. Sheet spelling, case and quoted Text remain
intact. References outside the supported grid do not toggle. Analysis retains up
to 1,024 references within the 64-KiB draft bound (visual cues stay capped at 64).
A toggle is one native draft Undo step; Enter submits the ordinary managed edit.
Literal Text/read-only editors do not toggle. Body/frozen delegates share this
behavior; no REST mutation, hooks or history occur merely from pressing F4.

## Completed PRs

1. [F4 reference cycling #75](https://github.com/hybridmachine/DataHorizon/pull/75): locally verified, author-reviewed and merged.

2. [Cross-sheet reference picking #76](https://github.com/hybridmachine/DataHorizon/pull/76): locally verified, author-reviewed and merged.

## Cross-sheet reference picking

Other sheet… beside the source bar (also Edit) transfers an unsubmitted in-cell
draft to the source editor and opens a separate read-only, virtualized preview.
Choose another sheet and click/drag a rectangle, or enter A1/range and press Enter
to select it. Insert reference quotes/escapes the sheet name and retains the
original source cell, input kind and draft. The preview supports 2–64 sheets and
at most 10,000 selected cells; no whole-workbook cache or writes are introduced.

A fresh single-cell REST read verifies the captured workbook state before one
native draft insertion. Changed service, target, state, cursor/selection or later
typing cancels the reply. Expired preview disables Insert; cancellation retains
the draft. Enter still submits one ordinary managed edit. Preview model callbacks
are lifetime-guarded, including replies arriving after the dialog is destroyed.

Unfinished/invalid address entry disables Insert until a valid Enter selection or a grid click replaces it; an older range is never used implicitly.

3. [Trace navigation #77](https://github.com/hybridmachine/DataHorizon/pull/77): locally verified, author-reviewed and merged.

## Trace navigation

Trace reports list navigable source locations. Go to selected (or double-click)
shows the first cell of a compressed range, switches to its sheet and reveals
that row/column if hidden. Broken references remain notices, not destinations.
Targets must match known sheet identity/name and canonical in-grid geometry.

A fresh REST metadata read verifies the original trace state before navigation.
Changed selection, draft/address entry, service, workbook or state drops the
pending jump; it never discards an editor draft. Read-only workbooks remain
navigable. Jumping is a local view operation, with bounded viewport reads and no
stored edits, formula evaluation request beyond ordinary display, hooks or Undo.

4. [Format Cells #78](https://github.com/hybridmachine/DataHorizon/pull/78): locally verified, visually inspected, author-reviewed and merged.

## Format Cells

Format → Format Cells… (Ctrl+1 through Qt platform shortcuts) provides Number,
Font, Alignment, Fill and Border tabs for the supported styles. Keep existing
and tri-state boxes leave each cell's unchosen properties intact; invalid RGB
colors disable OK. Clear all formatting is explicit and can be combined with
chosen replacements. An unchanged dialog or Cancel submits nothing.

One bounded managed edit applies the selected properties or full style reset,
without changing stored sources/kinds/formulas, and supports ordinary Undo/Redo.
The original rectangle, state, current cell, service and draft/address fences are
checked after the dialog. Read-only and projected/hidden scopes retain existing
guards. Applicable interchange mappings remain the existing style tier; later
increments extend this dialog and the shared style contract together.

5. [Decimal precision #79](https://github.com/hybridmachine/DataHorizon/pull/79): locally built/tested and author-reviewed before merge.

## Decimal precision

Number and Percentage in Format Cells accept 0–15 decimal places (default two).
Keep existing precision is separate from category. More/Fewer decimal places in
the toolbar use the active cell's precision, clamped to 0–15, for one selected
rectangle; each percentage cell retains its category. One managed edit and Undo
cover the rectangle. Numbers, formula source, identifiers, Boolean and NULL
inputs keep their types and stored values. General/Text ignore decimal metadata.

Native-v1 styles add `format: number|percent` and integer `decimals: 0..15`.
Legacy fixed2/percent2 remain exactly two places. A precision-only style patch
selects Number unless the existing cell is a percentage; exact replace_style
preserves the supplied metadata, and precision without a category means Number.
The invariant decimal separator is a dot. Percentage scales display only, using
the existing bounded decimal display arithmetic; it never scales stored values.

XLSX exports simple zero-placeholder formats; ODS uses number/percentage styles
with explicit decimal places. Import accepts this grammar only, reports other
formats, and canonicalizes two-place styles to legacy tokens. Interchange keeps
numeric values and effective display, rather than native style token identity.
Earlier services may reject these additive style properties; keep the native
workbook and use a compatible service. Evidence: `evidence/decimal-verification.txt`.

6. [Thousands grouping #80](https://github.com/hybridmachine/DataHorizon/pull/80): locally built/tested and author-reviewed before merge.

## Thousands grouping

Format Cells has a tri-state Thousands separator control. Checked/unchecked
sets `grouping: true|false`; partially checked keeps each cell's existing choice.
Number/Percentage display uses comma groups of three and a dot decimal separator,
independent of host locale. A grouping-only patch activates Number or preserves
an existing Percentage category and its effective precision. General/Text ignore
stored grouping metadata. Text identifiers and all stored magnitudes stay intact.

The additive native-v1 boolean maps to XLSX `#,##0[.zeros][%]` and ODS grouping
attributes. Built-in XLSX formats 3/4 now map too. CSV retains canonical values;
display search sees the explicitly formatted text. Unsupported localized or more
complex custom formats remain reported. Evidence: `evidence/grouping-verification.txt`.

7. [Explicit currency labels #81](https://github.com/hybridmachine/DataHorizon/pull/81): locally built/tested and author-reviewed before merge.

## Explicit currency labels

Format Cells → Currency uses an explicit three-uppercase-ASCII-letter label
(default USD), 0–15 decimal places and optional comma grouping. Display places
that label after the number, for example `-1,234.50 EUR`. This is presentation:
labels are not validated against an ISO list, inferred from locale, or converted
at an exchange rate. Text, Boolean, NULL, numeric inputs and formulas retain
stored types and magnitude. Precision/grouping-only edits preserve Currency.

Native-v1 adds `format: currency` and `currency: ABC` (default USD when absent).
XLSX maps the supported number pattern followed by a quoted `" ABC"` literal;
ODS uses number + space + explicit currency-symbol and sets the numeric cell's
currency attribute. Import accepts this explicit suffix grammar; ambiguous
symbols, accounting patterns and other currency layouts are reported as omitted.
Two-letter/four-letter/lowercase/non-ASCII labels are rejected transactionally.
Independent readers check numeric values and labels, without claiming target
recalculation or locale-identical rendering. Evidence: `evidence/currency-verification.txt`.

The portable ODS currency representation follows the [OASIS currency-style and
currency-symbol contract](https://docs.oasis-open.org/office/OpenDocument/v1.3/os/part3-schema/OpenDocument-v1.3-os-part3-schema.html).

8. [Font formatting #82](https://github.com/hybridmachine/DataHorizon/pull/82): locally built/tested, visually inspected and author-reviewed before merge.

## Font formatting

Format Cells → Font adds a family entry with installed-font completion, integer
6–72pt size and RGB text color, each initially Keep existing. The native style
keys are `font_family`, `font_size`, `text_color`. Family names are 1–64 trimmed
characters without ASCII controls, quotes, comma or backslash: one portable
family, not a font fallback list. An unavailable family remains stored by name;
Qt may substitute at render time. Explicit row sizes still control clipping;
resize or fit rows for larger fonts. Calculation errors keep their visible error
color; normal text uses the chosen color. Borders retain the system palette.

Style edits preserve stored types/source and support history/reopen. Clear all
formatting restores defaults. XLSX maps explicit name, integer size and untinted
RGB font color; it no longer invents Calibri/11 when exporting an unspecified
native font. ODS maps direct font attributes and resolves named font-face
families on import. Theme/indexed/tinted colors, relative/fractional sizes,
unsupported families, rich-text runs and advanced typography remain reported.
Fonts are not embedded and target rendering depends on available fonts. The ODS
mapping follows [text properties in ODF 1.3](https://docs.oasis-open.org/office/OpenDocument/v1.3/os/part3-schema/OpenDocument-v1.3-os-part3-schema.html).
Evidence: `evidence/font-verification.txt`.

9. [Border edges and line styles #83](https://github.com/hybridmachine/DataHorizon/pull/83): locally built/tested, visually inspected and author-reviewed before merge.

## Border edges and line styles

Format Cells → Border independently patches left/right/top/bottom with Keep
existing, None, Thin, Medium, Thick, Dashed or Dotted. Unchosen edges retain each
cell's current style. Legacy All edges/None and menu presets reset prior edge
overrides; explicit edge choices in that same patch then override the preset.

Native-v1 adds `border_left|right|top|bottom` with the six values above. Missing
edges fall back to thin for legacy `border: box`, otherwise none. Qt paints each
cell independently (2/3/4 logical-pixel solid strokes; dashed/dotted use 2).
These are cell edges; outer-selection borders, merged-cell borders, diagonal
edges and conflict reconciliation between adjacent cells remain unavailable.
Colors use the native system palette independently of text color; exports use
black and imports report that border colors map to the native default.

XLSX maps the supported edge style names. ODS maps 0.75/1.5/2.25pt solid widths,
and 0.75pt dashed/dotted, using per-edge attributes; 1px thin imports too.
Unsupported patterns/thicknesses are reported. All-thin edges may canonicalize
to `box` on import; effective edges and inputs are preserved, with native exact
metadata retained through history/reopen. See [ODF border properties](https://docs.oasis-open.org/office/OpenDocument/v1.3/os/part3-schema/OpenDocument-v1.3-os-part3-schema.html).
Evidence: `evidence/border-edges-verification.txt`.
