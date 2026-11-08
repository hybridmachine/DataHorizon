"""Explicit deterministic import/entry profiles; never use the host locale."""
from datetime import date
import re
from .engine import require,typed,EngineError
from .temporal import import_temporal

PROFILES={'invariant':('.',None,None),'en_US':('.',',','mdy'),'de_DE':(',','.','dmy'),'fr_FR':(',','\u202f','dmy')}


def number(source,profile):
    decimal,group,_=PROFILES[profile]
    if profile=='invariant':return source
    source=source.strip(' \t');match=re.fullmatch(r'([+-]?)([^eE]+)([eE][+-]?[0-9]{1,4})?',source)
    require(match is not None,'INVALID_CONVERSION','Expected a plain localized decimal number')
    sign,mantissa,exponent=match.groups()
    if profile=='fr_FR':
        used={char for char in ('\u202f','\u00a0',' ') if char in mantissa}
        require(len(used)<=1,'INVALID_CONVERSION','Do not mix French grouping separators')
        if used:mantissa=mantissa.replace(next(iter(used)),group)
    pieces=mantissa.split(decimal);require(len(pieces)<=2,'INVALID_CONVERSION','Repeated decimal separator')
    whole=pieces[0];fraction=pieces[1] if len(pieces)==2 else None
    if group in whole:
        groups=whole.split(group);require(re.fullmatch(r'[0-9]{1,3}',groups[0]) and all(re.fullmatch(r'[0-9]{3}',part) for part in groups[1:]),'INVALID_CONVERSION','Grouping must use groups of three digits');whole=''.join(groups)
    require(re.fullmatch(r'[0-9]*',whole) is not None and (whole or fraction),'INVALID_CONVERSION','Invalid localized integer part')
    require(fraction is None or re.fullmatch(r'[0-9]+',fraction),'INVALID_CONVERSION','Invalid localized fractional part')
    return sign+(whole or '0')+('.'+fraction if fraction is not None else '')+(exponent or '')


def convert(request):
    source=request.get('source');kind=request.get('kind');profile=request.get('locale','invariant')
    require(isinstance(source,str) and len(source.encode())<=65536,'INVALID_CONVERSION','Provide at most 64 KiB of source Text')
    require(isinstance(profile,str) and profile in PROFILES,'INVALID_CONVERSION','Choose an explicit supported locale profile')
    require(kind in ('number','date','time','datetime','timestamp_utc'),'INVALID_CONVERSION','Choose an explicit conversion kind')
    if kind=='timestamp_utc':require(re.search(r'(?:Z|[+-][0-9]{2}:[0-9]{2})$',source) is not None,'INVALID_CONVERSION','UTC timestamp conversion requires an explicit offset')
    ambiguous=False;original=source
    try:
        if kind=='number':source=number(source,profile)
        elif kind in ('date','datetime') and not re.match(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',source):
            order=PROFILES[profile][2];require(order is not None,'INVALID_CONVERSION','Choose an explicit date-order profile')
            parts=source.split(' ',1);match=re.fullmatch(r'([0-9]{1,2})[/.]([0-9]{1,2})[/.]([0-9]{4})',parts[0])
            require(match is not None,'INVALID_CONVERSION','Expected a four-digit-year date in the selected order')
            first,second,year=map(int,match.groups());month,day=(first,second) if order=='mdy' else (second,first)
            ambiguous=first<=12 and second<=12 and first!=second;source=date(year,month,day).isoformat()+('T'+parts[1] if len(parts)==2 else '')
        if kind!='number':
            policy='utc' if kind=='timestamp_utc' else request.get('timezone_policy','reject');native_kind='datetime' if kind=='timestamp_utc' else kind
            source=str(import_temporal(source,native_kind,policy));kind=native_kind
        item=typed(dict(source=source,kind=kind,style={}))
    except ValueError as error:raise EngineError('INVALID_CONVERSION',str(error)) from error
    return dict(cell=dict(source=source,kind=kind),value=item['value'],ambiguous_date=ambiguous,
                report=[f'Explicit profile: {profile}; kind: {kind}.',f'Canonical input: {source}.',
                        'Ambiguous day/month order follows the selected profile.' if ambiguous else 'No ambiguous slash-date order.',
                        'Offset-bearing timestamps normalize to UTC civil datetime only when explicitly selected; original offset is not stored.',
                        'Numbers retain native binary64/large-integer precision rules. Existing Text is not changed automatically.'])
