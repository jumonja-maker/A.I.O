# Single-row UI follow-up — PASS

This supersedes the earlier ui-report for header geometry. Production UI frozen at the file hashes in ui-header-row-report.json.

Count, Filter, Add and Trash share one fixed row. Add is visible at0 files; reserved tabular count width prevents controls moving when the text grows. Full labels fit on roomy desktop; narrow panels use accessible icon controls.

Actual Qt/WebEngine passed31 normal-scale cases and27 cases with Qt150% (actual devicePixelRatio1.5). Every case checks counter/button vertical centers≤1px and unchanged header/control rects≤1px when comparing the same viewport across states. Synthetic4096/4096 and10000/10000 counters fit all5 sizes. Root document scrollHeight equals viewport; body.scrollTop remains0. Real Filter popovers and Qt Tab(Filter→Add) work in all5 sizes. Bounded workspace/list scrolling keeps all controls reachable.

Body overflow:hidden previously permitted programmatic scrollIntoView to move the whole shell73px; overflow:clip prevents that scroll owner. Raw body extent remains a diagnostic, while root document and actual shell positions stay fixed.

32 original XHTML copies completed real UI preflight→native PDF→Qt folder save;32/32 PDFs retained their level marker,00123,12345678901234567 andМосква. Existing layout suite:6 tests,10 real WebEngine save/cancel cases PASS.

Authoritative evidence: header-row-verified/evidence.json and header-row-scale150/evidence.json. Header screenshots ending -header.png in the scale150 directory show the single row at narrow and high DPI sizes. Earlier failed diagnostic runs are retained separately.

Scope: Linux production source Qt. Installed Windows/frozen acceptance remains with root; native save destinations are controlled, not manually selected OS dialogs.
