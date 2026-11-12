# Interchange corpus

The corpus is the set of real and hand-built workbooks that the Excel-fidelity programme measures itself
against. `provenance.json` lists every corpus file; `tests/corpus_scorecard.py` imports each one in `formulas`,
`mixed` and `values` mode and writes `docs/design/evidence/excel-fidelity-scorecard.json`;
`tests/test_corpus.py` keeps the list, the files and the scorecard honest.

## Contents today

| Group | Files | Producer |
| --- | --- | --- |
| Existing fixtures (referenced in place, not copied) | `tests/fixtures/{fidelity,inputs,markup,unsupported}.xlsx`, `tests/fixtures/{fidelity,inputs}.ods` | openpyxl 3.1.5 / odfpy 1.4.1 |
| Hand-built feature workbooks | the 15 `.xlsx` and 3 `.ods` files in this directory | `generate_feature_workbooks.py` (DataHorizon test generator, standard library) |

The feature workbooks each exercise one feature area (merged cells, widths/heights/hidden/frozen, custom number
formats, conditional formatting, data validation, hyperlink and comment, defined names, array and data-table
formulas, an Excel table, theme/indexed colours with a named style, the strict namespace, shared formulas, error
values, 3D and external references, a chart sheet beside a worksheet; ODS: merged cells, number style, validation). **They are hand-built
approximations** of what Excel or LibreOffice would write, kept minimal where the exact form was uncertain
(for example the theme holds only a colour scheme and the comment drawing is a bare VML note). They await
replacement by application-saved files and are labelled `hand_built: true`. Their producer is the generator,
never an application.

## How to read the scorecard

Each file is imported in `formulas`, `mixed` and `values` mode and gets one status per mode:

- `clean`: the importer produced no report entry. **This is not a fidelity claim**: it says only that nothing
  was reported as changed or left out. Since increment 1, every worksheet and workbook feature the importer does
  not read is reported (category `sheet.feature` or `workbook.feature`, detail the feature id), so a file with
  such a feature is `degraded`, not `clean`. Fidelity is measured by what each entry says, not by the status.
- `degraded`: at least one report entry says something was changed, converted or not imported.
- `rejected`: the import failed; the error code and message are recorded. A rejection in `mixed` or `formulas`
  mode fails the test.

Each provenance entry lists `unsupported_features`: the features in that file DataHorizon does not yet import
(short stable ids such as `merged_cells`). `FEATURE_REPORTS` in `tests/corpus_scorecard.py` names, for each id,
the report entry that must be present for the feature to count as reported (for example `merged_cells` needs a
`sheet.feature` entry with detail `merged_cells`; `defined_names` needs `workbook.feature`/`defined_names` or a
`formula.name` entry; `theme_colours` needs a `style.font` or `style.fill` entry whose detail mentions theme).
`summary.known_unreported` lists, per mode and whatever the file's status, every (file, feature) whose required
entry is absent: the measure of silent loss. **It is empty for every mode**, and `tests/test_corpus.py` asserts
that it stays empty. In `values` mode the formula-only ids (`array_formulas`, `data_tables`, `3d_references`)
need no entry, because that mode replaces every formula by its cached value by request and says so in its scope
line. `summary.status_meaning` repeats these meanings in the data itself.

Two caveats. The `theme_colours` check accepts the importer's general font and fill messages, which name theme
colours among other causes, so it shows that the colour was reported, not that it was recognised as a theme
colour. And the check only covers the ids a file lists: a feature present in a file but missing from its
`unsupported_features` is not checked, so list every feature when adding a file.

To extend the check when adding a file with a feature not yet listed, add its id to the file's
`unsupported_features` and to `FEATURE_REPORTS` with the entry that reports it (and, for a formula-only
feature, to `FEATURE_REPORTS_VALUES_MODE`); `test_every_listed_feature_id_has_a_required_report` fails until
both are done.

## Open gaps

These are recorded in the scorecard `summary.gaps` and clear automatically once a file with the matching
`producer` is listed.

1. **No Excel-saved files.** None has been supplied by the owner (design plan section 14, item 1). The
   increment is not complete until at least one is added or the owner waives it in writing in the design document.
2. **No LibreOffice-saved files.** LibreOffice (`soffice`) is not installed on the reference machine and was not
   installed without the owner's agreement. With it, re-save each seed with
   `soffice --headless --convert-to xlsx` and `--convert-to ods` into this directory, covering merged cells,
   custom number formats, conditional formatting, validation, comments, hyperlinks, defined names, array
   formulas, an Excel table and theme colours.

## Adding a file saved by an application

1. Save the workbook in the real application (do not re-save it elsewhere; the last saver is the producer). It
   must be small (the whole corpus stays under 2 MiB) and contain no personal data.
2. Copy it into this directory.
3. Add an entry to `provenance.json` (a list, sorted by `file`):
   `file` (path from the repository root), `sha256` (`shasum -a 256 FILE`), `producer` (`Microsoft Excel` or
   `LibreOffice Calc`), `producer_version` (from the application's About box, or `docProps/app.xml`
   `AppVersion` for Excel), `saved_on` (`YYYY-MM-DD`), `exercises` (list of feature areas), `source` (who
   supplied it and how it was made), `format` (`xlsx` or `ods`) and `hand_built` (`false`).
4. Regenerate the scorecard: `PYTHONPATH=service python3 tests/corpus_scorecard.py`
   (`--check` verifies the checked-in file instead of writing it).
5. Run `PYTHONPATH=service python3 -m unittest discover -s tests -p test_corpus.py -v`.

A file the importer rejects in `mixed` or `formulas` mode is a finding to fix in the importer, never a reason to
leave the file out.

Regenerating the feature workbooks: `python3 tests/fixtures/corpus/generate_feature_workbooks.py`
(`--check` compares with the files on disk). Their ZIP times are fixed, so the output is byte-identical.
