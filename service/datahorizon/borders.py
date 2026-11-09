# SPDX-License-Identifier: GPL-3.0-only
"""Portable border edges; colors remain the native palette / export black."""
import re
SIDES=("left","right","top","bottom")
BORDER_STYLES=("none","thin","medium","thick","dashed","dotted")
ODS_BORDERS={"none":"none","thin":"0.75pt solid #000000","medium":"1.5pt solid #000000","thick":"2.25pt solid #000000","dashed":"0.75pt dashed #000000","dotted":"0.75pt dotted #000000"}


def border_edges(style):
    return {side:style.get("border_"+side,"thin" if style.get("border")=="box" else "none") for side in SIDES}


def parse_ods_border(value):
    if value=="none":return "none"
    match=re.fullmatch(r"(0\.75pt|1px|1\.5pt|2\.25pt) (solid|dashed|dotted) #[0-9a-fA-F]{6}",value or "")
    if not match:return None
    width,pattern=match.groups()
    if pattern!="solid":return pattern if width in ("0.75pt","1px") else None
    return {"0.75pt":"thin","1px":"thin","1.5pt":"medium","2.25pt":"thick"}[width]
