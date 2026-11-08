import unittest
import tempfile
from pathlib import Path
import sqlite3
import uuid
import json
import os
import subprocess
import sys
from datetime import datetime
from unittest.mock import patch

from datahorizon.temporal import Temporal, from_serial, to_serial
from datahorizon.engine import Workbook, EngineError, display
from datahorizon.client import LocalService, APIError
from datahorizon.formula import Calculator, Parser, FormulaError


def evaluate(source):
    return Calculator(lambda *args:None,lambda name:name,seconds=1).evaluate(Parser(source.removeprefix("=")).parse(),"Sheet1")


class CalendarFunctionTests(unittest.TestCase):
    def test_civil_arithmetic_comparison_and_type_separation(self):
        for formula,expected in (("DATE(1900,3,1)-DATE(1900,2,28)",1),("DATE(2024,2,28)+1","2024-02-29"),("1+DATE(2024,2,29)","2024-03-01"),("DATE(2024,2,29)+.5","2024-02-29T12:00:00"),("DATE(2024,2,29)+TIME(12,0,0)","2024-02-29T12:00:00"),("TIME(23,0,0)+TIME(2,0,0)","01:00:00"),("TIME(12,0,0)-TIME(6,0,0)",.25),("DATE(2024,2,29)<DATE(2024,3,1)",True),("ISTEXT(DATE(2024,2,29))",False),("DATE(2024,2,29)&\"!\"","2024-02-29!")):
            self.assertEqual(evaluate(formula),expected,formula)
        for formula in ("DATE(2024,1,1)*2","DATE(2024,1,1)+DATE(2024,1,1)","1-DATE(2024,1,1)","DATE(2024,1,1)=\"2024-01-01\"","DATE(2024,1,1)=TIME(0,0,0)"):
            with self.assertRaises(FormulaError) as error:evaluate(formula)
            self.assertEqual(error.exception.code,"#VALUE!",formula)
        with self.assertRaises(FormulaError) as error:evaluate("DATE(9999,12,31)+1")
        self.assertEqual(error.exception.code,"#NUM!")

    def test_time_normalization_and_components(self):
        for formula,expected in (("TIME(27,0,0)","03:00:00"),("TIME(0,750,0)","12:30:00"),("TIME(0,0,2000)","00:33:20"),("TIME(12.9,34.9,56.9)","12:34:56"),("HOUR(1.5)",12),("MINUTE(TIME(12,34,56))",34),("SECOND(TIME(12,34,56))",56),("HOUR(DATE(2024,2,29))",0)):
            self.assertEqual(evaluate(formula),expected,formula)
        self.assertEqual(evaluate("TIME(0,0,0)").kind,"time")
        for formula,code in (("TIME(-1,0,0)","#NUM!"),("TIME(32768,0,0)","#NUM!"),("HOUR(-.5)","#NUM!"),("TIME(\"12\",0,0)","#VALUE!"),("SECOND(A1:A2)","#VALUE!"),("YEAR(TIME(1,2,3))","#VALUE!"),("HOUR()","#NAME?")):
            with self.assertRaises(FormulaError) as error:evaluate(formula)
            self.assertEqual(error.exception.code,code,formula)

    def test_date_normalization_and_extraction(self):
        for formula,expected in (("DATE(2024,2,29)","2024-02-29"),("DATE(24,14,0)","1925-01-31"),("DATE(1900,2,29)","1900-03-01"),("DATE(2024,1,-1)","2023-12-30"),("YEAR(DATE(2024,2,29))",2024),("MONTH(61)",3),("DAY(59.9)",28)):
            self.assertEqual(evaluate(formula),expected,formula)
        self.assertEqual(evaluate("DATE(2024,2,29)").kind,"date")
        for formula,code in (("DATE(-1,1,1)","#NUM!"),("DATE(9999,13,1)","#NUM!"),("DATE(1e200,1,1)","#NUM!"),("YEAR(60)","#NUM!"),("YEAR(\"2024-02-29\")","#VALUE!"),("DATE(2024,1)","#NAME?"),("YEAR(A1:A2)","#VALUE!"),("DATE(1/0,1,1)","#DIV/0!")):
            with self.assertRaises(FormulaError) as error:evaluate(formula)
            self.assertEqual(error.exception.code,code,formula)


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
    def test_chronological_sort_and_explicit_typed_filters(self):
        self.apply(cells=[dict(row=row,col=1,source=source,kind=kind) for row,source,kind in ((1,"2024-03-01","date"),(2,"2024-02-29T12:00:00","datetime"),(3,"2024-02-29","date"),(4,"2024-02-29","text"),(5,"=DATE(2024,1,1)","formula"))])
        spec=dict(r2=5,sort=[dict(col=1)])
        self.assertEqual(self.book.list_view("Sheet1",spec)["rows"],[5,3,2,1,4])
        self.assertEqual(self.book.list_view("Sheet1",{**spec,"filters":[dict(col=1,op=">=",value="2024-02-29",value_kind="date")]})["rows"],[3,2,1])
        self.assertEqual(self.book.list_view("Sheet1",{**spec,"filters":[dict(col=1,op="=",value="2024-02-29")]})["rows"],[4])
        with self.assertRaises(EngineError):self.book.list_view("Sheet1",{**spec,"filters":[dict(col=1,op="=",value="2023-02-29",value_kind="date")]})

    def test_hook_clock_and_generated_temporal_upgrade_roll_back_with_parent(self):
        self.apply(cells=[dict(row=1,col=2,source="=NOW()"),dict(row=1,col=3,source="=NOW()")])
        self.book.handler=Path(__file__).parent/"fixtures"/"temporal_hook.py"
        with patch("datahorizon.engine.utc_clock",return_value=datetime(2024,12,31,23,59,59)) as clock:
            with self.assertRaises(EngineError):self.apply(cells=[dict(row=1,col=1,source="-1")])
            self.assertEqual(clock.call_count,1)
            self.assertEqual(self.book.info()["format_version"],1)
            self.assertIsNone(self.book.raw(self.book.sheet("Sheet1"),2,1))
            self.apply(cells=[dict(row=1,col=1,source="1")])
            self.assertEqual(clock.call_count,2)
            self.assertEqual(self.book.info()["format_version"],2)
            self.assertEqual(self.book.read("Sheet1",dict(r1=2,r2=2))["cells"][0]["value_kind"],"date")

    def test_volatile_clock_is_one_sample_per_snapshot_and_never_writes(self):
        self.apply(cells=[dict(row=1,col=1,source="=NOW()"),dict(row=2,col=1,source="=NOW()"),dict(row=3,col=1,source="=TODAY()")])
        state=self.book.state();revision=self.book.info()["state"]
        first=datetime(2024,12,31,23,59,59,123456);second=datetime(2025,1,1)
        with patch("datahorizon.formula.utc_clock",side_effect=[first,second]) as clock:
            cells=self.book.read_workbook()["sheets"][0]["cells"]
            self.assertEqual([c["value"] for c in cells],["2024-12-31T23:59:59.123456"]*2+["2024-12-31"])
            self.assertEqual([c["value_kind"] for c in cells],["datetime","datetime","date"])
            self.assertEqual(clock.call_count,1)
            self.assertEqual(self.book.read("Sheet1",dict(r2=3))["cells"][2]["value"],"2025-01-01")
            self.assertEqual(clock.call_count,2)
        self.assertEqual(self.book.state(),state);self.assertEqual(self.book.info()["state"],revision)
        for formula in ("TODAY(1)","NOW(1)"):
            with self.assertRaises(FormulaError) as error:evaluate(formula)
            self.assertEqual(error.exception.code,"#NAME?")

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

    def test_temporal_formats_preserve_inputs_and_history(self):
        self.apply(cells=[dict(row=1,col=1,source="2024-02-29T12:34:56.123456",kind="datetime")])
        self.apply(cells=[dict(row=1,col=1,style={"format":"date_dmy"})])
        cell=self.book.read("Sheet1",{})["cells"][0]
        self.assertEqual(cell["display"],"29/02/2024");self.assertEqual(cell["value_kind"],"datetime")
        self.apply("undo");self.assertEqual(self.book.read("Sheet1",{})["cells"][0]["display"],"2024-02-29T12:34:56.123456")
        self.assertEqual(display(61,{"format":"date_iso"}),"1900-03-01")
        self.assertEqual(display(60,{"format":"date_iso"}),"####")
        self.assertEqual(display(.5,{"format":"time24"}),"12:00:00")
        self.assertEqual(display("00123",{"format":"date_iso"}),"00123")


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
