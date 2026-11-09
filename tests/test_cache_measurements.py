"""Measurement provenance must not turn denied cache eviction into cold evidence."""
import subprocess
import unittest
from unittest.mock import patch
from cache_rss_benchmark import measurements,purge_os_cache


class CacheMeasurementTests(unittest.TestCase):
    def test_denied_purge_fails_before_any_measured_service_is_started(self):
        with patch('cache_rss_benchmark.sys.platform','darwin'),patch('cache_rss_benchmark.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','password is required')):
            with self.assertRaisesRegex(RuntimeError,'password is required'):purge_os_cache()

    def test_purge_runs_once_after_fixture_creation_before_first_sample(self):
        calls=[]
        def unavailable():calls.append('purge');raise RuntimeError('eviction unavailable')
        with self.assertRaisesRegex(RuntimeError,'eviction unavailable'):
            measurements(2,10,sparse_rows=10,purge=unavailable)
        self.assertEqual(calls,['purge'])
