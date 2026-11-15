# SPDX-License-Identifier: GPL-3.0-only
"""Format-neutral chart export: collecting stored charts, deciding which can be written and building them.

``collect`` reads the described charts of a workbook; ``drafts`` turns the ones a file can hold into export drafts
(the inverse of the import drafts) and reports every other chart as ``chart.skipped``; ``build`` hands each draft to a
format writer.  A chart is written whole or left out whole.
"""
import json
import re

from . import charts
from .engine import EngineError

SKIPPED = "chart.skipped"
CELL_LENGTH = 32  # the chart name a report entry carries


def collect(db):
    """Every stored chart, described, as ``{"sheet_id", "sheet", "chart"}``, by sheet position, then ``z``, then id.

    Empty when the workbook format is below the one that stores charts."""
    if db.execute("SELECT format_version FROM dh_meta").fetchone()[0] < charts.FORMAT_VERSION:
        return []
    rows = db.execute("SELECT s.position, s.id, s.name, o.id, o.payload FROM dh_objects o "
                      "JOIN sheets s ON s.id = o.sheet_id WHERE o.namespace = 'chart'")
    found = []
    for position, sheet_id, sheet, chart_id, text in rows:
        z, payload = 0, None
        try:
            payload = json.loads(text)
            chart = charts.describe(db, sheet_id, chart_id, payload)
            z = payload["z"]
        except (ValueError, EngineError):
            # An unreadable chart stays an entry so that ``drafts`` can report it; nothing else is contained.
            name = payload.get("name") if isinstance(payload, dict) else None
            chart = {"id": chart_id, "sheet_id": sheet_id, "issue": "INVALID_FORMAT",
                     "name": name if isinstance(name, str) and name else "Chart"}
        found.append(((position, sheet_id, z, chart_id), {"sheet_id": sheet_id, "sheet": sheet, "chart": chart}))
    found.sort(key=lambda item: item[0])
    return [entry for _, entry in found]


# References.

_PLAIN_XLSX = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_CELL_LIKE = re.compile(r"[A-Za-z]{1,3}[0-9]+|[Rr][0-9]*[Cc][0-9]*")
_PLAIN_ODS = re.compile(r"[^.\s'$:\[\]()]+")


def _column_name(number):
    letters = ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _cell(row, col):
    return f"${_column_name(col)}${row}"


def format_reference(ref, dialect):
    """The reference text of ``ref`` in the ``"xlsx"`` or ``"ods"`` dialect; the inverse of ``parse_reference``, absolute."""
    sheet, row1, col1, row2, col2 = ref
    first, last = _cell(row1, col1), _cell(row2, col2)
    single = (row1, col1) == (row2, col2)
    if dialect == "xlsx":
        plain = _PLAIN_XLSX.fullmatch(sheet) and not _CELL_LIKE.fullmatch(sheet)
        name = sheet if plain else "'" + sheet.replace("'", "''") + "'"
        return f"{name}!{first}" if single else f"{name}!{first}:{last}"
    name = sheet if _PLAIN_ODS.fullmatch(sheet) else "'" + sheet.replace("'", "''") + "'"
    return f"${name}.{first}" if single else f"${name}.{first}:.{last}"


# Drafts.

