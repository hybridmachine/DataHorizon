# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, structured record of what an import converted, omitted or could not evaluate."""

SEVERITIES = ("converted", "approximated", "omitted", "unevaluable")
MAX_SAMPLES = 100
MAX_ENTRIES = 200
MAX_DETAIL = 160
MAX_CATEGORY = 64
MAX_SAMPLE_SHEET = 64
MAX_SAMPLE_CELL = 32
TRUNCATED_CATEGORY = "report.truncated"
# A worksheet or workbook feature DataHorizon does not import; the detail is its feature id (see xlsx_features).
SHEET_FEATURE = "sheet.feature"
WORKBOOK_FEATURE = "workbook.feature"
FEATURE_CATEGORIES = (SHEET_FEATURE, WORKBOOK_FEATURE)
TRUNCATED_DETAIL = "further distinct entries were not itemized"
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}


def place(sample):
    """A sample as text: Sheet!A1, a whole sheet, or a bare cell."""
    if sample.get("cell") is None: return sample.get("sheet") or "workbook"
    return f"{sample['sheet']}!{sample['cell']}" if sample.get("sheet") else sample["cell"]


class ConversionReport:
    def __init__(self):
        # Keyed by (category, severity, detail); dicts keep first-seen order.
        self._items = {}
        self._overflow = 0

    def add(self, category, severity, detail="", *, sheet=None, cell=None, count=1, places=None, at_least=False):
        """Record `count` items at one place: a cell, a whole sheet (cell None) or the workbook (both None).

        `places` instead lists several (sheet, cell) places for the same items, such as the hidden sheets. `at_least`
        marks a count that saturated at a scanner bound: the entry then reads "at least" its count."""
        if severity not in _SEVERITY_RANK:
            raise ValueError(f"Unknown severity {severity!r}; expected one of {SEVERITIES}")
        if not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        if places is None: places = [(sheet, cell)]
        samples = [{"sheet": where[:MAX_SAMPLE_SHEET] if where is not None else None,
                    "cell": at[:MAX_SAMPLE_CELL] if at is not None else None}
                   for where, at in places if where is not None or at is not None]
        self._record((category[:MAX_CATEGORY], severity, detail[:MAX_DETAIL]), count, samples, at_least)

    def merge(self, other):
        for key, item in other._items.items():
            self._record(key, item["count"], item["samples"], item.get("at_least", False))
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

    def _record(self, key, count, samples, at_least=False):
        item = self._items.get(key)
        if item is None:
            if len(self._items) >= MAX_ENTRIES - 1:
                self._overflow += count
                return
            item = self._items[key] = {"count": 0, "samples": []}
        item["count"] += count
        if at_least: item["at_least"] = True
        room = MAX_SAMPLES - len(item["samples"])
        if room > 0:
            item["samples"].extend(dict(sample) for sample in samples[:room])

    @staticmethod
    def _entry(key, item):
        category, severity, detail = key
        entry = {"category": category, "severity": severity, "detail": detail,
                 "count": item["count"], "samples": [dict(sample) for sample in item["samples"]]}
        # Only a saturated count carries the key, so every other entry keeps its shape.
        if item.get("at_least"): entry["at_least"] = True
        return entry

    @staticmethod
    def _line(entry):
        noun = "item" if entry["category"] in FEATURE_CATEGORIES else "cell"
        line = f"{'at least ' if entry.get('at_least') else ''}{entry['count']} {noun}{'' if entry['count'] == 1 else 's'}: {entry['detail'] or entry['category']} ({entry['severity']})"
        if entry["samples"]:
            line += f"; first at {place(entry['samples'][0])}"
        return line
