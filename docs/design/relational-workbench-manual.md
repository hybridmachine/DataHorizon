# Relational workbench manual acceptance

This guide tests the bounded native v4 development preview. Automated results
and physical GUI/VoiceOver acceptance are separate. Use a copy of the supplied
workbook for each pass. Native files retain tables, keys, relationships, indexes
and saved definitions; spreadsheet exports retain values within their reported
compatibility tier. Windows/Linux, clean-machine distribution, signing and the
other M1/M7/release gates remain open.

## Start

The local handoff directory is `build/relational-handoff/`. It contains a pristine
`CustomersOrders.dhwb`, generated source sheets and a saved **Customer totals**
query. The final evidence identifies the tested source and relocated installed
app. Open a copy with the installed app. To activate the trusted fixture rule,
launch the application executable with:

```sh
DataHorizon.app/Contents/MacOS/DataHorizon --handler /absolute/path/to/tests/fixtures/relational_rules.py /absolute/path/to/CustomersOrders-copy.dhwb
```

For this local handoff, double-click **Launch with fixture rules.command** in
`build/relational-handoff/`. It opens `CustomersOrders-manual.dhwb` with the
relocated app at `Installed/DataHorizon.app` and the adjacent rule file. The
pristine fixture remains available for new copies. The archive and executable
hashes are recorded in [task 10 evidence](evidence/relational-task10.json).

Choose **Data → Tables and queries**. The browser has Tables, Sheets and
Queries / views. Tables and **Read-only SQL** use separate central grids. Opening
a saved definition never runs it automatically. **Refresh tables** / **Refresh
stored data** reads independently changed native data; **Run / Refresh** explicitly
runs SQL against a new private snapshot.

## Core walkthrough

1. Inspect Customers: required UNIQUE Text CustomerID and Text Name; C001/Ada,
   C002/Grace. Inspect Orders: O100/C001/2/12.5, O101/C001/1/10, O102/C002/3/7.
   Row identity differs from display position. The source sheets remain intact.
2. Open **Relationships**. CustomerOrders references Customers.CustomerID from
   Orders.CustomerID with RESTRICT updates/deletes. Its supporting index is owned
   by the relationship. Attempting to delete C001 or replace a child key with an
   unknown customer must leave the data unchanged and show the failure.
3. Sort/filter Orders by Quantity and another key. Use Next/Previous pages and
   change displayed columns. The supplied large benchmark fixture, if generated,
   demonstrates full-table paging; only a bounded page is retained. Clear criteria
   by removing the sort/filter rows and applying the review.
4. Select O100 Quantity. Use **Edit cell**, double-click or F2. With the fixture
   handler active, entering 0 must show the rule failure and retain the input.
   Cancel the rejected draft. Enter 4 and Save; the record and its generated
   Automation!A1 marker commit together. Undo from the table controls restores
   Quantity 2 and its prior marker. Retry after a rejection only after correcting
   the reviewed values. Unknown outcomes require operation-ID reconciliation.
5. Test Add record, explicit selected-record deletion, Clear cells and Paste
   values on disposable copies. NULL has a separate control from empty Text.
   Required means non-NULL. A single invalid field rejects the whole bulk edit.
   Native copy preserves scalar kinds/NULL; external TSV has fewer distinctions.
   Paste affects explicit loaded record/column IDs and does not append rows.
6. Review a column rename/type/constraint change on a copy. The preview reports
   conversions and removals. Retained identities/high-water marks persist;
   successful schema changes clear session Undo/Redo. Dependent queries,
   indexes and incoming relationships can block a schema change. Oversized
   rebuilds reject within the ordinary 10,000-field/five-second tier.
7. Select **Customer totals** in Queries / views. Confirm no results appear until
   Run. Review minimum_total = 0: Ada has two orders/35; Grace has one/21.
   At 30 only Ada remains. Invocation values do not silently replace defaults.
