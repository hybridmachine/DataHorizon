# Charts: table and saved-query sources implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A chart series, category list or x list can come from a column of a structured table or a column of a saved query's result, with the same resolve, render, undo and interface behaviour charts already have over cell ranges.

**Architecture:** Two source kinds join `range` in specification version 1. A `table` source names a table and a column by identity and is read whole, in stored row order, from the chart job's read-only snapshot. A `query` source names a saved query by identity and a result column by name; resolving it runs the saved query once per job on a private snapshot through a new `QueryJobs.run_once`, which reuses the existing authorised, bounded, cancellable execution. Dropping a table, removing a column or deleting a saved query marks the chart sources that used it broken, as sheet deletion does today.

**Tech Stack:** Python standard library only; SQLite; the existing `PreparationJobs`, `QueryJobs` and chart modules; `unittest`.

**Spec:** [charts-design.md](charts-design.md) Draft 0.4, sections 4, 5 and 8. As built so far: [charts-stage-2-result.md](charts-stage-2-result.md).

**Owner decisions for this block (November 13, 2026):**
- Table and saved-query sources are the next block, before the desktop.
- A table source is one column of a table in stored row order, refused when the table has more rows than the point limit. It carries no filter or order. Filtering and ordering are done with a saved query. The design's "read through a saved view" is dropped: the code has no saved view over a table, and what the project calls saved views are saved queries.

## Global Constraints

- The service imports nothing outside the Python standard library, including in tests that run in the `service` suite.
- Every new source file starts with `# SPDX-License-Identifier: GPL-3.0-only`.
- Specification version stays 1. Native format stays v8. No stored chart written before this block changes meaning.
- Existing limits are unchanged and apply to every source kind: 10,000 points per series (`LIMIT_POINTS`), 1,000 for a pie (`LIMIT_PIE_POINTS`), 64 series, 100,000 source cells per chart (`LIMIT_CELLS`), 10,000 values per render (`LIMIT_RENDER_POINTS`). A table or query that yields more rows than a limit is refused at resolve with that limit's code. Nothing is truncated or sampled.
- Resolve and render never write to the workbook and never return partial series. A query source never reuses an earlier result.
- A chart job stays inside its 30-second budget and stays cancellable while it reads a table or runs a query.
- Chart operations raise no Python events. Marking a chart source broken is not a cell change.
- Sources are held by identity (`table_id`, `column_id`, `query_id`), so renaming a table, a column or a saved query needs no chart maintenance. A query result column is held by its name, because result columns have no identity.
- Errors use `EngineError(code, message, status)`. Existing codes are reused: `SOURCE_BROKEN` (409), `TABLE_NOT_FOUND` and `QUERY_NOT_FOUND` (404) on input, `COLUMN_NOT_FOUND` (400) on input, and the query engine's own codes (`QUERY_LIMIT`, `QUERY_FAILED`, `QUERY_VALUE`, `QUERY_DEPENDENCY_CHANGED`, `SQL_DENIED`, `INVALID_PARAMETERS`) pass through unchanged from a query run.
- Focused tests: `cd tests && PYTHONPATH=../service python3 -m unittest <module> -v`. Whole suite: `ctest --test-dir build -R service`.

## Review Focus

Each line has a test in the task named.

1. **The table, the column or the saved query a chart reads is dropped.** The source is marked broken, the receipt lists the chart, resolve refuses with `SOURCE_BROKEN`, and the source is never repointed at something else of the same name. (Task 2)
2. **A saved query is saved again with a different result shape, so the chart's column is gone.** Resolve refuses with `SOURCE_BROKEN` and names the column; it does not plot another column or an old result. (Task 5)
3. **A table grows past the point limit after the chart was created.** Resolve refuses with `LIMIT_POINTS` (or `LIMIT_PIE_POINTS`) and says how many rows the table holds; it never plots the first 10,000. (Task 3)
4. **The workbook changes between reading two columns of the same table, or while a query's snapshot is being taken.** The job ends stale with no series; two series are never drawn from different states. (Tasks 3 and 5)
5. **A saved query is slow, is cancelled, or returns text, NULL, a very large integer or a BLOB.** Cancellation and the job deadline stop the query; text and NULL are counted gaps; a large integer plots; a BLOB fails the job with the query engine's `QUERY_VALUE`. (Tasks 4 and 5)

