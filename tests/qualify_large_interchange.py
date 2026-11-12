# SPDX-License-Identifier: GPL-3.0-only
"""Measure the large XLSX/ODS interchange path end to end through the SDK and a real LocalService, per phase.

For each size and format: generate the source, then in a fresh service upload -> prepare (mode mixed) -> apply into an
empty workbook -> close the workbook and stop the service; then in a second fresh service open the native file ->
read the first viewport -> export job -> download, and, for the largest size measured, upload, prepare and apply the
exported file into a second workbook and compare the cell counts. Each phase records wall seconds and the peak resident
memory of the service child process (sampled every 50 ms from outside it); each service also reports ru_maxrss at exit.

The ODS source of a size is DataHorizon's own ODS export of that size's imported XLSX workbook (an unmeasured setup
step), because the generator writes XLSX only. Measurements are specific to the host, its disk and its OS cache state,
which is not controlled.
"""
import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"service"))
sys.path.insert(0, str(ROOT/"tests"/"fixtures"))
from datahorizon import engine, service as service_module, xlsx_stream, xlsx_styles  # noqa: E402
from datahorizon.client import LocalService  # noqa: E402
from generate_large_xlsx import generate  # noqa: E402

SIZES = (100_000, 500_000, 1_000_000)
FORMATS = ("xlsx", "ods")
SAMPLE_SECONDS = .05
PHASE_SECONDS = 300
SERVICE_MEMORY_TARGET = 256*1024*1024  # macos-capacity-qualification.md
OPENING_BUDGET_SECONDS = 5  # native-opening-performance.md
VIEWPORT = {"r1": 1, "c1": 1, "r2": 32, "c2": 16}
QUALIFYING = ("upload", "prepare", "create_workbook", "apply", "close", "open", "first_viewport", "export", "download",
              "reimport_upload", "reimport_prepare", "reimport_apply")


class _RUsageInfoV2(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8*16)]+[(name, ctypes.c_uint64) for name in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins", "ri_wired_size",
        "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime", "ri_proc_exit_abstime", "ri_child_user_time",
        "ri_child_system_time", "ri_child_pkg_idle_wkups", "ri_child_interrupt_wkups", "ri_child_pageins",
        "ri_child_elapsed_abstime", "ri_diskio_bytesread", "ri_diskio_byteswritten")]


def resident_reader():
    """A function returning another process's current resident bytes (None once it has gone)."""
    if sys.platform == "darwin":
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        def read(pid):
            info = _RUsageInfoV2()
            return info.ri_resident_size if libproc.proc_pid_rusage(pid, 2, ctypes.byref(info)) == 0 else None
        return read
    if sys.platform.startswith("linux"):
        def read(pid):
            try:
                with open(f"/proc/{pid}/status") as status:
                    for line in status:
                        if line.startswith("VmRSS:"): return int(line.split()[1])*1024
            except OSError: return None
        return read
    raise RuntimeError("Sampling another process's resident memory is implemented for macOS and Linux only")


def maxrss_bytes(usage):
    return usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss*1024


class MeasuredService:
    """A LocalService whose child is sampled per phase; stop() reaps it with wait4 to read its ru_maxrss."""

    def __init__(self, temp, role):
        self.role, self.temp = role, Path(temp)
        self.temp.mkdir(parents=True, exist_ok=True)
        self.read = resident_reader()
        self.phases, self.phase, self.files = [], None, {}
        self.lock, self.done = threading.Lock(), threading.Event()
        previous = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = str(self.temp)  # the service's private staging directory is created here
        try: self.local = LocalService()
        finally:
            if previous is None: os.environ.pop("TMPDIR", None)
            else: os.environ["TMPDIR"] = previous
        self.client, self.pid = self.local.client, self.local.process.pid
        self.baseline = self.read(self.pid)
        self.sampler = threading.Thread(target=self._sample, daemon=True)
        self.sampler.start()

    def _sample(self):
        while not self.done.wait(SAMPLE_SECONDS): self._take()

    def _take(self):
        value = self.read(self.pid)
        with self.lock:
            if value is not None and self.phase is not None:
                self.phase["peak_rss_bytes"] = max(self.phase["peak_rss_bytes"], value)
                self.phase["samples"] += 1
            for path in self.temp.glob("datahorizon-*/*"):
                kind = path.name.split("-", 1)[0]
                try: size = path.stat().st_size
                except OSError: continue
                if size > self.files.get(kind, 0): self.files[kind] = size

    @contextmanager
    def measure(self, name):
        with self.lock:
            self.phase = {"name": name, "service": self.role, "peak_rss_bytes": 0, "samples": 0}
        self._take()
        started = time.perf_counter()
        try:
            yield self.phase
        except BaseException:
            self.phase["failed"] = True
            raise
        finally:
            seconds = time.perf_counter()-started
            self._take()
            with self.lock:
                self.phase["seconds"] = round(seconds, 3)
                self.phases.append(self.phase)
                self.phase = None

    def stop(self):
        """Stop the child and return its ru_maxrss in bytes (None if it could not be reaped here)."""
        self.done.set(); self.sampler.join(timeout=1)
        process, peak = self.local.process, None
        if process.poll() is None:
            process.terminate()
            try:
                _, status, usage = os.wait4(process.pid, 0)
                process.returncode = os.waitstatus_to_exitcode(status)
                peak = maxrss_bytes(usage)
            except ChildProcessError: pass
        self.local.close()
        return peak


