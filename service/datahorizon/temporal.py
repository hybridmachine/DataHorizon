"""Civil ISO date/time values and explicit Excel serial conversion.

No locale or host timezone participates. Temporal is a JSON-compatible ISO
string carrying a distinct in-process kind; wire responses expose value_kind.
"""
from datetime import date, datetime, time, timedelta, timezone
import math
import re

KINDS = ("date", "time", "datetime")
DAY_US = 86_400_000_000


def utc_clock():
    """A calculation's UTC civil sample, independent of the host timezone."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


PATTERNS = {
    "date": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
    "time": r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?",
    "datetime": r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?",
}


class Temporal(str):
    def __new__(cls, source, kind):
        if kind not in KINDS or not isinstance(source, str) or not re.fullmatch(PATTERNS[kind], source):
            raise ValueError("Expected strict ISO " + str(kind) + " without timezone offsets")
        clock = source[11:] if kind == "datetime" else source
        if kind != "date" and (int(clock[:2]) > 23 or int(clock[3:5]) > 59 or int(clock[6:8]) > 59):
            raise ValueError("Clock must be within 00:00:00–23:59:59.999999")
        parsed = {"date": date, "time": time, "datetime": datetime}[kind].fromisoformat(source)
        canonical = parsed.isoformat()
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        value = super().__new__(cls, canonical)
        value.kind = kind
        return value

    def parsed(self):
        return {"date": date, "time": time, "datetime": datetime}[self.kind].fromisoformat(self)


def to_serial(value, system="1900"):
    if system not in ("1900", "1904"):
        raise ValueError("Unknown Excel date system")
    parsed = value.parsed()
    if value.kind == "time":
        return (parsed.hour*3_600_000_000 + parsed.minute*60_000_000 + parsed.second*1_000_000 + parsed.microsecond)/DAY_US
    day = parsed.date() if value.kind == "datetime" else parsed
    base = date(1899,12,31) if system == "1900" else date(1904,1,1)
    days = (day-base).days
    if system == "1900" and day >= date(1900,3,1):
        days += 1
    fraction = to_serial(Temporal(parsed.time().isoformat(), "time")) if value.kind == "datetime" else 0
    return days + fraction


def from_serial(serial, kind="datetime", system="1900"):
    if type(serial) not in (int,float) or not math.isfinite(serial) or kind not in KINDS or system not in ("1900","1904"):
        raise ValueError("Expected finite serial, temporal kind and Excel date system")
    day = math.floor(serial)
    micros = round((serial-day)*DAY_US)
    if micros == DAY_US:
        day, micros = day+1, 0
    if kind == "time":
        return Temporal((datetime(2000,1,1)+timedelta(microseconds=micros)).time().isoformat(), "time")
    if system == "1900" and day == 60:
        raise ValueError("Excel serial 60 denotes nonexistent 1900-02-29")
    base = datetime(1899,12,31) if system == "1900" else datetime(1904,1,1)
    try:
        parsed = base + timedelta(days=day-int(system=="1900" and day>60), microseconds=micros)
    except (OverflowError,ValueError) as error:
        raise ValueError("Serial exceeds years 0001–9999") from error
    return Temporal(parsed.date().isoformat() if kind == "date" else parsed.isoformat(), kind)
