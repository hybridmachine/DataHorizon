# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, structured record of what an import converted, omitted or could not evaluate."""

SEVERITIES = ("converted", "approximated", "omitted", "unevaluable")
MAX_SAMPLES = 100
MAX_ENTRIES = 200
MAX_DETAIL = 160
TRUNCATED_CATEGORY = "report.truncated"
TRUNCATED_DETAIL = "further distinct entries were not itemized"
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}


class ConversionReport:
    def __init__(self):
        # Keyed by (category, severity, detail); dicts keep first-seen order.
        self._items = {}
        self._overflow = 0

    def add(self, category, severity, detail="", *, sheet=None, cell=None, count=1):
        if severity not in _SEVERITY_RANK:
            raise ValueError(f"Unknown severity {severity!r}; expected one of {SEVERITIES}")
        if not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        samples = [{"sheet": sheet, "cell": cell}] if cell is not None else []
        self._record((category, severity, detail[:MAX_DETAIL]), count, samples)

    def merge(self, other):
        for key, item in other._items.items():
            self._record(key, item["count"], item["samples"])
        self._overflow += other._overflow

    def entries(self):
        result = [self._entry(key, item) for key, item in self._items.items()]
        result.sort(key=lambda e: (-_SEVERITY_RANK[e["severity"]], -e["count"], e["category"]))
        if self._overflow:
            result.append({"category": TRUNCATED_CATEGORY, "severity": "omitted",
                           "detail": TRUNCATED_DETAIL, "count": self._overflow, "samples": []})
        return result

    def lines(self):
        return [self._line(entry) for entry in self.entries()]

    def totals(self):
        totals = dict.fromkeys(SEVERITIES, 0)
        for key, item in self._items.items():
            totals[key[1]] += item["count"]
        totals["omitted"] += self._overflow
        return totals

    def _record(self, key, count, samples):
        item = self._items.get(key)
        if item is None:
            if len(self._items) >= MAX_ENTRIES - 1:
                self._overflow += count
                return
            item = self._items[key] = {"count": 0, "samples": []}
        item["count"] += count
        room = MAX_SAMPLES - len(item["samples"])
        if room > 0:
            item["samples"].extend(dict(sample) for sample in samples[:room])

    @staticmethod
    def _entry(key, item):
        category, severity, detail = key
        return {"category": category, "severity": severity, "detail": detail,
                "count": item["count"], "samples": [dict(sample) for sample in item["samples"]]}

    @staticmethod
    def _line(entry):
        noun = "cell" if entry["count"] == 1 else "cells"
        line = f"{entry['count']} {noun}: {entry['detail'] or entry['category']} ({entry['severity']})"
        if entry["samples"]:
            first = entry["samples"][0]
            place = f"{first['sheet']}!{first['cell']}" if first["sheet"] else first["cell"]
            line += f"; first at {place}"
        return line