def package_sizes(path):
    with zipfile.ZipFile(path) as archive:
        return {"package_bytes": Path(path).stat().st_size, "expanded_bytes": sum(i.file_size for i in archive.infolist())}


def prepare(client, path, fmt):
    """Upload, then prepare as Client.prepare_spreadsheet_file does, but returning each step so it can be timed apart."""
    upload = client.upload(path)["upload_id"]
    job = uuid.uuid4().hex
    def run():
        view = client.start_job("/v1/import/"+fmt+"/jobs", {"job_id": job, "scope": "workbook", "mode": "mixed",
                                                             "timezone_policy": "reject", "upload_id": upload})
        try: return client.finish_job(view, None, "Preparation")
        finally: client.release_upload(upload)
    return job, run


def export(book, fmt):
    job = uuid.uuid4().hex
    view = book.client.start_job(book.base+"/export/"+fmt+"/jobs", {"job_id": job, "mode": "formulas"})
    return book.client.finish_job(view, None, "Export")


def import_into(service, client, source, fmt, native, prefix, record):
    """upload -> prepare -> create an empty workbook -> apply; returns (summary, receipt) and records the stage size."""
    with service.measure(prefix+"upload"): job, run = prepare(client, source, fmt)
    with service.measure(prefix+"prepare"): summary = run()
    stage = next(service.temp.glob(f"datahorizon-*/stage-{job}.stage"), None)
    if stage is not None: record[prefix+"stage_bytes"] = stage.stat().st_size
    with service.measure(prefix+"create_workbook"): book = client.open(native, create=True)
    with service.measure(prefix+"apply"): receipt = book.import_staged(summary)
    return book, summary, receipt


