# SPDX-License-Identifier: GPL-3.0-only
"""Format-neutral chart export: collecting stored charts, deciding which can be written and building them.

``collect`` reads the described charts of a workbook; ``drafts`` turns the ones a file can hold into export drafts
(the inverse of the import drafts) and reports every other chart as ``chart.skipped``; ``build`` hands each draft to a
format writer.  A chart is written whole or left out whole.
"""
import json
import re

from . import charts
from .conversion_report import ConversionReport
from .engine import EngineError

SKIPPED = "chart.skipped"
CELL_LENGTH = 32  # the chart name a report entry carries


def collect(db, sheet_id=None):
    """Every stored chart, described, as ``{"sheet_id", "sheet", "chart"}``, by sheet position, then ``z``, then id; only
    those on the sheet ``sheet_id`` when it is given (a selection export).

    The table, query and sheet catalogs are each read at most once, however many sources the charts have.  A stored
    chart that cannot be read (not JSON, not an object, or not a valid stored chart) is an entry with the issue
    ``INVALID_FORMAT``, which ``drafts`` reports.  Empty when the workbook format is below the one that stores charts."""
    if db.execute("SELECT format_version FROM dh_meta").fetchone()[0] < charts.FORMAT_VERSION:
        return []
    query = ("SELECT s.position, s.id, s.name, o.id, o.payload FROM dh_objects o "
             "JOIN sheets s ON s.id = o.sheet_id WHERE o.namespace = 'chart'")
    rows = db.execute(query) if sheet_id is None else db.execute(query + " AND o.sheet_id = ?", (sheet_id,))
    lookups = {}
    found = []
    for position, chart_sheet, sheet, chart_id, text in rows:
        z, payload = 0, None
        try:
            payload = json.loads(text) if isinstance(text, (str, bytes)) else None
            if not isinstance(payload, dict):
                raise ValueError("a stored chart is an object")
            chart = charts.describe(db, chart_sheet, chart_id, payload, lookups)
            z = payload["z"]
        except (ValueError, EngineError):
            # An unreadable chart stays an entry so that ``drafts`` can report it; nothing else is contained.
            name = payload.get("name") if isinstance(payload, dict) else None
            chart = {"id": chart_id, "sheet_id": chart_sheet, "issue": "INVALID_FORMAT",
                     "name": name if isinstance(name, str) and name else "Chart"}
        found.append(((position, chart_sheet, z, chart_id), {"sheet_id": chart_sheet, "sheet": sheet, "chart": chart}))
    found.sort(key=lambda item: item[0])
    return [entry for _, entry in found]


# References.

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
        # Always quoted: a bare name such as R, C, TRUE or FALSE could be read as something other than a sheet.
        name = "'" + sheet.replace("'", "''") + "'"
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


def build(drafts, build_one, report, *, step=None):
    """``(draft, built)`` for each draft ``build_one`` accepts; one that fails (not an ``EngineError``) is reported.

    ``step()``, when given, is called before each chart is built (a streamed export's checkpoint: what it raises, a
    cancellation included, stops the build)."""
    built = []
    for draft in drafts:
        if step is not None:
            step()
        try:
            built.append((draft, build_one(draft)))
        except EngineError:
            raise
        except Exception:
            report.add(SKIPPED, "omitted", "unwritable", sheet=draft["sheet"], cell=draft["name"][:CELL_LENGTH])
    return built


def package(built, package_one, empty, report):
    """``(packaged, written)``: ``package_one(built, notes)``, the format's packaging of the built charts, and the pairs
    it holds.

    ``notes`` is a ``ConversionReport`` merged into ``report`` once packaging succeeds.  When packaging raises anything
    but an ``EngineError``, the export goes on without any chart: ``(empty, [])`` is returned and every built chart is
    reported ``unwritable`` in ``report``, so the caller counts it as left out."""
    notes = ConversionReport()
    try:
        packaged = package_one(built, notes)
    except EngineError:
        raise
    except Exception:
        for draft, _ in built:
            report.add(SKIPPED, "omitted", "unwritable", sheet=draft["sheet"], cell=draft["name"][:CELL_LENGTH])
        return empty, []
    report.merge(notes)
    return packaged, built


SIZE_CODES = ("LIMIT", "READ_LIMIT")  # an inline export's expanded-size and response-size failures


def fit(built, attempt):
    """``attempt(kept)`` for the most leading charts of ``built`` whose inline export fits its size limits.

    ``attempt`` assembles the whole export with the charts ``kept`` (a prefix of ``built``), reporting the rest as left
    out (``oversize``), and raises an ``EngineError`` when the file or its response is over a limit.  The export with
    every chart is tried first, so an export that fits is assembled once.  When it does not fit, the export without
    charts decides: if the cells alone are over a limit that failure is raised, as it was before charts were written;
    otherwise the charts are what did not fit, and the longest prefix that does is kept."""
    try:
        return attempt(built)
    except EngineError as error:
        if not built or error.body["code"] not in SIZE_CODES:
            raise
    result = attempt([])
    fits, over = 0, len(built)
    while over - fits > 1:
        middle = (fits + over) // 2
        try:
            result = attempt(built[:middle])
            fits = middle
        except EngineError as error:
            if error.body["code"] not in SIZE_CODES:
                raise
            over = middle
    return result


def oversize(pairs, report):
    """Report each built chart of ``pairs`` as left out because the export had no room for it; how many there were."""
    for draft, _ in pairs:
        report.add(SKIPPED, "omitted", "size_limit", sheet=draft["sheet"], cell=draft["name"][:CELL_LENGTH])
    return len(pairs)


def lines(written, left_out):
    """The report's one summary line for the charts of an export, or none when there were none."""
    if not written and not left_out:
        return []
    if not left_out:
        return [f"Charts: {written} written."]
    return [f"Charts: {written} written, {left_out} left out; each is listed below with its reason."]


def snapshot_charts(snapshot, build_one, report, *, name="Sheet1"):
    """``(built, left_out)`` for the charts of an inline export's read snapshot: ``build``'s pairs and how many charts
    were left out, each reported in ``report``.

    A whole-workbook snapshot (``sheets``) writes the charts of every sheet; a selection snapshot (``sheet``, ``range``)
    writes those of its sheet that it holds whole, on the sheet ``name``.  A snapshot without ``chart_entries`` (the
    list and relational exports) has no charts."""
    entries = snapshot.get("chart_entries")
    if not entries:
        return [], 0
    selection = None if "sheets" in snapshot else {"sheet_id": snapshot["sheet"], "range": snapshot["range"], "name": name}
    found = ConversionReport()
    built = build(drafts(entries, snapshot["chart_sheets"], found, selection=selection), build_one, found)
    report.merge(found)
    return built, _left_out(found)


def workbook_drafts(catalog, report):
    """``(drafts, left_out)`` for the charts of a streamed whole-workbook export, from its native ``catalog``
    (``Workbook.native_catalog(charts=True)``): the drafts its writer builds, each checkpointed, and how many charts no
    file can hold, each reported in ``report``.  A chart the writer then fails to build is reported by ``build`` and
    left out too: the writer returns how many it wrote."""
    entries = catalog.get("chart_entries")
    if not entries:
        return [], 0
    found = ConversionReport()
    result = drafts(entries, catalog["chart_sheets"], found)
    report.merge(found)
    return result, _left_out(found)


def _left_out(report):
    return sum(entry["count"] for entry in report.entries() if entry["category"] == SKIPPED)
