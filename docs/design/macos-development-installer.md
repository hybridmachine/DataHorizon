# macOS development disk image

Task 10 adds a source-bound, ad-hoc signed development DMG for macOS 15+ ARM64.
It contains DataHorizon.app and an Applications destination link. Installation
is a normal copy; the builder does not modify the user's Applications directory.

The builder requires committed source and a matching installed source stamp.
It verifies the app's strict resource signature, every Mach-O non-system library
reference resolves inside the app, and no deployed Mach-O minimum exceeds macOS
15. It packages the exact Git source archive, checksum-verified QtBase/QtSvg and
CPython sources and Python build provenance. The manifest records exact sizes,
SHA-256 values, tested source and explicit qualification limits.

```sh
python3 tools/macos_installer.py --prefix /new/package --output /new/artifacts --dependency-dir /trusted/downloads
```

Official archives must already be downloaded under their original filenames (the
Python full archive may use `cpython-3.13.18-full.tar.zst`). No downloaded file is
executed by the builder. The image is mounted read-only in an owned private path,
audited again, copied to a private installation prefix and subjected to relocated
GUI/managed CLI/SDK/service smoke with developer PATH overrides removed. The owned
mount is detached even when verification fails.

This is not a qualified release installer. No Developer ID identity is available
on this desktop, and notarization credentials and an actual clean Mac/VM have
not been provided. The manifest therefore retains `release_qualified=false`,
`developer_id_signed=false`, `notarized=false` and `clean_machine_qualified=false`.
Physical input, VoiceOver, signed first-launch Gatekeeper behavior and full
corresponding dependency-source/distribution review remain release gates. The
included Python full archive contains build artifacts/recipes and license data;
it is not a complete source tree for every bundled third-party library.

Do not publish a public release from this development artifact. Qualify the
final exact source and artifact on a clean Mac, complete matching dependency
source materials and distribution review, then sign/notarize with the owner's
Developer ID identity and repeat installation/launch tests before release.
