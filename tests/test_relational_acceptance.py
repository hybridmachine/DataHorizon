# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
import tempfile
import unittest
from datahorizon.client import LocalService,APIError
from relational_fixture import create,exercise,independent,totals

class RelationalAcceptanceTests(unittest.TestCase):
    def test_customers_orders_managed_handlers_history_export_and_direct_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'CustomersOrders.dhwb';rules=Path(__file__).parent/'fixtures/relational_rules.py'
            with LocalService() as client:fixture=create(client,path);self.assertEqual(fixture['format_version'],4)
            with LocalService(rules) as client:result=exercise(client,path,handler=True,exports=Path(temp)/'exports');self.assertTrue(result['required_rule_checked']);self.assertEqual(len(result['exports']),3)
            independent(path);independent(path,orphan=True)
            with LocalService(rules) as client:
                with self.assertRaises(APIError) as failure:client.open(path)
                self.assertEqual(failure.exception.body['code'],'DATA_REPAIR_REQUIRED')
                with client.open(path,repair=True) as book:
                    report=book.inspect_relationships();self.assertEqual(len(report['orphans']),1);orphan=report['orphans'][0];review=book.preview_orphan_repair(orphan['relationship'],'update',edits=[dict(row_id=orphan['row_id'],values=dict(CustomerID='C002'))]);book.apply_orphan_repair(review)
                with client.open(path) as book:self.assertEqual(totals(book),[['C001','Ada',2,35.0],['C002','Grace',1,28.0]])
