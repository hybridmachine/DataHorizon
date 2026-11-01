import json
from http.client import IncompleteRead
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from datahorizon.client import APIError, LocalService, UnknownOutcome


class ServiceTests(unittest.TestCase):
    def test_rest_sdk_cli_share_managed_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "parity.dhwb"
            with LocalService(Path(__file__).parent / "fixtures" / "validation_hook.py") as client:
                book = client.open(path, create=True)
                book.set(1, 1, "8")
                self.assertEqual(book.read(r2=1, c2=2)["cells"][1]["value"], 16)
                env = {**os.environ, "DATAHORIZON_TOKEN": client.token}
                run = subprocess.run([sys.executable, "-m", "datahorizon.cli", "--url", client.url, str(path), "set", "A1", "9"],
                                     env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(book.read(r2=1, c2=2)["cells"][1]["value"], 18)
                book.refresh()
                with self.assertRaises(APIError) as rejected:
                    book.set(1, 1, "-1")
                self.assertEqual(rejected.exception.body["code"],"HOOK_FAILED")
                self.assertEqual(rejected.exception.body["outcome"],"rolled_back")
                book.refresh()
                self.assertEqual(book.read(r2=1, c2=2)["cells"][0]["value"], 9)
                with self.assertRaises(HTTPError) as raised:
                    urlopen(client.url + "/v1/capabilities")
                self.assertEqual(raised.exception.code, 401)
                raised.exception.close()
                request = Request(client.url + "/v1/capabilities", headers={"Authorization": "Bearer " + client.token, "Origin": "https://example.com"})
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)
                self.assertEqual(raised.exception.code, 403)
                raised.exception.close()
                with urlopen(client.url + "/openapi.json") as response:
                    self.assertEqual(json.load(response)["openapi"], "3.0.3")
                preview = client.request("/v1/import/csv/preview", {"csv": "ID,Formula\n00123,=1+1"})
                self.assertEqual(preview["rows"][1], ["00123", "=1+1"])

    def test_headless_cli_file_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "cli.dhwb")
            for command in (["create"], ["set", "A1", "=SUM(7,13.5)"], ["read", "A1"]):
                result = subprocess.run([sys.executable, "-m", "datahorizon.cli", path, *command], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["cells"][0]["value"], 20.5)

    def test_client_handles_release_without_closing_other_clients(self):
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            path=Path(temp)/"leases.dhwb"
            first=client.open(path,create=True)
            second=client.open(path)
            self.assertNotEqual(first.info["id"],second.info["id"])
            first.close()
            second.set(1,1,"7")
            self.assertEqual(second.read(r2=1,c2=1)["cells"][0]["value"],7)
            second.close()
            with client.open(path) as reopened:
                self.assertEqual(reopened.read(r2=1,c2=1)["cells"][0]["value"],7)

    def test_lost_reply_exposes_id_and_blocks_further_sdk_writes(self):
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            book=client.open(Path(temp)/"lost.dhwb",create=True)
            original=client.request
            def lose_acknowledgement(path,body=None):
                result=original(path,body)
                if path.endswith("/operations"):
                    raise ConnectionError("Acknowledgement discarded after server commit")
                return result
            client.request=lose_acknowledgement
            with self.assertRaises(UnknownOutcome) as unknown:
                book.set(1,1,"7")
            operation_id=unknown.exception.operation_id
            with self.assertRaises(UnknownOutcome):
                book.set(1,1,"8")
            client.request=original
            self.assertEqual(book.outcome(operation_id)["outcome"],"committed")
            self.assertEqual(book.read(r2=1,c2=1)["cells"][0]["value"],7)
            book.set(1,1,"8")
            self.assertEqual(book.read(r2=1,c2=1)["cells"][0]["value"],8)

    def test_receipts_with_url_delimiters_are_retrievable(self):
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            with client.open(Path(temp)/"ids.dhwb",create=True) as book:
                for operation_id in ("batch/1?name=é#%", "literal%2Fsegment"):
                    response=book.apply("edit",operation_id=operation_id,cells=[{"row":1,"col":1,"source":operation_id}])
                    self.assertEqual(book.outcome(operation_id),response)

    def test_incomplete_or_invalid_receipts_keep_pending_operation(self):
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            book=client.open(Path(temp)/"invalid-receipts.dhwb",create=True)
            original=client.request
            for invalid in ("truncated",None,[],{},"wrong_id","missing_state"):
                with self.subTest(reply=invalid):
                    def break_receipt(path,body=None):
                        result=original(path,body)
                        if not path.endswith("/operations"):
                            return result
                        if invalid == "truncated":
                            raise IncompleteRead(b'{"operation_id":',100)
                        if invalid == "wrong_id":
                            return {**result,"operation_id":"wrong"}
                        if invalid == "missing_state":
                            result.pop("state")
                            return result
                        return invalid
                    client.request=break_receipt
                    with self.assertRaises(UnknownOutcome) as unknown:
                        book.set(1,1,"7")
                    self.assertEqual(book.pending_operation,unknown.exception.operation_id)
                    with self.assertRaises(UnknownOutcome):
                        book.set(1,1,"8")
                    client.request=original
                    self.assertEqual(book.outcome(unknown.exception.operation_id)["outcome"],"committed")
            book.close()

    def test_cli_apply_accepts_complete_operation_with_sheet(self):
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            path=Path(temp)/"apply.dhwb"
            with client.open(path,create=True) as book:
                book.apply("sheet_add",name="Other")
                request={"action":"edit","sheet":"Other","operation_id":"cli/apply","expected_state":book.state,
                         "cells":[{"row":1,"col":1,"source":"7"}]}
                result=subprocess.run([sys.executable,"-m","datahorizon.cli","--url",client.url,str(path),"apply"],
                                      input=json.dumps(request),env={**os.environ,"DATAHORIZON_TOKEN":client.token},capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(book.read(sheet="Other",r2=1,c2=1)["cells"][0]["value"],7)

    def test_receipt_lookup_survives_owned_service_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"restart.dhwb"
            with LocalService() as client:
                book=client.open(path,create=True)
                receipt=book.set(1,1,"7")
            with LocalService() as client:
                with client.open(path) as reopened:
                    self.assertEqual(reopened.outcome(receipt["operation_id"]),receipt)
                    self.assertEqual(reopened.read(r2=1,c2=1)["cells"][0]["value"],7)

    def test_invalid_cli_addresses_return_structured_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            for command in (["read","XFE1"],["set","A0","7"],["read","A"+"9"*5000]):
                with self.subTest(command=command[0]):
                    result=subprocess.run([sys.executable,"-m","datahorizon.cli",str(Path(temp)/"unused.dhwb"),*command],capture_output=True,text=True,timeout=10)
                    self.assertEqual(result.returncode,1)
                    self.assertEqual(json.loads(result.stderr)["code"],"INVALID_ADDRESS")
                    self.assertNotIn("Traceback",result.stderr)

    def test_clipboard_read_uses_bulk_limit_in_one_consistent_state(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            with client.open(Path(temp)/"clipboard.dhwb",create=True) as book:
                book.set(10000,1,"tail")
                with self.assertRaises(APIError):
                    book.read(r2=4097,c2=1)
                clipboard=client.request(book.base+"/clipboard",{"r1":1,"r2":10000,"c1":1,"c2":1})
                self.assertEqual(clipboard["state"],book.state)
                self.assertEqual(clipboard["cells"][0]["source"],"tail")
                with self.assertRaises(APIError):
                    client.request(book.base+"/clipboard",{"r2":10001,"c2":1})


if __name__ == "__main__":
    unittest.main()
