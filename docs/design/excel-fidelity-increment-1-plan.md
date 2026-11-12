# Excel Fidelity Increment 1 (Import Foundations) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real XLSX and ODS workbooks import without one unsupported cell rejecting the file, at sizes up to a measured limit, with a structured conversion report, and export back the same way.

**Architecture:** The importer stops rejecting unsupported content and records it in a structured report; formulas DataHorizon cannot evaluate are stored with their source. A lenient reference scanner keeps references correct in formulas the calculator cannot parse. Large files take a new path: streamed upload to a private staging directory, streaming XML read into an on-disk stage database, review of a report and sample, then one atomic engine operation that loads the stage. Export mirrors it with a streamed download.

**Tech Stack:** Python 3.11+ standard library service (`service/datahorizon`), SQLite, Qt 6 Widgets/C++17 (`desktop/`), `unittest`, Qt Test, CMake/CTest. openpyxl/odfpy remain build-only verification tools (`tests/requirements-fidelity.txt`).

**Spec:** [excel-fidelity-plan.md](excel-fidelity-plan.md), sections 5 and 6. Executors read both.

**Precondition:** branch from `main` at or after commit `bef8404`. Line numbers below refer to that commit; re-locate by symbol name if they have moved.

## Global Constraints

- The service uses the Python standard library only. No new runtime dependency.
- The GUI never touches SQLite. Every new behaviour is reachable through REST, CLI and SDK, with an OpenAPI schema and an executed example.
- `MAX_CELLS = 10_000` in `engine.py` does not change. Selection-scope import and export keep their existing routes, limits and hooks.
- No silent loss: anything skipped, converted or approximated has a report entry before Apply or save.
- A malformed package (bad ZIP, invalid XML, broken relationships, encryption, DTD or entity) still rejects the whole file with `INVALID_SPREADSHEET`. Degradation is only for valid content DataHorizon does not support.
- The package is never extracted to disk, no external resource is fetched, and imported code never activates.
- Whole-workbook and additional-sheets import are each one transaction, clear Undo/Redo, raise the existing structural event and no per-cell change events. An activated required handler that has not opted into the structural event rejects before any change.
- Large-path targets, to be replaced by measured values in Task 19: 1,000,000 stored cells, 256 MiB expanded, 64 MiB compressed, 255 sheets, 300 seconds per preparation, import or export.
- Report severities are exactly `converted`, `approximated`, `omitted`, `unevaluable`. At most 100 sample locations per entry and 200 entries per report.
- Import modes are exactly `formulas`, `mixed`, `values`. The API default stays `formulas`; the desktop preselects `mixed`.
- Every source file starts with the SPDX line `GPL-3.0-only`. Qt widgets get `objectName`s. User-visible strings use `tr()`.
- Errors use `require(condition, CODE, message, status)` from `engine.py`.
- Commands, from the repository root:
  - One Python file: `PYTHONPATH=service python3 -m unittest discover -s tests -p <file> -v`
  - Python suite: `PYTHONPATH=service python3 -m unittest discover -s tests -v`
  - Build: `cmake --build build --parallel`
  - One Qt slot: `QT_QPA_PLATFORM=offscreen DATAHORIZON_VIEW_STATE=$PWD/build/test-views.ini ./build/desktop_tests <slot>`
  - Everything: `ctest --test-dir build --output-on-failure`
- One branch and pull request per group: A (tasks 1–6), B (7–9), C (10–16), D (17–18), E (19–21). Each task ends with a commit. Record evidence as `docs/design/evidence/excel-fidelity-1-task<N>.json` using the keys of `python-automation-task6.json`.

## Review Focus

Inputs the design implies but that are easy to miss. Each has a test in the task that owns the code.

1. **Formula text that only looks like a reference** (`="see A1"`, a sheet named `'It''s [1] A1'`, `{"A1","B2"}`): a structural edit or shared-formula expansion leaves it byte for byte unchanged. Test in task 7.
2. **A ZIP entry whose real expanded size exceeds its declared size**: the streaming reader counts the bytes it actually reads and rejects at the limit; it does not trust the header. Test in task 10.
3. **A sheet where every cell degrades, or a sheet with no cells**: the sheet is still created and the import is still offered. Test in task 5.
4. **A file replaced or a stage altered between review and Apply**: Apply is refused with `STALE_PLAN` because the digest no longer matches what was reviewed. Test in task 14.
5. **One problem repeated 500,000 times** (the same unsupported function down a column): one report entry with `count` 500000 and 100 samples, not 500,000 entries or a multi-megabyte response. Test in task 1.

---

## File structure

| File | Responsibility |
| --- | --- |
| `service/datahorizon/conversion_report.py` (new) | Structured report: add, merge, serialise, legacy text lines |
| `service/datahorizon/formula_admission.py` (new) | Decide whether DataHorizon can evaluate a formula, and why not |
| `service/datahorizon/xlsx_future_functions.py` (new) | The `_xlfn.` function-name table |
| `service/datahorizon/formula_refs.py` (new) | Lenient reference scanner and rewriter for formulas the calculator cannot parse |
| `service/datahorizon/import_stage.py` (new) | On-disk stage database for a prepared import: sheets, cells, styles, report, digest |
| `service/datahorizon/staging.py` (new) | Private staging directory: uploads, stages, exports, retention |
| `service/datahorizon/xlsx_stream.py` (new) | Streaming XLSX package reader and writer |
| `service/datahorizon/ods_stream.py` (new) | Streaming ODS reader and writer |
| `service/datahorizon/interchange.py`, `ods.py`, `xlsx_styles.py` | Degrade instead of block; strict namespaces; shared cell conversion used by both paths |
| `service/datahorizon/formula.py` | Fall back to `formula_refs` when the strict parser fails |
| `service/datahorizon/engine.py` | `unmaintained` kind and v7 gate; `workbook_import` from a stage; additional sheets |
| `service/datahorizon/service.py`, `client.py`, `cli.py` | Upload, stage, export-job and download routes; SDK and CLI |
| `desktop/mainwindow.cpp`, `restclient.cpp/.h`, `conversionreportview.h` (new) | Upload/download, mode chooser, grouped report, add-sheets target |
| `tests/xlsx_builder.py` (new) | Hand-built XLSX packages for tests |
| `tests/corpus_scorecard.py`, `tests/qualify_large_interchange.py`, `tests/fixtures/corpus/` (new) | Corpus runner and capacity measurement |

---

## Group A — Degrade instead of block

### Task 1: Structured conversion report

**Files:**
- Create: `service/datahorizon/conversion_report.py`
- Test: `tests/test_conversion_report.py`

**Interfaces:**
- Produces:
  - `SEVERITIES = ("converted","approximated","omitted","unevaluable")`
  - `class ConversionReport`: `add(category: str, severity: str, detail: str = "", *, sheet: str | None = None, cell: str | None = None, count: int = 1) -> None`; `merge(other: ConversionReport) -> None`; `entries() -> list[dict]`; `lines() -> list[str]`; `totals() -> dict[str,int]`
  - An entry is `{"category","severity","detail","count","samples":[{"sheet","cell"}]}`. Entries are keyed by `(category, severity, detail)`. `entries()` is sorted by severity in `SEVERITIES` order reversed (`unevaluable` first), then count descending, then category.
  - `lines()` gives one human sentence per entry (`"12 cells: VLOOKUP is not available (unevaluable); first at Sheet1!B3"`) for the existing `report` list.

