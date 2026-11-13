# SPDX-License-Identifier: GPL-3.0-only
"""OpenAPI contract for charts: managed chart operations and the chart catalog read."""
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
    schemas["Receipt"]["properties"].update(
        chart_id={**chart_id, "description": "Chart created, changed or deleted by this operation"},
        changed_charts={"type": "integer", "minimum": 0, "description": "Charts written by this operation"})