def ods_source(work, size, native, args, log):
    """DataHorizon's ODS export of the size's imported XLSX workbook (importing one first when none was kept)."""
    started = time.perf_counter()
    temp = work/"setup"
    try:
        if native is None:
            xlsx = work/f"setup-{size}.xlsx"
            generate(xlsx, size, args.sheets, args.formula_share, args.strings,
                     long_text_share=args.long_text_share)
            native = work/f"setup-{size}.dhwb"
            with LocalService() as client:
                summary = client.prepare_spreadsheet_file(xlsx, mode="mixed")
                with client.open(native, create=True) as book: book.import_staged(summary)
            xlsx.unlink()
        target = work/f"source-{size}.ods"
        previous = os.environ.get("TMPDIR"); os.environ["TMPDIR"] = str(temp); temp.mkdir(exist_ok=True)
        try:
            with LocalService() as client, client.open(native, readonly=True) as book:
                book.export_spreadsheet_file(target, format="ods")
        finally:
            if previous is None: os.environ.pop("TMPDIR", None)
            else: os.environ["TMPDIR"] = previous
        log(f"  ODS source written in {time.perf_counter()-started:.1f} s")
        return target, {"origin": "DataHorizon ODS export of the imported generated XLSX workbook",
                        "setup_seconds": round(time.perf_counter()-started, 3), **package_sizes(target)}
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def run_case(work, size, fmt, args, *, reimport, kept_native=None, log=print):
    """One size and format; returns the case record (with "error" when a phase failed)."""
    case = {"cells": size, "format": fmt, "phases": [], "services": [], "files": {}, "counts": {}}
    services = []
    native, second = work/f"{fmt}-{size}.dhwb", work/f"{fmt}-{size}-reimport.dhwb"
    exported = work/f"{fmt}-{size}-export.{fmt}"
    def finish(measured, book=None):
        if book is not None and not book._closed: book.close()
        peak = measured.stop()
        case["services"].append({"role": measured.role, "baseline_rss_bytes": measured.baseline, "ru_maxrss_at_exit_bytes": peak,
                                 "staging_file_peaks_bytes": dict(measured.files)})
        case["phases"].extend(measured.phases)
        services.remove(measured)
    try:
        if fmt == "xlsx":
            source = work/f"source-{size}.xlsx"
            started = time.perf_counter()
            case["source"] = generate(source, size, args.sheets, args.formula_share, args.strings,
                     long_text_share=args.long_text_share)
            case["source"]["generate_seconds"] = round(time.perf_counter()-started, 3)
        else:
            try: source, case["source"] = ods_source(work, size, kept_native, args, log)
            except Exception as error:
                error.setup = "ODS source: DataHorizon ODS export of the imported XLSX workbook"
                raise
        log(f"  source: {case['source']['package_bytes']:,} bytes, {case['source']['expanded_bytes']:,} expanded")
        first = MeasuredService(work/f"tmp-{fmt}-{size}-import", "import"); services.append(first)
        book, summary, receipt = import_into(first, first.client, source, fmt, native, "", case["files"])
        case["counts"].update(prepared=summary["stored_cells"], imported=receipt["imported"]["cells"],
                              sheets=receipt["imported"]["sheets"])
        case["report_totals"] = summary["totals"]
        with first.measure("close"): book.close()
        finish(first)
        case["files"]["native_bytes"] = native.stat().st_size
        log(f"  imported {receipt['imported']['cells']:,} cells; native {native.stat().st_size:,} bytes")
        second_service = MeasuredService(work/f"tmp-{fmt}-{size}-reopen", "reopen"); services.append(second_service)
        client = second_service.client
        with second_service.measure("open"): book = client.open(native)
        sheet = summary["sheets"][0]["name"]
        with second_service.measure("first_viewport"): cells = book.read(sheet=sheet, **VIEWPORT)["cells"]
        case["counts"]["first_viewport_cells"] = len(cells)
        with second_service.measure("export"): result = export(book, fmt)
        try:
            with second_service.measure("download"):
                client.download(result["export_id"], exported, expected=result, native=str(native))
        finally: client.release_export(result["export_id"])
        case["files"]["snapshot_bytes"] = second_service.files.get("snapshot")
        case["files"]["export"] = package_sizes(exported)
        log(f"  exported {exported.stat().st_size:,} bytes")
        if reimport:
            again, again_summary, again_receipt = import_into(second_service, client, exported, fmt, second, "reimport_", case["files"])
            again.close()
            case["counts"]["reimported"] = again_receipt["imported"]["cells"]
            case["counts"]["reimport_matches"] = again_receipt["imported"]["cells"] == case["counts"]["imported"]
            log(f"  re-imported {again_receipt['imported']['cells']:,} cells")
        finish(second_service, book)
    except Exception as error:
        case["error"] = {"type": type(error).__name__, "message": str(error)[:2000],
                         "body": getattr(error, "body", None), "during": getattr(error, "setup", None)
                         or next((p["name"] for p in reversed(case["phases"]+[q for m in services for q in m.phases]) if p.get("failed")), None)}
        log(f"  FAILED: {case['error']}")
        for measured in list(services):
            try: finish(measured)
            except Exception: pass
    finally:
        for path in (source if "source" in case else None, second, exported, *work.glob(f"{fmt}-{size}*-journal")):
            if path is not None: Path(path).unlink(missing_ok=True)
        for path in work.glob(f"tmp-{fmt}-{size}-*"): shutil.rmtree(path, ignore_errors=True)
    return case


