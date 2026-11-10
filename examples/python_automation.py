# SPDX-License-Identifier: GPL-3.0-only
"""Create/inspect the example without activation, or exercise an approved copy."""
import argparse
import json
from pathlib import Path
import uuid
import datahorizon
from datahorizon.client import APIError,LocalService

DIRECTORY=Path(__file__).resolve().parent
DECLARATIONS=[dict(name='adjust',parameters=[dict(name='row_id',type='text',default='1'),dict(name='delta',type='integer',default=3)],result=dict(type='integer')),dict(name='spin',parameters=[],result=dict(type='integer'))]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['create','inspect','exercise']);parser.add_argument('workbook',type=Path);parser.add_argument('--approve-code',action='append',default=[]);parser.add_argument('--report',type=Path);args=parser.parse_args()
    with LocalService(DIRECTORY/'automation_rules.py',approved_code=args.approve_code) as client,client.open(args.workbook,create=args.action=='create') as book:
        if args.action=='create':
            book.create_table('Measurements',[dict(name='Value',type='integer',required=True)])
            book.insert_records('Measurements',[dict(Value=10)])
            book.apply('edit',cells=[dict(row=1,col=1,source='Ready',kind='text'),dict(row=1,col=2,source='No validation edits',kind='text')])
            book.save_module('MeasurementsCode',(DIRECTORY/'automation_module.py').read_text(),DECLARATIONS,fingerprint=book.code()['fingerprint'])
        code=book.code();report=dict(sdk_module=str(Path(datahorizon.__file__).resolve()),format_version=book.refresh()['format_version'],activation_key=code['activation_key'],fingerprint=code['fingerprint'],approved=code['approved'])
        if args.action=='exercise':
            before=book.read(r2=1,c2=2)['cells'];operation=str(uuid.uuid4())
            result=book.run_macro('MeasurementsCode','adjust',fingerprint=code['fingerprint'],operation_id=operation)
            assert result['macro_result']==13,result
            assert book.outcome(operation)['macro_result']==13
            changed=book.read(r2=1,c2=2)['cells'];assert [cell['value'] for cell in changed]==['Updated to 13','Validated'],changed
            book.apply('undo');assert book.read(r2=1,c2=2)['cells']==before
            book.apply('redo');assert [cell['value'] for cell in book.read(r2=1,c2=2)['cells']]==['Updated to 13','Validated']
            book.apply('undo')
            try:book.run_macro('MeasurementsCode','adjust',fingerprint=code['fingerprint'],arguments=dict(delta=-100))
            except APIError as error:assert error.body['code']=='HOOK_FAILED' and error.body['outcome']=='rolled_back';failure=error.body
            else:raise AssertionError('Required fixture rule did not reject a negative value')
            assert book.read(r2=1,c2=2)['cells']==before
            table=book.read_table('Measurements');column=table['columns'][0]['id'];assert table['records'][0]['values'][column]==10
            report.update(macro_result=13,generated_hook_edit=True,undo_redo_restored=True,durable_result_lookup=True,required_rule_rollback=True,success_receipt=result,failure_receipt=failure)
        else:report['modules']=code['modules']
        report['final_state']=book.state
    output=json.dumps(report,indent=2)
    if args.report:args.report.write_text(output+'\n')
    print(output)


if __name__=='__main__':main()
