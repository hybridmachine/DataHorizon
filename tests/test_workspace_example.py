# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WorkspaceExampleTests(unittest.TestCase):
    def test_storyboard_example_runs_after_owner_channel_approval(self):
        script=Path(__file__).resolve().parents[1]/'examples/supplies_workspace.py'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'Supplies.dhwb'
            created=subprocess.run([sys.executable,str(script),'create',str(path)],capture_output=True,text=True,check=True,timeout=30)
            report=json.loads(created.stdout)
            self.assertFalse(report['approved']);self.assertEqual(report['format_version'],6)
            self.assertEqual(sorted(item['name'] for item in report['names']),['PenQuantity','Quantities','Total'])
            self.assertEqual([(item['event'],item['function'],item['required']) for item in report['bindings']],[('cell_change','check_quantity',True)])
            checked=subprocess.run([sys.executable,str(script),'exercise',str(path)],capture_output=True,text=True,check=True,timeout=60)
            report=json.loads(checked.stdout)
            self.assertTrue(report['approved'])
            self.assertEqual(report['total_before'],18);self.assertEqual(report['macro_result'],20.5);self.assertEqual(report['total_after_run'],20.5)
            self.assertEqual(report['pen_quantity_after_run'],6)
            self.assertEqual(report['total_after_undo'],18);self.assertEqual(report['pen_quantity_after_undo'],4)
            failure=report['failed_run']
            self.assertEqual((failure['code'],failure['outcome']),('MACRO_FAILED','rolled_back'));self.assertIn('Below zero',failure['message'])
            self.assertEqual(report['total_after_failure'],18)
            rejected=report['rejected_edit']
            self.assertEqual((rejected['code'],rejected['outcome']),('BINDING_FAILED','rolled_back'))
            self.assertIn('supplies.check_quantity',rejected['message']);self.assertIn('Below zero',rejected['message'])
            self.assertEqual(report['quantity_after_rejection'],2)
            self.assertEqual(report['accepted_edit_total'],21.5)


if __name__=='__main__':unittest.main()
