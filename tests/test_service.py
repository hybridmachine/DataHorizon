import json
import io
from contextlib import closing, redirect_stderr, redirect_stdout
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sqlite3
from socketserver import TCPServer
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from datahorizon.client import APIError, Client, LocalService, UnknownOutcome, WorkbookClient
from datahorizon import cli
from datahorizon.engine import EngineError, Workbook
from datahorizon.service import Service


class ServiceTests(unittest.TestCase):
    def test_cli_preserves_a_committed_receipt_when_handle_release_fails(self):
        class ReleaseFailure(BaseHTTPRequestHandler):
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status=200
                if self.path=="/v1/open": result={"id":"test","state":"initial"}
                elif self.path.endswith("/operations"):
                    self.server.last_operation=request["operation_id"]
                    result={"operation_id":request["operation_id"],"outcome":"committed","state":"updated","changed_cells":1}
                else:
                    status=503; result={"code":"RELEASE_FAILED","message":"Injected release failure","outcome":"rejected"}
                payload=json.dumps(result).encode()
                closing=self.path.endswith("/close")
                if closing and self.server.failure=="json": status=200; payload=b'not-json'
                truncated=(closing and self.server.failure=="truncated") or (self.path.endswith("/operations") and self.server.failure=="unknown")
                if truncated: status=200
                self.send_response(status)
                self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(payload)+(1000 if truncated else 0)))
                self.end_headers(); self.wfile.write(payload)
            def log_message(self,*args): pass
        class Server(ThreadingHTTPServer):
            def server_bind(self): TCPServer.server_bind(self)
        with tempfile.TemporaryDirectory() as temp,Server(("127.0.0.1",0),ReleaseFailure) as server:
            worker=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.02},daemon=True); worker.start()
            try:
                for failure in ("http","json","truncated","unknown"):
                    with self.subTest(failure=failure):
                        server.failure=failure
                        run=subprocess.run([sys.executable,"-m","datahorizon.cli","--url",f"http://127.0.0.1:{server.server_address[1]}",str(Path(temp)/"test.dhwb"),"set","A1","7"],
                                           env={**os.environ,"DATAHORIZON_TOKEN":"test"},capture_output=True,text=True,timeout=10)
                        diagnostic=json.loads(run.stderr)
                        if failure=="unknown":
                            self.assertEqual(run.returncode,1); self.assertEqual(run.stdout,"")
                            self.assertEqual(diagnostic["outcome"],"unknown")
                            self.assertEqual(diagnostic["operation_id"],server.last_operation)
                        else:
                            self.assertEqual(run.returncode,0,run.stderr)
                            receipt=json.loads(run.stdout)
                            self.assertEqual(receipt["outcome"],"committed")
                            self.assertEqual(diagnostic["code"],"CLEANUP_FAILED")
                            self.assertEqual(diagnostic["phase"],"cleanup")
                            self.assertEqual(diagnostic["operation_id"],receipt["operation_id"])
                            self.assertEqual(diagnostic["operation_outcome"],"committed")
                            self.assertNotIn("outcome",diagnostic)
            finally:
                server.shutdown(); worker.join(3)

    def test_direct_cli_owns_cte_mutations_until_output_can_be_serialized(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"cte.db"
            with closing(sqlite3.connect(path)) as setup,setup:
                setup.execute("CREATE TABLE data(value)"); setup.execute("INSERT INTO data VALUES(1)")
            def run(sql):
                return subprocess.run([sys.executable,"-m","datahorizon.cli",str(path),"direct-sql",sql],capture_output=True,text=True,timeout=10)
            failed=run("/* leading comment */ WITH selected AS (SELECT 1) UPDATE data SET value=1e999 RETURNING value")
            self.assertEqual(failed.returncode,1); self.assertEqual(failed.stdout,"")
            self.assertEqual(json.loads(failed.stderr)["outcome"],"rejected")
            with closing(sqlite3.connect(path)) as reader:
                self.assertEqual(reader.execute("SELECT value FROM data").fetchall(),[(1,)])
            success=run("WITH selected(value) AS (SELECT 2) INSERT INTO data SELECT value FROM selected RETURNING value")
            self.assertEqual(success.returncode,0,success.stderr)
            self.assertEqual(json.loads(success.stdout)["rows"],[[2]])
            for sql in ("CREATE TABLE extra(value)","ALTER TABLE extra ADD COLUMN note TEXT","DROP TABLE extra"):
                result=run(sql); self.assertEqual(result.returncode,0,result.stderr)
            with closing(sqlite3.connect(path)) as reader:
                self.assertEqual(reader.execute("SELECT value FROM data ORDER BY value").fetchall(),[(1,),(2,)])

    def test_direct_cli_rejects_commands_outside_its_owned_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"transaction.db"; attached=Path(temp)/"attached.db"; vacuum=Path(temp)/"vacuum.db"
            with closing(sqlite3.connect(path)) as setup,setup:
                setup.execute("CREATE TABLE data(value)"); setup.execute("INSERT INTO data VALUES(1)")
            statements=("BEGIN","COMMIT","ROLLBACK","SAVEPOINT user_savepoint","RELEASE user_savepoint", "ROLLBACK TO user_savepoint",
                        "PRAGMA journal_mode=WAL","ATTACH DATABASE '"+str(attached).replace("'","''")+"' AS other", "DETACH DATABASE main", "VACUUM", "VACUUM INTO '"+str(vacuum).replace("'","''")+"'")
            for sql in statements:
                with self.subTest(sql=sql):
                    run=subprocess.run([sys.executable,"-m","datahorizon.cli",str(path),"direct-sql",sql],capture_output=True,text=True,timeout=10)
                    self.assertEqual(run.returncode,1); self.assertEqual(run.stdout,"")
                    error=json.loads(run.stderr)
                    self.assertEqual(error["outcome"],"rejected")
                    if not sql.startswith("VACUUM"): self.assertEqual(error["code"],"UNSUPPORTED_DIRECT_SQL")
            self.assertFalse(attached.exists()); self.assertFalse(vacuum.exists())
            with closing(sqlite3.connect(path)) as reader:
                self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0],"delete")
                self.assertEqual(reader.execute("SELECT value FROM data").fetchall(),[(1,)])

    def test_openapi_action_examples_execute_every_advertised_operation(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            spec=client.request("/openapi.json")
            schemas=spec["components"]["schemas"]
            operation=schemas["OperationRequest"]
            mapping=operation["discriminator"]["mapping"]
            self.assertEqual(set(mapping),set(client.request("/v1/capabilities")["operations"]))
            self.assertEqual({item["$ref"] for item in operation["oneOf"]},set(mapping.values()))
            for path,methods in spec["paths"].items():
                if "post" in methods:
                    schema=methods["post"]["requestBody"]["content"]["application/json"]["schema"]
                    self.assertIn(schema["$ref"].rsplit("/",1)[-1],schemas,path)
            with client.open(Path(temp)/"examples.dhwb",create=True) as book:
                sequence=("edit","paste","fill","move","replace_all","undo","redo", "sheet_add","sheet_rename","sheet_duplicate","sheet_reorder","sheet_delete")
                self.assertEqual(set(sequence),set(mapping))
                for action in sequence:
                    with self.subTest(action=action):
                        schema=schemas[mapping[action].rsplit("/",1)[-1]]
                        request={**schema["example"],"operation_id":"openapi-"+action,"expected_state":book.state}
                        self.assertEqual(request["action"],action)
                        self.assertEqual(schema["properties"]["action"]["enum"],[action])
                        self.assertTrue(set(schema["required"]).issubset(request))
                        self.assertTrue(set(request).issubset(schema["properties"]))
                        if action=="move": request["cut_state"]=book.state
                        if action=="sheet_reorder": request["order"]=[sheet["id"] for sheet in reversed(book.info["sheets"])]
                        result=client.request(book.base+"/operations",request)
                        self.assertEqual(result["outcome"],"committed")
                        book.refresh()

    def test_direct_cli_reports_results_only_after_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"locked.db"
            connect=sqlite3.connect
            with closing(connect(path)) as setup,setup:
                setup.execute("CREATE TABLE data(value)"); setup.execute("INSERT INTO data VALUES(1)")
            with closing(connect(path)) as reader:
                reader.execute("BEGIN"); reader.execute("SELECT * FROM data").fetchall()
                stdout,stderr=io.StringIO(),io.StringIO()
                def limited_connect(*args,**kwargs): return connect(*args,**{**kwargs,"timeout":0.02})
                with patch.object(sys,"argv",["datahorizon",str(path),"direct-sql","INSERT INTO data VALUES(2) RETURNING value"]), \
                        patch("datahorizon.cli.sqlite3.connect",side_effect=limited_connect),redirect_stdout(stdout),redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as failure: cli.main()
                self.assertEqual(failure.exception.code,1)
                self.assertEqual(stdout.getvalue(),"")
                self.assertEqual(json.loads(stderr.getvalue())["code"],"CLIENT_ERROR")
                self.assertEqual(reader.execute("SELECT value FROM data").fetchall(),[(1,)])
            run=subprocess.run([sys.executable,"-m","datahorizon.cli",str(path),"direct-sql","INSERT INTO data VALUES(3) RETURNING value"],capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(json.loads(run.stdout)["rows"],[[3]])
            invalid=subprocess.run([sys.executable,"-m","datahorizon.cli",str(path),"direct-sql","UPDATE data SET value=1e999 RETURNING value"],capture_output=True,text=True,timeout=10)
            self.assertEqual(invalid.returncode,1); self.assertEqual(invalid.stdout,"")
            self.assertEqual(json.loads(invalid.stderr)["code"],"CLIENT_ERROR")
            with closing(connect(path)) as reader:
                self.assertEqual(reader.execute("SELECT value FROM data ORDER BY value").fetchall(),[(1,),(3,)])

    def test_cli_reports_truncated_http_reads_as_json(self):
        class Truncated(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length",0)))
                result={"id":"test","state":"test"} if self.path=="/v1/open" else {"closed":True}
                payload=json.dumps(result).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(payload)+1000 if self.path.endswith("/range") else len(payload)))
                self.end_headers(); self.wfile.write(payload)
            def log_message(self,*args): pass
        class Server(ThreadingHTTPServer):
            def server_bind(self): TCPServer.server_bind(self)
        with tempfile.TemporaryDirectory() as temp,Server(("127.0.0.1",0),Truncated) as server:
            worker=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.02},daemon=True); worker.start()
            try:
                run=subprocess.run([sys.executable,"-m","datahorizon.cli","--url",f"http://127.0.0.1:{server.server_address[1]}",str(Path(temp)/"test.dhwb"),"read","A1"],
                                   env={**os.environ,"DATAHORIZON_TOKEN":"test"},capture_output=True,text=True,timeout=10)
                self.assertEqual(run.returncode,1)
                self.assertEqual(run.stdout,"")
                error=json.loads(run.stderr)
                self.assertEqual(error["code"],"CLIENT_ERROR")
                self.assertEqual(error["outcome"],"rejected")
                self.assertIn("bytes read",error["message"])
            finally:
                server.shutdown(); worker.join(3)

    def test_sdk_and_cli_bypass_ambient_proxies(self):
        intercepted=[]
        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                intercepted.append((self.path,self.headers.get("Authorization"),self.rfile.read(int(self.headers.get("Content-Length",0)))))
                self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(b'{"proxy":true}')
            do_POST=do_GET
            def log_message(self,*args): pass
        class Server(ThreadingHTTPServer):
            def server_bind(self): TCPServer.server_bind(self)
        with tempfile.TemporaryDirectory() as temp,Server(("127.0.0.1",0),Proxy) as proxy:
            worker=threading.Thread(target=proxy.serve_forever,kwargs={"poll_interval":0.02},daemon=True); worker.start()
            proxy_url=f"http://127.0.0.1:{proxy.server_address[1]}"
            settings={name:proxy_url for name in ("http_proxy","HTTP_PROXY","https_proxy","HTTPS_PROXY","all_proxy","ALL_PROXY")}
            settings.update(no_proxy="",NO_PROXY="")
            try:
                with patch.dict(os.environ,settings),patch("urllib.request._opener",None),LocalService() as client:
                    capabilities=client.request("/v1/capabilities")
                    self.assertEqual(len(intercepted),0,"A proxy received a localhost bearer token")
                    self.assertEqual(capabilities["api_version"],1)
                    path=Path(temp)/"private.dhwb"
                    with client.open(path,create=True) as book:
                        book.set(1,1,"private SDK contents")
                        run=subprocess.run([sys.executable,"-m","datahorizon.cli","--url",client.url,str(path),"set","A1","private CLI contents"],
                                           env={**os.environ,"DATAHORIZON_TOKEN":client.token},capture_output=True,text=True,timeout=10)
                        self.assertEqual(run.returncode,0,run.stderr)
                        self.assertEqual(book.read(r2=1,c2=1)["cells"][0]["value"],"private CLI contents")
                    self.assertEqual(len(intercepted),0,"A proxy received credentials or workbook contents")
            finally:
                proxy.shutdown(); worker.join(3)

    def test_sdk_never_follows_service_redirects(self):
        intercepted=[]
        class Receiver(BaseHTTPRequestHandler):
            def do_GET(self):
                intercepted.append(self.command)
                self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(b'{}')
            do_POST=do_GET
            def log_message(self,*args): pass
        class Server(ThreadingHTTPServer):
            def server_bind(self): TCPServer.server_bind(self)
        with Server(("127.0.0.1",0),Receiver) as receiver:
            class Redirect(Receiver):
                def do_GET(self):
                    self.rfile.read(int(self.headers.get("Content-Length",0)))
                    self.send_response(int(self.path[1:]) if self.path[1:].isdigit() else 307)
                    self.send_header("Location",f"http://127.0.0.1:{receiver.server_address[1]}/capture")
                    self.send_header("Content-Type","application/json"); self.end_headers()
                    self.wfile.write(b'{"operation_id":"redirected-operation","outcome":"rolled_back","code":"FAKE","message":"Not a terminal receipt"}')
                do_POST=do_GET
            with Server(("127.0.0.1",0),Redirect) as origin:
                servers=(origin,receiver)
                workers=[threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.02},daemon=True) for server in servers]
                for worker in workers: worker.start()
                try:
                    client=Client(f"http://127.0.0.1:{origin.server_address[1]}","redirect-test-token")
                    for status in (301,302,303,307,308):
                        for body in (None,{"private":"workbook contents"}):
                            with self.subTest(status=status,body=body):
                                with self.assertRaisesRegex(ValueError,"redirect"):
                                    client.request(f"/{status}",body)
                                self.assertEqual(len(intercepted),0)
                    book=WorkbookClient(client,{"id":"test","state":"state"})
                    with self.assertRaises(UnknownOutcome):
                        book.apply("edit",operation_id="redirected-operation",cells=[{"row":1,"col":1,"source":"private"}])
                    self.assertEqual(book.pending_operation,"redirected-operation")
                    self.assertEqual(len(intercepted),0)
                finally:
                    for server in servers: server.shutdown()
                    for worker in workers: worker.join(3)

    def test_loopback_startup_does_not_depend_on_hostname_resolution(self):
        with patch("socket.getfqdn",side_effect=AssertionError("Loopback startup must not resolve hostnames")),Service() as server:
            self.assertEqual(server.server_name,"localhost")
            self.assertEqual(server.server_address[0],"127.0.0.1")
            self.assertGreater(server.server_port,0)

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

    def test_owned_service_ignores_working_directory_and_pythonpath_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); previous=Path.cwd(); handler=Path(__file__).resolve().parent/"fixtures"/"validation_hook.py"
            for attack in ("cwd","pythonpath"):
                with self.subTest(attack=attack):
                    work=root/attack; work.mkdir(); ambient=work/"ambient"; ambient.mkdir()
                    marker=work/"executed"
                    payload=f"from pathlib import Path\nPath({str(marker)!r}).write_text('untrusted import')\nraise RuntimeError('untrusted import')\n"
                    if attack=="cwd":
                        package=work/"datahorizon"; package.mkdir(); (package/"__init__.py").write_text(payload)
                    else:
                        (ambient/"json.py").write_text(payload)
                    try:
                        os.chdir(work)
                        with patch.dict(os.environ,{"PYTHONPATH":str(ambient)+os.pathsep}):
                            try: owned=LocalService(handler)
                            except RuntimeError:
                                self.assertFalse(marker.exists(),"Owned service imported an ambient module")
                                raise
                            with owned as client:
                                self.assertFalse(marker.exists())
                                self.assertEqual(client.request("/v1/capabilities")["api_version"],1)
                                with client.open(work/"trusted.dhwb",create=True) as book:
                                    book.set(1,1,"7")
                                    self.assertEqual(book.read(r2=1,c2=2)["cells"][1]["value"],14)
                    finally:
                        os.chdir(previous)

    def test_failed_open_does_not_publish_a_handle_or_leak_an_engine(self):
        with tempfile.TemporaryDirectory() as temp,Service() as server:
            path=Path(temp)/"failed-open.dhwb"; created=[]
            def construct(*args,**kwargs):
                book=Workbook(*args,**kwargs); created.append(book); return book
            with patch("datahorizon.service.Workbook",side_effect=construct),patch.object(Workbook,"info",side_effect=EngineError("TEST_FAILURE","Injected metadata failure")):
                with self.assertRaises(EngineError): server.dispatch("POST","/v1/open",{"path":str(path),"create":True})
            self.assertEqual(server.workbooks,{})
            with self.assertRaises(sqlite3.ProgrammingError): created[0].db.execute("SELECT 1")
            info=server.dispatch("POST","/v1/open",{"path":str(path)})
            book=server.workbooks[info["id"]]
            with patch.object(book,"info",side_effect=EngineError("TEST_FAILURE","Injected metadata failure")):
                with self.assertRaises(EngineError): server.dispatch("POST","/v1/open",{"path":str(path)})
            self.assertEqual(list(server.workbooks),[info["id"]])
            self.assertEqual(book.db.execute("SELECT 1").fetchone()[0],1)

    def test_headless_cli_file_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "cli.dhwb")
            for command in (["create"], ["set", "A1", "=SUM(7,13.5)"], ["read", "A1"]):
                result = subprocess.run([sys.executable, "-m", "datahorizon.cli", path, *command], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["cells"][0]["value"], 20.5)

    def test_open_requires_boolean_flags_before_creating_or_leasing_a_workbook(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            for field in ("create","readonly"):
                for index,invalid in enumerate(("false","true",0,1,None,[],{})):
                    path=Path(temp)/f"{field}-{index}.dhwb"
                    with self.subTest(field=field,value=invalid):
                        with self.assertRaises(APIError) as failure:
                            client.request("/v1/open",{"path":str(path),"create":True,field:invalid})
                        self.assertEqual(failure.exception.body["code"],"INVALID_ARGUMENT")
                        self.assertFalse(path.exists())
            with client.open(Path(temp)/"valid.dhwb",create=True) as book:
                with self.assertRaises(APIError) as failure:
                    client.request("/v1/open",{"path":book.info["path"],"readonly":"false"})
                self.assertEqual(failure.exception.body["code"],"INVALID_ARGUMENT")
                book.set(1,1,"7")

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

    def test_retry_waiting_for_writer_keeps_pending_until_original_commit_is_reconciled(self):
        with tempfile.TemporaryDirectory() as temp,Service() as original,Service() as retry:
            servers=(original,retry)
            servers_running=[threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.02},daemon=True) for server in servers]
            for thread in servers_running: thread.start()
            release=threading.Event(); writing=threading.Event(); results=[]; failures=[]
            writer=None
            try:
                clients=[Client(f"http://127.0.0.1:{server.server_port}",server.token) for server in servers]
                first=clients[0].open(Path(temp)/"retry.dhwb",create=True)
                second=clients[1].open(first.info["path"])
                retry.workbooks[second.info["id"]].db.execute("PRAGMA busy_timeout=30")
                def hold_commit(sql):
                    if sql=="COMMIT":
                        writing.set(); release.wait(5)
                original.workbooks[first.info["id"]].db.set_trace_callback(hold_commit)
                operation="concurrent-retry"
                cells=[{"row":1,"col":1,"source":"7"}]
                state=first.state
                def write():
                    try: results.append(first.apply("edit",operation_id=operation,cells=cells))
                    except Exception as error: failures.append(error)
                writer=threading.Thread(target=write,daemon=True); writer.start()
                self.assertTrue(writing.wait(5),"Original operation never reached its uncommitted receipt")
                with self.assertRaises(UnknownOutcome):
                    second.apply("edit",operation_id=operation,expected_state=state,cells=cells)
                self.assertEqual(second.pending_operation,operation)
                with self.assertRaises(UnknownOutcome): second.set(2,1,"wrong")
                with self.assertRaises(UnknownOutcome): second.outcome(operation)
                self.assertEqual(second.pending_operation,operation)
                release.set(); writer.join(5)
                self.assertFalse(writer.is_alive()); self.assertEqual(failures,[])
                self.assertEqual(second.outcome(operation),results[0])
                self.assertIsNone(second.pending_operation)
                self.assertEqual(second.read(r2=1,c2=1)["cells"][0]["value"],7)
                second.set(2,1,"9")
                first.close(); second.close()
            finally:
                release.set()
                if writer is not None: writer.join(5)
                for server in servers: server.shutdown()
                for thread in servers_running: thread.join(3)

    def test_unmatched_error_responses_never_clear_pending_operation(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            original=client.request
            for index,identity in enumerate((None,"different-operation")):
                book=client.open(Path(temp)/f"generic-error-{index}.dhwb",create=True)
                body={"code":"STORAGE_ERROR","message":"database is locked","outcome":"rejected"}
                if identity is not None: body["operation_id"]=identity
                def reject(path,request=None):
                    if path.endswith("/operations"): raise APIError(409,body)
                    return original(path,request)
                with patch.object(client,"request",side_effect=reject):
                    with self.assertRaises(UnknownOutcome) as failure: book.set(1,1,"7")
                self.assertEqual(book.pending_operation,failure.exception.operation_id)
                with self.assertRaises(UnknownOutcome): book.set(2,1,"wrong")

    def test_local_write_preconditions_do_not_create_pending_operations(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/"preflight.dhwb"
            with client.open(path,create=True) as book:
                with patch.object(client,"request",side_effect=AssertionError("Local rejection must not send a request")):
                    with self.assertRaises(ValueError):
                        book.apply("edit",cells=[{"row":1,"col":1,"source":"x"*(4*1024*1024)}])
                    self.assertIsNone(book.pending_operation)
                    for operation in ("",0,False,[],{},"x"*101,"\ud800"):
                        with self.subTest(operation_id=operation):
                            with self.assertRaises(ValueError): book.apply("edit",operation_id=operation,cells=[])
                            self.assertIsNone(book.pending_operation)
            with client.open(path,readonly=True) as book:
                with patch.object(client,"request",side_effect=AssertionError("Read-only mutation must not be sent")):
                    with self.assertRaises(APIError) as failure: book.set(1,1,"7")
                    self.assertEqual(failure.exception.body["code"],"READ_ONLY")
                    self.assertIsNone(book.pending_operation)

    def test_invalid_mutations_keep_durable_failure_receipts_and_release_sdk_writes(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            with client.open(Path(temp)/"invalid-mutations.dhwb",create=True) as book:
                book.apply("edit",cells=[{"row":1,"col":1,"source":"original","style":{"bold":True}},
                                         {"row":1,"col":2,"source":"1"}])
                invalid=[
                    ("edit",{"cells":[{"row":2,"col":1,"source":"partial"},{"row":1,"col":1,"style":None}]}),
                    ("sheet_reorder",{"order":[{}]}),
                    ("paste",{"row":2,"col":1,"rows":[[{"source":None,"input_kind":"auto"}]]}),
                    ("paste",{"row":2,"col":1,"origin_row":"bad","rows":[[{"source":"=B1","input_kind":"formula"}]]}),
                    ("fill",{"r1":1,"r2":2,"c1":2,"c2":2,"mode":"series","step":10**500}),
                    ("edit",{"cells":[{"row":2,"col":1,"source":"\ud800"}]}),
                    ("edit",{"label":{},"cells":[{"row":2,"col":1,"source":"bad label"}]}),
                    ("edit",{"sheet":{},"cells":[{"row":2,"col":1,"source":"bad sheet"}]}),
                    ("paste",{"row":2,"col":1,"mode":"values","rows":[[{"value":{"nested":"object"}}]]}),
                ]
                for index,(action,arguments) in enumerate(invalid):
                    with self.subTest(action=action,arguments=arguments):
                        before=book.read(r2=2,c2=2)["cells"]
                        with self.assertRaises(APIError) as failure: book.apply(action,**arguments)
                        error=failure.exception
                        self.assertEqual(error.status,400)
                        self.assertEqual(error.body["outcome"],"rolled_back")
                        self.assertIn(error.body["code"],("INVALID_ARGUMENT","INVALID_STYLE"))
                        self.assertIsNone(book.pending_operation)
                        self.assertEqual(book.outcome(error.body["operation_id"]),error.body)
                        self.assertEqual(book.read(r2=2,c2=2)["cells"],before)
                        book.set(3,1,str(index))

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

    def test_direct_cli_honors_readonly_and_returns_structured_sqlite_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"direct.db"
            with closing(sqlite3.connect(path)) as direct,direct:
                direct.execute("CREATE TABLE data(value)")
                direct.execute("INSERT INTO data VALUES(7)")
            def run(sql,file=path):
                return subprocess.run([sys.executable,"-m","datahorizon.cli","--readonly",str(file),"direct-sql",sql],capture_output=True,text=True,timeout=10)
            for sql in ("UPDATE data SET value=9","DELETE FROM data"):
                rejected=run(sql)
                self.assertEqual(rejected.returncode,1)
                error=json.loads(rejected.stderr)
                self.assertEqual(error["code"],"READ_ONLY")
                self.assertEqual(error["outcome"],"rejected")
            selected=run("SELECT value FROM data")
            self.assertEqual(selected.returncode,0,selected.stderr)
            self.assertEqual(json.loads(selected.stdout)["rows"],[[7]])
            missing=path.with_name("missing.db")
            self.assertEqual(run("SELECT 1",missing).returncode,1)
            self.assertFalse(missing.exists())

    def test_csv_preview_bounds_blank_records_as_well_as_fields(self):
        with LocalService() as client:
            accepted=client.request("/v1/import/csv/preview",{"csv":"\n"*10000})
            self.assertEqual(len(accepted["rows"]),10000)
            self.assertEqual(accepted["cells"],0)
            for csv in ("\n"*10001,"value\n"+"\n"*10000):
                with self.assertRaises(APIError) as failure:
                    client.request("/v1/import/csv/preview",{"csv":csv})
                self.assertEqual(failure.exception.body["code"],"LIMIT")

    def test_direct_cli_represents_blobs_without_losing_other_sql_types(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"blob.db"
            with closing(sqlite3.connect(path)):
                pass
            result=subprocess.run([sys.executable,"-m","datahorizon.cli","--readonly",str(path),"direct-sql",
                                   "SELECT X'0001FF', X'', NULL, 7, 1.5, 'text'"],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)["rows"],[[{"type":"blob","base64":"AAH/"},
                                                                  {"type":"blob","base64":""},None,7,1.5,"text"]])

    def test_missing_receipt_and_failed_lookup_keep_the_pending_outcome_unknown(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            book=client.open(Path(temp)/"missing-receipt.dhwb",create=True)
            original=client.request
            operation="not-admitted"
            def lose_request(path,body=None):
                if path.endswith("/operations"): raise ConnectionError("Connection lost before admission was confirmed")
                return original(path,body)
            client.request=lose_request
            with self.assertRaises(UnknownOutcome):
                book.apply("edit",operation_id=operation,cells=[{"row":1,"col":1,"source":"7"}])
            client.request=original
            with self.assertRaises(APIError) as missing:
                client.request(book.base+"/operations/"+operation)
            self.assertEqual(missing.exception.body["outcome"],"unknown")
            self.assertEqual(missing.exception.body["operation_id"],operation)
            with self.assertRaises(UnknownOutcome) as lookup:
                book.outcome(operation)
            self.assertEqual(lookup.exception.operation_id,operation)
            self.assertEqual(book.pending_operation,operation)
            with self.assertRaises(UnknownOutcome): book.set(1,1,"8")


if __name__ == "__main__":
    unittest.main()
