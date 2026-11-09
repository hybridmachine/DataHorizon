# macOS native interaction qualification

Task 6 repairs the development bundle signature after executable or resource
updates, and verifies the full resource envelope during ordinary builds. This
is an ad-hoc development signature, not Developer ID signing or notarization.
An actual copied-bundle regression damages a sealed icon, requires rejection,
repairs the signature and verifies idempotent subsequent checks.

The native-plugin probe now distinguishes OS window activation from Tab focus.
It still requires both; inability to activate is a failure, not an excuse to
weaken acceptance. Hosted Cocoa checks remain synthetic Qt events and do not
replace physical keyboard, external clipboard or native file-dialog acceptance.

This desktop's native automation launch timed out for both the original bundle
and the repaired, strictly verified bundle. The original bundle failed codesign
verification; the repaired bundle passes. That fix did not establish UI launch
or manual acceptance. No successful physical interaction is claimed. A Supplies
fixture was prepared for the walkthrough but never edited through native input.

Pending: launch/activation on this desktop, the full Supplies walkthrough with
physical keyboard and external clipboard, native open/save/overwrite/cancel
flows, app lifecycle, appearance and multiple-monitor/high-DPI checks. Task 6
remains open until these results are recorded on a usable Mac session.
