# SPDX-License-Identifier: GPL-3.0-only
"""Supplemental REST resource checks on relational_benchmark.py's disposable file."""
from pathlib import Path
import argparse
import json
import time
from datahorizon.client import APIError,LocalService
from relational_benchmark import wait


def run(path):
    report={'native_bytes':path.stat().st_size,'provenance':'Already warmed disposable large fixture; sequential edits and one query worker in the shared service.'}
    with LocalService() as client:
        with client.open(path) as book:
            first=book.read_table('Large',limit=1);original=first['records'][0]
            payload_id=next(column['id'] for column in first['columns'] if column['name']=='Payload')
            row=original['row_id'];payload=original['values'][payload_id]
            timings={'without_query_ms':[],'with_executing_query_ms':[]}
            try:
                for active,key in ((False,'without_query_ms'),(True,'with_executing_query_ms')):
                    for index in range(5):
                        result=None
                        if active:
                            result=book.run_query('SELECT count(*) FROM Large a CROSS JOIN Large b')
                            deadline=time.monotonic()+10
                            while result['status']=='running' and 'Executing' not in result['phase'] and time.monotonic()<deadline:
                                time.sleep(.01);result=book.query(result['job_id'])
                            assert result['status']=='running' and 'Executing' in result['phase'],result
                        started=time.perf_counter()
                        book.update_records('Large',[dict(row_id=row,values=dict(Payload=f'Resource probe {active} {index}'))])
                        timings[key].append((time.perf_counter()-started)*1000)
                        if result:
                            book.query(result['job_id'],'cancel');wait(book,result);book.query(result['job_id'],'release')
            finally:
                book.update_records('Large',[dict(row_id=row,values=dict(Payload=payload))])
            report['managed_record_edits']=timings
            # 80,000 x 1-KiB payloads exhaust bytes before the 100,000-row limit.
            result=wait(book,book.run_query('SELECT Payload FROM Large LIMIT 80000'))
            assert result['status']=='failed' and result['error']['code']=='QUERY_LIMIT',result
            assert 'result' not in result
            try:book.query(result['job_id'],'page')
            except APIError as error:page_error=error.body['code']
            else:raise AssertionError('Failed spool unexpectedly published a page')
            report['actual_spool_limit']={'status':result['status'],'error':result['error'],'page_error':page_error,'successful_partial_result':False}
            book.query(result['job_id'],'release')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('workbook',type=Path);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=run(args.workbook);args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
