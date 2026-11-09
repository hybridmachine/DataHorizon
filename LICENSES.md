# DataHorizon licensing

All original DataHorizon components, including the desktop GUI, service, managed
CLI, Python SDK, tooling and documentation, are licensed under the GNU General
Public License version 3 only (SPDX: GPL-3.0-only). See [COPYING](COPYING).
The existing GUI copy in `desktop/COPYING` contains the same license. This applies
the owner's November 8, 2026 decision; historical qualification reports retain
the licensing state they recorded at the time.

Third-party dependencies retain their own copyrights and licenses. The macOS
ARM64 distribution candidate dynamically links Qt Core, GUI, Widgets, Network,
DBus and SVG, including deployed platform/image plugins. Qt Test is build/test
only. Qt 6.12.0 source license texts, attribution records and vendor SBOMs are
preserved under `packaging/qt/6.12.0`, with official source URLs and SHA-256 values.
These source records are a superset of the deployed dependencies, not a claim
that every Qt source component is included. Different Qt versions require their
own matching materials before distribution.

The pinned Astral CPython 3.13.18 runtime retains its Python and dependency
licenses. `packaging/python/PYTHON.json` and `packaging/python/licenses/` come from
the matching checksum-verified full distribution; the installed runtime also
retains its standard-library/pip notices. Runtime and provenance archive hashes
are in `packaging/runtime-macos-arm64.json`. No application pip dependencies are
required. Optional test bindings are not shipped in the application.

A distributable artifact must accompany the exact DataHorizon source, build and
installation scripts and matching dependency source/materials. Developer ID
signing, notarization, clean-machine acceptance and complete distribution review
remain release gates. Ad-hoc development signing does not establish those gates.
