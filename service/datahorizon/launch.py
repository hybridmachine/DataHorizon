"""Trusted entry point for an owned service, invoked by absolute path with -I."""

import sys


def main():
    if not sys.flags.isolated:
        raise SystemExit("Start the service with python -I -B /absolute/path/to/datahorizon/launch.py")
    from pathlib import Path

    # Only the installed/source service root joins the interpreter's trusted paths.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from datahorizon.service import main as serve

    serve()


if __name__ == "__main__":
    main()