- [ ] **Step 1: Write the failing tests**

```python
def test_repeated_problem_is_one_entry_with_bounded_samples(self):
    report=ConversionReport()
    for row in range(1,500001):
        report.add("formula.unsupported_function","unevaluable","VLOOKUP",sheet="Data",cell=f"B{row}")
    (entry,)=report.entries()
    self.assertEqual((entry["count"],len(entry["samples"])),(500000,100))
    self.assertEqual(entry["samples"][0],{"sheet":"Data","cell":"B1"})
    self.assertLess(len(json.dumps(report.entries())),16384)

def test_entry_cap_folds_overflow_into_one_entry(self):
    report=ConversionReport()
    for index in range(250): report.add("style.number_format","omitted",f"code {index}")
    entries=report.entries()
    self.assertEqual(len(entries),200)
    self.assertEqual(entries[-1]["category"],"report.truncated")
    self.assertEqual(sum(e["count"] for e in entries),250)

def test_unknown_severity_is_a_programming_error(self):
    with self.assertRaises(ValueError): ConversionReport().add("x","lost")
```

Also test `merge` (counts add, samples stay at most 100) and `totals()` (`{"unevaluable":…,"omitted":…}` with every severity present, zero when unused).

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest discover -s tests -p test_conversion_report.py -v` — expected FAIL, module missing.
- [ ] **Step 3: Implement `ConversionReport`** as specified. `detail` is truncated to 160 characters.
- [ ] **Step 4: Run the file** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add the structured conversion report"`

### Task 2: Formula admission and the `mixed` mode for XLSX

**Files:**
- Create: `service/datahorizon/formula_admission.py`, `service/datahorizon/xlsx_future_functions.py`, `tests/xlsx_builder.py`
- Modify: `service/datahorizon/interchange.py` (`mode`, `local_formula`, `xlsx_formula_to_native`, `native_formula_to_xlsx`, `_xlsx_preview` cell loop)
- Test: `tests/test_formula_admission.py`, `tests/test_xlsx_degrade.py`

**Interfaces:**
- Consumes: `ConversionReport` (Task 1).
- Produces:
  - `formula_admission.status(source: str, sheet_names: Collection[str] | None) -> tuple[str, str] | None`. `None` means the calculator can evaluate it. Otherwise `(category, detail)` with category one of `formula.unsupported_function` (detail: the function name, upper case), `formula.external_reference`, `formula.missing_sheet` (detail: the sheet name), `formula.array`, `formula.name` (detail: the identifier), `formula.syntax`.
  - `xlsx_future_functions.FUTURE: frozenset[str]` and `PREFIX: dict[str,str]` mapping an upper-case name to its file spelling (`"XLOOKUP" → "_xlfn.XLOOKUP"`, `"FILTER" → "_xlfn._xlws.FILTER"`). Build it from Microsoft's published `_xlfn.` function list; it replaces `FUTURE_XLSX_FUNCTIONS`.
  - `interchange.mode(request)` accepts `formulas`, `mixed`, `values`.
  - `tests/xlsx_builder.py`: `workbook_bytes(sheets: dict[str, str], *, strict: bool = False, styles_xml: str | None = None, shared_strings: list[str] | None = None, extra_parts: dict[str, bytes] | None = None) -> bytes`, where each value is the inner XML of `<sheetData>`. `strict=True` uses the `http://purl.oclc.org/ooxml/...` namespaces.
  - `_xlsx_preview` result gains `"report_entries": list[dict]` and `"totals": dict`. `"report"` stays a list of strings (fixed scope lines followed by `ConversionReport.lines()`).

Behaviour, formula cells:

| Mode | Evaluable | Not evaluable |
| --- | --- | --- |
| `formulas` | formula | formula stored verbatim (after `_xlfn.` removal); entry with severity `unevaluable` |
| `mixed` | formula | cached value as a literal, entry `converted` under the same category; if the cache is missing or an error value, the formula is kept as in `formulas` |
| `values` | cached value | cached value |

`local_formula` keeps its strictness for selection-scope **export** bounds checks only; import no longer calls it.

- [ ] **Step 1: Write the failing tests**

```python
# test_formula_admission.py
CASES=[("=SUM(A1:A3)",None),
       ("=VLOOKUP(A1,B:C,2,0)",("formula.unsupported_function","VLOOKUP")),
       ("=[1]Sheet1!A1",("formula.external_reference","")),
       ("=Other!A1",("formula.missing_sheet","Other")),
       ("=Rate*2",("formula.name","Rate")),
       ("={1,2}",("formula.array","")),
       ("=SUM(A1:A3",("formula.syntax",""))]
def test_status(self):
    for source,expected in CASES:
        with self.subTest(source): self.assertEqual(status(source,["Sheet1"]),expected)

# test_xlsx_degrade.py
SHEET=('<row r="1"><c r="A1"><v>2</v></c>'
       '<c r="B1"><f>_xlfn.IFS(A1&gt;1,"big")</f><v>big</v></c>'  # not in the 53 → unevaluable
       '<c r="C1"><f>SUM(A1:A1)</f><v>2</v></c></row>')
def test_formulas_mode_keeps_source_and_reports(self):
    result=preview(SHEET,mode="formulas")
    self.assertTrue(result["can_import"]); self.assertEqual(result["issues"],[])
    self.assertEqual(cell(result,"B1"),{"source":'=IFS(A1>1,"big")',"kind":"formula"})
    self.assertIn({"category":"formula.unsupported_function","severity":"unevaluable","detail":"IFS",
                   "count":1,"samples":[{"sheet":"Sheet1","cell":"B1"}]},result["report_entries"])
def test_mixed_mode_uses_cache_for_unevaluable_only(self):
    result=preview(SHEET,mode="mixed")
    self.assertEqual(cell(result,"B1"),{"source":"big","kind":"text"})
    self.assertEqual(cell(result,"C1")["kind"],"formula")
```

Add: `mixed` with a missing cache keeps the formula; round trip of `=IFS(...)` through `xlsx_export` writes `_xlfn.IFS(`; an applied unevaluable formula reads back from the engine with its source intact and a `#NAME?` display.

- [ ] **Step 2: Run both files** — expected FAIL.
- [ ] **Step 3: Implement** `status` by parsing with `formula.Parser` and walking the tree as `local_formula` does today. When the parser raises `FormulaError`, classify in this order, ignoring text inside double-quoted strings: a `[…]` workbook prefix before a sheet name or cell is `formula.external_reference`; a `{` is `formula.array`; anything else is `formula.syntax`. Rewire the cell loop.
- [ ] **Step 4: Run both files and `test_interchange.py`, `test_workbook_interchange.py`, `test_fidelity.py`.** Update existing assertions that expected a blocked import for an unsupported function to expect the new report entry. Expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Import formulas DataHorizon cannot evaluate instead of rejecting the file"`

### Task 3: Cell-level degradation for XLSX

**Files:**
- Modify: `service/datahorizon/interchange.py` (`_xlsx_preview` cell loop), `service/datahorizon/xlsx_styles.py` (`loss` → report)
- Test: `tests/test_xlsx_degrade.py`

**Interfaces:**
- Consumes: `ConversionReport`.
- Produces: `interchange.convert_xlsx_cell(cell, *, context) -> dict | None`, the single per-cell conversion used by this path and by Task 10's streaming path. `context` carries mode, shared-string lookup, styles, date system, sheet names, sheet name and the report. It returns an edit `{"row","col","source","kind","style","replace_style"}` or `None` when the cell is skipped. It raises only for a malformed package.
- `XlsxStyles.loss(message)` becomes `XlsxStyles.loss(category, detail)` writing `omitted`/`approximated` entries into a `ConversionReport` passed to the constructor; `XlsxStyles.report()` is removed.

