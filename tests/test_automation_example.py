# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class AutomationExampleTests(unittest.TestCase):
    def test_published_example_runs_with_explicit_operator_activation(self):
        script=Path(__file__).resolve().parents[1]/'examples/python_automation.py'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'example.dhwb'
            created=subprocess.run([sys.executable,str(script),'create',str(path)],capture_output=True,text=True,check=True,timeout=20)
            report=json.loads(created.stdout);self.assertFalse(report['approved']);self.assertEqual(report['format_version'],5)
            checked=subprocess.run([sys.executable,str(script),'exercise',str(path),'--approve-code',report['activation_key']],capture_output=True,text=True,check=True,timeout=20)
            report=json.loads(checked.stdout);self.assertEqual(report['macro_result'],13);self.assertTrue(report['undo_redo_restored']);self.assertTrue(report['required_rule_rollback'])


if __name__=='__main__':unittest.main()
