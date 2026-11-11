# Interchange corpus

The corpus is the set of real and hand-built workbooks that the Excel-fidelity programme measures itself
against. `provenance.json` lists every corpus file; `tests/corpus_scorecard.py` imports each one in `formulas`,
`mixed` and `values` mode and writes `docs/design/evidence/excel-fidelity-scorecard.json`;
`tests/test_corpus.py` keeps the list, the files and the scorecard honest.

## Contents today

| Group | Files | Producer |
| --- | --- | --- |
| Existing fixtures (referenced in place, not copied) | `tests/fixtures/{fidelity,inputs,markup,unsupported}.xlsx`, `tests/fixtures/{fidelity,inputs}.ods` | openpyxl 3.1.5 / odfpy 1.4.1 |
| Hand-built feature workbooks | the 14 `.xlsx` and 3 `.ods` files in this directory | `generate_feature_workbooks.py` (DataHorizon test generator, standard library) |

The feature workbooks each exercise one feature area (merged cells, widths/heights/hidden/frozen, custom number
formats, conditional formatting, data validation, hyperlink and comment, defined names, array and data-table
formulas, an Excel table, theme/indexed colours with a named style, the strict namespace, shared formulas, error
values, 3D and external references; ODS: merged cells, number style, validation). **They are hand-built
approximations** of what Excel or LibreOffice would write, kept minimal where the exact form was uncertain
(for example the theme holds only a colour scheme and the comment drawing is a bare VML note). They await
replacement by application-saved files and are labelled `hand_built: true`. Their producer is the generator,
never an application.

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
