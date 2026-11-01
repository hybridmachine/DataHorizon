# License scope

The desktop GUI sources under `desktop/` and the Qt desktop test source
`tests/test_desktop.cpp` are licensed under the GNU General Public License,
version 3 only (SPDX: GPL-3.0-only). The complete license is
[desktop/COPYING](desktop/COPYING). Source files carry the SPDX identifier.

Licenses for the service, Python SDK, other tooling and documentation remain
undecided. This file makes no repository-wide license grant. Resolve those
licenses and dependency distribution materials before a public binary release.

The development build dynamically links Qt Core, GUI, Widgets and Network.
Qt Test is used only by the tests. Python and SQLite come from the separately
installed Python runtime. Local development installation is not a completed
license/distribution audit or a bundled Python runtime. No additional pip
packages are required by the implemented service.