Rules:

| Input | Result | Entry |
| --- | --- | --- |
| Error cell, no formula (`t="e"`) | Text holding the error string | `cell.error_value`, `converted` |
| `values` mode, cached error | Text holding the error string | `cell.error_value`, `converted` |
| `values` mode, formula with no cache | skipped | `cell.missing_cache`, `omitted` |
| Unknown `t` value | skipped | `cell.unsupported_type`, `omitted`, detail = the type |
| Text longer than 65,536 UTF-8 bytes | skipped | `cell.text_too_long`, `omitted` |
| Number that `typed()` rejects (non-finite, out of range) | Text holding the original characters | `cell.number_out_of_range`, `converted` |
| Serial 60 in the 1900 system with a date format, or other failed date conversion | number kept, date format dropped | `cell.date_conversion`, `approximated` |
| Duplicate cell coordinates, bad shared-string index, bad `r` | **reject the file** (`INVALID_SPREADSHEET`) | — |

`issues` remains in the result for compatibility and is always `[]` for content problems.

- [ ] **Step 1: Write failing tests**, one per table row, each asserting the produced edit (or its absence) and the exact entry. Include `test_same_oversize_shared_string_used_many_times_is_one_entry`: 300 cells referencing one 70,000-byte shared string give one entry with `count` 300.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Extract `convert_xlsx_cell`** from the loop body and apply the table.
- [ ] **Step 4: Run `test_xlsx_degrade.py`, `test_xlsx_styles.py`, `test_temporal_interchange.py`, `test_interchange.py`** — expected PASS after updating assertions that read `styles.report()`.
- [ ] **Step 5: Commit** `git commit -m "Skip and report unsupported XLSX cells instead of rejecting the file"`

### Task 4: The same policy for ODS

**Files:**
- Modify: `service/datahorizon/ods.py` (`ods_to_native`, `_ods_preview`, `OdsStyles`)
- Test: `tests/test_ods_degrade.py`

**Interfaces:**
- Consumes: `ConversionReport`, `formula_admission.status`.
- Produces: `ods.convert_ods_cell(cell, *, context) -> dict | None` with the same contract as `convert_xlsx_cell`; `_ods_preview` result gains `report_entries` and `totals`; modes `formulas`, `mixed`, `values`.

The ODS-specific categories: `formula.unsupported_syntax` (OpenFormula constructs `ods_to_native` cannot translate; the formula is stored as text of its original `of:` source in `formulas` mode with severity `converted`, because an untranslated OpenFormula string is not a native formula), plus the shared categories of Tasks 2–3.

- [ ] **Step 1: Write failing tests** mirroring Task 2's three mode tests and Task 3's error-value, unknown-value-type and oversize-text rows, using `tests/fixtures/generate_ods.py` helpers to build inputs.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `test_ods_degrade.py`, `test_ods.py`, `test_workbook_interchange.py`** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Apply the degrade policy to ODS import"`

### Task 5: Whole-workbook preview no longer blocks

**Files:**
- Modify: `service/datahorizon/interchange.py` (`prepare_workbook`, `workbook_report`), `service/datahorizon/service.py` (`SpreadsheetPreparation` schema: `mode` enum, `report_entries`, `totals`), `service/datahorizon/client.py` (`prepare_spreadsheet` accepts `mode="mixed"`)
- Test: `tests/test_workbook_interchange.py`, `tests/test_xlsx_degrade.py`

**Interfaces:**
- Produces: `prepare_workbook` result always has `can_import: True` when the package is valid and has at least one sheet; `report_entries` is the merge of every sheet's report with `sheet` filled in each sample; `workbook_sheets` includes sheets with no cells.

- [ ] **Step 1: Write the failing tests**

```python
def test_sheet_where_every_cell_degrades_is_still_created(self):
    data=workbook_bytes({"Bad":'<row r="1"><c r="A1" t="zz"><v>1</v></c></row>',"Empty":""})
    result=xlsx_preview({"file_base64":b64(data),"scope":"workbook","mode":"mixed"})
    self.assertTrue(result["can_import"])
    self.assertEqual([(s["name"],s["cells"]) for s in result["workbook_sheets"]],[("Bad",[]),("Empty",[])])
    self.assertEqual(result["totals"]["omitted"],1)
```

Add a REST test: prepare, apply through `client.import_prepared`, reopen, and find both sheets present.

- [ ] **Step 2: Run** — expected FAIL (`can_import` false today).
- [ ] **Step 3: Implement.** Replace the existing test `test_missing_sheet_reference_blocks_whole_preview_and_values_mode_is_explicit` with one asserting a `formula.missing_sheet` entry and a stored formula.
- [ ] **Step 4: Run the Python suite** — expected PASS. Regenerate the OpenAPI example if `test_automation_openapi.py` or `test_job_openapi.py` pins the schema.
- [ ] **Step 5: Commit** `git commit -m "Offer whole-workbook import whenever the package is valid"`

### Task 6: Strict OOXML namespaces

**Files:**
- Modify: `service/datahorizon/interchange.py` (constants `S`, `R`; `_xlsx_preview`; `related_part`), `service/datahorizon/xlsx_styles.py`
- Test: `tests/test_xlsx_strict.py`

**Interfaces:**
- Produces: `interchange.Namespaces` (a frozen dataclass with `main`, `rel`, `doc_rel`) and `interchange.namespaces(workbook_root) -> Namespaces`, which returns the transitional or the strict set from the workbook root's tag and raises `INVALID_SPREADSHEET` for anything else. Readers take a `Namespaces` instead of the module constants; the constants remain for export, which always writes transitional.

- [ ] **Step 1: Write the failing test**

```python
def test_strict_and_transitional_packages_import_identically(self):
    sheet='<row r="1"><c r="A1" t="d"><v>2026-11-11</v></c><c r="B1"><f>SUM(A1:A1)</f><v>0</v></c></row>'
    a=xlsx_preview({"file_base64":b64(workbook_bytes({"S":sheet}))})
    b=xlsx_preview({"file_base64":b64(workbook_bytes({"S":sheet},strict=True))})
    self.assertEqual(a["cells"],b["cells"]); self.assertEqual(a["report_entries"],b["report_entries"])
```

Add: a root in an unknown namespace is rejected with `INVALID_SPREADSHEET`.

- [ ] **Step 2: Run** — expected FAIL ("Only transitional SpreadsheetML XLSX is supported").
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `test_xlsx_strict.py`, `test_interchange.py`, `test_xlsx_styles.py`** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Accept strict OOXML workbooks"`; open the Group A pull request.

---

## Group B — References in formulas the calculator cannot parse

### Task 7: Lenient reference scanner

**Files:**
- Create: `service/datahorizon/formula_refs.py`
- Test: `tests/test_formula_refs.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Ref:
    start: int; end: int            # span of the cell/row/column part in source, excluding any sheet prefix
    prefix_start: int               # equals start when there is no sheet prefix
    sheet: str | None               # unquoted first sheet name
    sheet_last: str | None          # second sheet of a 3D reference, else None
    external: bool                  # a [n] or [Book.xlsx] workbook prefix
    kind: str                       # "cell", "column" or "row"
    row: int | None; col: int | None
    abs_row: bool; abs_col: bool

@dataclass(frozen=True)
class RefRange:
    first: Ref; last: Ref           # REF ":" REF with nothing but optional spaces between

def scan(source: str) -> list[Ref | RefRange] | None   # None: not tokenizable
def rewrite(source: str, change: Callable[[Ref | RefRange], str | None]) -> str  # None keeps the text
```

