# macOS runtime and distribution baseline

Task 9 applies the owner's GPL-3.0-only decision to all DataHorizon components.
The shared execution engine stays in the service. The existing standard-library
HTTP service remains a bounded localhost development service; this runtime
increment does not qualify external TLS/mTLS binding or close M8 hardening.

The initial distribution target is macOS 15+ on Apple Silicon. Qt 6.12.0 builds
set a 15.0 deployment target explicitly instead of inheriting the build host's
macOS version. Windows, Linux, Intel Macs and a universal binary are deferred.

Python is CPython 3.13.18, GIL enabled, from Astral python-build-standalone release
20261026. [Its documentation](https://docs.astral.sh/python-build-standalone/distributions/)
describes the relocatable install-only archive. The pinned manifest identifies
both install-only and full provenance archives. SHA-256 is verified before
extracting or executing the runtime. Staging refuses existing destinations and
escaping archive members/symlinks. The corresponding full archive supplies
Python build metadata and dependency license texts, retained with Qt source
attributions/SBOMs and source archive hashes in `packaging/`.

```sh
python3 tools/stage_runtime.py --archive /trusted/download.tar.gz --destination /new/runtime
cmake -S . -B build -DCMAKE_PREFIX_PATH=/path/to/Qt/6.12.0/macos -DDATAHORIZON_RUNTIME_ROOT=/new/runtime -DCMAKE_INSTALL_PREFIX=/new/package
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
cmake --install build
python3 tests/package_smoke.py /new/package
```

The app resolves its sibling `Resources/runtime/bin/python3` before external
Python. Its service starts with isolated mode and an explicit trusted launcher;
spawned hooks/calculations use that interpreter. `Contents/MacOS/datahorizon-cli`
provides the installed CLI with the same isolated runtime and relocated service
root. External SDK programs may still use their own supported Python to call
REST. Ordinary direct SQLite remains a separate explicitly bypassing path.

This is a development distribution baseline. Signing identities available on
this desktop report zero valid identities. Developer ID signing/notarization
and actual clean-system acceptance remain pending. The artifact must include
exact corresponding source and matching dependency materials before release.

The combined implementation passes 223 Qt checks, 464 Python tests (19 optional
skips), the resource-signature regression and installed GUI/managed CLI/SDK smoke
with bundled CPython 3.13.18 / SQLite 3.53.1 and developer PATH removed. The
initial runtime source passed all three hosted macOS jobs in [run 42555956850](https://github.com/hybridmachine/DataHorizon/actions/runs/42555956850);
downloaded JSON and screenshots were reviewed. The [source record](evidence/task9-verification.json)
distinguishes initial hosted and integrated local evidence. Task 9's documented
runtime/licensing baseline is complete; it does not close production or release
gates.

The integrated source also passes all three hosted macOS jobs in
[run 42559302708](https://github.com/hybridmachine/DataHorizon/actions/runs/42559302708),
including the bounded accessibility tier and bundle-signature regression.
