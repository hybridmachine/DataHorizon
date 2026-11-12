# SPDX-License-Identifier: GPL-3.0-only
"""Record the tested development baseline; no release/installer qualification."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys


def runtime():
    if sys.version_info < (3, 11) or sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError('The full contract/experiment suite needs Python 3.11+ and SQLite 3.35+ (RETURNING fixtures)')
    return {'python': platform.python_version(), 'implementation': platform.python_implementation(),
            'sqlite': sqlite3.sqlite_version, 'system': platform.system(), 'architecture': platform.machine()}


def build_report(build, grid_path):
    metadata = json.loads((build/'build-toolchain.json').read_text(encoding='utf-8'))
    grid = json.loads(grid_path.read_text(encoding='utf-8'))
    current = runtime()
    if metadata['python'] != current['python'] or metadata['qt'] != grid['qt']:
        raise RuntimeError('Build interpreter/Qt and measured runtime do not match')
    if metadata['cxx_standard'] != 17 or not metadata['absolute_install_prefix']:
        raise RuntimeError('Baseline requires C++17 and an absolute configured install prefix')
    if metadata['system'] != current['system']:
        raise RuntimeError('Build and measured platform do not match')
    result = subprocess.run(['ctest', '--test-dir', str(build), '-C', metadata['build_type'] or 'Release', '--show-only=json-v1'],
                            capture_output=True, text=True, check=True, timeout=30)
    tests = json.loads(result.stdout)['tests']
    budgets = {test['name']: int(next(p['value'] for p in test['properties'] if p['name']=='TIMEOUT'))
               for test in tests}
    expected = {'desktop': 600, 'python_workspace': 600, 'service': 600}
    if current['system']=='Darwin':
        expected['development_bundle_signature']=30
    if budgets != expected:
        raise RuntimeError(f'Test budgets differ from the documented baseline: {budgets}')
    frozen = grid['frozen_row']
    if grid['platform_plugin']=='offscreen' and not frozen['focused_address_retention_checked_all_samples']:
        raise RuntimeError('Offscreen grid evidence lacks focused input verification')
    if frozen['managed_edit_requests'] != 0 or frozen['cache']['tiles'] > 16:
        raise RuntimeError('Grid artifact exceeds the declared operation/cache contract')
    return {'build': metadata, 'runtime': current, 'ctest_suite_seconds': budgets,
            'grid_evidence': {'qt': grid['qt'], 'platform_plugin': grid['platform_plugin'],
                              'focused_address_retention_checked': frozen['focused_address_retention_checked_all_samples']},
            'distribution': {'python_bundled': False, 'clean_machine_qualified': False,
                             'gui_license': 'GPL-3.0-only', 'service_sdk_license': 'GPL-3.0-only'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, default=Path('build'))
    parser.add_argument('--grid-report', type=Path)
    parser.add_argument('--runtime-only', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'recorded_at_utc': datetime.now(timezone.utc).isoformat()}
    report.update({'runtime': runtime()} if args.runtime_only else
                  build_report(args.build, args.grid_report or args.build/'desktop-benchmark.json'))
    args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
