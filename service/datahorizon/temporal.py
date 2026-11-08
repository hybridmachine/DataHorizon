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


def import_temporal(source,kind,timezone_policy='reject'):
    """Import shorthand to strict native ISO; offset conversion is explicit."""
    if timezone_policy not in ('reject','utc'):raise ValueError('timezone_policy must be reject or utc')
    if not isinstance(source,str):raise ValueError('ISO input must be Text')
    source=source.replace(' ','T',1) if kind=='datetime' else source
    clock=source[11:] if kind=='datetime' else source
    if kind in ('time','datetime') and re.fullmatch(r'[0-9]{2}:[0-9]{2}',clock):source+=':00'
    offset=re.fullmatch(r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?)(Z|[+-][0-9]{2}:[0-9]{2})',source)
    if kind=='datetime' and offset:
        if timezone_policy!='utc':raise ValueError('Offset-bearing timestamps require explicit UTC normalization')
        Temporal(offset[1],'datetime')
        zone=offset[2]
        if zone!='Z' and (int(zone[1:3])>14 or int(zone[4:])>59 or (int(zone[1:3])==14 and int(zone[4:])!=0)):raise ValueError('UTC offset must be within ±14:00')
        try:source=datetime.fromisoformat(source).astimezone(timezone.utc).replace(tzinfo=None).isoformat()
        except OverflowError as error:raise ValueError('UTC normalization exceeds years 0001–9999') from error
    return Temporal(source,kind)


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


def temporal_key(value):
    parsed=value.parsed()
    clock=parsed if value.kind=="time" else parsed.time() if value.kind=="datetime" else time()
    micros=((clock.hour*60+clock.minute)*60+clock.second)*1_000_000+clock.microsecond
    return ("time",micros) if value.kind=="time" else ("calendar",(parsed.date() if value.kind=="datetime" else parsed).toordinal()*DAY_US+micros)


def temporal_arithmetic(left, right, op):
    """Civil day offsets and elapsed differences; never traverse fake serial 60."""
    lt,rt=isinstance(left,Temporal),isinstance(right,Temporal)
    if op not in ("+","-"):raise TypeError("Temporal values support only + and -")
    if lt and rt:
        a,b=temporal_key(left),temporal_key(right)
        if op=="-" and a[0]==b[0]:return (a[1]-b[1])/DAY_US
        if right.kind=="time" and (op=="+" or left.kind!="time"):
            amount=b[1]/DAY_US
            if left.kind=="date":left=Temporal(str(left)+"T00:00:00","datetime")
            return temporal_arithmetic(left,amount,op)
        if op=="+" and left.kind=="time":return temporal_arithmetic(right,left,op)
        raise TypeError("Incompatible temporal arithmetic")
    if not lt:
        if op!="+":raise TypeError("A numeric offset cannot subtract a temporal value")
        left,right=right,left
    if type(right) not in (int,float,bool,type(None)):raise TypeError("Day offset must be numeric")
    amount=0 if right is None else right
    if not math.isfinite(amount):raise ValueError("Day offset must be finite")
    micros=round(amount*DAY_US)*(1 if op=="+" else -1)
    if left.kind=="time":
        total=(temporal_key(left)[1]+micros)%DAY_US
        return Temporal((datetime(2000,1,1)+timedelta(microseconds=total)).time().isoformat(),"time")
    parsed=left.parsed()
    if left.kind=="date" and micros%DAY_US==0:
        return Temporal((parsed+timedelta(days=micros//DAY_US)).isoformat(),"date")
    if left.kind=="date":parsed=datetime.combine(parsed,time())
    return Temporal((parsed+timedelta(microseconds=micros)).isoformat(),"datetime")


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


def from_duration(source):
    """ODF durations within one day are clocks; longer/negative periods fail."""
    match=re.fullmatch(r"P(?:(0)D)?T(?:(\d{1,5})H)?(?:(\d{1,5})M)?(?:(\d{1,5}(?:\.\d{1,6})?)S)?",source)
    if not match or not any(match[i] for i in (2,3,4)):raise ValueError("Only nonnegative ODS clock durations within one day are supported")
    from decimal import Decimal
    micros=int((Decimal(match[2] or 0)*3600+Decimal(match[3] or 0)*60+Decimal(match[4] or 0))*1_000_000)
    if not 0<=micros<DAY_US:raise ValueError("ODS duration exceeds a civil clock day")
    return Temporal((datetime(2000,1,1)+timedelta(microseconds=micros)).time().isoformat(),"time")


def to_duration(value):
    parsed=value.parsed()
    seconds=f"{parsed.second:02}"+("."+f"{parsed.microsecond:06}".rstrip("0") if parsed.microsecond else "")
    return f"PT{parsed.hour}H{parsed.minute}M{seconds}S"


def export_temporal_styles(snapshots):
    """Give unformatted temporal values/formulas a portable calendar display."""
    result=[]
    for sheet in snapshots:
        cells=[]
        for original in sheet["cells"]:
            cell=dict(original);value=cell.get("value")
            kind=value.kind if isinstance(value,Temporal) else cell.get("value_kind",cell.get("kind"))
            if kind in KINDS and cell.get("style",{}).get("format","general")=="general":
                cell["style"]={**cell.get("style",{}),"format":{"date":"date_iso","time":"time24","datetime":"datetime_iso"}[kind]}
            cells.append(cell)
        result.append({**sheet,"cells":cells})
    return result
