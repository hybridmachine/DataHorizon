# SpreadsheetML specification coverage

November 11, 2026 | Increment 1 of the [Excel fidelity programme](excel-fidelity-plan.md), section 6

This is the clause-by-clause check of the programme's gap table (plan section 3)
against ECMA-376 Part 1, fifth edition, section 18 (SpreadsheetML). It lists each
construct once, with what DataHorizon does with it today and which increment owns
it. It describes the standard in our own words; read the standard for the
definitions.

**Clause numbers.** Rows marked with a clause were checked against Microsoft's
Open XML SDK reference pages for `worksheet`, `workbook`, `c`, `f`, `styleSheet`
and `xf`, which cite ISO/IEC 29500-1 (the same SpreadsheetML
subclause numbering as ECMA-376 Part 1 section 18). A clause in *italics* (or marked *unverified*)
was written from knowledge and not checked against a source; the fifth-edition text
itself was not consulted. (The SDK page for `legacyDrawing` was read too, but it
gives only its parents' subclauses, so its own row stays unverified.)

**Status today** (after increment 1):

- *imported*: stored natively (possibly converted, with a report entry).
- *reported*: not imported; every occurrence produces a report entry named in the
  next column, before the user applies the import. Apart from the two rows marked
  **not reported** (a cell's `extLst` and the document core properties), nothing
  in a valid package is dropped without an entry (`tests/test_corpus.py` checks
  this for the corpus).
- *rejected*: a malformed or unsafe use rejects the package (`INVALID_SPREADSHEET`
  or `LIMIT`); valid use is imported or reported.
- *no content*: carries nothing to lose in the form found (for example a default
  value, or derived data DataHorizon recomputes); not reported, so a default file
  shows no noise.

`sheet.feature` and `workbook.feature` entries have severity `omitted`, the feature
id as detail and a count of items. Owner: the increment of the
[plan](excel-fidelity-plan.md#4-the-ten-increments) that adds the feature, or the
non-goal (NG) of plan section 2 that excludes it. **Call** marks an owner assigned
by this check (listed at the end of plan section 14).

## Worksheet (`worksheet`, §18.3.1.99), children in schema order

| Element | Clause | Holds | Status | Report entry | Owner |
| --- | --- | --- | --- | --- | --- |
| `sheetPr` | §18.3.1.82 | Tab colour, outline summary positions, fit-to-page flag, code name | reported (tab colour, fit to page); no content otherwise | `sheet.feature` `tab_colour`, `page_setup` | 2 (tab colour); NG print setup |
| `dimension` | §18.3.1.35 | The used range, derived from the cells | no content | — | — |
| `sheetViews` / `sheetView` / `pane` / `selection` | §18.3.1.88 | Window view: gridlines, zoom, right-to-left, frozen or split panes, selection | reported when a pane or non-default view state is present; selection is no content | `frozen_panes`, `split_panes`, `sheet_view` | 2 (frozen panes); **Call** NG (split panes, zoom, gridlines, right-to-left) |
| `sheetFormatPr` | §18.3.1.81 | Default row height and column width, zero-height rows | the default column width is used to judge column widths; reported only when the default row height is marked custom or rows are hidden by default | `sheet_format_defaults` | 2 |
| `cols` / `col` | §18.3.1.17 | Column widths, hidden columns, outline level, column default style | reported per column: a width only when marked custom or more than 0.1 character from the default width (from `sheetFormatPr`, else the most common width) | `column_widths`, `hidden_rows_columns`, `outline`, `row_column_styles` | 2 (sizes, hidden, outline as hidden); 3 (default style) |
| `sheetData` / `row` | §18.3.1.80, §18.3.1.73 | Rows and cells; row height, hidden, outline level, row default style | cells imported; row attributes reported per row (a height only when marked custom) | `row_heights`, `hidden_rows_columns`, `outline`, `row_column_styles` | 2, 3 |
| `sheetCalcPr` | §18.3.1.79 | Full recalculation on load | no content (DataHorizon recalculates) | — | — |
| `sheetProtection` | §18.3.1.85 | Sheet protection flags and password hash | reported when protection is on | `sheet_protection` | NG protection |
| `protectedRanges` | §18.3.1.72 | Ranges editable under protection | reported | `protected_ranges` | NG protection |
| `scenarios` | §18.3.1.76 | What-if scenarios | reported | `scenarios` | **Call** NG (what-if tools) |
| `autoFilter` | §18.3.1.2 | Filter buttons and criteria on a range | reported | `auto_filter` | **Call** 8a (with Excel tables onto native lists) |
| `sortState` | §18.3.1.92 | The last sort's keys | reported | `sort_state` | **Call** NG (sort is an operation, not stored state) |
| `dataConsolidate` | §18.3.1.29 | Consolidation settings | reported | `data_consolidation` | **Call** NG (what-if tools) |
| `customSheetViews` | §18.3.1.27 | Named custom views | reported | `custom_views` | **Call** NG (view state) |
| `mergeCells` / `mergeCell` | §18.3.1.55 | Merged ranges | reported per range | `merged_cells` | 2 |
| `phoneticPr` | §18.4.3 | Settings for phonetic guides | no content (the guides themselves are reported) | — | — |
| `conditionalFormatting` / `cfRule` | §18.3.1.18 | Conditional format rules | reported per rule | `conditional_formatting` | 7 |
| `dataValidations` / `dataValidation` | §18.3.1.33 | Validation rules | reported per rule | `data_validation` | 6 |
| `hyperlinks` / `hyperlink` | §18.3.1.48 | Links on cells | reported per link | `hyperlinks` | 5 |
| `printOptions` | §18.3.1.70 | Print gridlines, headings, centring | reported when any is on | `page_setup` | NG print setup |
| `pageMargins` | §18.3.1.62 | Page margins | reported unless Excel's, the older Excel/openpyxl or LibreOffice's default margins | `page_setup` | NG print setup |
| `pageSetup` | §18.3.1.63 | Orientation, scale, fit, paper | reported for print choices; paper size and resolution are no content | `page_setup` | NG print setup |
| `headerFooter` | §18.3.1.46 | Headers and footers | reported when a text goes beyond the stock LibreOffice texts (sheet name, page number) | `page_setup` | NG print setup |
| `rowBreaks`, `colBreaks` | §18.3.1.74, §18.3.1.14 | Manual page breaks | reported | `page_setup` | NG print setup |
| `customProperties` | §18.3.1.23 | Custom sheet properties | reported | `custom_properties` | **Call** NG |
| `cellWatches` | §18.3.1.9 | Watch window entries | reported | `cell_watches` | **Call** NG (view state) |
| `ignoredErrors` | §18.3.1.51 | Suppressed error indicators | reported per rule | `ignored_errors` | **Call** NG (no error indicators natively) |
| `smartTags` | §18.3.1.90 | Smart tags on cells | reported | `smart_tags` | **Call** NG (deprecated feature) |
| `drawing` | §18.3.1.36 | The sheet's DrawingML layer: shapes, pictures, charts | reported | `drawings` | NG charts, drawings, images |
| `legacyDrawing` | *unverified* (§18.3.1) | VML layer: comment boxes, form controls | reported as comments when the sheet has a comments part, otherwise as legacy drawings | `comments` or `legacy_drawings` | 5 (comments); NG (shapes, controls) |
| `legacyDrawingHF` | *unverified* (§18.3.1) | VML pictures in headers and footers | reported | `page_setup` | NG print setup |
| `drawingHF` | §18.3.1.37 | DrawingML pictures in headers and footers | reported | `page_setup` | NG print setup |
| `picture` | §18.3.1.67 | Sheet background picture | reported | `background_image` | NG images |
| `oleObjects` | §18.3.1.60 | Embedded OLE objects | reported per object | `ole_objects` | NG |
| `controls` | §18.3.1.21 | Embedded ActiveX and form controls | reported per control | `form_controls` | NG (code-bearing) |
| `webPublishItems` | §18.3.1.98 | Web publishing items | reported | `web_publish` | **Call** NG (deprecated feature) |
| `tableParts` / `tablePart` | §18.3.1.95 | Excel tables on the sheet (`xl/tables`) | reported per table | `excel_tables` | 8a |
| `extLst` | §18.2.10 | Later-version data: x14 sparklines, conditional formats, validations, slicers | named items reported per item (an x14 twin of a rule already counted is not counted again); any other extension reported once | `sparklines`, `conditional_formatting`, `data_validation`, `slicers`, `protected_ranges`, `ignored_errors`, `extensions` | 7, 6 (x14 rules); NG (sparklines, slicers) |
| foreign elements (`mc:AlternateContent`) | Part 3 | Markup-compatibility wrappers, usually around controls | reported | `extensions` | NG |

## Workbook (`workbook`), children in schema order

| Element | Clause | Holds | Status | Report entry | Owner |
| --- | --- | --- | --- | --- | --- |
| `fileVersion` | §18.2.13 | Application that wrote the file | no content | — | — |
| `fileSharing` | §18.2.12 | Read-only recommendation, reservation password | reported when present with settings | `workbook.feature` `file_sharing` | NG protection |
| `workbookPr` | §18.2.28 | 1900/1904 date system, code name, defaults | date system imported; rest no content | — | — |
| `workbookProtection` | §18.2.29 | Structure, window and revision locks | reported when a lock is on | `workbook_protection` | NG protection |
| `bookViews` | §18.2.1 | Window positions, active tab | no content (view state) | — | — |
| `sheets` / `sheet` | §18.2.20 | Sheet names, order, visibility | names and order imported; hidden and very hidden reported, with the sheets as samples | `hidden_sheets` | 2 |
| `functionGroups` | §18.2.15 | Macro function categories | reported | `function_groups` | NG VBA and macro sheets |
| `externalReferences` | §18.2.9 | Links to other workbooks | reported per link; formulas that use them are kept and reported | `external_references`; `formula.external_reference` | NG external links |
| `definedNames` / `definedName` | §18.2.6 | Workbook and sheet-scoped names, print areas | reported per name; formulas that use names are kept and reported | `defined_names`; `formula.name` | 8a |
| `calcPr` | §18.2.2 | Calculation mode, iteration, precision as displayed | reported when manual, iterative or reduced precision | `calculation_settings` | **Call** 10 |
| `oleSize` | §18.2.16 | Size when embedded | no content | — | — |
| `customWorkbookViews` | §18.2.4 | Named custom views | reported per view | `custom_views` | **Call** NG (view state) |
| `pivotCaches` | §18.2.18 | Pivot caches | reported through the pivot table parts | `pivot_tables` | NG pivot tables |
| `smartTagPr`, `smartTagTypes` | §18.2.21, §18.2.23 | Smart tag settings | reported | `smart_tags` | **Call** NG |
| `webPublishing`, `webPublishObjects` | §18.2.24, §18.2.26 | Web publishing settings | reported | `web_publish` | **Call** NG |
| `fileRecoveryPr` | §18.2.11 | Recovery settings | no content | — | — |
| `extLst` | §18.2.10 | x15 workbook properties and calculation features (no content); slicer caches, timelines, data model | named items reported; Excel's default extensions are no content; others reported | `slicers`, `data_model`, `pivot_tables`, `extensions` | NG |
| foreign elements (`mc:AlternateContent` absolute path, `xr:revisionPtr`) | Part 3 | Where the file was saved, revision pointers | no content | — | — |

## Cell (`c`, §18.3.1.4)

| Attribute or child | Clause | Holds | Status | Report entry | Owner |
| --- | --- | --- | --- | --- | --- |
| `r` | §18.3.1.4 | Cell reference | imported; invalid or duplicate rejects | — | — |
| `s` | §18.3.1.4 | Index into `cellXfs` | imported through the style mapping; an invalid index rejects | `style.*` | 3, 4 |
| `t="n"` (default) | §18.18.11 | Number | imported; out-of-range numbers become text | `cell.number_out_of_range` | — |
| `t="s"` | §18.18.11 | Shared string index | imported; invalid index rejects | — | — |
| `t="str"` | §18.18.11 | Formula string result | imported | — | — |
| `t="inlineStr"` | §18.18.11 | Inline rich string (`is`, §18.3.1.53) | text imported; formatted runs reported | `sheet.feature` `rich_text` | 5 |
| `t="b"` | §18.18.11 | Boolean | imported; invalid value skipped | `cell.invalid_value` | — |
| `t="d"` | §18.18.11 | ISO 8601 date | imported as a native date/time | `cell.invalid_value` | — |
| `t="e"` | §18.18.11 | Error value | imported as text | `cell.error_value` (converted) | 8a |
| other `t` | — | Not in the standard | cell skipped | `cell.unsupported_type` | — |
| `v` | §18.3.1.96 | Value or formula cache | imported (values mode, mixed fallback) | — | — |
| `f` | §18.3.1.40 | Formula (next table) | imported or degraded | `formula.*` | 8 |
| `cm` | §18.3.1.4 | Cell metadata index (dynamic arrays) | no content beyond the array formula, which is reported | `formula.array` | 8b |
| `vm` | §18.3.1.4 | Value metadata (rich values: pictures in cells, data types) | reported per cell; the cached value is imported | `sheet.feature` `rich_values` | **Call** NG (images); data types NG |
| `ph` | §18.3.1.4 | Show phonetic guide | reported per cell | `sheet.feature` `phonetic` | **Call** 5 (with rich text) |
| `extLst` | §18.2.10 | Later-version cell data | **not reported** (the only unreported element in this table): the reader does not look inside a cell beyond `f`, `v` and `is`, and no corpus file has one | — | **Call** open: report it if an application-saved file shows one |

## Formula (`f`, §18.3.1.40)

| Attribute or type | Holds | Status | Report entry | Owner |
| --- | --- | --- | --- | --- |
| `t="normal"` | Ordinary formula | imported; unevaluable formulas kept and reported, or replaced by the cache in mixed mode | `formula.unsupported_function`, `formula.name`, `formula.missing_sheet`, `formula.external_reference`, `formula.unsupported_reference`, `formula.syntax` | 8 |
| `t="shared"`, `si`, `ref` | Shared formula group | expanded per stored member, any extent in the grid; a broken group rejects; large path keeps 65,536 groups open at once per sheet | — | — |
| `t="array"`, `ref`, `aca` | Legacy array formula | kept as unevaluable or replaced by caches | `formula.array` | 8b |
| `t="dataTable"`, `dt2D`, `dtr`, `r1`, `r2`, `del1`, `del2` | What-if data table | caches used or omitted | `formula.data_table` | **Call** NG (what-if tools) |
| `ca` | Recalculate flag | no content (DataHorizon recalculates); invalid value rejects | — | — |
| `bx` | Formula assigns a name (macro sheets) | no content outside macro sheets | — | NG macro sheets |
| other `t` | Not in the standard | rejects | — | — |

## Styles (`styleSheet`), top-level collections and constructs

| Construct | Clause | Holds | Status | Report entry | Owner |
| --- | --- | --- | --- | --- | --- |
| `numFmts` / `numFmt` | §18.8.31, *§18.8.30* | Custom number format codes | twelve fixed formats imported; others reported | `style.number_format` | 4 |
| `fonts` / `font` | §18.8.23, *§18.8.22* | Font family, size, colour, bold, italic, underline, strike, script | family, integer size, RGB colour, bold, italic imported; rest reported | `style.font` | 3 |
| `fills` / `fill` | §18.8.21, *§18.8.20* | Pattern and gradient fills | solid RGB imported; rest reported | `style.fill` | 3 |
| `borders` / `border` | §18.8.5, *§18.8.4* | Edges, line styles, colours, diagonals | four edges, five styles imported; colour approximated; rest reported | `style.border` | 3 |
| `cellStyleXfs` | §18.8.9 | Named style formats | inheritance reported | `style.named_style` | 3 |
| `cellXfs` / `xf` | §18.8.10 | Cell formats referenced by `c/@s`; `applyX`, `quotePrefix`, `pivotButton` | mapped; `quotePrefix` no content (text stays text) | `style.*` | 3 |
| `alignment` | §18.8.1 | Horizontal, vertical, wrap, rotation, indent, shrink | horizontal (3 values) and wrap imported; rest reported | `style.alignment` | 3 |
| `protection` | §18.8.33 | Cell locked and hidden flags | no content while sheet protection is not kept (protection itself is reported) | — | NG protection |
| `cellStyles` / `cellStyle` | §18.8.8, *§18.8.7* | Style names | reported through named-style inheritance | `style.named_style` | 3 |
| `dxfs` / `dxf` | §18.8.15, *§18.8.14* | Formats used by conditional formats and tables | reported through the rules that use them | `conditional_formatting` | 7 |
| `tableStyles` / `tableStyle` | §18.8.42, *§18.8.40* | Table styles | reported through the tables that use them | `excel_tables` | 8a |
| `colors` (indexed palette, MRU) | §18.8.11 | Custom indexed palette | indexed colours reported (the palette is not applied) | `style.font`, `style.fill` | 3 |
| theme colours (`theme`, `tint` on colours) | *§20.1* (DrawingML theme) | Colours from the theme part | reported where a style uses one; the theme part itself is not reported separately | `style.font`, `style.fill` | 3 |
| `extLst` | §18.2.10 | x14 slicer styles and later data | no content until the features that use them exist | — | NG slicers |

## Shared strings (`sst`)

| Construct | Clause | Holds | Status | Report entry | Owner |
| --- | --- | --- | --- | --- | --- |
| `si` with `t` | *§18.4.8* | Plain string | imported | — | — |
| `si` with `r` runs and `rPr` | *§18.4.4* | Rich text runs | text imported; formatted runs reported once per string | `workbook.feature` `rich_text` | 5 |
| `rPh` (phonetic runs) | *§18.4.6* | Phonetic readings | text excluded; reported once per string | `workbook.feature` `phonetic` | **Call** 5 |
| `phoneticPr` | §18.4.3 | Phonetic display settings | no content | — | — |

## Related parts

| Part | Holds | Status | Report entry | Owner |
| --- | --- | --- | --- | --- |
| Tables (`xl/tables/*`) | Excel table definitions | reported through `tableParts`; structured references kept as unevaluable formulas | `excel_tables`; `formula.syntax` | 8a |
| Comments (`xl/comments*`) and threaded comments | Notes and threads | reported per sheet through the sheet's relationships, counting the comments in the part (1 when it is missing or unreadable) | `comments` | 5 |
| Sheet relationship parts (`xl/worksheets/_rels/*.rels`) | Links from a sheet to its comments, drawings, tables, hyperlink targets | read as a stream for the report; absent or empty declares nothing; unreadable is reported; a DTD or entity rejects | `relationships_unreadable` | — |
| Drawings and charts (`xl/drawings/*`, `xl/charts/*`, `xl/media/*`) | Shapes, pictures, charts | drawings reported per sheet; charts reported per part | `drawings`, `workbook.feature` `charts` | NG charts, drawings, images |
| Chart sheets, dialog sheets, macro sheets | Sheets that are not worksheets | reported, one entry per kind with the sheets as samples; no native sheet is created; choosing one at selection scope is `INVALID_ARGUMENT` | `workbook.feature` `chart_sheets`, `dialog_sheets`, `macro_sheets` | **Call** NG (charts, macro sheets) |
| Pivot tables and caches (`xl/pivotTables/*`, `xl/pivotCache/*`) | Pivot tables | reported per pivot table part | `pivot_tables` | NG pivot tables |
| External links (`xl/externalLinks/*`) | Linked workbooks and their cached values | reported per `externalReference` | `external_references` | NG external links |
| Defined-name scopes (`localSheetId`) | Sheet-scoped names | counted with the names | `defined_names` | 8a |
| Calculation chain (`xl/calcChain.xml`) | Excel's last calculation order | no content (derived; DataHorizon builds its own dependencies) | — | — |
| Theme (`xl/theme/*`) | Colours and fonts | reported only through the styles that use theme colours | `style.*` | 3 |
| Custom XML (`customXml/item*.xml`) | Data islands | reported per item | `custom_xml` | NG |
| VBA (`xl/vbaProject.bin`) | Macro code | reported; never activates | `vba` | NG VBA |
| Connections and query tables (`xl/connections.xml`, `xl/queryTables/*`) | External data connections | reported | `connections`, `query_tables` | NG external data |
| Embeddings (`xl/embeddings/*`) | Embedded objects | reported per part | `embedded_objects` | NG |
| Slicers, timelines (`xl/slicers/*`, `xl/timelines/*`) | Slicer definitions | reported per part | `slicers` | NG slicers |
| Data model (`xl/model/*`) | Power Pivot model | reported | `data_model` | NG data model |
| Rich values and metadata (`xl/richData/*`, `xl/metadata.xml`) | Rich cell values, dynamic-array metadata | reported through the cells (`vm`, array formulas) | `rich_values`, `formula.array` | NG / 8b |
| Custom document properties (`docProps/custom.xml`) | User-defined document properties | reported | `document_properties` | **Call** NG |
| Core and app properties (`docProps/core.xml`, `app.xml`) | Author, dates, application statistics | **not reported**: every file has them and they describe the file, not the workbook's content | — | **Call** open for the owner (section 14) |
| Printer settings (`xl/printerSettings/*`) | Printer driver data | no content (printer specific) | — | NG print setup |

## ODS counterparts

The ODS readers report the same feature ids where OpenDocument has the feature:
spanned and covered cells (`merged_cells`), column styles whose width differs
from the sheet's most common width (`column_widths`), row styles with a custom
height (`row_heights`), collapsed or filtered rows and columns
(`hidden_rows_columns`), row and column groups (`outline`), column default cell
styles other than `Default` (`row_column_styles`), content validations used by
the sheet's cells (`data_validation`, counted per validation), annotations
(`comments`), links in cell text (`hyperlinks`), styled spans in cell text
(`rich_text`), LibreOffice conditional formats or, without them, cell styles
with `style:map` conditions (`conditional_formatting`), filtered database ranges
(`auto_filter`), protected tables (`sheet_protection`), print ranges
(`page_setup`), shapes and frames (`drawings`), scenarios, form controls; and for
the workbook: named ranges and expressions (`defined_names`), DDE links and
linked sheets (`external_references`), tables whose style hides them
(`hidden_sheets`), structure protection (`workbook_protection`), iteration and
precision-as-shown (`calculation_settings`), tracked changes, label ranges,
consolidation, data pilot tables (`pivot_tables`), embedded objects and Basic
macros (`macros`). Frozen and split panes are read from `settings.xml`; an
unreadable `settings.xml` skips that detection and is reported
(`view_settings_unreadable`). A spanning cell repeated across columns counts once
per repeat. Not
detected in ODS: sheet tab colour and page styles (master pages with headers
and footers), because LibreOffice writes view and page defaults in every file
and telling a chosen value from its default needs a reference to the producing
application's defaults; both are listed for the owner in plan section 14.
