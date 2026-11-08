# Date and time increments

Authorized November 7, 2026. Deliver ten sequential PRs, each locally built,
tested, reviewed at its published head and merged. Hosted CI is not qualification
evidence for this batch. Existing M0–M8 and platform/release gates remain open.

1. Types, timezone rules and Excel serial conversions.
2. Native storage, validation and format-version handling.
3. Explicit GUI, REST, CLI and SDK entry.
4. Format Cells date/time presentation.
5. DATE, YEAR, MONTH, DAY.
6. TIME, HOUR, MINUTE, SECOND.
7. TODAY/NOW and one calculation clock.
8. Arithmetic, sorting and filters.
9. Typed CSV interchange.
10. XLSX/ODS interchange and complete local qualification.

## Value contract

`date` is a Gregorian calendar day (0001–9999), `time` a civil wall clock within
one day, and `datetime` a civil day and clock. ISO sources are strictly
`YYYY-MM-DD`, `HH:MM:SS[.ffffff]`, and `YYYY-MM-DDTHH:MM:SS[.ffffff]`.
Seconds are required. Fractions have at most six digits; canonical values trim
trailing fractional zeros. No leap seconds, timezone offsets, DST inference,
two-digit years or automatic date guessing. Text that looks like a date stays
Text until explicitly typed. Stored sources remain separate from canonical values.

These are civil values, not timezone-bearing instants. No host locale/timezone
converts stored data. TODAY/NOW sample UTC once for each shared Calculator;
NOW represents that UTC civil datetime, TODAY its date. Read/export/list snapshots
share that sample across all cells. A later calculation can receive a new sample.
No timer writes cells, increments revisions, or invokes change hooks.

Native types are distinct from Text and numeric Excel serials. JSON values use
canonical ISO strings with a `value_kind` discriminator. Values-only operations
must retain the discriminator. Formatting never changes stored input types.

Explicit serial conversion supports Excel 1900 and 1904 epochs. 1900 serial 1
is 1900-01-01; 59 is 1900-02-28; 61 is 1900-03-01. Serial 60 (including its
fraction) is rejected, because 1900-02-29 is not a Gregorian date. 1904 serial 0
is 1904-01-01. Time fractions are fractions of 86,400 seconds; serial import
rounds to microseconds. Binary64 serials can lose microsecond precision for
large dates; ISO storage does not. Date extraction discards the clock. Serial
date conversion supports negative values within years 0001–9999, a deliberate
extension beyond Excel's display range. Time extraction uses the fractional day.

Sources: [Excel date systems](https://support.microsoft.com/en-us/excel/date-systems-in-excel),
[1900 leap-year compatibility bug](https://learn.microsoft.com/en-us/office/troubleshoot/excel/wrongly-assumes-1900-is-leap-year).

## Presentation

Format Cells offers invariant ISO, MM/DD/YYYY, DD/MM/YYYY, 24-hour time and ISO
date plus time. Time formats display whole seconds; General retains fractional
precision. Applying these styles never changes source/kind. Numeric cells are
displayed using the 1900 serial system; invalid calendar serials show `####`.
Text is never parsed by formatting. Time-only values retain their canonical clock
when given a date format; dates display midnight when given a time format.

## Calendar functions

DATE returns a native date, with numeric components truncated toward zero,
Excel's 0–1899 year offset and normalized month/day overflow. Normalization uses
the real Gregorian calendar: DATE(1900,2,29) becomes 1900-03-01, an explicit
departure from Excel's fictitious leap day. YEAR/MONTH/DAY accept dates,
datetimes or explicit numeric 1900 serials; they reject time-only values and
Text. Ranges fail; ordinary formula errors propagate. See catalog rules for
arity, blank/Boolean coercion and bounds. [Excel DATE reference](https://support.microsoft.com/en-us/excel/functions/date-function).

TIME truncates each numeric/Boolean/blank component, accepts 0–32767 and wraps
the total within one day. Negative components are outside this explicit tier.
HOUR/MINUTE/SECOND accept time/datetime, date as midnight, or a nonnegative
numeric serial's fractional day. SECOND discards fractional seconds. Text and
ranges fail. These functions return native time or numeric components, not
localized Text. [Excel TIME reference](https://support.microsoft.com/en-gb/excel/functions/time-function).

TODAY/NOW use an injectable Calculator clock sampled from UTC once, including
whole-workbook cross-sheet reads. Each hook dispatch batch and its generated
change rounds also share one clock, even when context.value creates a fresh
calculator to observe prior transactional edits. Read-only calculation never
changes revision/history or runs change hooks. Values can change between two
snapshots with the same stored-input state token; it is not a clock token.

## Arithmetic and ordering

Native date/datetime plus/minus a numeric day offset uses civil Gregorian days,
rounded to microseconds. Whole-day offsets preserve date; fractions promote to
datetime. Adding a time to a date/datetime combines its clock as an offset;
time plus time/numeric days wraps within 24 hours. Subtraction of two calendar
values or two times returns elapsed days. Date/datetime comparisons promote dates
to midnight; clocks compare only with clocks. Other temporal operators/type
mixtures fail explicitly. Text conversion/concatenation uses ISO; numeric
aggregates do not implicitly include native temporal values. Exact lookups and
conditional equality distinguish temporal values from Text and numeric serials.

List sorting groups blank, numbers, temporal values (calendar then clocks), Text,
then Booleans, with chronological ordering and stable source rows. Filters use
an explicit `value_kind` (`date`, `time`, `datetime`) and ISO `value`; omitted
kind preserves existing scalar behavior. Date and datetime filters compare at
midnight. Contains is Text-only. GUI filter types and local saved definitions
retain this discriminator. Existing scope, response and calculation limits apply.

## CSV

CSV fields use canonical ISO values with full native microsecond precision,
independent of display format. Explicit ISO/slash-date column conversions now
produce native dates; ISO time/datetime column types are also available. Default
CSV import remains Text. Earlier stored ISO Text is not migrated automatically.

Exports with temporal values include `temporal_schema`: version 1, SHA-256 of
decoded UTF-8 CSV text, and bounded 1-based row/column/kind entries. Passing it
to CSV preview restores only temporal kinds, including formula results and mixed
date/Text columns. The digest, kinds, bounds and unique coordinates are validated;
conflicting column/header overrides reject. Other types/styles remain outside
CSV's fidelity contract. Selection and projected-list exports both carry metadata.

The desktop writes `<path>.dh-types.json` beside temporal CSV exports, offers
restoration when importing the companion, and shows it in conversion review.
Existing companion files block export; choose a new destination. Each file uses
QSaveFile; a two-file export cannot be atomic as a pair. A failed CSV commit can
leave its companion for inspection, and a hash mismatch blocks restoration.
Keep both files together; CSV alone remains portable but untyped. REST/SDK users
retain and supply `temporal_schema` in their export/preview JSON.