## File Structure

| File | Change |
| --- | --- |
| `service/datahorizon/charts.py` | Input and stored forms for `table` and `query` sources; description; input-form rebuild; `break_table`, `break_query`. |
| `service/datahorizon/chart_sources.py` (new) | Reading a table column and a query column into the same cell shape `chart_resolve` already classifies. One responsibility: turn a non-range source into values. |
| `service/datahorizon/chart_resolve.py` | Dispatch by source kind; dynamic limits; `suggest` from a table. |
| `service/datahorizon/query_jobs.py` | `QueryJobs.run_once`; the row loop of `_execute` extracted so both callers share it. |
| `service/datahorizon/chart_jobs.py` | Passes a query runner into `resolve`. |
| `service/datahorizon/relational.py`, `query_views.py` | Call the chart break functions from `table_drop`, `table_schema` and `query_delete`. |
| `service/datahorizon/charts_api.py`, `client.py`, `cli.py` | `ChartSource` schema for three kinds; `suggest` from a table. |
| `tests/test_chart_table_sources.py`, `test_chart_query_sources.py` (new) | One file per source kind. Existing chart test files gain cases where they own the behaviour. |

---

### Task 1: Source kinds in the specification

**Files:**
- Modify: `service/datahorizon/charts.py` (`_input_source` ~80, `_stored_source` ~106, `_check` ~148, `limit_error` ~298, `_describe_source` ~326, `_readable_sources` ~427, `_input_form` ~559)
- Create: `tests/test_chart_table_sources.py`, `tests/test_chart_query_sources.py`

**Interfaces:**
- Input forms accepted by `normalise_spec`:
  - `{"kind": "table", "table": <name or id>, "column": <column name or id>}`
  - `{"kind": "query", "query": <name or id>, "column": <result column name>}`
- Stored forms:
  - `{"kind": "table", "table_id": str | None, "column_id": str | None}`
  - `{"kind": "query", "query_id": str | None, "column": str}`
  A broken table source has both ids `None`. A broken query source has `query_id` `None` and keeps `column`.
- `describe` adds to a table source `table` and `column` (current names, `None` when broken) and `broken`; to a query source `query` (current name or `None`) and `broken`, which is true when the query is gone or its saved result shape has no single column of that name.
- Produces: `charts.source_kind(source) -> "range" | "table" | "query"`; `charts.is_broken(source) -> bool` (stored form only, no database).

Rules:
- A series `name` source must be a `range`; a `table` or `query` name source is `INVALID_SPEC`. Use `{"text": ...}` for a fixed name.
- `values`, `categories` and `x` accept all three kinds, and one chart may mix them.
- On input the table is found with `relational.table(db, value)` and the column by id, else by name ignoring case; an unknown column is `COLUMN_NOT_FOUND`. The saved query is found with `query_views.query(db, value)`; `column` must match exactly one name in the saved `shape`, else `COLUMN_NOT_FOUND`.
- A table or query source contributes nothing to the input-time size checks, because its size is known only when it is read. `limit_error` and `over_limit` keep counting range sources only.
- `_readable_sources` (which feeds the sheet and row maintenance) returns range sources only.

- [ ] **Step 1: Write the failing tests.** Fixtures: a workbook with table `Sales` (columns `Month` text, `Units` integer, `Cost` number, `Paid` boolean, `Day` date; twelve rows) and saved query `Top` (`SELECT Month, Units FROM Sales ORDER BY Units DESC`), built as `tests/test_query_views.py:18-24` builds its table and saved query.