8. Set O100 Quantity to 4. Prior query output clears as stale. Run again at 0:
   Ada = 60, Grace = 21. Undo the edit and rerun: 35 and 21. SQL results are
   read-only and cannot become record-edit targets. SQL/parameter edits also
   invalidate the displayed execution. Command/Ctrl+Return runs from the SQL pane;
   Tab leaves the editor for the next control.
9. Save / Save as reviews the complete definition, prior definition, explicit
   defaults, dependencies and result labels. Choose the defaults policy
   deliberately. Close/reopen and verify stable native definitions without
   automatic execution. Rename through Save and test deletion on a copy.
10. Export the complete table/filter or completed query to CSV, XLSX and ODS.
    Review row/column counts, exact-integer Text fallback and actual native
    omissions. The baseline two query records total 56. Exports contain the
    complete frozen scope, independent of the visible page. The initial tier
    caps exports at 10,000 values including headers and a 4-MiB response; larger
    output must reject without truncation. Test cancel, overwrite and native-file
    alias rejection. Open outputs in independent applications.

## Independent SQLite edits and repair

Close extra services/windows using the same copy before changing its open mode.
The helper deliberately uses an independent SQLite connection, never runs skipped
handlers and leaves the Automation marker untouched:

```sh
PYTHONPATH=service python3 tests/relational_fixture.py direct-edit /absolute/path/to/CustomersOrders-copy.dhwb
```

Refresh stored data. O102 Quantity is now 4, so Grace's total is 28. The fixture
marker must remain unchanged. Undo history from before the external edit is no
longer available. Then introduce one orphan:

```sh
PYTHONPATH=service python3 tests/relational_fixture.py orphan /absolute/path/to/CustomersOrders-copy.dhwb
```

Normal refresh/open must report that repair is required. Choose **File → Open
for relationship repair**, select the same copy, then **Inspect relationship
violations**. Review the orphan record, set its child key to C002 or explicitly
review deletion. Only orphan child-key updates/deletions are admitted; unrelated
writes are disabled. Required rules still run, and all relationship orphans must
be resolved before commit. Reopen normally afterward. Unsupported schema or scalar
corruption is separate from this orphan-repair tier.

## Native interaction and accessibility

Record results at normal/narrow widths and both display scales. Test native file
pickers, keyboard focus/Tab, F2/Return editing, Command/Ctrl copy/paste and Undo,
modal cancellation, clipboard round trips, high contrast and VoiceOver reading of
table/result headers, cells, NULL controls, errors and status. Cancel a slow SQL
job and close while it is starting/running. Confirm cleanup or retain the shown
job/recovery identifier when cleanup cannot be confirmed. Retained SQL and record
drafts must not move silently to another target.

A useful slow read-only test is:

```sql
WITH RECURSIVE n(x) AS (
  VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000
)
SELECT sum(x) FROM n;
```

Queries have bounded execution, spooling and paging. Failed/cancelled/incomplete
jobs publish no successful partial result; service restart requires rerun.

## Reproduce the fixture and checks

```sh
PYTHONPATH=service python3 tests/relational_fixture.py create /new/path/CustomersOrders.dhwb
PYTHONPATH=service python3 tests/relational_fixture.py exercise /new/path/CustomersOrders.dhwb --handler tests/fixtures/relational_rules.py --exports /new/export/directory
PYTHONPATH=service python3 tests/relational_fixture.py verify /new/path/CustomersOrders.dhwb
```

Record the app/source hash, OS/scale, steps passed, failures, screenshots and any
workarounds. Pending physical/VoiceOver/clean-machine checks remain pending even
when the corresponding Qt automation passes.

The local automated pass used an Apple M1/8-GiB macOS host, Qt 6.12 and the
bundled CPython 3.13.18 runtime. A separate 343-MiB native fixture measured bounded
pages and query spools; these measurements do not raise the 10,000-value export
tier. Synthetic Cocoa checks used scale requests 1/2 (actual Retina pixel ratios
2/4). Screenshot capture at the larger ratio exceeded the ordinary 256-MiB GUI
target; the separate run without captures stayed below it. Physical input,
VoiceOver and clean-machine installation still need owner acceptance.
