# SPDX-License-Identifier: GPL-3.0-only
"""OpenAPI contract for charts: managed chart operations and the chart catalog read."""
from .charts import LEGEND_POSITIONS, LIMIT_CHARTS, LIMIT_SERIES, SPEC_VERSION, TITLE_LENGTH, TYPES
from .formula import MAX_COLS, MAX_ROWS

EXAMPLE_ID = "00000000-0000-4000-8000-000000000000"
EXAMPLE_SPEC = {"spec_version": 1, "type": "column",
                "series": [{"name": {"text": "Values"},
                            "values": {"source": {"kind": "range", "sheet": "Sheet1", "ref": "A1:A3"}}}]}


def install(schemas, actions):
    sheet = {"type": "string", "minLength": 1, "description": "Sheet name or stable sheet ID", "default": "Sheet1"}
    chart_id = {"type": "string", "format": "uuid"}
    name = {"type": "string", "minLength": 1, "maxLength": 64, "description": "Display name; defaults to the lowest unused 'Chart N' on the sheet"}
    anchor = {"type": "object", "additionalProperties": False, "required": ["row", "col"],
              "properties": {"row": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}, "col": {"type": "integer", "minimum": 1, "maximum": MAX_COLS}},
              "description": "Top-left cell the chart is placed over; defaults to row 1, column 1"}
    offset = {"type": "object", "additionalProperties": False, "required": ["x", "y"],
              "properties": {"x": {"type": "integer", "minimum": 0, "maximum": 4096}, "y": {"type": "integer", "minimum": 0, "maximum": 4096}},
              "description": "Pixel offset from the anchor cell; defaults to 0, 0"}
    size = {"type": "object", "additionalProperties": False, "required": ["width", "height"],
            "properties": {"width": {"type": "integer", "minimum": 64, "maximum": 4096}, "height": {"type": "integer", "minimum": 48, "maximum": 4096}},
            "description": "Pixel size; defaults to 480 by 300"}
    spec = {"type": "object", "description": "Chart specification in input form: sources are ChartSource objects, a range, a table column or a saved query result column"}
    placement = {"name": name, "anchor": anchor, "offset": offset, "size": size}
    actions["chart_create"] = (["spec"], {"sheet": sheet, **placement, "spec": spec},
                               {"sheet": "Sheet1", "spec": EXAMPLE_SPEC})
    actions["chart_update"] = (["chart_id"], {
        "chart_id": chart_id, **placement,
        "z": {"type": "integer", "minimum": -1000000, "maximum": 1000000, "description": "Stacking order; not with order"},
        "order": {"type": "string", "enum": ["front", "back"], "description": "Move above or below every other chart on the sheet; not with z"},
        "spec": {**spec, "description": spec["description"] + ". Replaces the specification; not with spec_patch"},
        "spec_patch": {"type": "object", "description": "RFC 7386 merge patch applied to the input form of the stored specification; "
                       "refused with SOURCE_BROKEN when a source is broken, or with the chart's issue code. A merge patch keeps keys it "
                       "is not told to remove, so changing a source's kind sets the old kind's keys to null in the patch, for example "
                       "{kind: table, table: Sales, column: Units, sheet: null, ref: null}; or replace the specification with spec"}},
        {"chart_id": EXAMPLE_ID, "name": "Revenue"})
    actions["chart_delete"] = (["chart_id"], {"chart_id": chart_id}, {"chart_id": EXAMPLE_ID})
    schemas["ChartsReadRequest"] = {"type": "object", "additionalProperties": False, "required": ["expected_state"],
                                    "properties": {"expected_state": {"type": "string", "minLength": 1}, "sheet": sheet, "chart_id": chart_id},
                                    "example": {"expected_state": "copy-from-workbook-info"}}
    _install_specification(schemas)
    _install_catalog(schemas, sheet, chart_id, name, anchor, offset, size)
    _install_jobs(schemas, sheet, chart_id)
    schemas["Receipt"]["properties"].update(
        chart_id={**chart_id, "description": "Chart created, changed or deleted by this operation"},
        changed_charts={"type": "integer", "minimum": 0, "description": "Charts written by this operation"},
        charts_over_limit={"type": "array", "items": {"type": "string"},
                           "description": "Charts this row or column edit pushed past a size limit (sorted chart IDs)"},
        broken_charts={"type": "array", "items": {"type": "string"},
                       "description": "Charts with a source this operation broke (chart IDs): a sheet delete, row or column delete, "
                                      "table drop, column removal in a schema change or saved query delete. Present only when it breaks one"})


