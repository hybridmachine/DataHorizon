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
