# SPDX-License-Identifier: GPL-3.0-only
"""Executable baseline for the M3–M5 completion batch."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from datahorizon.engine import Workbook
from datahorizon.formula import translate, transform_structure


class CompletionContracts(unittest.TestCase):
    def test_declared_coercion_error_and_temporal_matrix(self):
        cases=json.loads((Path(__file__).parent/'fixtures'/'m3_m5_conformance.json').read_text())['cases']
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'matrix.dhwb',create=True)) as book:
            book.mutate(dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=book.state(),cells=[dict(row=r,col=1,source=case['source']) for r,case in enumerate(cases,1)]))
            cells=book.read('Sheet1',dict(r2=len(cases)))['cells']
            for case,cell in zip(cases,cells):
                self.assertEqual(cell['source'],case['source'])
                if 'error' in case:self.assertEqual(cell['display'],case['error'])
                else:
                    self.assertEqual(cell['value'],case['value'])
                    if 'value_kind' in case:self.assertEqual(cell['value_kind'],case['value_kind'])

    def test_reference_copy_and_structure_contract(self):
        self.assertEqual(translate('=SUM(A1:$B$3)+"A1"',2,1),'=SUM(B3:$B$3)+"A1"')
        transformed,broken=transform_structure('=SUM(A1:A3)+$B$2','Sheet1','Sheet1','row',2,1,False)
        self.assertEqual(transformed,'=SUM(A1:A4)+$B$3');self.assertEqual(broken,0)