def _ref(name):
    return {"$ref": "#/components/schemas/" + name}


def _part(what):
    return {"type": "object", "additionalProperties": False, "required": ["source"],
            "properties": {"source": _ref("ChartSource")}, "description": what}


def _install_specification(schemas):
    schemas["ChartRangeSource"] = {
        "type": "object", "additionalProperties": False, "required": ["kind", "sheet", "ref"],
        "properties": {"kind": {"type": "string", "enum": ["range"]},
                       "sheet": {"type": "string", "minLength": 1, "description": "Sheet name or stable sheet ID"},
                       "ref": {"type": "string", "minLength": 2, "description": "One cell or one rectangle without a sheet, such as A2:A13"}},
        "description": "A cell range", "example": {"kind": "range", "sheet": "Sheet1", "ref": "A2:A13"}}
    schemas["ChartSource"] = {
        "oneOf": [
            _ref("ChartRangeSource"),
            {"type": "object", "additionalProperties": False, "required": ["kind", "table", "column"],
             "properties": {"kind": {"type": "string", "enum": ["table"]},
                            "table": {"type": "string", "minLength": 1, "description": "Table name or stable table ID"},
                            "column": {"type": "string", "minLength": 1, "description": "Column name or stable column ID"}},
             "description": "One column of a structured table, read in stored row order",
             "example": {"kind": "table", "table": "Sales", "column": "Units"}},
            {"type": "object", "additionalProperties": False, "required": ["kind", "query", "column"],
             "properties": {"kind": {"type": "string", "enum": ["query"]},
                            "query": {"type": "string", "minLength": 1, "description": "Saved query name or stable query ID"},
                            "column": {"type": "string", "minLength": 1, "description": "Name of a column of the query result"}},
             "description": "One column of a saved query's result, run afresh for every resolve or render",
             "example": {"kind": "query", "query": "Totals", "column": "Total"}}],
        "description": "A source in input form, by kind. Values, categories and x sources are one row or one column; a name source is one "
                       "cell and so only a range. A table or saved query is held by identity once stored, so renaming it needs no chart "
                       "maintenance; a query result column is held by its name",
        "example": {"kind": "range", "sheet": "Sheet1", "ref": "A2:A13"}}
    nullable_text = {"type": "string", "nullable": True}
    series = {"type": "object", "additionalProperties": False, "required": ["name", "values"], "properties": {
        "name": {"oneOf": [
            {"type": "object", "additionalProperties": False, "required": ["text"], "properties": {"text": {"type": "string"}}},
            {"type": "object", "additionalProperties": False, "required": ["source"], "properties": {"source": _ref("ChartRangeSource")},
             "description": "One cell of a range; a table or saved query is not a name source"}]},
        "values": _part("Numeric values"),
        "x": {**_part("Numeric x values; required for scatter charts and refused otherwise"), "nullable": True}}}
    schemas["ChartSpec"] = {
        "type": "object", "additionalProperties": False, "required": ["spec_version", "type", "series"],
        "properties": {
            "spec_version": {"type": "integer", "enum": [SPEC_VERSION]},
            "type": {"type": "string", "enum": list(TYPES)},
            "grouping": {"type": "string", "enum": ["clustered", "stacked"], "nullable": True,
                         "description": "Column, bar and area charts; defaults to clustered. Null for other types"},
            "title": {"type": "object", "additionalProperties": False, "required": ["text"],
                      "properties": {"text": {**nullable_text, "maxLength": TITLE_LENGTH}}},
            "categories": {**_part("Category labels; not for scatter charts"), "nullable": True},
            "series": {"type": "array", "minItems": 1, "maxItems": LIMIT_SERIES, "items": series,
                       "description": "A pie chart has exactly one series"},
            "axes": {"type": "object", "additionalProperties": False, "properties": {
                "category": {"type": "object", "additionalProperties": False, "required": ["title"], "properties": {"title": nullable_text}},
                "value": {"type": "object", "additionalProperties": False, "required": ["title", "min", "max"], "properties": {
                    "title": nullable_text, "min": {"type": "number", "nullable": True}, "max": {"type": "number", "nullable": True}}}}},
            "legend": {"type": "object", "additionalProperties": False, "required": ["position"],
                       "properties": {"position": {"type": "string", "enum": list(LEGEND_POSITIONS)}}}},
        "description": "Chart specification in input form. Sources together hold at most 100,000 cells; a series at most 10,000 points "
                       "(1,000 for a pie). A render draws at most 10,000 values across all series",
        "example": EXAMPLE_SPEC}


