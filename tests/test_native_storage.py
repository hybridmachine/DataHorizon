"""Independent native lifecycle and physical-layout experiment checks."""
import unittest
from native_storage_experiment import native_contracts, layout_measurements


class NativeStorageTests(unittest.TestCase):
    def test_inputs_identity_styles_external_refresh_and_version_boundary(self):
        result = native_contracts()
        self.assertEqual(result['stale_write_code'], 'STALE_STATE')
        self.assertFalse(result['external_handler_replay'])
        self.assertEqual(result['external_formula_display'], '10.00')
        self.assertTrue(result['snapshot_matches'])
        self.assertTrue(result['future_version_rejected_without_repair'])

    def test_both_physical_candidates_keep_all_rows_and_indexed_reads(self):
        result = layout_measurements(count=100, samples=3)
        self.assertEqual(len(result), 4)
        for measurement in result.values():
            self.assertEqual(measurement['cells'], 100)
            self.assertEqual(measurement['warm_narrow_read']['samples'], 3)
            self.assertTrue(all('SEARCH' in plan for plan in measurement['query_plan']))


if __name__ == '__main__':
    unittest.main()