def judge(case):
    """The qualification rule applied to one case, with its arithmetic."""
    phases = [p for p in case["phases"] if p["name"] in QUALIFYING]
    slowest = max(phases, key=lambda p: p["seconds"], default=None)
    largest = max(phases, key=lambda p: p["peak_rss_bytes"], default=None)
    exits = [s["ru_maxrss_at_exit_bytes"] for s in case["services"] if s["ru_maxrss_at_exit_bytes"] is not None]
    by_name = {p["name"]: p for p in phases}
    opening = by_name["open"]["seconds"]+by_name["first_viewport"]["seconds"] if {"open", "first_viewport"} <= set(by_name) else None
    checks = {
        "completed": "error" not in case,
        "time": slowest is not None and slowest["seconds"] <= PHASE_SECONDS,
        "sampled_memory": largest is not None and largest["peak_rss_bytes"] < SERVICE_MEMORY_TARGET,
        "exit_memory": bool(exits) and max(exits) < SERVICE_MEMORY_TARGET,
        "reopen": opening is not None and opening <= OPENING_BUDGET_SECONDS and case["counts"].get("first_viewport_cells", 0) > 0,
        "counts": case["counts"].get("imported") == case["cells"] and case["counts"].get("reimport_matches", True),
    }
    arithmetic = []
    if slowest: arithmetic.append(f"slowest phase {slowest['name']} {slowest['seconds']} s {'<=' if checks['time'] else '>'} {PHASE_SECONDS} s")
    if largest: arithmetic.append(f"largest sampled service RSS {largest['peak_rss_bytes']:,} B in {largest['name']} "
                                  f"{'<' if checks['sampled_memory'] else '>='} {SERVICE_MEMORY_TARGET:,} B")
    if exits: arithmetic.append(f"largest service ru_maxrss at exit {max(exits):,} B {'<' if checks['exit_memory'] else '>='} {SERVICE_MEMORY_TARGET:,} B")
    if opening is not None: arithmetic.append(f"open {by_name['open']['seconds']} s + first viewport {by_name['first_viewport']['seconds']} s = "
                                              f"{round(opening, 3)} s {'<=' if opening <= OPENING_BUDGET_SECONDS else '>'} {OPENING_BUDGET_SECONDS} s opening budget")
    arithmetic.append(f"imported {case['counts'].get('imported')} of {case['cells']} cells"
                      +(f"; re-imported {case['counts']['reimported']}" if "reimported" in case["counts"] else ""))
    return {"cells": case["cells"], "format": case["format"], "qualifies": all(checks.values()), "checks": checks,
            "slowest_phase": slowest and {"name": slowest["name"], "seconds": slowest["seconds"]},
            "peak_service_rss_bytes": max([largest["peak_rss_bytes"] if largest else 0, *exits]), "arithmetic": arithmetic}


def decide(cases, formats):
    verdicts = [judge(case) for case in cases]
    per_format = {}
    for fmt in formats:
        mine = [v for v in verdicts if v["format"] == fmt]
        passing = sorted(v["cells"] for v in mine if v["qualifies"])
        failing = sorted(v["cells"] for v in mine if not v["qualifies"])
        # The largest measured size below which every measured size qualified.
        largest = max((size for size in passing if all(other > size for other in failing)), default=None)
        per_format[fmt] = {"measured": sorted(v["cells"] for v in mine), "qualified": passing, "not_qualified": failing,
                           "largest_qualifying_cells": largest}
    limits = [entry["largest_qualifying_cells"] for entry in per_format.values()]
    cells = min(limits) if limits and None not in limits else None
    at_limit = [case for case in cases if case["cells"] == cells and "error" not in case]
    sizes = [case["source"] for case in at_limit]+[case["files"]["export"] for case in at_limit if "export" in case["files"]]
    return {"rule": (f"A size qualifies for a format when every phase finishes within {PHASE_SECONDS} s, the service's peak RSS stays "
                     f"under {SERVICE_MEMORY_TARGET:,} bytes in every phase (sampled every {int(SAMPLE_SECONDS*1000)} ms) and at exit "
                     f"(ru_maxrss), the reopened file opens and serves its first viewport within the {OPENING_BUDGET_SECONDS}-second "
                     "opening budget, and every generated cell is imported (and re-imported, where measured)."),
            "cases": verdicts, "formats": per_format, "largest_qualifying_cells_all_formats": cells,
            "measured_at_that_size": {"max_package_bytes": max((s["package_bytes"] for s in sizes), default=None),
                                      "max_expanded_bytes": max((s["expanded_bytes"] for s in sizes), default=None)}}


def git(*arguments):
    try: return subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError): return None


def host():
    description = {"platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count()}
    if sys.platform == "darwin":
        for key, name in (("cpu", "machdep.cpu.brand_string"), ("memory_bytes", "hw.memsize"), ("model", "hw.model")):
            try: value = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError): value = ""
            description[key] = int(value) if value.isdigit() else value
    return description