```python
# test_chart_table_sources.py, class Spec
def test_normalise_stores_table_and_column_identities(self):
    out = charts.normalise_spec(self.book, spec(values=table("Sales", "Units"), categories=table("sales", "month")))
    self.assertEqual(out["series"][0]["values"]["source"],
                     {"kind": "table", "table_id": self.table_id, "column_id": self.column_id["Units"]})
def test_table_and_column_may_be_given_by_id(self): ...
def test_refusals(self):       # unknown table → TABLE_NOT_FOUND 404; unknown column → COLUMN_NOT_FOUND;
                               # extra key → INVALID_SPEC; name source of kind table → INVALID_SPEC
def test_stored_form_round_trips_and_a_broken_source_is_valid(self): ...     # validate_stored is None for both
def test_describe_adds_current_names(self): ...                              # after table_schema renames the column
def test_spec_patch_keeps_a_table_source(self): ...                          # chart_update spec_patch title → source unchanged
def test_range_charts_are_untouched(self): ...                               # a stage 2 range payload validates and describes as before

# test_chart_query_sources.py, class Spec
def test_normalise_stores_query_identity_and_column_name(self): ...
def test_refusals(self):       # unknown query → QUERY_NOT_FOUND 404; column not in shape → COLUMN_NOT_FOUND;
                               # a shape with two columns of that name → COLUMN_NOT_FOUND
def test_describe_marks_a_missing_column_broken(self): ...
def test_mixed_kinds_in_one_chart(self): ...                                 # range categories, table values, query x for scatter
```

- [ ] **Step 2: Run.** Expected: FAIL with `INVALID_SPEC` ("a source kind must be 'range'").
- [ ] **Step 3: Implement.** Branch `_input_source` and `_stored_source` on `kind`; keep the range code path byte-for-byte. `_input_form` rebuilds each kind's input form from `describe` output (`table`/`column` ids for a table source so a rename cannot change the target; `query` id and `column`).
- [ ] **Step 4: Run** `test_chart_table_sources test_chart_query_sources test_charts test_chart_structure test_format_v8`. Expected: PASS.
- [ ] **Step 5: Commit** — `Accept table and saved-query sources in chart specifications`.

---

### Task 2: Following tables and saved queries

**Files:**
- Modify: `service/datahorizon/charts.py`, `service/datahorizon/relational.py` (`table_schema` branch ~236, `table_drop` branch ~259), `service/datahorizon/query_views.py` (`mutate`, delete branch ~197)
- Extend: `tests/test_chart_table_sources.py`, `tests/test_chart_query_sources.py`

**Interfaces:**
- Produces: `charts.break_table(book, table_id, kept_column_ids=None) -> list[str]`: with `kept_column_ids` `None` every source on the table is broken; otherwise only sources whose `column_id` is not kept. `charts.break_query(book, query_id) -> list[str]`. Both return the sorted ids of charts with a newly broken source, write through `charts.store`, write only charts that changed, skip unreadable charts, and return at once below format 8.
- Receipts of `table_drop`, `table_schema` and `query_delete` gain `"broken_charts": [...]` when not empty.

Rules: `table_drop` breaks every source on the table. `table_schema` breaks sources whose column no longer exists in the new schema; renaming or retyping a column breaks nothing. `query_delete` breaks every source on the query. `query_rename` and `query_save` touch no chart. These actions are structural and already clear undo history, so the chart changes ride with them.

- [ ] **Step 1: Write the failing tests**

```python
def test_dropping_the_table_breaks_its_sources(self):                 # Review Focus 1
    receipt = self.apply("table_drop", table="Sales")
    self.assertEqual(receipt["broken_charts"], [self.chart])
    source = self.read_chart()["spec"]["series"][0]["values"]["source"]
    self.assertEqual((source["table_id"], source["column_id"], source["broken"]), (None, None, True))
def test_a_new_table_of_the_same_name_does_not_repair_the_chart(self): ...   # Review Focus 1
def test_removing_the_column_breaks_only_that_source(self): ...
def test_renaming_or_retyping_a_column_breaks_nothing(self): ...
def test_renaming_the_table_breaks_nothing(self): ...
def test_broken_source_survives_reopen_and_blocks_spec_patch(self): ...      # SOURCE_BROKEN, as for ranges
def test_deleting_the_saved_query_breaks_its_sources(self): ...              # Review Focus 1
def test_renaming_the_saved_query_breaks_nothing(self): ...
def test_charts_without_these_sources_are_not_rewritten(self): ...           # changed_charts counts only changed charts
```

