# SPDX-License-Identifier: GPL-3.0-only
"""Trusted entry point for an owned service, invoked by absolute path with -I."""

import sys


def main():
    if sys.version_info < (3,11):
        raise SystemExit("DataHorizon requires Python 3.11 or newer with SQLite. Choose a supported trusted interpreter.")
    if not sys.flags.isolated:
        raise SystemExit("Start the service with python -I -B /absolute/path/to/datahorizon/launch.py")
    from pathlib import Path

    # Only the installed/source service root joins the interpreter's trusted paths.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from datahorizon.service import main as serve

    serve()


if __name__ == "__main__":
    main()
