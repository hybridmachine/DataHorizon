"""Public metadata for implemented formula functions; no workbook access."""
from copy import deepcopy

FUNCTIONS = {}


def register(name, signature, summary, rules, formula, result, category, inputs=None):
    FUNCTIONS[name] = dict(name=name,signature=signature,summary=summary,rules=rules,
                           examples=[dict(formula=formula,result=result,**({"inputs":inputs} if inputs else {}))],category=category)


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


CONDITIONAL_RULES='Cell/rectangular ranges up to 10,000 cells; shared work/input/deadline limits apply. Scalar criteria: finite numbers (including numeric criteria Text), typed Boolean, or case-insensitive Unicode-folded Text with =, <>, <, >, <=, >=. Numeric criteria may compare finite numeric Text; blank criteria become zero. Empty Text criteria match blank/NULL/empty Text. Equality/inequality supports * and ? with ~ escaping; criteria Text is capped at 255 characters. Ordinary errors in criteria-range cells do not match; cycle/resource failures propagate. This is an explicit stage, not every Excel coercion/locale.'
CONDITIONAL_EXAMPLE=[dict(row=row,col=2,source=label,kind='text') for row,label in enumerate(('Pen','Book','Pen'),1)]+[dict(row=row,col=3,source=str(value)) for row,value in enumerate((2,5,3),1)]
register('COUNTIF','COUNTIF(range, criteria)','Count matching cells in one bounded range.',CONDITIONAL_RULES,'=COUNTIF(B1:B3,"Pen")',2,'Conditional',CONDITIONAL_EXAMPLE)
register('SUMIF','SUMIF(range, criteria[, sum_range])','Sum numeric values aligned with matching criteria cells.',CONDITIONAL_RULES+' Sum range defaults to criteria range and must have the exact same shape. Only matching sum cells are evaluated; Text/Boolean/blank sums are ignored. Matching errors and nonfinite running sums propagate.','=SUMIF(B1:B3,"Pen",C1:C3)',5,'Conditional',CONDITIONAL_EXAMPLE)


LOOKUP_EXAMPLE=[dict(row=1,col=2,source='00123',kind='text'),dict(row=2,col=2,source='00456',kind='text'),dict(row=1,col=3,source='Pen',kind='text'),dict(row=2,col=3,source='Book',kind='text')]
register('XLOOKUP','XLOOKUP(value, lookup_range, return_range[, if_not_found[, match_mode[, search_mode]]])','Return the first exact matching value from aligned vectors.','3–6 arguments; same-shaped single-row/column ranges up to 10,000 cells. Only match_mode=0 and search_mode=1 are supported. Numbers/Booleans/Text/blank remain distinct; Text uses Unicode casefolding without numeric parsing, trimming or wildcards. Only the matched return cell and needed fallback are evaluated. Missing without fallback is #N/A. Blank returns/fallbacks become zero, empty Text remains Text. Lookup errors before a match propagate; later entries are not read. Shared work/input/deadline and Text-result limits apply.','=XLOOKUP("00123",B1:B2,C1:C2)','Pen','Lookup',LOOKUP_EXAMPLE)


for name,formula,result in (
    ('ISBLANK','=ISBLANK(B1)',True),('ISNUMBER','=ISNUMBER("00123")',False),
    ('ISTEXT','=ISTEXT("00123")',True),('ISLOGICAL','=ISLOGICAL(TRUE)',True),
    ('ISERROR','=ISERROR(1/0)',True),('ISNA','=ISNA(XLOOKUP(7,B1,C1))',True)):
    register(name,name+'(value)','Inspect a scalar type or ordinary error without coercion.','One scalar; ranges/arrays unavailable. Blank/NULL differs from empty Text; Boolean is not Number. Ordinary errors make type predicates false; ISERROR recognizes ordinary spreadsheet errors and ISNA only #N/A. Cycles/resource failures propagate.',formula,result,'Inspection')


register('IFNA','IFNA(value, fallback)','Return a lazy fallback only for #N/A.','Two scalar expressions. Other ordinary errors, cycles and resource failures propagate. Successful blank/NULL becomes empty Text, as for IFERROR; arrays unavailable.','=IFNA(XLOOKUP(7,B1,C1),"missing")','missing','Logical')


register('EXACT','EXACT(text1, text2)','Compare scalar Text with case-sensitive Unicode equality.',TEXT_RULES.removeprefix('One scalar argument. ')+' Two scalars; formatting is ignored. No trimming, case folding or Unicode normalization.','=EXACT("Data","data")',False,'Text')


register('SUBSTITUTE','SUBSTITUTE(text, old_text, new_text[, occurrence])','Replace literal non-overlapping Text occurrences.',TEXT_RULES.removeprefix('One scalar argument. ')+' Occurrence requires a numeric/Boolean scalar at least 1, with finite fractions truncated; numeric Text is rejected. Case-sensitive, no wildcards/normalization. Omitted occurrence replaces all; supplied occurrence is 1-based. Empty old_text leaves input unchanged. Result size is checked before replacement allocation.','=SUBSTITUTE("a-b-a","a","x",2)','a-b-x','Text')


register('REPLACE','REPLACE(text, start, count, new_text)','Replace characters at a 1-based Unicode position.',TEXT_RULES.removeprefix('One scalar argument. ')+' Four scalars. Numeric/Boolean/blank positions/counts, finite fractions truncate; numeric Text rejected. Start at least 1, count at least 0. Beyond-end start appends; count clamps without padding. Zero count inserts. Result built with bounded concatenation.','=REPLACE("00123",3,2,"AB")','00AB3','Text')


register('FIND','FIND(find_text, within_text[, start])','Find a case-sensitive literal Unicode Text position.',TEXT_RULES.removeprefix('One scalar argument. ')+' Two or three scalars. Returns 1-based code-point position, start defaults to 1. Numeric/Boolean start, finite fractions truncate; numeric Text rejected. Start must be 1 through length+1; empty needle returns start. Missing match is #VALUE!. Wildcards are literal; no normalization.','=FIND("a","Data",3)',4,'Text')


def function_catalog():
    return dict(version=1,functions=deepcopy([FUNCTIONS[name] for name in sorted(FUNCTIONS)]),
                notes=['This catalog describes the implemented development tier, not full Excel compatibility.',
                       'Formula source: at most 64 KiB UTF-8; parser: 1,024 tokens and nesting 128.',
                       'Calculation is bounded by 10,000 work steps, 8 MiB inputs and the calling operation deadline; referenced ranges are at most 10,000 cells.',
                       'Numbers are finite binary64. Text, empty Text, blank/NULL and Booleans have explicit function-specific rules.'])
