Route/preflight work in `/workspace/AIO-completion`, base `b7216d1210489a5fd1eadb21daf2e3b3849e30f4`.

- Removed the implicit 15-second import cap for preparation jobs of 32 or more files. Every source now gets the configured preflight timeout; explicit shorter/longer requests and cancellation keep their existing behavior.
- Specialized GlobalDocument/ReportDefinition exporters cannot be advertised from LOK's broad Writer service union or TypeDetection alone. Actual loaded UNO model services continue to enable their installed native exporters. No source extension or source contract can enable those routes.
- Preserved the two exact generated XHTML sources from the pinned previous Windows diagnostic archive, byte for byte, with SHA-256 provenance. Added a reusable real native 32-source bulk preparation and PDF save/text driver. Original ODM/OTM survey reads the pinned 22-source archive and never converts an import failure into invented support.

Verification:

- `new-regressions-on-baseline.json`: five new tests on original b7216d1; three intended failures, zero errors. They expose bulk timeout shortening, false LOK master support and type-only specialized model admission.
- Patched five tests pass. The related 69 route/preflight/cancellation/source-contract tests also pass outside the sandbox; their existing localhost HTTP case cannot bind inside the sandbox. The initial sandbox attempt is preserved in `unit-after.log` as one infrastructure error.
- `native-baseline/evidence.json`: real Linux native 32/32 XHTML imports/PDF eligibility, two saved PDFs retain the AIO marker, leading zeroes, 17-digit integer and Cyrillic. Driver fails because the budget is 15 seconds and ordinary XHTML falsely advertises ODM/OTM.
- `native-after/evidence.json`: same native controls pass with 120 seconds, no false specialized support, 32/32 imports/PDF routes, two saved PDFs and required text retained. Total 9.226 seconds.
- `baseline-exact-imports.json`: the four original ODM/OTM files do not import on this installed Linux engine. They remain undetermined; this is not evidence of universal model support or a Windows importer fix.

The two small XHTML files convert on this fast Linux host even in the baseline. Original Windows bulk timeout reproduction and installed UI remain separate acceptance. Historical 178-file FULLTEST and a genuine Base ReportDefinition positive were not run; M14 remains open. M02, retired Before/After and BlackBox were not changed.

Independent DBF review:

- Strict representability, per-sheet option scoping, formula errors and a second mixed-display representability pass look sound. An independent harness with numeric 42 displayed as `42漢字` plus Cyrillic text correctly changes the mixed Memo export to UTF-8 (76).
- CLI recovery retains UTF-8 (76), and the outer CPG/warning derives from actual resulting encoding metadata.
- Flagged LDID mutation before symlink/regular-file validation. DBF owner added regular-file/reparse guards, POSIX nofollow opens and a victim-preservation regression; independently verified the updated test passes.
- `dbf-review.json` proves an intermediate CP1251/C9 Memo DBF was re-imported with UTF-8 (76), because the existing source hint inspects only ASCII Memo pointers while text lives in DBT. Owner now conservatively keeps long/Memo/mixed/declared-M outputs in UTF-8 instead of broadening unproved source encoding admission.
- `dbf-review-spaces.fods` proved the first CLI Memo exclusion could be bypassed by compressed `<text:s text:c="300"/>` content: 308 rendered bytes initially selected 34. Owner now rejects such whitespace controls and accumulates nested cell text; independently verified the same fixture selects 76.
- Independently reran the final 29 tests in native encoding/codepage/legacy worker modules: PASS. Fresh native Memo UTF-8 and AIO readback validation remains the DBF owner's ongoing final run.
