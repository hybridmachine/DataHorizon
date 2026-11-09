# SPDX-License-Identifier: GPL-3.0-only
import base64
from pathlib import Path
import tempfile
import subprocess
import os
import sys
import unittest
from datahorizon.conversion import convert
from datahorizon.csv_import import preview_csv
from datahorizon.engine import EngineError
from datahorizon.client import LocalService
from datahorizon.interchange import xlsx_preview
from test_interchange import fixture


class LocaleConversionTests(unittest.TestCase):
    def test_explicit_profiles_grouping_precision_and_ambiguity(self):
        for profile,source,canonical in (('en_US','1,234.50','1234.50'),('de_DE','1.234,50','1234.50'),('fr_FR','1\u202f234,50','1234.50'),('fr_FR','1\u00a0234,50','1234.50')):
            result=convert(dict(source=source,kind='number',locale=profile));self.assertEqual(result['cell']['source'],canonical)
        for profile,source in (('en_US','12,34.5'),('de_DE','1,2,3'),('fr_FR','1 234\u202f567,8'),('en_US','9007199254740992')):
            with self.assertRaises(EngineError):convert(dict(source=source,kind='number',locale=profile))
        us=convert(dict(source='03/04/2026',kind='date',locale='en_US'));eu=convert(dict(source='03/04/2026',kind='date',locale='de_DE'))
        self.assertTrue(us['ambiguous_date']);self.assertEqual(us['cell']['source'],'2026-03-04');self.assertEqual(eu['cell']['source'],'2026-04-03')

    def test_csv_default_text_and_explicit_locale_types(self):
        plain=preview_csv(dict(csv='00123;1.234,50',delimiter=';'));self.assertNotIn('inputs',plain)
        converted=preview_csv(dict(csv='00123;1.234,50;03/04/2026;12:30',delimiter=';',locale='de_DE',column_types={'2':'number','3':'date_locale','4':'time_iso'}))
        self.assertTrue(converted['can_import']);self.assertEqual([c['source'] for c in converted['inputs'][0]],['00123','1234.50','2026-04-03','12:30:00']);self.assertEqual(converted['ambiguous_dates'],1)
        invalid=preview_csv(dict(csv='12,34.5',delimiter=';',locale='en_US',column_types={'1':'number'}));self.assertFalse(invalid['can_import']);self.assertNotIn('inputs',invalid)

    def test_timestamp_utc_policy_is_explicit_and_preserves_microseconds(self):
        source='2024-02-29T23:30:12.123456-06:00'
        result=convert(dict(source=source,kind='timestamp_utc'));self.assertEqual(result['cell'],dict(source='2024-03-01T05:30:12.123456',kind='datetime'))
        with self.assertRaises(EngineError):convert(dict(source=source,kind='datetime'))
        for source in ('2024-01-01T00:00:00+14:01','0001-01-01T00:00:00+14:00'):
            with self.assertRaises(EngineError):convert(dict(source=source,kind='timestamp_utc'))
        data=base64.b64encode(fixture('<c r="A1" t="d"><v>2024-02-29T23:30:00-06:00</v></c>')).decode()
        self.assertFalse(xlsx_preview(dict(file_base64=data))['can_import'])
        self.assertEqual(xlsx_preview(dict(file_base64=data,timezone_policy='utc'))['cells'][0]['source'],'2024-03-01T05:30:00')

    def test_sdk_conversion_still_uses_a_managed_edit(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/'locale.dhwb',create=True) as book:
            before=book.state;book.set_localized(1,1,'1.234,50','number','de_DE');self.assertNotEqual(book.state,before)
            self.assertEqual(book.read(r2=1,c2=1)['cells'][0]['value'],1234.5);book.apply('undo');self.assertEqual(book.read(r2=1,c2=1)['cells'],[])
            command=[sys.executable,'-m','datahorizon.cli','--url',client.url,str(Path(temp)/'locale.dhwb'),'set','A1','1.234,50','--kind','number','--locale','de_DE']
            completed=subprocess.run(command,capture_output=True,text=True,env={**os.environ,'DATAHORIZON_TOKEN':client.token},timeout=10);self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertEqual(book.read(r2=1,c2=1)['cells'][0]['value'],1234.5)
