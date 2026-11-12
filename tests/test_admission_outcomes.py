# SPDX-License-Identifier: GPL-3.0-only
"""Admission refusals that provably precede any transaction are rejected outcomes naming the operation."""
import unittest

from datahorizon.client import APIError, UnknownOutcome, WorkbookClient
from test_export_unevaluable import ServiceCase


class ReadOnlyAdmissionTests(ServiceCase):
    def test_a_read_only_handle_rejects_with_the_operation_id(self):
        path = self.temp/"book.dhwb"
        self.client.open(path, create=True).close()
        info = self.client.request("/v1/open", {"path": str(path), "readonly": True})
        request = {"operation_id": "op-read-only", "expected_state": info["state"], "action": "edit", "sheet": "Sheet1",
                   "cells": [{"row": 1, "col": 1, "source": "1"}]}
        with self.assertRaises(APIError) as raised:
            self.client.request(f"/v1/workbooks/{info['id']}/operations", request)
        body = raised.exception.body
        self.assertEqual((raised.exception.status, body["code"], body["outcome"], body["operation_id"]), (403, "READ_ONLY", "rejected", "op-read-only"))
        # The SDK treats it as a definite refusal: no pending operation is left to reconcile.
        book = WorkbookClient(self.client, {**info, "readonly": False})
        with self.assertRaises(APIError) as refused:
            book.set(1, 1, "1")
        self.assertNotIsInstance(refused.exception, UnknownOutcome)
        self.assertIsNone(book.pending_operation)
        self.assertEqual(refused.exception.body["outcome"], "rejected")


if __name__ == "__main__":
    unittest.main()
