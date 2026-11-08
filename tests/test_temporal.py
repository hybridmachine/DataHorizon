import unittest

from datahorizon.temporal import Temporal, from_serial, to_serial


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
