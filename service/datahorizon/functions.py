"""Public metadata for implemented formula functions; no workbook access."""
from copy import deepcopy

FUNCTIONS = {}


def register(name, signature, summary, rules, formula, result, category):
    FUNCTIONS[name] = dict(name=name,signature=signature,summary=summary,rules=rules,
                           examples=[dict(formula=formula,result=result)],category=category)


register('SUM','SUM(value, …)','Add numeric scalars and ranges.','Text, blanks and Booleans are ignored. Referenced errors propagate; nonfinite subtotals fail. Zero arguments returns zero.','=SUM(2,3)',5,'Aggregate')
for name,summary,example,result in (
    ('AVERAGE','Arithmetic mean of numeric inputs.','=AVERAGE(2,4)',3),
    ('MIN','Smallest numeric input.','=MIN(2,4)',2),('MAX','Largest numeric input.','=MAX(2,4)',4),
    ('COUNT','Count numeric inputs.','=COUNT(2,4)',2),('COUNTA','Count nonblank inputs.','=COUNTA(2,4)',2)):
    register(name,name+'(value, …)',summary,
             '1–255 arguments. Referenced Text/Booleans are ignored by numeric aggregates; direct arguments have explicit coercion rules. COUNTA includes empty Text and ordinary errors; COUNT ignores ordinary referenced errors. AVERAGE with no numeric inputs is #DIV/0!; MIN/MAX return zero.',example,result,'Aggregate')
register('IF','IF(condition, if_true[, if_false])','Choose one scalar result.','Numeric, Boolean or blank condition; evaluate only the selected branch. Omitted false result is FALSE.','=IF(TRUE,7,1/0)',7,'Logical')
for name,formula,result in (('AND','=AND(TRUE,1)',True),('OR','=OR(FALSE,1)',True)):
    register(name,name+'(value, …)','Combine logical inputs.','1–255 arguments. Referenced Text/blanks are ignored. Direct TRUE/FALSE Text is accepted. All arguments are evaluated; errors propagate. No eligible inputs is #VALUE!.',formula,result,'Logical')
register('NOT','NOT(value)','Invert a logical input.','One scalar argument. Numeric/Boolean/blank and TRUE/FALSE Text are accepted; other Text and ranges fail.','=NOT(FALSE)',True,'Logical')
register('IFERROR','IFERROR(value, fallback)','Return fallback for an ordinary spreadsheet error.','Two scalar arguments; fallback is lazy. Blank success becomes empty Text. Cycle and resource failures remain visible.','=IFERROR(1/0,9)',9,'Logical')
register('ABS','ABS(number)','Absolute numeric value.','One scalar numeric argument. Boolean/blank numeric coercions apply; Text and ranges fail.','=ABS(-2)',2,'Numeric')
for name,rule,result in (('ROUND','Half away from zero.',1.25),('ROUNDUP','Away from zero.',1.26),('ROUNDDOWN','Toward zero.',1.25)):
    register(name,name+'(number, digits)','Round a number in decimal notation.',rule+' Two scalar numeric arguments; integer digits from -15 to 15. Uses the shortest decimal spelling of binary64 input. No implicit Text conversion. Nonfinite results fail.','='+name+'(1.254,2)',result,'Numeric')


TEXT_RULES='One scalar argument. Blank/NULL becomes empty Text, Boolean becomes TRUE/FALSE, and numbers use invariant unformatted canonical text; existing Text is never parsed. Errors propagate. Text results are limited to 32,767 Unicode characters and 64 KiB UTF-8; limits stay visible through IFERROR.'
register('LEN','LEN(text)','Count Unicode code points, including spaces.',TEXT_RULES+' Combining marks and variation selectors count separately; no UTF-16 unit or grapheme counting.','=LEN("Data")',4,'Text')
register('TRIM','TRIM(text)','Remove outer ASCII spaces and collapse internal runs.',TEXT_RULES+' Only U+0020 spaces are changed; tabs, newlines and nonbreaking spaces remain.','=TRIM("  Data   Horizon  ")','Data Horizon','Text')
register('UPPER','UPPER(text)','Convert Text to Unicode uppercase.',TEXT_RULES+' Uses Python Unicode casing, independent of locale; casing can expand characters.','=UPPER("Data")','DATA','Text')
register('LOWER','LOWER(text)','Convert Text to Unicode lowercase.',TEXT_RULES+' Uses Python Unicode casing, independent of locale; casing can expand characters.','=LOWER("Data")','data','Text')


SUBSTRING_RULES=TEXT_RULES.removeprefix('One scalar argument. ')+' Positions/counts require numeric/Boolean/blank scalars, never numeric Text; finite fractional values truncate toward zero. Negative counts and MID start below 1 are #VALUE!. Counts beyond the input clamp through slicing; no padding. Unicode code points, not bytes or grapheme clusters.'
register('LEFT','LEFT(text[, count])','Return the first count characters (default 1).',SUBSTRING_RULES,'=LEFT("Data",2)','Da','Text')
register('RIGHT','RIGHT(text[, count])','Return the last count characters (default 1).',SUBSTRING_RULES,'=RIGHT("Data",2)','ta','Text')
register('MID','MID(text, start, count)','Return characters at a 1-based starting position.',SUBSTRING_RULES,'=MID("Data",2,2)','at','Text')


register('CONCAT','CONCAT(text, …)','Join scalar arguments and rectangular ranges in row-major order.','1–253 arguments. '+TEXT_RULES.removeprefix('One scalar argument. ')+' Blank/NULL contributes empty Text. All referenced errors propagate; no delimiter is inserted. Results are built incrementally and bounded before joining.','=CONCAT("Data"," ","Horizon")','Data Horizon','Text')


def function_catalog():
    return dict(version=1,functions=deepcopy([FUNCTIONS[name] for name in sorted(FUNCTIONS)]),
                notes=['This catalog describes the implemented development tier, not full Excel compatibility.',
                       'Formula source: at most 64 KiB UTF-8; parser: 1,024 tokens and nesting 128.',
                       'Calculation is bounded by 10,000 work steps, 8 MiB inputs and the calling operation deadline; referenced ranges are at most 10,000 cells.',
                       'Numbers are finite binary64. Text, empty Text, blank/NULL and Booleans have explicit function-specific rules.'])