class _Skip(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _sources(spec):
    """``(role, source)`` for every source of a readable stored specification."""
    if spec["categories"] is not None:
        yield "categories", spec["categories"]["source"]
    for item in spec["series"]:
        for role in ("name", "values", "x"):
            if item[role] is not None and "source" in item[role]:
                yield role, item[role]["source"]


def _cells(source):
    return (source["row2"] - source["row1"] + 1) * (source["col2"] - source["col1"] + 1)


def _inside(row, col, rectangle):
    return rectangle[0] <= row <= rectangle[2] and rectangle[1] <= col <= rectangle[3]


def _check(chart, sheet_names, selection):
    """The reason ``chart`` cannot be written, as ``_Skip``; returns quietly when it can."""
    if chart["issue"] is not None:
        raise _Skip("unsupported")
    found = list(_sources(chart["spec"]))
    if any(charts.is_broken(source) for _, source in found):
        raise _Skip("broken_source")
    kinds = {charts.source_kind(source) for _, source in found}
    if "table" in kinds:
        raise _Skip("table_source")
    if "query" in kinds:
        raise _Skip("query_source")
    for role, source in found:
        if source["sheet_id"] not in sheet_names:
            raise _Skip("source")
        wide = source["row2"] > source["row1"] and source["col2"] > source["col1"]
        if wide or (role == "name" and _cells(source) > 1):
            raise _Skip("source")
    if selection is not None:
        rectangle = selection["range"]
        corners = [(source["row1"], source["col1"]) for _, source in found] \
            + [(source["row2"], source["col2"]) for _, source in found]
        if not _inside(chart["anchor"]["row"], chart["anchor"]["col"], rectangle) \
                or any(source["sheet_id"] != selection["sheet_id"] for _, source in found) \
                or not all(_inside(row, col, rectangle) for row, col in corners):
            raise _Skip("source")


def _ref(source, sheet_names, selection):
    name = sheet_names[source["sheet_id"]] if selection is None else selection["name"]
    return (name, source["row1"], source["col1"], source["row2"], source["col2"])


def _draft(entry, sheet_name, sheet_names, selection):
    chart, spec = entry["chart"], entry["chart"]["spec"]

    def ref(part):
        return None if part is None else _ref(part["source"], sheet_names, selection)

    def label(part):
        if part is None:
            return None
        return ("text", part["text"]) if "text" in part else ("ref", _ref(part["source"], sheet_names, selection))

    value_axis = spec["axes"]["value"]
    return {"name": chart["name"], "sheet": sheet_name,
            "anchor": dict(chart["anchor"]), "offset": dict(chart["offset"]), "size": dict(chart["size"]),
            "type": spec["type"], "grouping": spec["grouping"], "title": spec["title"]["text"],
            "category_title": spec["axes"]["category"]["title"], "value_title": value_axis["title"],
            "min": value_axis["min"], "max": value_axis["max"], "legend": spec["legend"]["position"],
            "categories": ref(spec["categories"]),
            "series": [{"name": label(item["name"]), "values": ref(item["values"]), "x": ref(item["x"])}
                       for item in spec["series"]]}


def drafts(entries, sheet_names, report, *, selection=None):
    """The export drafts of the charts that can be written, in entry order; every other chart is reported once."""
    result = []
    for entry in entries:
        if selection is not None and entry["sheet_id"] != selection["sheet_id"]:
            continue
        chart = entry["chart"]
        sheet_name = selection["name"] if selection is not None else sheet_names.get(entry["sheet_id"], entry["sheet"])
        try:
            _check(chart, sheet_names, selection)
        except _Skip as skip:
            report.add(SKIPPED, "omitted", skip.reason, sheet=sheet_name, cell=str(chart["name"])[:CELL_LENGTH])
            continue
        result.append(_draft(entry, sheet_name, sheet_names, selection))
    return result


def build(drafts, build_one, report):
    """``(draft, built)`` for each draft ``build_one`` accepts; one that fails (not an ``EngineError``) is reported."""
    built = []
    for draft in drafts:
        try:
            built.append((draft, build_one(draft)))
        except EngineError:
            raise
        except Exception:
            report.add(SKIPPED, "omitted", "unwritable", sheet=draft["sheet"], cell=draft["name"][:CELL_LENGTH])
    return built


def lines(written, left_out):
    """The report's one summary line for the charts of an export, or none when there were none."""
    if not written and not left_out:
        return []
    if not left_out:
        return [f"Charts: {written} written."]
    return [f"Charts: {written} written, {left_out} left out; each is listed below with its reason."]