def limits():
    return {"LARGE_CELLS": xlsx_stream.LARGE_CELLS, "LARGE_EXPANDED": xlsx_stream.LARGE_EXPANDED, "LARGE_PACKAGE": xlsx_stream.LARGE_PACKAGE,
            "LARGE_SHEETS": xlsx_stream.LARGE_SHEETS, "MAX_RECORD_BYTES": xlsx_stream.MAX_RECORD_BYTES,
            "SNAPSHOT_BYTES": engine.SNAPSHOT_BYTES, "MAX_EXPORT_STYLES": xlsx_styles.MAX_STYLES,
            "MAX_UPLOADS": service_module.MAX_UPLOADS, "MAX_DOWNLOADS": service_module.MAX_DOWNLOADS,
            "LARGE_PREPARATION_SECONDS": service_module.LARGE_PREPARATION_SECONDS, "EXPORT_JOB_SECONDS": service_module.EXPORT_JOB_SECONDS,
            "UPLOAD_SECONDS": service_module.UPLOAD_SECONDS, "DOWNLOAD_SECONDS": service_module.DOWNLOAD_SECONDS,
            "MAX_CELLS": engine.MAX_CELLS, "opening_budget_seconds": OPENING_BUDGET_SECONDS}


def qualify(sizes, formats, work, args, log=print):
    """Run every case under `work`; return the report."""
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(), "host": host(),
              "python": platform.python_version(), "python_executable": sys.executable, "sqlite": sqlite3.sqlite_version,
              "commit": git("rev-parse", "HEAD"), "uncommitted_paths": (git("status", "--porcelain") or "").splitlines(),
              "limits_in_force": limits(), "generator": {"sheets": args.sheets, "formula_share": args.formula_share, "long_text_share": args.long_text_share,
                            "strings": args.strings},
              "sizes": list(sizes), "formats": list(formats), "sample_interval_seconds": SAMPLE_SECONDS,
              "limitations": ["Host-specific: one machine, its disk and an uncontrolled (warm) OS file cache; not OS-cold.",
                              "Peak RSS is the service child's resident memory sampled every 50 ms plus its ru_maxrss at exit; the driver is excluded.",
                              "Phase seconds for jobs include the SDK's polling interval (at most 0.5 s).",
                              "One generated content mix; other workbook shapes (more long text, more formulas, more styles) can cost more."],
              "cases": []}
    largest = max(sizes)
    for size in sorted(sizes):
        kept = None
        for fmt in formats:
            log(f"{fmt} {size:,} cells")
            case = run_case(work, size, fmt, args, reimport=size == largest, kept_native=kept, log=log)
            report["cases"].append(case)
            native = work/f"{fmt}-{size}.dhwb"
            if fmt == "xlsx" and "error" not in case: kept = native
            elif native.exists(): native.unlink()
        if kept is not None: kept.unlink(missing_ok=True)
        for path in work.glob(f"*-{size}*"):
            if path.is_file(): path.unlink()
    report["decision"] = decide(report["cases"], formats)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", default=",".join(map(str, SIZES)), help="comma-separated cell counts")
    parser.add_argument("--formats", default=",".join(FORMATS), help="comma-separated: xlsx, ods")
    parser.add_argument("--quick", action="store_true", help="100,000 cells only (a smoke run)")
    parser.add_argument("--sheets", type=int, default=3)
    parser.add_argument("--formula-share", type=float, default=.05)
    parser.add_argument("--long-text-share", type=float, default=.05)
    parser.add_argument("--strings", choices=("shared", "inline"), default="shared")
    parser.add_argument("--work", type=Path, help="directory for generated files (default: a new temporary directory, removed afterwards)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sizes = [100_000] if args.quick else [int(item) for item in args.sizes.split(",")]
    formats = [item.strip() for item in args.formats.split(",")]
    if not sizes or any(size < 1 for size in sizes) or not formats or set(formats)-set(FORMATS):
        parser.error("Positive sizes and formats from xlsx, ods are required")
    log = lambda text: print(text, file=sys.stderr, flush=True)
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        report = qualify(sizes, formats, args.work.resolve(), args, log)
    else:
        with tempfile.TemporaryDirectory(prefix="dh-capacity-") as temp:
            report = qualify(sizes, formats, Path(temp).resolve(), args, log)
    output = json.dumps(report, indent=2)+"\n"
    if args.output: args.output.write_text(output, encoding="utf-8")
    else: print(output, end="")


if __name__ == "__main__":
    main()
