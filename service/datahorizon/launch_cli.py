# SPDX-License-Identifier: GPL-3.0-only
"""Trusted installed CLI entry point, invoked by the owned runtime with -I."""
import sys
from pathlib import Path


if __name__ == '__main__':
    if not sys.flags.isolated or sys.version_info < (3, 11):
        raise SystemExit('Use the DataHorizon launcher with isolated Python 3.11+.')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from datahorizon.cli import main
    main()
