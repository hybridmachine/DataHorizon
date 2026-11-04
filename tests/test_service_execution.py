"""Compare service adapters through real sockets and the shared engine."""
from pathlib import Path
import tempfile
import unittest

from datahorizon.client import APIError, Client
from service_execution_experiment import contracts, edit, measurements, serve, stored


class ServiceExecutionTests(unittest.TestCase):
    def test_both_adapters_preserve_transactions_failures_and_durable_receipts(self):
        for adapter in ('threaded_http', 'serial_wsgi'):
            with self.subTest(adapter=adapter):
                result = contracts(adapter)
                self.assertEqual(result['generated_edit'], {'outcome': 'committed', 'changed_cells': 2})
                for name in ('exception', 'worker_crash', 'worker_timeout'):
                    self.assertEqual(result[name]['outcome'], 'rolled_back')
                self.assertEqual(result['lost_acknowledgement'], {'outcome': 'committed', 'changed_cells': 2})
                self.assertTrue(result['receipt_survives_restart'])

    def test_authentication_rejects_before_engine_mutation(self):
        for adapter in ('threaded_http', 'serial_wsgi'):
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as temp:
                with serve(Path(temp)/'auth.dhwb', adapter) as (app, client):
                    request = edit(client, 7)
                    before = stored(app)
                    with self.assertRaises(APIError) as rejected:
                        Client(client.url, 'wrong').request('/edit', request)
                    self.assertEqual(rejected.exception.status, 401)
                    self.assertEqual(stored(app), before)

    def test_fast_inline_and_spawned_measurements_keep_generated_edits(self):
        # Samples are validated by stored semantics; timings are not pass gates.
        for adapter in ('threaded_http', 'serial_wsgi'):
            for inline in (False, True):
                with self.subTest(adapter=adapter, inline=inline):
                    result = measurements(adapter, 2, inline)
                    self.assertEqual(result['samples'], 2)
                    self.assertGreater(result['p95_ms'], 0)


if __name__ == '__main__':
    unittest.main()
