# Next five editing and CSV increments

November 5, 2026. Continue everyday features after PRs #14–#18. Implement,
verify locally, open/review/fix and merge each item before starting the next.
Hosted CI remains unavailable under the user's local-verification authorization.

1. Sheet rename with formula reference updates.
2. Sheet duplication with reference preservation and copied self-references.
3. Sheet deletion with broken-reference transformation and warnings.
4. CSV encoding and delimiter controls.
5. CSV column typing with explicit conversion review.

## Sheet rename

Rename preserves stable sheet identity and retargets qualified name references
across the workbook using the existing formula parser's reference spans. New
names are quoted and apostrophes escaped; strings, unqualified references,
absolute markers, cell spelling and references through stable sheet UUIDs remain
intact. Name lookup follows SQLite's ASCII NOCASE rules. Native Text inputs that
look like formulas remain literal.

The ordinary tier scans at most 10,000 formulas and 8 MiB of sources/styles,
with the existing five-second operation deadline and input/history byte limits.
Unsupported formula syntax, overlong expanded sources, collisions and active
required handlers reject atomically. A structure-event hook contract remains
open. Successful rename clears history; its dialog states that consequence.
Rollback and replay retain the existing durable operation receipt contract.

Verification covers quoted/case/UUID/range/string references, reopen, Text
preservation, source/scan limits, collision, injected write failure and receipt
replay; SDK/REST and the real Qt rename dialog exercise the same operation.
