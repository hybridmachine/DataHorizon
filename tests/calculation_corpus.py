# SPDX-License-Identifier: GPL-3.0-only
"""What the formula evaluator returns today, recorded as a corpus (docs/design/calculation-stage-2-plan.md, Task 1).

CASES is a list of {"name", "cells": {(sheet name, row, col): source}}; a source is text, or (text, kind) for a typed input such as a date.
results(case) reads every formula cell twice, alone (a fresh Workbook per cell) and together (each sheet's used rectangle in one read).
Each result is [calculation, display, value]; a temporal value is recorded as {"temporal": kind, "value": text}.
Keys are "Sheet!R<row>C<col>". Every case stays under today's limits: fewer than 5,000 cells read by the formulas of one sheet, no NOW or TODAY.
"""
from contextlib import closing
from pathlib import Path
import json
import subprocess
import sqlite3
import tempfile
import uuid

from datahorizon.engine import Workbook

S = "Sheet1"
CASES = []


def case(name, cells, **named):
    """Add a case; cells maps "A1" (Sheet1) or "Sheet!A1" to a source, or takes keys already shaped (sheet, row, col)."""
    assert name not in {c["name"] for c in CASES}, name
    shaped = {}
    for key, source in cells.items():
        if isinstance(key, str):
            sheet, _, ref = key.rpartition("!") if "!" in key else (S, "", key)
            letters = ref.rstrip("0123456789")
            col = 0
            for ch in letters: col = col*26 + ord(ch)-64
            key = (sheet or S, int(ref[len(letters):]), col)
        shaped[key] = source
    CASES.append({"name": name, "cells": shaped})


def column(letter, sources, start=1):
    return {f"{letter}{start+i}": s for i, s in enumerate(sources) if s is not None}


# ---- every range-reading function over a 6 by 3 range, over nothing, over one cell, and over errors ----
def calls(full, look=None, ret=None, index="1,1"):
    look, ret = look or full, ret or full
    return {"SUM": f"=SUM({full})", "AVERAGE": f"=AVERAGE({full})", "MIN": f"=MIN({full})", "MAX": f"=MAX({full})",
            "COUNT": f"=COUNT({full})", "COUNTA": f"=COUNTA({full})", "AND": f"=AND({full})", "OR": f"=OR({full})",
            "CONCAT": f"=CONCAT({full})", "TEXTJOIN": f'=TEXTJOIN(",",TRUE,{full})',
            "COUNTIF": f'=COUNTIF({full},">0")', "SUMIF": f'=SUMIF({full},">0")',
            "COUNTIFS": f'=COUNTIFS({full},">0")', "SUMIFS": f'=SUMIFS({full},{full},">0")',
            "XLOOKUP": f"=XLOOKUP(7,{look},{ret})", "MATCH": f"=MATCH(7,{look},0)", "INDEX": f"=INDEX({full},{index})"}


MIXED = {"A1": "5", "B1": "x", "C1": "TRUE", "A2": "hello", "B2": ("2024-01-15", "date"),  # C2 holds no cell
         "A3": "=A1*2", "B3": "=1/0", "C3": "7", "A4": "3", "B4": "x", "C4": "-2",
         "D1": "=B2", "A6": "=SUM(A1:A1)", "B6": "", "C6": "'7"}  # row 5 holds no cell at all
for name, formula in calls("A1:C6", "C1:C6", "A1:A6", "3,2").items():
    case(f"{name}-mixed-6x3", {**MIXED, "E1": formula})
for name, formula in calls("A1:C6", "C1:C6", "A1:A6", "3,2").items():
    case(f"{name}-no-stored-cell", {"E1": formula})
for name, formula in calls("A1:A1").items():
    case(f"{name}-single-cell", {"A1": "5", "E1": formula})
    case(f"{name}-single-empty-cell", {"E1": formula})
for name, formula in calls("A1:A4", index="2,1").items():
    case(f"{name}-errors-div-then-ref", {"A1": "1", "A2": "=1/0", "A3": "=Nowhere!A1", "A4": "7", "E1": formula})
    case(f"{name}-errors-ref-then-div", {"A1": "1", "A2": "=Nowhere!A1", "A3": "=1/0", "A4": "7", "E1": formula})
    case(f"{name}-errors-column-order", {"A1": "=Nowhere!A1", "B1": "=1/0", "A2": "=1/0", "E1": formula.replace("A1:A4", "A1:B2")})

