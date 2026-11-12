# SPDX-License-Identifier: GPL-3.0-only
import base64
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
import zipfile
from datahorizon.client import LocalService, APIError
from datahorizon.engine import Workbook,EngineError
from datahorizon.interchange import xlsx_export
from datahorizon.ods import ods_export
from datahorizon.workbook_code import fingerprint

UNTOKENIZABLE='=A5 ` 1'
MESSAGE='References in this formula were not maintained through a structural edit. Re-enter the formula.'


def format_version(book):
    return book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]


def mutate(book,**request):
    return book.mutate(dict(operation_id=str(uuid.uuid4()),expected_state=book.state(),**request))


def edit(book,cell,source,kind='auto',sheet='Sheet1',style=None):
    row=int(cell[1:]);col=ord(cell[0])-64
    item=dict(row=row,col=col,source=source,kind=kind,**({'style':style} if style else {}))
    return mutate(book,action='edit',sheet=sheet,cells=[item])


def read(book,cell,sheet='Sheet1'):
    row=int(cell[1:]);col=ord(cell[0])-64
    cells=book.read(sheet,{'r1':row,'c1':col})['cells']
    return cells[0] if cells else None


class FormatV7Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'v7.dhwb'

    def new_book(self):
        book=Workbook(self.path,create=True);self.addCleanup(book.close);return book

    def raw(self,statement,*args):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(statement,args);db.commit()

    def marked_book(self):
        book=self.new_book();edit(book,'A5','5');edit(book,'B1',UNTOKENIZABLE,kind='formula',style={'bold':True})
        return book,mutate(book,action='rows_insert',sheet='Sheet1',start=2,count=1)

    def test_structural_edit_marks_untokenizable_formula_and_bumps_to_v7(self):
        book=self.new_book();edit(book,'A5','5');edit(book,'B1',UNTOKENIZABLE,kind='formula')
        self.assertEqual(format_version(book),1)
        receipt=mutate(book,action='rows_insert',sheet='Sheet1',start=2,count=1)
        # The existing structural reference report is flat on the receipt (reference_warnings, reference_issues).
        self.assertEqual(receipt['unmaintained'],[{'sheet':'Sheet1','cell':'B1'}])
        self.assertEqual(receipt['unmaintained_count'],1)
        cell=read(book,'B1')
        self.assertEqual((cell['kind'],cell['source'],cell['display']),('unmaintained',UNTOKENIZABLE,'#REF!'))
        self.assertEqual((cell['input_kind'],cell['error'],cell['calculation']),('unmaintained',MESSAGE,'error'))
        self.assertEqual(format_version(book),7)
        self.assertEqual(read(book,'A6')['value'],5)

    def test_marked_cell_keeps_style_reopens_and_dependents_see_ref_error(self):
        book,_=self.marked_book();edit(book,'C1','=B1')
        self.assertEqual(read(book,'B1')['style'],{'bold':True})
        self.assertEqual((read(book,'C1')['display'],read(book,'C1')['calculation']),('#REF!','error'))
        book.close();reopened=Workbook(self.path);self.addCleanup(reopened.close)
        self.assertEqual(read(reopened,'B1')['kind'],'unmaintained');self.assertEqual(format_version(reopened),7)
        self.assertEqual(reopened.info()['format_version'],7)

    def test_reentering_the_formula_clears_the_state(self):
        book,_=self.marked_book()
        edit(book,'B1','=A6')
        cell=read(book,'B1');self.assertEqual((cell['kind'],cell['value'],cell['style']),('formula',5,{'bold':True}))
        edit(book,'B1',UNTOKENIZABLE)
        self.assertEqual(read(book,'B1')['kind'],'formula')
        self.assertEqual(format_version(book),7)

    def test_clients_cannot_write_the_kind_directly(self):
        book=self.new_book()
        for request in (dict(action='edit',cells=[dict(row=1,col=1,source=UNTOKENIZABLE,kind='unmaintained')]),):
            with self.assertRaises(EngineError) as raised:mutate(book,**request)
            self.assertEqual(raised.exception.body['code'],'INVALID_INPUT')
        self.assertIsNone(read(book,'A1'));self.assertEqual(format_version(book),1)

    def test_undo_restores_formula_kind_and_keeps_v7(self):
        book=self.new_book();edit(book,'A5','5');edit(book,'B1',UNTOKENIZABLE)
        receipt=mutate(book,action='move',sheet='Sheet1',r1=5,c1=1,r2=5,c2=1,row=6,col=1,cut_state=book.state())
        self.assertEqual((receipt['unmaintained'],receipt['unmaintained_count']),([{'sheet':'Sheet1','cell':'B1'}],1))
        self.assertEqual(read(book,'B1')['kind'],'unmaintained');self.assertEqual(format_version(book),7)
        mutate(book,action='undo')
        cell=read(book,'B1');self.assertEqual((cell['kind'],cell['source']),('formula',UNTOKENIZABLE))
        self.assertEqual(read(book,'A5')['value'],5);self.assertEqual(format_version(book),7)
        mutate(book,action='redo')
        self.assertEqual(read(book,'B1')['kind'],'unmaintained')

    def test_v6_file_with_unmaintained_row_is_invalid_format(self):
        book=self.new_book()
        mutate(book,action='module_save',fingerprint=fingerprint(book.db),name='m',source='def f(ctx):\n    return 1\n',macros=[])
        sheet=book.info()['sheets'][0]['id'];book.close()
        self.raw('UPDATE dh_meta SET format_version=6')
        self.raw("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,1,1,?,'unmaintained')",sheet,UNTOKENIZABLE)
        with self.assertRaises(EngineError) as raised:Workbook(self.path)
        self.assertEqual(raised.exception.body['code'],'INVALID_FORMAT')
        self.raw('UPDATE dh_meta SET format_version=7')
        reopened=Workbook(self.path);self.addCleanup(reopened.close)
        self.assertEqual(read(reopened,'A1')['kind'],'unmaintained')

    def test_v8_is_rejected_on_open(self):
        self.new_book().close();self.raw('UPDATE dh_meta SET format_version=8')
        with self.assertRaises(EngineError) as raised:Workbook(self.path)
        self.assertEqual(raised.exception.body['code'],'FORMAT_VERSION')

    def test_formula_on_untouched_sheet_without_sheet_reference_is_not_marked(self):
        book=self.new_book();mutate(book,action='sheet_add',name='Other')
        edit(book,'A1',UNTOKENIZABLE,sheet='Other');edit(book,'A2','=Sheet1!A5 ` 1',sheet='Other')
        receipt=mutate(book,action='rows_insert',sheet='Sheet1',start=2,count=1)
        self.assertEqual(read(book,'A1',sheet='Other')['kind'],'formula')
        self.assertEqual(read(book,'A2',sheet='Other')['kind'],'unmaintained')
        self.assertEqual(receipt['unmaintained'],[{'sheet':'Other','cell':'A2'}])

    def test_unmarked_operation_keeps_version_and_receipt_shape(self):
        book=self.new_book();mutate(book,action='sheet_add',name='Other');edit(book,'A1',UNTOKENIZABLE,sheet='Other')
        receipt=mutate(book,action='rows_insert',sheet='Sheet1',start=2,count=1)
        self.assertNotIn('unmaintained',receipt);self.assertEqual(format_version(book),1)

    def test_moved_formula_is_marked_at_its_destination(self):
        book=self.new_book();edit(book,'B3',UNTOKENIZABLE)
        receipt=mutate(book,action='rows_insert',sheet='Sheet1',start=1,count=2)
        self.assertEqual(receipt['unmaintained'],[{'sheet':'Sheet1','cell':'B5'}])
        self.assertEqual(read(book,'B5')['kind'],'unmaintained');self.assertIsNone(read(book,'B3'))
        receipt=mutate(book,action='rows_delete',sheet='Sheet1',start=1,count=1)
        self.assertNotIn('unmaintained',receipt);self.assertEqual(read(book,'B4')['kind'],'unmaintained')

    def test_sheet_rename_delete_and_duplicate_mark_qualified_formulas(self):
        book=self.new_book();mutate(book,action='sheet_add',name='Other')
        edit(book,'A1','=Sheet1!A5 ` 1',sheet='Other');edit(book,'A1','=Sheet1!A5 ` 1')
        receipt=mutate(book,action='sheet_rename',sheet='Sheet1',name='Main')
        self.assertEqual(receipt['unmaintained_count'],2)
        self.assertEqual(sorted(item['sheet'] for item in receipt['unmaintained']),['Main','Other'])
        edit(book,'A1','=Main!A5 ` 1',sheet='Main');edit(book,'A2',UNTOKENIZABLE,sheet='Main')
        receipt=mutate(book,action='sheet_duplicate',sheet='Main',name='Copy')
        self.assertEqual(receipt['unmaintained'],[{'sheet':'Copy','cell':'A1'}])
        self.assertEqual(read(book,'A1',sheet='Main')['kind'],'formula')
        self.assertEqual((read(book,'A1',sheet='Copy')['kind'],read(book,'A2',sheet='Copy')['kind']),('unmaintained','formula'))
        edit(book,'A1','=Main!A5 ` 1',sheet='Other');edit(book,'A2',UNTOKENIZABLE,sheet='Other')
        receipt=mutate(book,action='sheet_delete',sheet='Main')
        self.assertEqual(receipt['unmaintained'],[{'sheet':'Other','cell':'A1'}])
        self.assertEqual((read(book,'A1',sheet='Other')['kind'],read(book,'A2',sheet='Other')['kind']),('unmaintained','formula'))

    def test_copy_fill_and_paste_keep_the_kind_without_translation(self):
        book,_=self.marked_book()
        mutate(book,action='fill',sheet='Sheet1',r1=1,c1=2,r2=3,c2=2,mode='copy')
        self.assertEqual([(read(book,cell)['kind'],read(book,cell)['source']) for cell in ('B2','B3')],[('unmaintained',UNTOKENIZABLE)]*2)
        clipboard=book.read('Sheet1',{'r1':1,'c1':2})['cells']
        mutate(book,action='paste',sheet='Sheet1',row=4,col=4,origin_row=1,origin_col=2,rows=[clipboard])
        cell=read(book,'D4');self.assertEqual((cell['kind'],cell['source'],cell['style']),('unmaintained',UNTOKENIZABLE,{'bold':True}))
        with self.assertRaises(EngineError) as raised:
            mutate(book,action='paste',sheet='Sheet1',row=5,col=4,mode='values',rows=[clipboard])
        self.assertEqual(raised.exception.body['code'],'CALCULATION_ERROR')

    def test_untokenizable_formula_copy_needs_no_offset_or_keeps_existing_error(self):
        book=self.new_book();edit(book,'B1',UNTOKENIZABLE)
        clipboard=book.read('Sheet1',{'r1':1,'c1':2})['cells']
        mutate(book,action='paste',sheet='Sheet1',row=1,col=2,rows=[clipboard])
        self.assertEqual(read(book,'B1')['kind'],'formula')
        with self.assertRaises(EngineError) as raised:
            mutate(book,action='paste',sheet='Sheet1',row=2,col=2,origin_row=1,origin_col=2,rows=[clipboard])
        self.assertEqual(raised.exception.body['code'],'OPERATION_FAILED')
        self.assertEqual(read(book,'B1')['kind'],'formula');self.assertEqual(format_version(book),1)

    def test_xlsx_and_ods_export_write_source_and_report_approximation(self):
        book,_=self.marked_book()
        for export,part,expected in ((xlsx_export,'xl/worksheets/sheet1.xml','<f>A5 ` 1</f>'),(ods_export,'content.xml','of:=A5 ` 1')):
            with self.subTest(export=export.__name__):
                result=export(book.read_workbook(),{'scope':'workbook','mode':'formulas'})
                content=zipfile.ZipFile(io.BytesIO(base64.b64decode(result['file_base64']))).read(part).decode()
                self.assertIn(expected,content)
                entry=next(item for item in result['report_entries'] if item['category']=='formula.unmaintained')
                self.assertEqual((entry['severity'],entry['count'],entry['samples']),('approximated',1,[{'sheet':'Sheet1','cell':'B1'}]))
                self.assertTrue(any('formula.unmaintained' in line or 'not maintained' in line for line in result['report']))

    def test_selection_formula_export_refuses_unmaintained_cells(self):
        book,_=self.marked_book();edit(book,'C1','7')
        selection=book.read('Sheet1',{'r1':1,'c1':2,'r2':1,'c2':3},native=True)
        for export in (xlsx_export,ods_export):
            for mode,code in (('formulas','UNSUPPORTED_FORMULA'),('values','CALCULATION_ERROR')):
                with self.subTest(export=export.__name__,mode=mode):
                    with self.assertRaises(EngineError) as raised:export(selection,{'mode':mode})
                    self.assertEqual(raised.exception.body['code'],code)
                    if mode=='formulas':self.assertIn('Export the whole workbook or choose values',str(raised.exception))
            self.assertIn('file_base64',export(book.read_workbook(),{'scope':'workbook','mode':'formulas'}))

    def test_rest_exposes_kind_versions_and_rejects_direct_kind(self):
        with LocalService() as client:
            capabilities=client.request('/v1/capabilities');api=client.request('/openapi.json')
            self.assertEqual((capabilities['format_version'],capabilities['supported_format_versions']),(7,[1,2,3,4,5,6,7]))
            schemas=api['components']['schemas']
            self.assertIn('unmaintained',schemas['ClipboardCell']['properties']['kind']['enum'])
            self.assertIn('unmaintained',schemas['ClipboardCell']['properties']['input_kind']['enum'])
            self.assertNotIn('unmaintained',schemas['CellEdit']['properties']['kind']['enum'])
            self.assertIn('unmaintained_count',schemas['Receipt']['properties'])
            with client.open(self.path,create=True) as book:
                book.apply('edit',cells=[dict(row=1,col=2,source=UNTOKENIZABLE)]);book.refresh()
                receipt=book.apply('rows_insert',start=1,count=1)
                self.assertEqual((receipt['unmaintained'],receipt['unmaintained_count']),([{'sheet':'Sheet1','cell':'B2'}],1))
                book.refresh();cell=book.read(r1=2,c1=2,r2=2,c2=2)['cells'][0]
                self.assertEqual((cell['kind'],cell['display'],cell['error']),('unmaintained','#REF!',MESSAGE))
                self.assertEqual(book.refresh()['format_version'],7)
                with self.assertRaises(APIError) as raised:
                    book.apply('edit',cells=[dict(row=3,col=3,source='=A1',kind='unmaintained')])
                self.assertEqual(raised.exception.body['code'],'INVALID_INPUT')


if __name__=='__main__':unittest.main()