The scanner recognises, and skips without interpreting: double-quoted strings with `""` escapes; quoted sheet names with `''` escapes; error literals; numbers; identifiers including dots and `_xlfn.` prefixes; array constants `{…}`; structured-reference brackets `[…]` with nesting; the operators `+ - * / ^ & = < > <= >= <> , ; : % @ # ( )` and whitespace. It recognises as references: `A1` forms with `$`, whole columns (`A:A`, `$A:$C`), whole rows (`1:1`, `$1:$5`), any of them with a sheet prefix, a quoted sheet prefix, a 3D prefix (`Sheet1:Sheet3!A1`) or an external prefix. Anything else, an unterminated string or an unbalanced bracket returns `None`. Coordinates outside 1,048,576 by 16,384 are not references.

- [ ] **Step 1: Write the failing tests**

```python
UNCHANGED=['="see A1"', "='It''s [1] A1'!B2+1", '={"A1","B2"}', "=Table1[[#This Row],[A1]]", '=LEFT("A1:B2",2)&A1']
def test_text_that_looks_like_a_reference_is_never_rewritten(self):
    for source in UNCHANGED:
        with self.subTest(source):
            refs=scan(source); self.assertIsNotNone(refs)
            self.assertEqual(rewrite(source,lambda item:None),source)
    self.assertEqual(len(scan('="see A1"')),0)
    self.assertEqual([r.sheet for r in scan("='It''s [1] A1'!B2+1")],["It's [1] A1"])
    self.assertEqual(len(scan('=LEFT("A1:B2",2)&A1')),1)

def test_reference_forms(self):
    (r,)=scan("=VLOOKUP(X,$B:$C,2,0)"); self.assertIsInstance(r,RefRange)
    self.assertEqual((r.first.kind,r.first.col,r.first.abs_col,r.last.col),("column",2,True,3))
    (r,)=scan("=SUM(Jan:Mar!B2)"); self.assertEqual((r.sheet,r.sheet_last),("Jan","Mar"))
    (r,)=scan("=[1]Data!A1"); self.assertTrue(r.external)
    (r,)=scan("=A1#"); self.assertEqual((r.row,r.col),(1,1))

def test_untokenizable(self):
    for source in ('="open', "=Table1[open", "=A1 ` B2", "={1,2"):
        with self.subTest(source): self.assertIsNone(scan(source))
    self.assertEqual(len(scan("=SUM(A1")),1)   # unbalanced parentheses are the calculator's concern
```

- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** as a single left-to-right regular-expression tokenizer; no tree.
- [ ] **Step 4: Run** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add a lenient formula reference scanner"`

### Task 8: Structural and copy transforms fall back to the scanner

**Files:**
- Modify: `service/datahorizon/formula.py` (`translate`, `transform_structure`, `move_references`, `rename_sheet_references`, `delete_sheet_references`, `has_sheet_reference`), `service/datahorizon/formula_admission.py`, `service/datahorizon/interchange.py` (`SharedFormulas.source`)
- Test: `tests/test_formula_refs_transforms.py`

**Interfaces:**
- Consumes: `formula_refs.scan`, `rewrite`, `Ref`, `RefRange`.
- Produces:
  - `class Unmaintainable(FormulaError)` in `formula.py`, raised by the five transforms when the strict parser fails **and** `scan` returns `None`.
  - Each transform keeps its signature and return type. When the strict parser raises `FormulaError`, it applies the same adjustment rule through `rewrite`.
  - Whole-column and whole-row references adjust on the matching axis only: inserting columns before `B` turns `B:C` into `C:D`; inserting rows does not change `B:C`. A deleted whole column inside a range contracts it; a fully deleted range becomes `#REF!`, as for cell ranges.
  - 3D references adjust their cell part when the edited sheet lies between the two named sheets inclusive, by display position. External references are never adjusted.
  - `formula_admission.status` replaces its string checks with `scan`: any `Ref` with `external` set gives `formula.external_reference`; a whole-row, whole-column or 3D reference in a formula the strict parser rejects gives `formula.unsupported_reference` (a new category); the rest is unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_row_insert_adjusts_references_in_unevaluable_formula(self):
    source='=VLOOKUP(A5,Data!$B$2:$C$9,2,0)&" A5"'
    out,broken=transform_structure(source,"Sheet1","Sheet1","row",3,2)
    self.assertEqual((out,broken),('=VLOOKUP(A7,Data!$B$2:$C$9,2,0)&" A5"',0))
def test_column_insert_adjusts_whole_column_range(self):
    out,_=transform_structure("=VLOOKUP(A1,B:C,2,0)","Sheet1","Sheet1","column",2,1)
    self.assertEqual(out,"=VLOOKUP(A1,C:D,2,0)")
def test_shared_formula_member_translates_through_unevaluable_master(self):
    self.assertEqual(translate("=IFS(A1>1,B:B)",1,0),"=IFS(A2>1,B:B)")
def test_untokenizable_formula_raises_unmaintainable(self):
    with self.assertRaises(Unmaintainable): transform_structure('=A1 ` B2',"Sheet1","Sheet1","row",1,1)
```

Add one test each for `move_references`, `rename_sheet_references` (a quoted sheet name inside an unevaluable formula is renamed; the same text inside a string is not) and `delete_sheet_references`.

- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement the fallbacks.** Keep the strict path first so existing behaviour and its tests are untouched.
- [ ] **Step 4: Run the new file plus `test_move_formulas.py`, `test_names_structure.py`, `test_engine.py`, `test_xlsx_degrade.py`** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Keep references correct in formulas the calculator cannot parse"`

### Task 9: The `unmaintained` formula kind and native format v7

**Files:**
- Modify: `service/datahorizon/engine.py` (`typed`, kind validation on open/copy/refresh, `change_structure`, sheet rename/delete/move paths, version gate), `service/datahorizon/service.py` (`format_version`, `supported_format_versions`, `Cell` schema kind enum), `service/datahorizon/operation_jobs.py` (`append` kind list), `desktop/sheetmodel.cpp` (tooltip), `docs/design/native-format-v1.md`
- Test: `tests/test_format_v7.py`

**Interfaces:**
- Consumes: `Unmaintainable` (Task 8).
- Produces:
  - Cell kind `unmaintained`: the source is the original formula text. Its typed value is the error `#REF!` with message `References in this formula were not maintained through a structural edit. Re-enter the formula.` It has no dependencies and is never evaluated.
  - A workbook enters format v7 in the same transaction as the first cell written with that kind. Readers below v7 reject v7; an `unmaintained` row in a file below v7 is `INVALID_FORMAT`. Undo of the structural edit restores the `formula` kind; the version does not go back down.
  - A structural operation that meets `Unmaintainable` for a formula it must rewrite sets that cell's kind to `unmaintained` instead of failing, and its receipt's reference report gains `"unmaintained": [{"sheet","cell"}]` (at most 100) and `"unmaintained_count"`.
  - Editing the cell with any other kind clears the state. Copy, fill and paste of an `unmaintained` cell copy it as `unmaintained` without translation.
  - XLSX and ODS export write the source as a formula and add a `formula.unmaintained` report entry with severity `approximated`.

- [ ] **Step 1: Write the failing tests**

```python
def test_structural_edit_marks_untokenizable_formula_and_bumps_to_v7(self):
    book=self.new_book(); edit(book,"A5","5"); edit(book,"B1",'=A5 ` 1',kind="formula")
    receipt=mutate(book,action="rows_insert",sheet="Sheet1",start=2,count=1)
    self.assertEqual(receipt["references"]["unmaintained"],[{"sheet":"Sheet1","cell":"B1"}])
    cell=read(book,"B1")
    self.assertEqual((cell["kind"],cell["source"],cell["display"]),("unmaintained",'=A5 ` 1',"#REF!"))
    self.assertEqual(format_version(book),7)
def test_reentering_the_formula_clears_the_state(self): ...
def test_undo_restores_formula_kind_and_keeps_v7(self): ...
def test_v6_file_with_unmaintained_row_is_invalid_format(self): ...
def test_formula_on_untouched_sheet_without_sheet_reference_is_not_marked(self): ...
```

`edit`, `mutate`, `read` and `format_version` are small helpers local to the test file. Model the file-level tests on `tests/test_format_v6.py`.

- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.** Remove the conservative block in `has_sheet_reference`'s callers that rejects a structural edit for an unclassified `!`; that case now marks the cell.
- [ ] **Step 4: Run the Python suite and `ctest --test-dir build --output-on-failure`** — expected PASS. Update `test_format_v6.py` expectations of the newest supported version.
- [ ] **Step 5: Document** v7 at the top of `native-format-v1.md` in the style of the v6 section, then commit `git commit -m "Mark formulas whose references a structural edit could not maintain (native v7)"`; open the Group B pull request.

---

## Group C — Large files

### Task 10: Streaming XLSX reader

**Files:**
- Create: `service/datahorizon/xlsx_stream.py`
- Test: `tests/test_xlsx_stream.py`

**Interfaces:**
- Consumes: `convert_xlsx_cell` (Task 3), `Namespaces` (Task 6), `ConversionReport`.
- Produces:
  - Constants `LARGE_CELLS = 1_000_000`, `LARGE_EXPANDED = 256*1024*1024`, `LARGE_PACKAGE = 64*1024*1024`, `LARGE_SHEETS = 255`, `LARGE_PARTS = 4096`.
  - `open_package(path: Path) -> zipfile.ZipFile` enforcing the compressed size, part count, duplicate names, encryption and compression-method checks of `interchange.package`.
  - `read_part(archive, name, budget: ByteBudget) -> Iterator[tuple[str, Element]]`: `iterparse` events over a part, feeding 64-KiB chunks, counting **actual** decompressed bytes against a shared `ByteBudget(LARGE_EXPANDED)`, rejecting DTDs, entities, NUL bytes, non-UTF-8 and depth over 64, and clearing finished elements.
  - `read_workbook(path: Path, request: dict, sink: Sink, checkpoint) -> ConversionReport`, where `Sink` has `sheet(name: str) -> None` and `cells(batch: list[dict]) -> None` (batches of at most 1,000 edits for the current sheet). Shared strings are spooled through `sink.strings` (`put(index, text)`, `get(index)`), so they are not held in memory.
  - Shared formulas: the master's translated source is kept per group index; a member seen before its master is deferred and resolved at the end of the sheet. A group with no master in the sheet rejects the file as today.
  - `checkpoint(completed_cells, phase)` is called at least every 1,000 cells.

- [ ] **Step 1: Write the failing tests**

```python
def test_declared_size_is_not_trusted(self):
    path=self.write(lying_zip(real_bytes=2*1024*1024,declared=1024))   # helper patches the header
    with self.assertRaises(EngineError) as raised:
        list(read_part(open_package(path),"xl/worksheets/sheet1.xml",ByteBudget(1024*1024)))
    self.assertEqual(raised.exception.body["code"],"LIMIT")

def test_streaming_and_tree_readers_agree(self):
    data=Path("tests/fixtures/fidelity.xlsx").read_bytes()
    tree=xlsx_preview({"file_base64":b64(data),"scope":"workbook","mode":"mixed"})
    sink=ListSink(); report=read_workbook(self.write(data),{"mode":"mixed"},sink,lambda *_:None)
    self.assertEqual(sink.sheets,[(s["name"],s["cells"]) for s in tree["workbook_sheets"]])
    self.assertEqual(report.entries(),tree["report_entries"])

def test_shared_member_before_master_resolves(self): ...
def test_cell_count_over_limit_rejects_with_limit(self): ...   # patch LARGE_CELLS to 10
def test_cancel_through_checkpoint_stops_reading(self): ...
```

- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.** Styles and the workbook part are small and may be read whole through the existing `read_xml`; sheets and shared strings must stream.
- [ ] **Step 4: Run `test_xlsx_stream.py`** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Read XLSX packages as a stream"`

### Task 11: Staging directory and import stage database

**Files:**
- Create: `service/datahorizon/staging.py`, `service/datahorizon/import_stage.py`
- Test: `tests/test_import_stage.py`

**Interfaces:**
- Produces:
  - `class Staging(root: Path | None = None, *, capacity: int = 4, retention: int = 900)`: creates a private directory (mode `0o700`; default under `tempfile.gettempdir()`, name `datahorizon-<pid>-<random>`), removed by `close()`. `new(kind: str, identifier: str) -> Path` (kind is `upload`, `stage` or `export`; identifier matches `[A-Za-z0-9_-]{1,64}`; raises `JOB_LIMIT` (409) at capacity per kind, `JOB_CONFLICT` (409) if it exists). `path(kind, identifier) -> Path` raises `JOB_NOT_FOUND` (404). `release(kind, identifier)`. `sweep()` deletes entries older than `retention` seconds.
  - `class ImportStage(path: Path)` over a SQLite file with tables `meta(singleton, payload)`, `sheets(position, name)`, `styles(id, payload)`, `cells(sheet, row, col, source, kind, style_id, PRIMARY KEY(sheet,row,col)) WITHOUT ROWID`, `strings(idx, text)`. Methods: `sheet(name)`; `cells(batch)` (validates each edit with `engine.typed` and `validate_style`, interning styles; a duplicate coordinate raises `INVALID_SPREADSHEET`); `strings` property with `put`/`get`; `finish(report: ConversionReport, extra: dict) -> dict`, which drops `strings`, computes `digest` (SHA-256 over sheet names in order and every cell row in primary-key order) and returns the review summary; `summary() -> dict`; `sample(per_sheet: int = 100) -> list[dict]`; `sheets() -> list[str]`; `count() -> int`; `has_temporal() -> bool`.
  - The review summary is `{"scope":"workbook","stage_id","stage_digest","sheets":[{"name","stored_cells"}],"stored_cells","report","report_entries","totals","sample":[{"sheet","cells":[…]}],"can_import":True}` and is always under 1 MiB.

- [ ] **Step 1: Write failing tests**: digest is stable across two stages built from the same input in different batch sizes; digest changes when one cell's source changes; `sample` returns the first 100 cells per sheet in row-major order; a duplicate coordinate is rejected; `Staging` enforces capacity, identifier syntax and directory mode `0o700`; `sweep` removes an entry whose mtime is older than the retention.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add the import stage database and staging directory"`

### Task 12: Streamed upload over REST

**Files:**
- Modify: `service/datahorizon/service.py` (request handler body reading near `run_request`; `dispatch`; route table and OpenAPI; server owns one `Staging`)
- Test: `tests/test_upload.py`, `tests/test_http_limits.py`

**Interfaces:**
- Consumes: `Staging`.
- Produces:
  - `PUT /v1/uploads/{upload}` with `Content-Type: application/octet-stream` and a required `Content-Length` of 1 to `LARGE_PACKAGE` bytes. The body is copied to the staging file in 64-KiB chunks under one total read deadline of 300 seconds. Response `{"upload_id","bytes","sha256"}`. A short body, a timeout or a disconnect deletes the partial file and answers `REQUEST_TIMEOUT` (408) or `INVALID_UPLOAD` (400). Same bearer-token and origin checks as every route.
  - `POST /v1/uploads/{upload}/release` → `{"released":true}`.
  - JSON routes keep their 4-MiB limit; only this route accepts a non-JSON body.
  - The server object exposes its `Staging` as the attribute `staging`, so in-process tests can inspect staged files.
  - Capabilities gain `"uploads": true`, `"max_upload_bytes"`, `"max_import_cells"`.

- [ ] **Step 1: Write failing tests**: a 5-MiB upload round-trips with the right `sha256`; `Content-Length` above the limit is `413 REQUEST_LIMIT` without reading the body; a body shorter than its `Content-Length` leaves no file in the staging directory; a missing token is `401`; a fifth concurrent upload id is `409 JOB_LIMIT`; a JSON `POST` of 5 MiB to an ordinary route is still `413`.
- [ ] **Step 2: Run** — expected FAIL (`PUT` unsupported).
- [ ] **Step 3: Implement** `do_PUT` through the same `run_request` admission path.
- [ ] **Step 4: Run both files** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Accept streamed spreadsheet uploads"`

### Task 13: Large preparation job

**Files:**
- Modify: `service/datahorizon/jobs.py` (`PreparationJobs.start` takes `seconds: int | None = None`), `service/datahorizon/service.py` (`/v1/import/xlsx/jobs` dispatch and `SpreadsheetPreparation` schema)
- Test: `tests/test_large_preparation.py`

**Interfaces:**
- Consumes: `read_workbook` (Task 10), `ImportStage`, `Staging`, uploads (Task 12).
- Produces:
  - `POST /v1/import/xlsx/jobs` accepts exactly one of `file_base64` (existing path, unchanged), `upload_id`, or `source_path` (an absolute path readable by the service; local use). With `upload_id` or `source_path`, `scope` must be `workbook`; the job runs with a 300-second budget and its result is the Task 11 review summary. The stage id equals the job id.
  - With `source_path`, the file's size and modification time are recorded in the stage and checked again at Apply (Task 14).
  - Cancelling or releasing the job releases its stage. Releasing the upload is the client's call.
  - `Client.preparation` polling is unchanged.

- [ ] **Step 1: Write failing tests** through a real `LocalService`: upload `tests/fixtures/fidelity.xlsx`, prepare with `mode="mixed"`, poll to `completed`, and assert the summary's sheet names, `stored_cells`, a non-empty `stage_digest`, `sample`, and that the result has no `workbook_sheets` key. Add: `upload_id` with `scope="selection"` is `INVALID_ARGUMENT`; both `file_base64` and `upload_id` is `INVALID_ARGUMENT`; cancel during reading ends `cancelled` and removes the stage file; an encrypted package ends `failed` with `INVALID_SPREADSHEET`.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new file, `test_jobs.py`, `test_job_openapi.py`** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Prepare large XLSX imports into a stage"`

### Task 14: Apply a stage atomically; additional sheets

**Files:**
- Modify: `service/datahorizon/engine.py` (`import_workbook`, `mutate` deadline selection for this action), `service/datahorizon/service.py` (`workbook_import` schema; pass the server's `Staging` to the engine call), `service/datahorizon/interchange.py` (`workbook_report` text)
- Test: `tests/test_stage_import.py`

**Interfaces:**
- Consumes: `ImportStage`, `Staging`.
- Produces: the `workbook_import` action accepts
  - `sheets` (existing) **or** `stage_id` with `stage_digest`; never both.
  - `target`: `"replace_empty"` (default, today's rule) or `"add_sheets"`.
  - With a stage: the digest must equal the stage's, and a `source_path` stage must still match its recorded size and modification time; otherwise `STALE_PLAN` (409). Cells load in one transaction without per-cell change images; Undo/Redo is cleared; the format version is raised to 2 if the stage has temporal kinds. The operation budget is 300 seconds with the existing cooperative cancellation; cancellation or any failure rolls back sheets and cells together.
  - `add_sheets`: imported sheets are appended after existing ones. Any imported name that collides with an existing sheet (SQLite `NOCASE`) rejects the whole import with `SHEET_NAME_CONFLICT` (409) listing the names. The destination may contain cells, tables, modules, names and bindings.
  - Hooks: the existing structural `before`/`after` event; no change events. An activated required handler that has not opted in rejects before any change, as today.
  - The receipt gains `{"imported":{"sheets":n,"cells":n}}`.
  - Client apply of a staged import is not retried blindly: the same `operation_id` returns the stored receipt.

- [ ] **Step 1: Write the failing tests**, running the server in-process so the test can reach `server.staging` (`self.service` below is that server)

```python
def test_stage_altered_after_review_is_refused(self):
    summary=self.prepare(FIDELITY)
    with sqlite3.connect(self.service.staging.path("stage",summary["stage_id"])) as db:
        db.execute("UPDATE cells SET source='999' WHERE rowid IN (SELECT rowid FROM cells LIMIT 1)")
    with self.assertRaises(APIError) as raised: self.apply(summary)
    self.assertEqual(raised.exception.body["code"],"STALE_PLAN")
    self.assertEqual(self.sheet_names(),["Sheet1"])          # nothing changed

def test_source_file_replaced_after_review_is_refused(self): ...
def test_add_sheets_appends_and_keeps_existing_cells_and_modules(self): ...
def test_add_sheets_name_conflict_rejects_everything(self): ...
def test_failure_midway_rolls_back_sheets_cells_and_receipt(self): ...   # patch the loader to raise after one sheet
def test_required_handler_without_structure_opt_in_rejects_before_change(self): ...
def test_staged_import_equals_small_path_import(self): ...               # same file both ways, compare read_workbook()
```

- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.** The loader reads the stage through its own connection and inserts with `executemany` in batches; the engine does not `ATTACH` inside its open transaction.
- [ ] **Step 4: Run the new file, `test_workbook_interchange.py`, `test_engine.py`, `test_opening_validation.py`** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Apply a staged workbook import as one operation, with an add-sheets target"`

### Task 15: Streaming XLSX export and download

**Files:**
- Modify: `service/datahorizon/xlsx_stream.py` (writer), `service/datahorizon/engine.py` (`stream_workbook`), `service/datahorizon/service.py` (routes, binary response)
- Test: `tests/test_large_export.py`

**Interfaces:**
- Consumes: `Staging`, `PreparationJobs`, `xlsx_styles.export_styles`, `xlsx_future_functions.PREFIX`.
- Produces:
  - `Workbook.stream_workbook() -> ContextManager[Iterator[tuple[str, Iterator[dict]]]]`: sheet name and its cells in row-major order from one read transaction, without the `MAX_CELLS` bound.
  - `xlsx_stream.write_workbook(path: Path, sheets, report: ConversionReport, checkpoint) -> dict` writing each sheet part through `ZipFile.open(name, "w")`, inline strings (no shared-string table), formulas with `_xlfn.` prefixes restored and no cached values, and the stylesheet. Enforces `LARGE_CELLS` and `LARGE_EXPANDED`.
  - `POST /v1/workbooks/{workbook}/export/xlsx/jobs` with `{"job_id","mode":"formulas"}` → a preparation-style job whose result is `{"export_id","bytes","sha256","report","report_entries","totals"}`. `mode: "values"` is `UNSUPPORTED` (400) on this route until increment 10; the existing bounded route still offers it.
  - `GET /v1/exports/{export}` streams the file with `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` and `Content-Length`. `POST /v1/exports/{export}/release`.
  - The distinct-style bound stays 1,024 (increment 3 raises it); exceeding it fails the job with `LIMIT`.

- [ ] **Step 1: Write failing tests**: export a workbook holding an unevaluable `=IFS(...)`, a cross-sheet formula, a styled cell and an empty sheet; download; re-import through the stage path and compare `read_workbook()` before and after; an independent openpyxl read (skipped when openpyxl is absent, as `test_fidelity.py` does) sees the same values and `_xlfn.IFS`. Add: the download's `sha256` matches the job result; `mode="values"` is refused; cancel removes the partial file.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Export large workbooks as a streamed XLSX download"`

### Task 16: The large path for ODS

**Files:**
- Create: `service/datahorizon/ods_stream.py`
- Modify: `service/datahorizon/service.py` (`/v1/import/ods/jobs`, `/v1/workbooks/{workbook}/export/ods/jobs`)
- Test: `tests/test_ods_stream.py`

**Interfaces:**
- Consumes: `convert_ods_cell` (Task 4), `read_part`/`ByteBudget`/`open_package` (Task 10), `ImportStage`, `Staging`.
- Produces: `ods_stream.read_workbook(path, request, sink, checkpoint) -> ConversionReport` and `ods_stream.write_workbook(path, sheets, report, checkpoint) -> dict` with the same contracts as the XLSX pair. Repeated cells and rows (`number-columns-repeated`, `number-rows-repeated`) expand only for cells that hold content or a style; a repeat that would create more than `LARGE_CELLS` stored cells rejects with `LIMIT`. The ODS routes accept `upload_id`/`source_path` and offer export jobs exactly as Tasks 13 and 15 do.

- [ ] **Step 1: Write failing tests**: streaming and tree readers agree on `tests/fixtures/fidelity.ods`; a trailing `number-rows-repeated="1048576"` empty row adds no cells; a styled row repeated 2,000,000 times is `LIMIT`; staged import then export job then staged re-import preserves `read_workbook()`.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `test_ods_stream.py`, `test_ods.py`** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add the large import and export path for ODS"`; open the Group C pull request.

---

## Group D — Clients

### Task 17: SDK, CLI and OpenAPI

**Files:**
- Modify: `service/datahorizon/client.py`, `service/datahorizon/cli.py`, `service/datahorizon/service.py` (OpenAPI paths, schemas and examples for Tasks 12–16), `docs/design/python-automation-guide.md` (SDK section)
- Test: `tests/test_interchange_clients.py`, `tests/test_job_openapi.py`

**Interfaces:**
- Produces, on `Client`:
  - `upload(path: str | Path, upload_id: str | None = None) -> dict` (streams the file; never reads it whole)
  - `prepare_spreadsheet_file(path, *, format="xlsx", mode="mixed", job_id=None, local=False, timezone_policy="reject") -> dict` (`local=True` sends `source_path`; otherwise uploads first), returning the completed review summary after polling, or raising `APIError` with the job's error
  - `download(export_id: str, destination: str | Path, *, overwrite=False) -> dict`
- On the workbook handle: `import_staged(summary: dict, *, target="replace_empty") -> dict` and `export_spreadsheet_file(destination, *, format="xlsx", overwrite=False) -> dict` (runs the export job, downloads to a temporary sibling, renames into place).
- CLI: `datahorizon import-spreadsheet FILE.dhwb SOURCE [--mode formulas|mixed|values] [--add-sheets] [--report-json PATH] [--yes]` prints the grouped report and asks for confirmation unless `--yes`; `datahorizon export-spreadsheet FILE.dhwb DESTINATION [--overwrite]`. The format comes from the extension (`.xlsx`, `.ods`). Both work against a remote service with the existing connection options. Exit code 0 on success, non-zero with the structured error on failure.

- [ ] **Step 1: Write failing tests** driving the SDK and the CLI (as `tests/test_names_clients.py` drives its commands) through import with `--yes`, `--add-sheets`, a name conflict's exit code and message, `--report-json` content, and export with and without `--overwrite`. Extend the OpenAPI test so every new path has a schema and an executed example.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run both files and `test_automation_openapi.py`** — expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Expose large spreadsheet import and export through the SDK and CLI"`

### Task 18: Desktop import and export

**Files:**
- Create: `desktop/conversionreportview.h`
- Modify: `desktop/restclient.h/.cpp`, `desktop/mainwindow.h/.cpp` (`importFile`, `prepareImport`, `importSpreadsheetOptions`, `importSpreadsheetBytes`, the export path), `CMakeLists.txt`
- Test: `tests/test_desktop.cpp`

**Interfaces:**
- Consumes: REST routes of Tasks 12–16.
- Produces:
  - `RestClient::upload(const QString &id, const QString &filePath, Callback callback, std::function<void(qint64,qint64)> progress)` streaming from a `QFile`, and `RestClient::download(const QString &exportId, QSaveFile *destination, Callback callback, std::function<void(qint64,qint64)> progress)`.
  - `ConversionReportView` (`objectName` `conversionReport`): a `QTreeWidget` grouped by severity in the order unevaluable, omitted, approximated, converted; each row shows category text, detail and count; child rows show sample locations; a totals line (`objectName` `conversionTotals`).
  - Import options dialog: a mode chooser (`objectName` `importMode`) with "Formulas where possible, values otherwise" (mixed, preselected), "Keep every formula" and "Values only"; a scope chooser where "All sheets" offers a target (`objectName` `importTarget`): "Replace this empty workbook" (disabled with an explanation when the workbook is not empty) or "Add as new sheets".
  - "All sheets" always takes the upload and stage path; a single sheet into the current selection keeps the existing path. Progress shows upload bytes then cells read then applying, with Cancel at each stage. Apply shows the report first and is one explicit button.
  - Export of a whole workbook as formulas uses the export job and download into the existing `QSaveFile` publication checks; other export choices keep the existing path.
  - An active in-cell draft is resolved before import starts, as today.

- [ ] **Step 1: Write failing Qt slots**: `largeWorkbookImportUploadsReviewsAndApplies` (imports `fidelity.xlsx` through the new path into an empty workbook and finds its sheets in the tab bar); `importReportGroupsBySeverity` (a fixture with an unsupported function shows one unevaluable row with count and a sample child); `importAddsSheetsToNonEmptyWorkbook`; `importNameConflictLeavesWorkbookUnchanged` (message names the sheet); `cancelDuringUploadLeavesNoStage`; `wholeWorkbookExportDownloadsThroughJob`.
- [ ] **Step 2: Build and run the slots** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `ctest --test-dir build --output-on-failure`** — expected PASS, including the existing slots `xlsxImportReviewAndExportUseTheManagedGrid`, `wholeWorkbookImportReplacesEmptyCatalogAndExportsSnapshot` and `unifiedImportDetectsExtensions` (update the second for the new dialog).
- [ ] **Step 5: Capture** the import review dialog natively at scale 2 into `docs/design/evidence/`, inspect it, then commit `git commit -m "Import and export large workbooks from the desktop with a grouped report"`; open the Group D pull request.

---

## Group E — Measurement, corpus and documents

### Task 19: Capacity measurement and final limits

**Files:**
- Create: `tests/qualify_large_interchange.py`, `tests/fixtures/generate_large_xlsx.py`
- Modify: `service/datahorizon/xlsx_stream.py` (constants), `docs/design/excel-fidelity-plan.md` (section 5.2), `docs/design/compatibility-v0.md`
- Evidence: `docs/design/evidence/excel-fidelity-1-capacity.json`

**Interfaces:**
- Produces: `generate_large_xlsx.py --cells N --sheets S --formula-share F --out PATH` writing a deterministic workbook with the standard library (numbers, short and long text, dates, a share of supported and unsupported formulas, a few hundred distinct styles). `qualify_large_interchange.py` runs, for 100,000, 500,000 and 1,000,000 cells: generate, upload, prepare, apply, close, reopen, first viewport read, export job, download; recording wall seconds and peak resident memory of the service per phase (as `tests/cache_rss_benchmark.py` measures) and the native file size.

- [ ] **Step 1: Write both scripts**; run the 100,000-cell case and confirm it completes.
- [ ] **Step 2: Run all three sizes** on the reference Mac and record the JSON evidence.
- [ ] **Step 3: Decide the limits.** A size qualifies when every phase finishes inside 300 seconds and the service's peak resident memory stays under the 256-MiB service target recorded in `macos-capacity-qualification.md`. Set `LARGE_CELLS`, `LARGE_EXPANDED` and `LARGE_PACKAGE` to the largest qualifying values. If 1,000,000 cells does not qualify, first look for the phase that fails and fix an avoidable cause (an in-memory list, a per-cell commit); if it still does not qualify, lower the limit. Reopening counts: native open validates stored cells within an opening budget (`native-opening-performance.md`), so a size whose file cannot be reopened does not qualify, and that budget is not changed in this increment.
- [ ] **Step 4: Replace the targets** in section 5.2 of the design and in the compatibility contract with the measured values and a link to the evidence. Add `test_declared_limits_match_capabilities` asserting the service capabilities report the constants.
- [ ] **Step 5: Commit** `git commit -m "Qualify and record large interchange limits"`

### Task 20: Corpus and scorecard

**Files:**
- Create: `tests/fixtures/corpus/provenance.json`, `tests/fixtures/corpus/README.md`, `tests/corpus_scorecard.py`, `tests/test_corpus.py`
- Evidence: `docs/design/evidence/excel-fidelity-scorecard.json`

**Interfaces:**
- Produces:
  - `provenance.json`: a list of `{"file","sha256","producer","producer_version","saved_on","exercises":[…],"source"}`. `producer` is the application that last saved the file (`Microsoft Excel`, `LibreOffice Calc`, `openpyxl`).
  - `corpus_scorecard.py [--out PATH]`: imports every corpus file through the stage path in `formulas`, `mixed` and `values` modes and writes `{file: {mode: {"status": "clean"|"degraded"|"rejected", "stored_cells", "totals", "entries":[{"category","severity","detail","count"}]}}}` plus a summary of counts by category across the corpus.
  - `test_corpus.py`: every listed file exists with the listed hash; no file is `rejected` in `mixed` or `formulas` mode; the checked-in scorecard equals a fresh run.

- [ ] **Step 1: Seed the corpus** with the existing `fidelity.xlsx`, `inputs.xlsx`, `fidelity.ods` and `inputs.ods` (producer `openpyxl`/`odfpy`, from `fidelity-provenance.json`).
- [ ] **Step 2: Add LibreOffice-saved files.** `soffice` is not installed on the reference Mac. Ask the owner before installing it; with it, re-save each seed through `soffice --headless --convert-to xlsx` and `--convert-to ods` and add files that exercise merged cells, custom number formats, conditional formatting, validation, comments, hyperlinks, defined names, array formulas, an Excel table and theme colours. Without it, record the gap in the corpus README and the scorecard summary.
- [ ] **Step 3: Add Excel-saved files** when the owner supplies them (design section 14, item 1). The task and the increment are not complete until at least one Excel-saved file is in the corpus or the owner waives it in writing in the design document.
- [ ] **Step 4: Write `corpus_scorecard.py` and `test_corpus.py`; run them** — expected PASS, with any rejected file fixed in the importer, not excluded.
- [ ] **Step 5: Commit** `git commit -m "Add the interchange corpus and scorecard"`

### Task 21: Specification check and documents

**Files:**
- Modify: `docs/design/excel-fidelity-plan.md` (sections 3, 6 and status line), `docs/design/compatibility-v0.md`, `docs/design/interchange-next-five.md`, `docs/design/workbook-next-five.md`, `docs/design/implementation-status.md`, `docs/design/interfaces-and-interoperability.md`, `docs/design/README.md`
- Create: `docs/design/excel-spec-coverage.md`

- [ ] **Step 1: Check section 3 against the standard.** Using ECMA-376 Part 1, fifth edition, section 18 (SpreadsheetML), list every worksheet-level element (children of `worksheet`), workbook-level element (children of `workbook`), cell and formula attribute, and stylesheet element in `excel-spec-coverage.md`, each with: clause number, what it holds, the increment that owns it or the non-goal that excludes it, and its status today (imported, reported, ignored). Cite the clause; do not copy the standard's text.
- [ ] **Step 2: Reconcile.** Any element with no owner is added to section 3 of the design and assigned to an increment or to the non-goals, with the change marked **Call** for the owner to confirm.
- [ ] **Step 3: Make "ignored" impossible.** For every element the importer neither imports nor reports, add an `omitted` report entry (category `sheet.feature` or `workbook.feature`, detail = the element name) and a test in `test_xlsx_degrade.py` that a sheet containing `<mergeCells>`, `<conditionalFormatting>`, `<dataValidations>`, `<hyperlinks>` and `<cols>` reports each by name.
- [ ] **Step 4: Update the contracts.** Rewrite the XLSX and whole-workbook sections of the compatibility contract and the two interchange documents to describe the degrade policy, modes, limits, transport and report; record increment 1 as built in `implementation-status.md` with links to evidence; state in the design which of increment 1's "done when" criteria are met and which are waiting (the Excel-saved corpus).
- [ ] **Step 5: Run everything** (`ctest --test-dir build --output-on-failure`, the Python suite, `tests/corpus_scorecard.py`), record `docs/design/evidence/excel-fidelity-1-verification.txt`, commit `git commit -m "Record increment 1 contracts, specification coverage and evidence"`, and open the Group E pull request.

---

## Not in this plan

- Any new native feature other than the `unmaintained` formula kind: merged cells, sizes, styles, number formats, attachments, validation, conditional formatting (increments 2–7).
- New worksheet functions, names in formulas, arrays, a native error-literal kind (increment 8). Error-valued cells are stored as Text and reported until then.
- Structural edits and calculation at large sizes (increments 9 and 10). A large imported workbook can be viewed, cell-edited and exported as formulas; large values export waits for increment 10.
- Raising the 1,024 distinct-style export bound (increment 3).
- Selection-scope import or export beyond 10,000 cells.
