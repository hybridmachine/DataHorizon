# SPDX-License-Identifier: GPL-3.0-only
"""M1 native representation validation and bounded rowid-layout comparison."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time
import uuid

from datahorizon.engine import EngineError, SCHEMA, Workbook


def mutate(book, **arguments):
    return book.mutate({'action': 'edit', 'sheet': 'Sheet1', 'operation_id': str(uuid.uuid4()),
                        'expected_state': book.info()['state'], **arguments})


def native_contracts():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp)/'native.dhwb'
        with closing(Workbook(path, create=True)) as book:
            mutate(book, cells=[{'row': 1, 'col': 1, 'source': '00123', 'kind': 'text'},
                                {'row': 1, 'col': 2, 'source': '', 'kind': 'null'},
                                {'row': 1, 'col': 3, 'source': '', 'kind': 'text'},
                                {'row': 1, 'col': 4, 'source': '=A2+1', 'style': {'format': 'fixed2', 'bold': True}},
                                {'row': 1, 'col': 6, 'style': {'background': '#123456'}}])
            original = book.info()
            revision = book.db.execute('SELECT revision FROM dh_meta').fetchone()[0]
            sheet = original['sheets'][0]['id']
            rows = lambda: book.db.execute('SELECT * FROM cells ORDER BY row,col').fetchall()
            before = rows()
            cells = {c['col']: c for c in book.read('Sheet1', {'r1': 1, 'r2': 1, 'c1': 1, 'c2': 6})['cells']}
            assert cells[1]['value'] == '00123' and cells[2]['kind'] == 'null'
            assert cells[3]['value'] == '' and 5 not in cells and cells[6]['kind'] == 'blank'
            assert cells[4]['display'] == '1.00' and rows() == before
            columns = [r[1] for r in book.db.execute('PRAGMA table_info(cells)')]
            assert columns == ['sheet_id', 'row', 'col', 'source', 'kind', 'style']
            # Explicit opt-in fixture would reject -1 if change events replayed.
            book.handler = Path(__file__).resolve().parent/'fixtures'/'execution_hook.py'
            with closing(sqlite3.connect(path)) as direct, direct:
                direct.execute("UPDATE cells SET source='-1',kind='number' WHERE sheet_id=? AND row=1 AND col=1", (sheet,))
                direct.execute("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,2,1,'9','number')", (sheet,))
            refreshed = book.info()
            assert refreshed['state'] != original['state'] and refreshed['id'] == original['id']
            assert refreshed['sheets'] == original['sheets'] and refreshed['undo'] is None
            assert book.db.execute('SELECT revision FROM dh_meta').fetchone()[0] == revision
            assert book.raw(sheet, 1, 2)['kind'] == 'null'  # No generated handler edit.
            formula = book.read('Sheet1', {'r1': 1, 'r2': 1, 'c1': 4, 'c2': 4})['cells'][0]
            assert formula['source'] == '=A2+1' and formula['display'] == '10.00'
            stale_id = str(uuid.uuid4())
            try:
                book.mutate({'action': 'edit', 'sheet': 'Sheet1', 'operation_id': stale_id,
                             'expected_state': original['state'], 'cells': [{'row': 3, 'col': 1, 'source': '1'}]})
            except EngineError as error:
                assert error.body['outcome'] == 'rolled_back'
                assert book.operation(stale_id) == error.body
                stale_code = error.body['code']
            else:
                raise AssertionError('Stale write accepted')
            copy = Path(temp)/'snapshot.dhwb'
            book.snapshot(copy)
            expected = rows()
        with closing(Workbook(path, handler=Path(__file__).resolve().parent/'fixtures'/'execution_hook.py')) as reopened:
            assert reopened.id == original['id'] and reopened.raw(sheet, 1, 2)['kind'] == 'null'
            assert reopened.raw(sheet, 1, 4)['style'] == {'format': 'fixed2', 'bold': True}
        with closing(Workbook(copy)) as copied:
            assert copied.id == original['id']
            assert copied.db.execute('SELECT * FROM cells ORDER BY row,col').fetchall() == expected
        with closing(sqlite3.connect(copy)) as direct, direct:
            direct.execute('UPDATE dh_meta SET format_version=4')
        try:
            Workbook(copy)
        except EngineError as error:
            assert error.body['code'] == 'FORMAT_VERSION'
        else:
            raise AssertionError('Future version accepted')
        with closing(sqlite3.connect(copy)) as direct:
            assert direct.execute('SELECT format_version FROM dh_meta').fetchone()[0] == 4
        return {'typed_input_kinds': ['text_identifier', 'null', 'empty_text', 'formula', 'absence', 'styled_blank'],
                'stored_cell_columns': columns, 'read_preserves_inputs': True, 'external_handler_replay': False,
                'external_formula_display': '10.00', 'stale_write_code': stale_code,
                'managed_revision_unchanged_by_direct': True,
                'identity_styles_reopen': True, 'snapshot_matches': True, 'future_version_rejected_without_repair': True}


def layout_measurements(count=10_000, samples=100):
    results = {}
    with tempfile.TemporaryDirectory() as temp:
        for shape in ('sparse', 'dense'):
            for layout in ('without_rowid', 'rowid'):
                path = Path(temp)/(shape+'-'+layout+'.sqlite')
                sheet = str(uuid.uuid4())
                schema = SCHEMA if layout == 'without_rowid' else SCHEMA.replace(') WITHOUT ROWID;', ');')
                with closing(sqlite3.connect(path)) as db:
                    db.executescript(schema)
                    db.execute('INSERT INTO dh_meta VALUES(1,1,?,0)', (str(uuid.uuid4()),))
                    db.execute("INSERT INTO sheets VALUES(?,'Sheet1',0)", (sheet,))
                    def entries():
                        for i in range(count):
                            row, col = (i*100+1, 1) if shape == 'sparse' else (i//100+1, i%100+1)
                            yield sheet, row, col, str(i), 'number', '{}'
                    db.executemany('INSERT INTO cells VALUES(?,?,?,?,?,?)', entries())
                    db.commit()
                    assert db.execute('SELECT count(*) FROM cells').fetchone()[0] == count
                    query = 'SELECT source,kind,style FROM cells WHERE sheet_id=? AND row=? AND col BETWEEN 1 AND 16'
                    args = (sheet, (count//2)*100+1 if shape == 'sparse' else count//200+1)
                    durations = []
                    for _ in range(samples):
                        started = time.perf_counter()
                        fetched = db.execute(query, args).fetchall()
                        assert fetched
                        durations.append((time.perf_counter()-started)*1000)
                    results[shape+'_'+layout] = {'cells': count, 'database_bytes': path.stat().st_size,
                        'pages': db.execute('PRAGMA page_count').fetchone()[0],
                        'query_plan': [r[3] for r in db.execute('EXPLAIN QUERY PLAN '+query, args)],
                        'warm_narrow_read': {'samples': samples, 'median_ms': round(statistics.median(durations), 4),
                                             'p95_ms': round(sorted(durations)[math.ceil(samples*.95)-1], 4)}}
                if layout == 'rowid':
                    try:
                        Workbook(path)
                    except EngineError as error:
                        assert error.body['code'] == 'SCHEMA_CHANGED'
                    else:
                        raise AssertionError('Alternative layout accepted as native v1')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    report = {'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
              'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
              'native_contracts': native_contracts(), 'layouts': layout_measurements(samples=args.samples),
              'limitations': ['10000-cell fixtures and warm SQLite reads; no cold-cache, REST, RSS or unlimited-scale claim.',
                              'Rowid is an isolated comparison and is deliberately rejected by native format v1.',
                              'Advanced physical storage, migrations and production durability remain unqualified.']}
    output = json.dumps(report, indent=2)+'\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
