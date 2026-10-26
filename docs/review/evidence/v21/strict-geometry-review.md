# Independent final review — worksheet geometry / Strict SpreadsheetML

Reviewed frozen functional candidate `728131…`; found a blocking false-clean condition despite the earlier full 1031-PASS suite. Scope: `worksheet_geometry.py`, `comparison_analysis._xlsx_summary`, `quality._xlsx_values`. No broad format-conversion changes.

## Confirmed defect

The geometry parser recognized both Transitional and Strict SpreadsheetML. The installed openpyxl read-only worksheet parser recognizes Transitional row/cell tags only. Consequently a source XLSX with four actual Strict-namespace cells was summarized as a complete spreadsheet containing zero cells. Comparison against a genuinely empty XLSX returned **checked / cell_values_single_grid**, without findings. The reverse direction likewise missed added values. Optional quality inspection generated the digest of an empty row stream and reported checked_subset.

A full Strict package also makes this reader expose zero worksheets (Strict relationship types unsupported). Before the fix quality inspection treated this as the multisheet case and returned complete structural evidence rather than an unavailable workbook reading. Its values digest was already absent, so this second path is not claimed to be a false values comparison, but is now explicitly incomplete.

## Fix

- Recognized Strict worksheet XML raises `GeometryUnavailable('xlsx_strict_values_not_inspected')` before any value-reading/empty digest can be certified.
- Zero reader-visible worksheets raise optional `_Limited('xlsx_worksheets_not_inspected')`; actual multisheet structural-subset behavior is unchanged.
- No native import/export/preflight/route code was changed. This marks unavailable evidence, not an unsupported LibreOffice format.
- Byte-identical-file evidence remains independently valid; a Strict reader limitation does not negate exact file-byte equality.

## Evidence

Four new regressions in `tests/test_comparison_semantics_v21.py`:

1. Strict source with four cells → empty result cannot be called checked.
2. Empty source → Strict result with four cells cannot be called checked.
3. Quality inspector cannot hash ignored Strict cells as an empty sheet.
4. Full Strict workbook and relations are incomplete with explicit reasons; source bytes stay unchanged.

`strict-geometry-red.txt`: **4 FAIL** on the frozen code. `strict-geometry-green.txt`: **55 PASS**, covering semantics, existing quality reliability and FULLTEST contracts after the correction.

Added QA-only `tools/qa/guard_v21_xlsx_acceptance.py`: full Strict fixture and deliberately understated dimension A1 with actual 2×2 cells. Both pass real installed LibreOffice preflight, registered XLSX route and actual native CLI conversion. The output is independently read with openpyxl and all four exact strings match; originals remain unchanged. Strict optional comparison/quality stays incomplete, while the stale-dimension case is completely inspected.

Real Linux evidence: `strict-native-linux/evidence.json`, **2/2 PASS, native_cli**, ~10.9 seconds per synthetic conversion including preflight. This is a correctness control, not a general throughput benchmark. The corresponding Windows `--require-mode native_uno` gate is delegated to the root's new exact-source run.

## Other reviewed boundaries

- Actual row/cell coordinates bound dense allocation before worksheet iteration; understated and huge advisory dimension metadata does not control allocation.
- XML depth/node limits, byte budgets and geometry inspection cap yield unavailable optional evidence. They are not native file-size/row/format limits.
- Geometry parser matches missing-row/cell-reference inference for supported Transitional data. Ambiguous/duplicate/reversed references become unavailable evidence.
- Iterators and workbook/source streams close on errors; ZIP members are read from the archive, never extracted or fetched from relationship URLs. Unresolvable/nonstandard relationships produce unavailable evidence.
- No additional confirmed blockers found in this bounded review. This is not a proof of complete XLSX visual or formula-result fidelity.

Functional files and new QA harness are frozen after this note; root will publish the new SHA and rerun full acceptance.
