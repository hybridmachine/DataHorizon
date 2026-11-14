# SPDX-License-Identifier: GPL-3.0-only
"""Create, render, change, undo and delete the six chart types with no desktop.

    PYTHONPATH=service python3 examples/charts_script.py [--out build/charts-example]

Starts a local service, writes twelve months of data, and for each chart type
suggests a specification from the range, creates the chart, renders it to
``<out>/<type>.svg``, patches its title, and undoes and redoes the patch.
Finally it deletes one chart and undoes the delete.
"""
import argparse
from pathlib import Path

from datahorizon.client import LocalService

TYPES = ("column", "bar", "line", "area", "pie", "scatter")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fill(book):
    cells = [{"row": 1, "col": col, "source": text} for col, text in enumerate(("Month", "Sales", "Cost"), start=1)]
    for row, month in enumerate(MONTHS, start=2):
        cells.append({"row": row, "col": 1, "source": month})
        cells.append({"row": row, "col": 2, "source": str(100 + row * 12)})
        cells.append({"row": row, "col": 3, "source": f"=B{row}*0.6"})
    book.apply("edit", "Sheet1", cells=cells)


def title(book, chart_id):
    return book.charts(chart_id=chart_id)[0]["spec"]["title"]["text"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("build/charts-example"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    workbook = args.out / "charts-example.dhwb"
    for leftover in args.out.glob("charts-example.dhwb*"):
        leftover.unlink()

    with LocalService() as client, client.open(workbook, create=True) as book:
        fill(book)
        created = {}
        for chart_type in TYPES:
            spec = book.suggest_chart("Sheet1", r1=1, c1=1, r2=13, c2=3, type=chart_type)["spec"]
            spec["title"] = {"text": f"{chart_type.capitalize()} by month"}
            chart_id = book.create_chart(spec, name=f"{chart_type} chart")["chart_id"]
            created[chart_type] = chart_id

            rendered = book.render_chart(chart_id, 800, 500)
            (args.out / f"{chart_type}.svg").write_text(rendered["svg"], encoding="utf-8")

            book.update_chart(chart_id, spec_patch={"title": {"text": "Patched title"}})
            assert title(book, chart_id) == "Patched title"
            book.apply("undo")
            assert title(book, chart_id) == f"{chart_type.capitalize()} by month"
            book.apply("redo")
            assert title(book, chart_id) == "Patched title"
            print(f"{chart_type}: {rendered['counts']['points']} points, {len(rendered['svg'])} bytes of SVG")

        book.delete_chart(created["pie"])
        assert created["pie"] not in {chart["id"] for chart in book.charts()}
        book.apply("undo")
        assert created["pie"] in {chart["id"] for chart in book.charts()}
        print(f"{len(book.charts())} charts after deleting one and undoing the delete")


if __name__ == "__main__":
    main()
