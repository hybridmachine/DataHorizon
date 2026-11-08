import unittest
import tempfile
from pathlib import Path
import sqlite3
import uuid
import json
import os
import subprocess
import sys

from datahorizon.temporal import Temporal, from_serial, to_serial
from datahorizon.engine import Workbook, EngineError
from datahorizon.client import LocalService, APIError


class TemporalContractTests(unittest.TestCase):
    def test_strict_iso(self):
        for source, kind in (("2024-02-29","date"),("23:59:59.123400","time"),("0001-01-01T00:00:00","datetime")):
            value=Temporal(source,kind)
            self.assertEqual(value.kind,kind)
        self.assertEqual(Temporal("23:59:59.123400","time"),"23:59:59.1234")
        for source,kind in (("1900-02-29","date"),("24:00:00","time"),("12:30","time"),("2024-01-01T00:00:00Z","datetime"),("2024-01-01T00:00:00-06:00","datetime"),("1/2/24","date")):
            with self.assertRaises(ValueError): Temporal(source,kind)

    def test_excel_epochs_and_fake_leap_day(self):
        for serial,source in ((0,"1899-12-31"),(1,"1900-01-01"),(59,"1900-02-28"),(61,"1900-03-01"),(40729,"2011-07-05")):
            self.assertEqual(from_serial(serial,"date"),source)
            self.assertEqual(to_serial(Temporal(source,"date")),serial)
        self.assertEqual(from_serial(0,"date","1904"),"1904-01-01")
        self.assertEqual(to_serial(Temporal("2011-07-05","date"))-to_serial(Temporal("2011-07-05","date"),"1904"),1462)
        for serial in (60,60.5):
            with self.assertRaises(ValueError): from_serial(serial)
        self.assertEqual(from_serial(.5,"time"),"12:00:00")

    def test_precision_bounds_and_roundtrip(self):
        for source,kind in (("2024-02-29T12:34:56","datetime"),("23:59:59.123456","time"),("0001-01-01","date"),("9999-12-31","date")):
            value=Temporal(source,kind)
            self.assertEqual(from_serial(to_serial(value),kind),value)
        for value in (float("nan"),float("inf"),1e20,True):
            with self.assertRaises(ValueError): from_serial(value)


class TemporalStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/"dates.dhwb"
        self.book=Workbook(self.path,create=True)

    def tearDown(self):
        self.book.close();self.temp.cleanup()

    def apply(self,action="edit",**args):
        return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))

    def test_storage_upgrade_values_copy_history_reopen(self):
        self.assertEqual(self.book.info()["format_version"],1)
        self.apply(cells=[dict(row=1,col=1,source="2024-02-29",kind="date"),dict(row=1,col=2,source="12:34:56.120000",kind="time")])
        cells=self.book.read("Sheet1",dict(c2=2))["cells"]
        self.assertEqual([c["value_kind"] for c in cells],["date","time"])
        self.assertEqual(cells[1]["source"],"12:34:56.120000");self.assertEqual(cells[1]["value"],"12:34:56.12")
        self.assertEqual(self.book.info()["format_version"],2)
        self.apply("paste",row=2,col=1,mode="values",rows=[cells]);self.apply("undo");self.apply("redo")
        self.book.close();self.book=Workbook(self.path)
        self.assertEqual([c["input_kind"] for c in self.book.read("Sheet1",dict(r1=2,r2=2,c2=2))["cells"]],["date","time"])

    def test_invalid_batch_rolls_back_format_upgrade_and_rejects_external_damage(self):
        with self.assertRaises(EngineError):
            self.apply(cells=[dict(row=1,col=1,source="2024-02-29",kind="date"),dict(row=2,col=1,source="1900-02-29",kind="date")])
        self.assertEqual(self.book.info()["format_version"],1)
        self.assertEqual(self.book.read("Sheet1",{})["cells"],[])
        self.apply(cells=[dict(row=1,col=1,source="2024-02-29",kind="date")])
        with sqlite3.connect(self.path) as direct: direct.execute("UPDATE cells SET source='2023-02-29'")
        with self.assertRaises(EngineError): self.book.info()

    def test_v1_temporal_and_unknown_versions_fail_without_migration(self):
        self.apply(cells=[dict(row=1,col=1,source="2024-02-29",kind="date")])
        for version in (1,3):
            with sqlite3.connect(self.path) as direct:direct.execute("UPDATE dh_meta SET format_version=?",(version,))
            with self.assertRaises(EngineError) as caught:self.book.info()
            self.assertEqual(caught.exception.body["code"],"FORMAT_VERSION")


class TemporalInterfaceTests(unittest.TestCase):
    def test_rest_sdk_cli_and_automatic_text(self):
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            path=Path(temp)/"entry.dhwb"
            with client.open(path,create=True) as book:
                for col,source,kind in ((1,"2024-02-29","date"),(2,"12:34:56","time"),(3,"2024-02-29T12:34:56","datetime"),(4,"2024-02-29","auto")):
                    book.set(1,col,source,kind=kind)
                cells=book.read(r2=1,c2=4)["cells"]
                self.assertEqual([c["kind"] for c in cells],["date","time","datetime","text"])
                with self.assertRaises(APIError):book.set(1,1,"2023-02-29",kind="date")
                self.assertEqual(book.read(r2=1,c2=1)["cells"][0]["value"],"2024-02-29")
            result=subprocess.run([sys.executable,"-m","datahorizon.cli",str(path),"set","A2","2025-01-01","--kind","date"],capture_output=True,text=True,env={**os.environ,"PYTHONPATH":str(Path(__file__).resolve().parents[1]/"service")})
            self.assertEqual(result.returncode,0,result.stderr)
            with client.open(path) as book:self.assertEqual(book.read(r1=2,r2=2,c2=1)["cells"][0]["value_kind"],"date")