# ---- criteria that match blanks, blank lookup keys, an empty criteria cell ----
GAPS = {"A1": "1", "A2": "x", "A4": ("", "text"), "A5": "=\"\"", "A7": "0", "B1": "10", "B2": "20", "B3": "30", "B4": "40", "B5": "50", "B9": "90"}
for label, criteria in (("empty-text", '""'), ("not-x", '"<>x"'), ("equals", '"="'), ("not-equal-empty", '"<>"')):
    case(f"COUNTIF-criteria-{label}", {**GAPS, "E1": f"=COUNTIF(A1:A9,{criteria})"})
    case(f"SUMIF-criteria-{label}", {**GAPS, "E1": f"=SUMIF(A1:A9,{criteria},B1:B9)"})
    case(f"COUNTIFS-criteria-{label}", {**GAPS, "E1": f"=COUNTIFS(A1:A9,{criteria},B1:B9,\">0\")"})
    case(f"SUMIFS-criteria-{label}", {**GAPS, "E1": f"=SUMIFS(B1:B9,A1:A9,{criteria})"})
for name, formula in (("XLOOKUP", "=XLOOKUP(Z1,A1:A9,B1:B9)"), ("MATCH", "=MATCH(Z1,A1:A9,0)"),
                      ("XLOOKUP-text", '=XLOOKUP("",A1:A9,B1:B9)'), ("MATCH-text", '=MATCH("",A1:A9,0)')):
    case(f"{name}-blank-key", {**GAPS, "E1": formula})
case("SUMIFS-empty-criteria-cell-stored-sum-cell", {"B1": "10", "B2": "20", "E1": "=SUMIFS(B1:B3,A1:A3,D1)"})
case("SUMIFS-empty-criteria-text-stored-sum-cell", {"B1": "10", "B2": "20", "E1": '=SUMIFS(B1:B3,A1:A3,"")'})

# ---- extremes ----
case("SUM-overflow-then-div", {"A1": "1e308", "B1": "1e308", "C1": "=1/0", "E1": "=SUM(A1:C1)"})
case("SUMIF-overflow-then-div", {"A1": "1e308", "B1": "1e308", "C1": "=1/0", "E1": '=SUMIF(A1:C1,">0")'})
case("SUM-overflow", {"A1": "1e308", "B1": "1e308", "C1": "1", "E1": "=SUM(A1:C1)"})
case("SUMIF-overflow", {"A1": "1e308", "B1": "1e308", "C1": "1", "E1": '=SUMIF(A1:C1,">0")'})
case("SUM-cancellation", {**column("A", ["1e16", "-1e16", "1"]), "E1": "=SUM(A1:A3)"})
case("AVERAGE-cancellation", {**column("A", ["1e16", "-1e16", "1"]), "E1": "=AVERAGE(A1:A3)"})

# ---- cycles ----
case("cycle-self", {"A1": "=A1"})
case("cycle-pair", {"A1": "=B1", "B1": "=A1"})
case("cycle-untaken-if", {"A1": "=IF(FALSE,A1,42)"})
case("cycle-taken-if", {"A1": "=IF(TRUE,A1,1)"})
case("cycle-iferror-of-cycle", {"A2": "=B2", "B2": "=A2", "C2": "=IFERROR(B2,5)"})
case("cycle-iferror-of-self-reader", {"A2": "=IFERROR(B2,5)", "B2": "=A2"})
case("cycle-range-includes-own-formula", {"A1": "1", "A2": "2", "A3": "=SUM(A1:A3)"})
case("cycle-countif-includes-own-formula", {"A1": "1", "A2": "=COUNTIF(A1:A3,\">0\")", "A3": "2"})
case("cycle-range-through-untaken-branch", {"B1": "=SUM(A1:A2)", "A1": "=1/0", "A2": "=B1"})  # Review Focus 1
case("cycle-reader-of-member", {"A1": "=A1", "B1": "=A1+1", "C1": "=IFERROR(A1,9)", "D1": "=ISBLANK(A1)"})
case("cycle-reader-of-pair-member", {"A1": "=B1", "B1": "=A1", "C1": "=B1*2", "D1": "=SUM(A1:B1)"})
case("chain-128", {**{"A1": "=1"}, **{f"A{n}": f"=A{n-1}+1" for n in range(2, 129)}})
case("chain-129", {**{"A1": "=1"}, **{f"A{n}": f"=A{n-1}+1" for n in range(2, 130)}})

