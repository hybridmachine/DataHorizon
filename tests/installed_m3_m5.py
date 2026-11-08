"""Run against only the relocated installed SDK/service, from a neutral directory."""
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,sys.argv[1])
import datahorizon.client
from datahorizon.client import LocalService
assert Path(datahorizon.client.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/'installed.dhwb',create=True) as book:
    book.set_localized(1,1,'1.234,50','number','de_DE');book.set(1,2,'=A1*2');assert book.read(r2=1,c2=2)['cells'][1]['value']==2469
    book.set(1,1,'10');assert book.read(r2=1,c2=2)['cells'][1]['value']==20
    book.set_metadata('presentation','default',{});book.declare_list('Rows',r1=1,r2=1,c1=1,c2=2,header=False)
    assert book.refresh()['format_version']==3 and book.metadata()['objects']
    assert book.copy_records('Rows')['rows']
    calc=book.calculate(r2=1,c2=2)
    while calc['status']=='running':time.sleep(.01);calc=book.calculation(calc['job_id'])
    assert calc['status']=='completed';book.calculation(calc['job_id'],'release')
    job=book.create_job();identifier=job['job_id'];book.operation_job(identifier,'append',cells=[dict(row=2,col=1,source='2024-02-29',kind='date')]);job=book.operation_job(identifier,'seal');job=book.operation_job(identifier,'start',expected_state=book.state,plan_digest=job['plan_digest'])
    while job['status']=='running':time.sleep(.01);job=book.operation_job(identifier)
    assert job['status']=='completed' and job['batches'][0]['receipt']['outcome']=='committed';book.refresh();assert book.read(r1=2,r2=2,c2=1)['cells'][0]['value_kind']=='date';book.operation_job(identifier,'release')
print('Installed SDK/service: dependency invalidation, v3 views/records, localized conversion, background calculation and durable jobs passed')