def _install_catalog(schemas, sheet, chart_id, name, anchor, offset, size):
    schemas["Chart"] = {
        "type": "object", "required": ["id", "sheet_id", "name", "anchor", "offset", "size", "z", "spec_version", "spec", "issue", "over_limit"],
        "properties": {
            "id": chart_id, "sheet_id": {"type": "string", "description": "Stable ID of the sheet the chart is placed on"},
            "name": {key: value for key, value in name.items() if key != "description"},
            "anchor": anchor, "offset": offset, "size": size, "z": {"type": "integer"},
            "spec_version": {"type": "integer", "minimum": 1},
            "spec": {"type": "object", "description": "Stored specification: every key present, and each source as one of "
                     "{kind: range, sheet_id, row1, col1, row2, col2, broken, ref}, {kind: table, table_id, column_id, broken, table, column} or "
                     "{kind: query, query_id, column, broken, query}; a broken range has null coordinates and ref, ref is A1 text without a sheet, "
                     "and table, column and query are current names, null when broken"},
            "issue": {"type": "string", "nullable": True, "enum": ["INVALID_SPEC", "UNSUPPORTED_SPEC_VERSION", None],
                      "description": "Why the stored specification cannot be read; null for a readable chart"},
            "over_limit": {"type": "string", "nullable": True, "enum": ["LIMIT_POINTS", "LIMIT_PIE_POINTS", "LIMIT_CELLS", None],
                           "description": "The size limit a readable chart's sources now exceed after a row or column edit; resolve and "
                                          "render refuse it with that code until it fits again. Null otherwise"}}}
    schemas["ChartCatalog"] = {"type": "object", "required": ["state", "charts"], "properties": {
        "state": {"type": "string"}, "charts": {"type": "array", "maxItems": LIMIT_CHARTS, "items": _ref("Chart")}}}