# ---- a range used where a scalar is needed ----
for label, cells in (("plain", {"A1": "1", "A2": "2", "A3": "3"}), ("error-cell", {"A1": "1", "A2": "=1/0", "A3": "3"}),
                     ("empty-cells", {"A2": "2"})):
    for tag, formula in (("whole", "=A1:A3"), ("plus", "=A1:A3+1"), ("if", "=IF(A1:A3,1,2)"), ("isblank", "=ISBLANK(A1:A3)")):
        case(f"range-as-scalar-{tag}-{label}", {**cells, "E1": formula})

# ---- references across sheets ----
case("cross-sheet-cell", {"Other!A1": "4", "A1": "=Other!A1*2", "A2": "=SUM(Other!A1:A2)", "A3": "='Other'!A1"})
case("cross-sheet-chain", {"Other!A1": "=Sheet1!B1", "B1": "3", "A1": "=Other!A1+1"})
case("cross-sheet-cycle", {"Other!A1": "=Sheet1!A1", "A1": "=Other!A1"})
case("missing-sheet", {"A1": "=Nowhere!A1", "A2": "=SUM(Nowhere!A1:A3)", "A3": "=IFERROR(Nowhere!A1,1)"})
case("accent-sheets", {"É!A1": "1", "é!A1": "2", "A1": "=É!A1", "A2": "=é!A1", "A3": "='é'!A1", "A4": "='É'!A1", "A5": "=SUM('é'!A1:A1)"})

# ---- the largest ranges the corpus uses: 40 by 40 ----
BLOCK = {f"{chr(64+c) if c < 27 else 'A'+chr(64+c-26)}{r}": str((r*c) % 7) for r in range(1, 41) for c in range(1, 41)}
case("SUM-40x40", {**BLOCK, "AP1": "=SUM(A1:AN40)"})
case("COUNTIF-40x40", {**BLOCK, "AP1": '=COUNTIF(A1:AN40,">3")'})


# ---- recording ----
def make(case, folder):
    """Create the case's workbook in folder; returns its path."""
    path = Path(folder) / "corpus.dhwb"
    sheets = {key[0] for key in case["cells"]}
    with closing(Workbook(path, create=True)) as book:
        for sheet in sorted(sheets - {S}):
            book.mutate(dict(action="sheet_add", name=sheet, expected_state=book.state(), operation_id=uuid.uuid4().hex))
        ids = {sheet: book.sheet(sheet) for sheet in sheets | {S}}
    rows = []
    for (sheet, row, col), source in case["cells"].items():
        text, kind = source if isinstance(source, tuple) else (source, "formula" if source.startswith("=") else "auto")
        rows.append((ids[sheet], row, col, text, kind))
    with closing(sqlite3.connect(path)) as db:
        db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,?,?,?)", rows)
        if any(row[4] == "date" for row in rows):  # the engine's own step when a temporal input is first stored
            db.execute("UPDATE dh_meta SET format_version=2 WHERE format_version=1")
        db.commit()
    return path


def triple(cell):
    value = cell["value"]
    if "value_kind" in cell:
        value = {"temporal": cell["value_kind"], "value": str(value)}
    return [cell["calculation"], cell["display"], json.loads(json.dumps(value))]


def key(sheet, row, col):
    return f"{sheet}!R{row}C{col}"


def results(case):
    with tempfile.TemporaryDirectory() as folder:
        path = make(case, folder)
        formulas = sorted(k for k, s in case["cells"].items() if not isinstance(s, tuple) and s.startswith("="))
        alone, together = {}, {}
        for sheet, row, col in formulas:
            with closing(Workbook(path)) as book:
                args = dict(r1=row, r2=row, c1=col, c2=col, calculation="fresh")
                alone[key(sheet, row, col)] = triple(book.read(sheet, args)["cells"][0])
        with closing(Workbook(path)) as book:
            for sheet in sorted({k[0] for k in formulas}):
                coords = [(r, c) for s, r, c in case["cells"] if s == sheet]
                args = dict(r1=min(r for r, c in coords), r2=max(r for r, c in coords), c1=min(c for r, c in coords),
                            c2=max(c for r, c in coords), calculation="fresh")
                for cell in book.read(sheet, args)["cells"]:
                    if (sheet, cell["row"], cell["col"]) in formulas:
                        together[key(sheet, cell["row"], cell["col"])] = triple(cell)
        return {"alone": alone, "together": together}


def record(path):
    revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    data = {"revision": revision, "cases": {c["name"]: results(c) for c in CASES}}
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
