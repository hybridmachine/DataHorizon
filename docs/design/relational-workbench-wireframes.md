# Structured table and query interactions

Implementation contracts, November 9, 2026. These Qt Widgets wireframes define
the first relational tier before feature implementation. Read the
[implementation plan](relational-workbench-plan.md) for persistence, values,
transaction/hook semantics and limits. Controls become available as their shared
engine operations land.

## Workbook objects

```text
+--------------------+----------------------------------------------+
| Workbook objects   | Orders                         Stored         |
| Sheets             | Filter [CustomerID = C001] [Apply] [Clear]     |
|   Sheet1           | CustomerID [^]  Quantity  UnitPrice             |
| Tables             | C001                  2       12.5             |
|   Customers        | C001                  1       10               |
| > Orders           |                                              |
| Queries            | [Previous] [Next] [Refresh]                    |
|   Customer totals  | [Append] [Delete selected] [Paste]             |
+--------------------+----------------------------------------------+
| 2 records shown / 3 total                     Table data, editable  |
+-------------------------------------------------------------------+
```

Use a named QTreeWidget in a QDockWidget and an object-specific central widget.
The sheet address/formula fields belong to the sheet grid; they are hidden or
disabled for table/query objects. The table grid uses QTableView with a bounded
model, type delegates and record selection. Row identity is available in record
details and the schema browser, independently of displayed row numbers.
Tab reaches criteria, grid and actions in visual order; Enter edits the current
cell, Escape cancels the draft. Switching objects resolves drafts first. Editing
locks during an outstanding mutation; an unknown result retains its operation ID
and requires reconciliation. Stale pages show Refresh without retargeting edits.

Loading, no records, no filter matches, read-only, rejected, busy and unavailable
counts have separate text states. Pending counts never display loaded-row counts
as totals. Long errors are inspectable in Issues and retain rejected input.

## Table creation and column schema

```text
Create table from Sheet1!A1:D4
Name [Orders                 ]  [x] First row contains column names
Source header  SQL name     Type       Required  Unique
OrderID        [OrderID  ]  [Text    ]  [x]       [x]
CustomerID     [CustomerID] [Text    ]  [x]       [ ]
Quantity       [Quantity ]  [Integer ]  [x]       [ ]
UnitPrice      [UnitPrice]  [Number  ]  [x]       [ ]
Preview: 3 records; 0 conversion errors
Formula results become values. Source sheet and formulas are retained.
Native format upgrade: v1 -> v4; older readers cannot open the result.
[Save native copy...]                           [Cancel] [Create]
```

Use QDialog, QTableWidget/QComboBox delegates and QDialogButtonBox. Names and
types are explicit editable mappings. Invalid/duplicate names or conversion
errors withhold Create; an error pane identifies source coordinates and values.
Review is bound to the source object, rectangle, state and calculation clock.
Cancel sends no mutation. A stale review requires a new preview.

The column-schema dialog shares the same type/required/unique vocabulary. Add,
rename, change and delete actions show dependent relationships/queries, affected
record counts and history clearing. Conversion policy is explicit; dependent
rebuilds reject until their blocking dependencies are removed. A rejected schema
operation retains the previous table, source data, identities and history.

## Record editing and relationships

Append collects values by named column; missing nullable values become NULL.
Empty Text and NULL have distinct controls. Paste previews a rectangular matrix,
column IDs and reviewed record IDs. One invalid value rejects the entire paste.
Clear sets NULL and therefore rejects required fields. Delete confirms selected
record IDs and count, including targets outside a newly loaded page. Full-row
Undo/Redo remains a new managed operation with applicable hooks.

```text
Relationship
Child table [Orders   ]  Column [CustomerID]
Parent table[Customers]  Key    [CustomerID]
On parent delete: Restrict     On parent key update: Restrict
Unmatched non-NULL child records: 0
Supporting index: create if necessary
                                                [Cancel] [Apply]
```

Relationship review is a named QDialog. Only required unique same-type parent
keys appear. An orphan list is paged/read-only and provides exact row identities.
Apply requires a clean validation pass at the reviewed state. Direct-edit data
violations offer explicit restricted inspection/repair; normal edits remain
blocked. Schema damage offers recovery guidance instead of guessed repairs.

## Query workbench and saved views

```text
Query [Customer totals                 ] [Save] [Save as] [Delete]
+----------------------+--------------------------------------------+
| Tables and columns   | SELECT c.CustomerID, c.Name, ...            |
| Customers            | FROM Customers c LEFT JOIN Orders o ...    |
|   CustomerID Text UQ |                                            |
|   Name Text          |                                            |
| Orders               |                                            |
+----------------------+--------------------------------------------+
Parameters  minimum_total [Number] [0        ] [ ] Save as default
[Run] [Cancel]          Snapshot current / Running / Stale / Failed
+-------------------------------------------------------------------+
| CustomerID | Name  | OrderCount | Total                            |
| C001       | Ada   | 2          | 35                               |
| C002       | Grace | 1          | 21                               |
+-------------------------------------------------------------------+
[Previous] [Next] [Refresh] [Export complete result...]  2 result rows
```

Use QPlainTextEdit, typed parameter controls, a read-only QTableView and modeless
progress/status. Run creates a private snapshot job; Cancel waits for confirmed
terminal state. Changing SQL/parameters marks the previous execution distinct;
late results cannot replace a later run. Failed/incomplete jobs publish no
successful page or export. Native query definitions are saved without executing
them on reopen. Default parameter values require the explicit checkbox.

Export reviews the entire completed snapshot, row count, destination format,
type transformations and native-only omissions. Export is disabled for stale,
running or failed output. Closing cancels and reconciles admitted jobs before
release; a transport failure retains recovery identifiers rather than claiming
termination. Keyboard and bounded accessibility naming apply to all dialogs,
grids, parameters and result/error states. Physical/VoiceOver acceptance uses
the implementation plan's later owner walkthrough.
