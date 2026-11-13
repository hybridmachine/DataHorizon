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
    spec = {"type": "object", "description": "Chart specification in input form: sources are {kind: range, sheet, ref} with A1 text"}
    placement = {"name": name, "anchor": anchor, "offset": offset, "size": size}
    actions["chart_create"] = (["spec"], {"sheet": sheet, **placement, "spec": spec},
                               {"sheet": "Sheet1", "spec": EXAMPLE_SPEC})
    actions["chart_update"] = (["chart_id"], {
        "chart_id": chart_id, **placement,
        "z": {"type": "integer", "minimum": -1000000, "maximum": 1000000, "description": "Stacking order; not with order"},
        "order": {"type": "string", "enum": ["front", "back"], "description": "Move above or below every other chart on the sheet; not with z"},
        "spec": {**spec, "description": spec["description"] + ". Replaces the specification; not with spec_patch"},
        "spec_patch": {"type": "object", "description": "RFC 7386 merge patch applied to the input form of the stored specification; "
                       "refused with SOURCE_BROKEN when a source is broken, or with the chart's issue code"}},
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
                           "description": "Charts this row or column edit pushed past a size limit (sorted chart IDs)"})


def _ref(name):
    return {"$ref": "#/components/schemas/" + name}


def _part(what):
    return {"type": "object", "additionalProperties": False, "required": ["source"],
            "properties": {"source": _ref("ChartSource")}, "description": what}


def _install_specification(schemas):
    schemas["ChartSource"] = {
        "type": "object", "additionalProperties": False, "required": ["kind", "sheet", "ref"],
        "properties": {"kind": {"type": "string", "enum": ["range"]},
                       "sheet": {"type": "string", "minLength": 1, "description": "Sheet name or stable sheet ID"},
                       "ref": {"type": "string", "minLength": 2, "description": "One cell or one rectangle without a sheet, such as A2:A13"}},
        "description": "A source in input form. Values, categories and x sources are one row or one column; a name source is one cell",
        "example": {"kind": "range", "sheet": "Sheet1", "ref": "A2:A13"}}
    nullable_text = {"type": "string", "nullable": True}
    series = {"type": "object", "additionalProperties": False, "required": ["name", "values"], "properties": {
        "name": {"oneOf": [
            {"type": "object", "additionalProperties": False, "required": ["text"], "properties": {"text": {"type": "string"}}},
            {"type": "object", "additionalProperties": False, "required": ["source"], "properties": {"source": _ref("ChartSource")}}]},
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
            "spec": {"type": "object", "description": "Stored specification: every key present, and each source as {kind, sheet_id, row1, col1, "
                     "row2, col2, broken, ref}; a broken source has null coordinates and ref, and ref is A1 text without a sheet"},
            "issue": {"type": "string", "nullable": True, "enum": ["INVALID_SPEC", "UNSUPPORTED_SPEC_VERSION", None],
                      "description": "Why the stored specification cannot be read; null for a readable chart"},
            "over_limit": {"type": "string", "nullable": True, "enum": ["LIMIT_POINTS", "LIMIT_PIE_POINTS", "LIMIT_CELLS", None],
                           "description": "The size limit a readable chart's sources now exceed after a row or column edit; resolve and "
                                          "render refuse it with that code until it fits again. Null otherwise"}}}
    schemas["ChartCatalog"] = {"type": "object", "required": ["state", "charts"], "properties": {
        "state": {"type": "string"}, "charts": {"type": "array", "maxItems": LIMIT_CHARTS, "items": _ref("Chart")}}}


def _install_jobs(schemas, sheet, chart_id):
    cell = {"type": "integer", "minimum": 1}
    schemas["ChartSuggestRequest"] = {
        "type": "object", "required": ["expected_state"],
        "properties": {"expected_state": {"type": "string", "minLength": 1}, "sheet": sheet,
                       "r1": {**cell, "maximum": MAX_ROWS}, "c1": {**cell, "maximum": MAX_COLS},
                       "r2": {**cell, "maximum": MAX_ROWS}, "c2": {**cell, "maximum": MAX_COLS},
                       "type": {"type": "string", "enum": list(TYPES), "default": "column"}},
        "description": "A rectangle of at most 10,000 cells. A text first row is a header and a text first column holds categories",
        "example": {"expected_state": "copy-from-workbook-info", "sheet": "Sheet1", "r1": 1, "c1": 1, "r2": 13, "c2": 3}}
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
