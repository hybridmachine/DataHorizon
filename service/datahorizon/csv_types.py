# SPDX-License-Identifier: GPL-3.0-only
"""Optional hash-bound temporal companion metadata for otherwise untyped CSV."""
import hashlib


def temporal_schema(text, entries):
    return {"version":1,"sha256":hashlib.sha256(text.encode("utf-8")).hexdigest(),"cells":entries}