`table_drop` is refused today while a saved query, relationship or index depends on the table; the test drops a table with none, and the query test deletes the query first.

- [ ] **Step 2: Run.** Expected: FAIL (no `broken_charts`; sources unchanged).
- [ ] **Step 3: Implement** the two functions and the three call sites, after each action's own checks have passed and before it returns its report.
- [ ] **Step 4: Run** the two chart source modules plus `test_relational test_table_schema test_query_views`. Expected: PASS.
- [ ] **Step 5: Commit** — `Break chart sources when their table, column or saved query is removed`.

---

### Task 3: Resolving table sources

**Files:**
- Create: `service/datahorizon/chart_sources.py`
- Modify: `service/datahorizon/chart_resolve.py` (`_check_sources` ~25, `resolve` ~108)
- Extend: `tests/test_chart_table_sources.py`

**Interfaces:**
- Produces: `chart_sources.read_table(book, source, state, limit) -> list[dict]`: one cell dict per row in `_dh_row_id` order, in the shape `chart_resolve._classify` and `_text` already take: `{"value", "display", "calculation": "current"}` plus `"value_kind"` for date, time and datetime columns. It reads at most `limit + 1` rows; more than `limit` raises the caller's limit error (see below).
- Changes: `chart_resolve` reads a source through one function, `_cells(snapshot, source, state, role, chart_type, progress) -> list[cell | None]`, which returns the cells of any kind of source in order. Range sources keep their slice reads; the list is built from positions as today.

Rules:
- `read_table` takes `book.lock`, refreshes, opens a read transaction, requires `book.state() == state` (else `STALE_STATE`, 409), and runs one statement, `SELECT "<column>" FROM "<table>" ORDER BY "_dh_row_id" LIMIT ?`, under `sql_deadline(book.db, deadline, cancel_event=...)` with a two-second deadline, then rolls back. Identifiers go through `relational.quote`. This is the pattern of `table_browse.read` (`table_browse.py:65-70`).
- Values: integer and number columns give numbers; boolean gives `True`/`False` (`relational.from_sql`), which the existing classifier counts as a `text` gap; text gives text; date, time and datetime give ISO text with `value_kind` set; SQL `NULL` is an empty cell (`None` in the list).
- `display`, used for categories: text as stored; numbers through `chart_render.format_tick`; `TRUE` or `FALSE`; ISO text for temporal values; empty string for `NULL`.
- The row limit is `LIMIT_PIE_POINTS` for a pie's values and categories and `LIMIT_POINTS` otherwise. Exceeding it raises that code with a message that names the table and says the limit, for example "Table Sales holds more than 10,000 rows; the limit for a series is 10,000".
- `LIMIT_CELLS` becomes a running total over every source of every kind as it is read; passing 100,000 raises `LIMIT_CELLS`.
- A table source whose ids are `None`, or whose table or column no longer exists in the snapshot, raises `SOURCE_BROKEN` before anything is read.
- A `TIMEOUT` from the read becomes `CALCULATION_FAILED`, as for ranges.

- [ ] **Step 1: Write the failing tests**

