# SPDX-License-Identifier: GPL-3.0-only
"""Resolve and render stored charts as cancellable read-only background jobs.

A job reads its chart from a private read-only snapshot, so it never writes the
workbook. A completed job whose starting state is no longer the workbook's
state is reported as ``stale`` and its result is withheld.
"""
from contextlib import closing

from . import chart_render, chart_resolve, charts
from .engine import EngineError, Workbook, require
from .jobs import PreparationJobs
from .temporal import utc_clock

JOB_SECONDS = 30
KINDS = ("resolve", "render")
FIELDS = {"job_id", "expected_state", "chart_id", "kind", "width", "height"}
SIZE_LIMITS = {"width": (64, 4096), "height": (48, 4096)}


def _check_request(request):
    require(isinstance(request, dict), "INVALID_ARGUMENT", "A chart job request must be an object")
    unknown = sorted(set(request) - FIELDS)
    require(not unknown, "INVALID_ARGUMENT", f"Unsupported chart job field: {', '.join(map(str, unknown))}")
    require(isinstance(request.get("chart_id"), str), "INVALID_ARGUMENT", "A chart job needs chart_id as text")
    kind = request.get("kind")
    require(kind in KINDS, "INVALID_ARGUMENT", "kind must be 'resolve' or 'render'")
    for key, (low, high) in SIZE_LIMITS.items():
        if kind == "resolve":
            require(key not in request, "INVALID_ARGUMENT", f"A resolve job takes no {key}")
            continue
        value = request.get(key)
        valid = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
        require(valid, "INVALID_ARGUMENT", f"A render job needs an integer {key} from {low} to {high}")


class ChartJobs:
    def __init__(self, book):
        self.book = book
        self.jobs = PreparationJobs(seconds=JOB_SECONDS)
        self.metadata = {}

    def start(self, request):
        _check_request(request)
        with self.book.lock:
            self.book.refresh()
            state = self.book.state()
            require(request.get("expected_state") == state, "STALE_STATE", "Refresh before resolving the chart", 409)
            chart_id = request["chart_id"]
            charts.find(self.book, chart_id)
            kind = request["kind"]
            size = (request["width"], request["height"]) if kind == "render" else None
            self.metadata = {key: value for key, value in self.metadata.items() if key in self.jobs.entries}
            clock = utc_clock()

            def run(payload, checkpoint):
                with closing(Workbook(self.book.path, readonly=True, cancel_event=checkpoint.cancel_event)) as snapshot:
                    base = snapshot.calculator
                    shared = []

                    # One calculator for the whole job: the snapshot is read-only and each read is
                    # state-checked, so its cache stays valid and a formula chain that crosses a
                    # slice reuses the cells already calculated.
                    def calculator(seconds=.25, **kwargs):
                        if shared:
                            return shared[0]
                        calc = base(seconds=2, clock=clock, transactional=True)

                        # Not the calculator's own check: its 10,000-step budget would turn a large
                        # source into #LIMIT! cells. The job's deadline and cancellation bound it
                        # instead, with each read's own two-second deadline.
                        def checked():
                            calc.count += 1
                            checkpoint(calc.count, "Calculating")

                        calc.check = checked
                        shared.append(calc)
                        return calc

                    snapshot.calculator = calculator
                    checkpoint(0, "Reading snapshot")
                    _, stored = charts.find(snapshot, chart_id)
                    # A snapshot has its own epoch. Its reads are checked against its own state;
                    # access() withholds the result when the workbook has moved on from `state`.
                    result = chart_resolve.resolve(snapshot, stored, snapshot.state(), checkpoint)
                    result["state"] = state
                if size is not None:
                    checkpoint(result["counts"]["points"], "Rendering")
                    width, height = size
                    result.update(chart_render.render(result["resolved"], width, height), width=width, height=height)
                return result

            response = self.jobs.start(request, run, "chart")
            job_id = request["job_id"]
            self.metadata[job_id] = {"state": state, "chart_id": chart_id, "kind": kind}
            return {**response, **self.metadata[job_id]}

    def access(self, job_id, action=None):
        with self.book.lock:
            self.book.refresh()
            response = self.jobs.access(job_id, action)
            if action == "release":
                self.metadata.pop(job_id, None)
                return response
            metadata = self.metadata.get(job_id)
            if metadata is None:
                raise EngineError("JOB_NOT_FOUND", "Chart job expired or is unavailable", 404)
            # A job that saw the workbook change part way through fails with STALE_STATE; once the
            # workbook has moved on, that job is stale too.
            moved = metadata["state"] != self.book.state()
            changed = response["status"] == "failed" and response["error"]["code"] == "STALE_STATE"
            if moved and (response["status"] == "completed" or changed):
                response.pop("result", None)
                response["status"] = "stale"
            response.update(metadata)
            return response

    def close(self):
        self.jobs.close()