def _install_jobs(schemas, sheet, chart_id):
    cell = {"type": "integer", "minimum": 1}
    table_form = {"expected_state": "copy-from-workbook-info", "table": "Sales", "type": "column"}
    schemas["ChartSuggestRequest"] = {
        "type": "object", "required": ["expected_state"],
        "properties": {"expected_state": {"type": "string", "minLength": 1}, "sheet": sheet,
                       "r1": {**cell, "maximum": MAX_ROWS}, "c1": {**cell, "maximum": MAX_COLS},
                       "r2": {**cell, "maximum": MAX_ROWS}, "c2": {**cell, "maximum": MAX_COLS},
                       "table": {"type": "string", "minLength": 1, "description": "Table name or ID; not with sheet, r1, c1, r2 or c2"},
                       "type": {"type": "string", "enum": list(TYPES), "default": "column"}},
        "description": "Either a rectangle of at most 10,000 cells (sheet, r1, c1, r2, c2: a text first row is a header and a text first "
                       "column holds categories) or a table (integer and number columns are series in schema order; the first column that is "
                       "neither numeric nor boolean holds the categories; the suggestion names the table and columns by ID and reads no rows)",
        "example": {"expected_state": "copy-from-workbook-info", "sheet": "Sheet1", "r1": 1, "c1": 1, "r2": 13, "c2": 3},
        "x-examples": {"rectangle": {"expected_state": "copy-from-workbook-info", "sheet": "Sheet1", "r1": 1, "c1": 1, "r2": 13, "c2": 3},
                       "table": table_form}}
    schemas["ChartSuggestion"] = {"type": "object", "required": ["spec"], "properties": {"spec": _ref("ChartSpec")}}
    width = {"type": "integer", "minimum": 64, "maximum": 4096}
    height = {"type": "integer", "minimum": 48, "maximum": 4096}
    job_id = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,100}$"}
    schemas["ChartJobRequest"] = {
        "type": "object", "additionalProperties": False, "required": ["job_id", "expected_state", "chart_id", "kind"],
        "properties": {"job_id": job_id, "expected_state": {"type": "string", "minLength": 1}, "chart_id": chart_id,
                       "kind": {"type": "string", "enum": ["resolve", "render"]}, "width": width, "height": height},
        "oneOf": [
            {"title": "Resolve", "properties": {"kind": {"enum": ["resolve"]}}, "not": {"anyOf": [{"required": ["width"]}, {"required": ["height"]}]}},
            {"title": "Render", "properties": {"kind": {"enum": ["render"]}}, "required": ["width", "height"]}],
        "description": "Reads the chart's sources from a read-only snapshot, calculating formulas, within 30 seconds. "
                       "A render of more than 10,000 values across all series fails with LIMIT_RENDER_POINTS. "
                       "An unknown chart is CHART_NOT_FOUND before any job starts",
        "example": {"job_id": "example-chart-render", "expected_state": "copy-from-workbook-info", "chart_id": EXAMPLE_ID,
                    "kind": "render", "width": 480, "height": 300}}
    count = {"type": "integer", "minimum": 0}
    result = {"type": "object", "required": ["resolved", "counts", "state"], "properties": {
        "resolved": {"type": "object", "description": "The plain chart the renderer draws: type, grouping, title, categories, "
                     "series [{name, values, x}] with null for a gap, axes and legend"},
        "counts": {"type": "object", "required": ["points", "gaps"], "properties": {"points": count, "gaps": {
            "type": "object", "required": ["empty", "text", "error"], "properties": {"empty": count, "text": count, "error": count}}}},
        "state": {"type": "string"},
        "svg": {"type": "string", "description": "Render only: SVG using only svg, g, rect, line, path, circle and text elements"},
        "marks": {"type": "array", "items": {"type": "object"}, "description": "Render only: one rectangle per drawn point"},
        "width": width, "height": height},
        "description": "Present when completed and current. The whole result is at most 4 MiB, or the job fails with READ_LIMIT"}
    schemas["ChartJob"] = {
        "type": "object", "required": ["job_id", "status", "completed", "phase", "state", "chart_id", "kind"],
        "properties": {"job_id": job_id,
                       "status": {"type": "string", "enum": ["running", "completed", "failed", "cancelled", "stale"]},
                       "completed": count, "phase": {"type": "string"}, "error": _ref("Error"), "result": result,
                       "state": {"type": "string", "description": "Workbook state the job read"},
                       "chart_id": chart_id, "kind": {"type": "string", "enum": ["resolve", "render"]}},
        "description": "Poll while running. Once the workbook state differs from state, a completed job, or one that failed with "
                       "STALE_STATE because the workbook changed while it read, is stale and carries no result. "
                       "A failed job carries the structured error, for example SOURCE_BROKEN",
        "example": {"job_id": "example-chart-render", "status": "running", "completed": 0, "phase": "Queued",
                    "state": "copy-from-workbook-info", "chart_id": EXAMPLE_ID, "kind": "render"}}