```python
def test_resolves_table_columns_into_series_and_categories(self): ...
    # categories Month, values Units and Cost → 12 points each, no gaps; render accepts the result
def test_null_text_boolean_and_date_values_are_classified(self): ...
    # counts == {"points": n, "gaps": {"empty": 1, "text": 3, "error": 0}}; positions keep their index
def test_category_display_per_column_type(self): ...                   # "", "TRUE", "2026-01-31", "12.5"
def test_rows_come_in_stored_order_after_deletes_and_inserts(self): ...
def test_table_over_the_point_limit_is_refused_by_name(self):          # Review Focus 3
    # 10,001 rows → LIMIT_POINTS, message names Sales; 1,001 rows on a pie → LIMIT_PIE_POINTS; nothing returned
def test_running_total_enforces_limit_cells(self): ...                 # eleven series over a 10,000-row table → LIMIT_CELLS
def test_change_between_two_column_reads_is_stale(self):               # Review Focus 4
    # wrap chart_sources.read_table: after the first call, insert a row through a second Workbook; expect STALE_STATE
def test_broken_or_vanished_source_is_refused_before_reading(self): ...
def test_scatter_from_two_table_columns(self): ...
def test_mixed_range_categories_and_table_values(self): ...
def test_through_a_real_chart_job(self): ...                           # ChartJobs resolve and render over table sources
```

- [ ] **Step 2: Run.** Expected: FAIL (`KeyError: 'sheet_id'` in `_check_sources`).
- [ ] **Step 3: Implement** `chart_sources.read_table` and the dispatch in `chart_resolve`.
- [ ] **Step 4: Run** `test_chart_table_sources test_chart_resolve test_chart_jobs`. Expected: PASS.
- [ ] **Step 5: Commit** — `Resolve chart sources from structured table columns`.

---

### Task 4: Running a saved query once, outside the job store

**Files:**
- Modify: `service/datahorizon/query_jobs.py` (`start` ~38, `_execute` ~90)
- Extend: `tests/test_query_jobs.py`

**Interfaces:**
- Produces: `QueryJobs.run_once(self, query_id, *, state, cancel_event, seconds, max_rows) -> {"columns": [str, ...], "rows": [[value, ...], ...]}`.
  - It finds the saved query, repeats the checks `start` makes for a saved run (`prepare`, shape and dependencies unchanged else `QUERY_DEPENDENCY_CHANGED`, saved `defaults` bound with `bindings`), takes a private snapshot with `_snapshot` (which requires the workbook to be at `state`, else `STALE_STATE`), and executes with the same `configure`, `authorize`, deadline, cancellation and value rules as `_execute`.
  - Values are what `query_sql.scalar` returns. Rows are held in memory, so `max_rows` is required and small: more than `max_rows` rows raises `QUERY_LIMIT` (409) with the count limit in the message.
  - It uses no entry in `self.entries`: it does not count against the four retained jobs or the one running query, is not visible through the query job routes, and removes its temporary directory before returning or raising.
  - It refuses while the manager is stopping (`SERVICE_STOPPING`).
- Changes: the saved-run preparation in `start` and the execute-and-iterate core of `_execute` move into two private helpers that both `start`/`_execute` and `run_once` call. `_execute` keeps writing its spool and returns exactly what it returns today.

- [ ] **Step 1: Write the failing tests** in `tests/test_query_jobs.py`, using that file's fixtures.

```python
def test_run_once_returns_columns_and_rows_of_a_saved_query(self): ...
def test_run_once_binds_saved_defaults(self): ...                      # a saved query with a parameter default
def test_run_once_without_a_default_is_invalid_parameters(self): ...
def test_run_once_over_max_rows_is_query_limit(self): ...
def test_run_once_requires_the_given_state(self): ...                  # edit first → STALE_STATE
def test_run_once_detects_a_changed_dependency(self): ...              # QUERY_DEPENDENCY_CHANGED
def test_run_once_is_cancellable_and_bounded(self):                    # Review Focus 5
    # a recursive CTE that never ends: cancel_event set from a timer → CANCELLED; seconds=0.2 → TIMEOUT
def test_run_once_refuses_blob_results(self): ...                      # QUERY_VALUE; Review Focus 5
def test_run_once_uses_no_job_slot_and_leaves_no_files(self): ...      # entries unchanged; four retained jobs do not block it;
                                                                       # the staging directory is gone afterwards
def test_run_once_denies_what_a_query_job_denies(self): ...            # a saved query cannot be created with forbidden SQL;
                                                                       # assert the authoriser is installed by patching `authorize`
def test_existing_query_job_results_are_unchanged(self): ...           # same columns, row_count and first page as before the refactor
```

- [ ] **Step 2: Run.** Expected: FAIL, `QueryJobs` has no `run_once`.
- [ ] **Step 3: Implement.** Keep the refactor mechanical: the loop body that checks the deadline, counts rows and converts values becomes a generator both callers consume.
- [ ] **Step 4: Run** `test_query_jobs test_query_views test_relational_clients`. Expected: PASS, with the existing tests unchanged.
- [ ] **Step 5: Run the whole `service` suite** (this task edits the query engine). Expected: PASS.
- [ ] **Step 6: Commit** — `Run a saved query once on a private snapshot without a job slot`.

---

### Task 5: Resolving saved-query sources

**Files:**
- Modify: `service/datahorizon/chart_sources.py`, `service/datahorizon/chart_resolve.py`, `service/datahorizon/chart_jobs.py` (`compute`/`run` ~56-99)
- Extend: `tests/test_chart_query_sources.py`

**Interfaces:**
- Changes: `chart_resolve.resolve(snapshot, payload, state, checkpoint=None, run_query=None)`. `run_query(query_id) -> {"columns", "rows"}` is supplied by the chart job. A chart with a query source resolved without `run_query` raises `INVALID_ARGUMENT`.
- Produces: `chart_sources.query_cells(result, column) -> list[dict | None]` in the same cell shape as `read_table`.
- In `chart_jobs`, `run_query` calls `self.book.query_jobs().run_once(query_id, state=<the state the job started at>, cancel_event=checkpoint.cancel_event, seconds=<time left in the job, at most 20>, max_rows=LIMIT_POINTS + 1)`.

Rules:
- Each saved query runs once per resolve, however many sources read it; every source on that query uses the same result, so its series line up.
- The column is the one result column whose name equals the stored `column`. None or more than one raises `SOURCE_BROKEN` (409) with a message naming the query and the column.
- Values: an integer or float is a number; `{"type": "integer", "value": "<digits>"}` (the engine's tag for integers beyond 2^53) is the number `float(int(value))`; text is text; `None` is an empty cell. Query results carry no date or boolean types, so those arrive as text or numbers as SQLite returns them.
- `display` for categories: text as is; numbers through `chart_render.format_tick`; a tagged integer as its digits; empty string for `None`.
- Row limits as for tables: a result with more rows than the series or pie limit raises `LIMIT_POINTS` or `LIMIT_PIE_POINTS` naming the query. `run_once` is asked for one row more than `LIMIT_POINTS`, so its own `QUERY_LIMIT` is reached only by a result of more than 10,001 rows; `chart_sources` turns that `QUERY_LIMIT` into `LIMIT_POINTS` too, so the user sees one code.
- A `STALE_STATE` from `run_once` ends the job stale, as today. Other query errors (`QUERY_FAILED`, `QUERY_VALUE`, `QUERY_DEPENDENCY_CHANGED`, `INVALID_PARAMETERS`, `TIMEOUT`, `CANCELLED`) propagate unchanged, except that `TIMEOUT` becomes `CALCULATION_FAILED` as for other sources.
- A query source with `query_id` `None`, or naming a query that is gone from the snapshot, raises `SOURCE_BROKEN` before any query runs.

- [ ] **Step 1: Write the failing tests**

```python
def test_resolves_two_series_and_categories_from_one_query_run(self):
    # run_query is a counting wrapper round the real run_once → called exactly once; rows in the query's ORDER BY
def test_query_values_are_classified(self):                            # Review Focus 5
    # SELECT with a text, a NULL, 1.5 and 9007199254740993 → gaps text 1, empty 1; the large integer is a point
def test_blob_result_fails_the_job(self): ...                          # Review Focus 5: QUERY_VALUE, no series
def test_redefined_query_without_the_column_is_refused(self):          # Review Focus 2
    # save Top again as SELECT Month, Cost FROM Sales → resolve raises SOURCE_BROKEN naming "Units"
def test_ambiguous_column_name_is_refused(self): ...
def test_result_over_the_point_limit_is_refused_by_name(self): ...     # LIMIT_POINTS naming the query; also beyond 10,001 rows
def test_parameter_defaults_are_used(self): ...
def test_query_without_run_query_is_invalid_argument(self): ...
def test_broken_query_source_is_refused_before_running(self): ...
def test_through_a_real_chart_job(self): ...                           # resolve and render; column chart from a GROUP BY query
def test_job_is_stale_when_the_workbook_changes_during_the_snapshot(self):   # Review Focus 4
    # block QueryJobs._snapshot on an event, commit an edit, release → job status "stale", no result
def test_cancelling_the_chart_job_stops_a_slow_query(self): ...        # Review Focus 5: endless CTE; cancel → cancelled within 2 s
def test_a_user_query_job_can_run_while_a_chart_resolves(self): ...    # no JOB_BUSY from either side
```

- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `test_chart_query_sources test_chart_table_sources test_chart_resolve test_chart_jobs test_query_jobs`, then the whole `service` suite once. Expected: PASS.
- [ ] **Step 5: Commit** — `Resolve chart sources from saved-query results`.

---

### Task 6: Suggest from a table, and the interfaces

**Files:**
- Modify: `service/datahorizon/chart_resolve.py` (`suggest` ~173), `service/datahorizon/charts_api.py` (`ChartSource` ~61, `ChartSuggestRequest`), `service/datahorizon/client.py` (`suggest_chart` ~441), `service/datahorizon/cli.py` (charts group ~231, ~348)
- Extend: `tests/test_chart_table_sources.py`, `tests/test_chart_clients.py`, `tests/test_job_openapi.py`

**Interfaces:**
- `chart_resolve.suggest(book, args)` accepts either the existing range arguments or `{expected_state, table, type?}`; giving both `table` and a rectangle is `INVALID_ARGUMENT`. From a table it reads the schema only, no rows.
- `WorkbookClient.suggest_chart(self, sheet="Sheet1", *, r1=None, c1=None, r2=None, c2=None, table=None, type="column")`.
- CLI: `charts suggest --table NAME [--type T]` beside the existing range form.
- OpenAPI: `ChartSource` becomes `oneOf` three input forms with examples; `ChartSuggestRequest` documents both forms. The stored-form `Chart` schema lists the three stored forms.

Rules for a table suggestion: integer and number columns are data columns, in schema order. The first column that is not a data column and not boolean is the categories; with none, `categories` is `null`. Each data column is a series named `{"text": <column name>}`, at most 64 (more is `LIMIT_SERIES`). For `scatter` the first data column is `x` for every series and at least two data columns are needed. For `pie` only the first data column is used. No data column is `INVALID_SPEC`. The result is in input form, with the table and columns given by id, and passes `normalise_spec`.

- [ ] **Step 1: Write the failing tests**

```python
def test_suggest_from_a_table_picks_categories_and_numeric_series(self): ...
def test_suggest_scatter_and_pie_from_a_table(self): ...
def test_suggest_refusals(self): ...          # unknown table 404; no numeric column; table with a rectangle; 65 numeric columns
def test_suggestion_creates_and_renders(self): ...
def test_sdk_and_cli_suggest_from_a_table_agree(self): ...                   # test_chart_clients
def test_sdk_round_trip_over_table_and_query_sources(self): ...              # create → render → drop the query → SOURCE_BROKEN through the SDK
def test_openapi_chart_source_lists_three_kinds_with_runnable_examples(self): ...
```

- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `test_chart_table_sources test_chart_clients test_job_openapi test_service`, then the whole `service` suite once. Expected: PASS.
- [ ] **Step 5: Commit** — `Suggest charts from a table and document the three source kinds`.

---

### Task 7: Measurements, documents and the whole suite

**Files:**
- Modify: `tests/chart_render_benchmark.py`, `examples/charts_script.py`, `docs/design/charts-design.md`, `docs/design/native-format-v1.md`, `docs/design/charts-stage-2-result.md`, `docs/design/implementation-status.md`, `docs/design/README.md`, `docs/design/development.md`, `docs/design/interfaces-and-interoperability.md`
- Create: `docs/design/evidence/charts-stage-2/sources.json`

- [ ] **Step 1: Measure through the service** (extend the benchmark's `--service` run; nothing else heavy running). With `LocalService`:
  - a table of 10,000 rows with one text and four number columns: `resolve_chart` and `render_chart` at 800×500 for a one-series line chart, and resolve for a four-series chart;
  - a saved query grouping that table into 100 rows: resolve and render of a column chart;
  - the same query chart on a workbook padded with 200,000 sheet cells, to show what the private snapshot copy costs.
  Record wall times, result sizes and the workbook file size. Expected: every job completes inside the 30-second budget. A failure is recorded as a measured limit; do not change a constant to make it pass.
- [ ] **Step 2: Extend `examples/charts_script.py`** with a table chart and a saved-query chart, each created, rendered to a file and deleted. Run it. Expected: exit 0.
- [ ] **Step 3: Run every suite.** `cmake --build build && ctest --test-dir build`. Expected: all pass; report any failure by name.
- [ ] **Step 4: Write the documents.**
  - `charts-design.md` Draft 0.5: section 4 states the table source as decided (whole table, stored row order, no saved view) and why; the stored and input forms of the three kinds; section 5 gains reading a table, running a query once per job on a private snapshot, the value and display rules, dynamic limits, and breaking on drop; section 8 gains suggest from a table; the stages table marks stage 2 complete; section 13 records the owner decisions of November 13, 2026.
  - `native-format-v1.md` v8: the two new stored source forms and what breaks them.
  - `charts-stage-2-result.md`: a section for this block (what was built, measurements, decisions, known limits). State what is still not built (the desktop, workbook-Python chart operations) and that measurements are from one Mac.
  - Also record the limit the stage 2 documents omit: the engine's 8 MiB calculation input cap applies to a whole chart job.
  - `implementation-status.md`, `README.md` (this plan and its status), `development.md` (CLI and SDK examples with a table and a query source), `interfaces-and-interoperability.md`.
- [ ] **Step 5: Commit** — `Record table and saved-query chart sources: measurements and as-built design`.

---

## Decisions made in this plan

Each is the author's call and open to reversal before execution.

- **Specification version stays 1.** The new kinds are additions. A build from before this block lists a chart that uses them with issue `INVALID_SPEC` rather than "unsupported version". Nothing has been released, so no such reader is in use.
- **A query source holds its result column by name.** Result columns have no identity. If the saved query is redefined without that column, the chart is refused at resolve, not marked broken when the query is saved.
- **A series name cannot come from a table or a query.** A name is one cell; a table column is many rows. Fixed text covers the case, and suggest uses the column name.
- **Query runs for charts bypass the query job store.** `run_once` takes no job slot, so a chart cannot be blocked by, or block, a person's own query. It still makes the private snapshot copy every query run makes, which is the main cost on a large workbook; Task 7 measures it.
- **No over-limit flag for table and query sources in chart listings.** Knowing a table's row count or a query's result size means reading it. The limit is reported when the chart is resolved.
- **No suggestion from a saved query.** A saved query's result shape has names but no types, so choosing series would mean running it. Left out until the desktop needs it.
- **Removing a column breaks the source; retyping it does not.** A column changed to text simply resolves as gaps.
- **Large SQLite integers plot as floats.** A value beyond 2^53 loses precision as a plotted position, which is not visible at chart scale.
