# DBF completion evidence and limits

Changes are scoped to native LibreOffice exports. A.I.O does not transcode DBF
records or implement a replacement DBF exporter.

- Non-Memo sheets whose complete text (including literal headings and displayed
  errors) is strictly representable in Windows-1251 use native FilterOptions=34.
  LibreOfficeDev 26.8 leaves LDID unset even for this explicit option, so A.I.O
  changes only header byte 29 to the standard Windows-1251 language driver C9
  and writes matching `1251` CPG. Native records, field widths and DBT payloads
  are not patched.
- Other Unicode and possible Memo/mixed/long-text sheets remain native UTF-8
  (FilterOptions=76) with `UTF-8` CPG. Keeping Memo UTF-8 is intentional: current
  source admission does not independently confirm CP1251 DBT text. Broadening
  it to all dBASE dialects would create unsupported import claims.
- A.I.O chooses each sheet independently. No replacement-character fallback is
  used. Both the Windows UNO model and validated private FODS CLI snapshots are
  checked; original source bytes are never rewritten.
- LDID mutation rejects symbolic links, nonregular files and Windows reparse
  points, with no-follow opening where supported. Existing conflicting CPG
  metadata is still rejected.

`baseline.json` and `native-1/evidence.json` reproduce the external limitation:
LibreOfficeDev 26.8's automatic DBF import ignores UTF-8 CPG and also ignores
C9/CP1251 metadata, using a locale default. Explicit native import (`dBase:76`
or `dBase:34`) preserves values. GDAL/OGR 3.10.3 does honor CPG/LDID. Therefore
FT4-04 cannot honestly be declared fixed for every automatic external consumer.
Both export encodings now carry explicit warnings to select the encoding in
LibreOffice; DBF stays available for consumers supporting its metadata.

The first native run also reproduced LibreOffice's CSV negative-decimal field
width defect: `-12.750` is rejected when the inferred Decimal field is six
characters wide. The original CSV, native output/diagnostic and evidence remain
in `native-1`. The reusable driver retains this as a separate control requiring
correct numeric readback or an explicit failed job with no ready result. The
positive CSV codepage control uses integers; XLSX/ODS controls retain signed
fractional numbers and dates.

# M29 scope

Cyrillic source/value preservation is checked through independent binary DBF/DBT
reading, a second native LibreOffice import, optional GDAL/OGR, A.I.O's own
reimport and source SHA-256 checks. Complex literal-heading/mixed-column schema
mapping requires the Windows native UNO worker; Linux explicitly reports it
NOT_RUN and `--require-uno` makes this a required Windows acceptance gate.

No actual VBA-bearing fixture was supplied or found in the attached workspace.
Existing generated DOCM/XLSM-style probes do not establish macro preservation.
The adapter runs all imports with MacroExecutionMode=0, UpdateDocMode=0 and a
private macro-disabled profile. This establishes execution prevention policy,
not retention of real VBA code, signatures or event bindings. M29's real-macro
preservation remains unverified; no blanket guarantee is made.

# Final validation

Final run `native-3/evidence.json`: PASS, 9 PASS cases, one explicit NOT_RUN for Windows UNO mapping on Linux. Source hashes unchanged. Own CP1251 DBF and UTF8 DBT roundtrips passed; folder/ZIP manifests and reports passed. Negative-decimal CSV produced SAFE_REFUSAL (failed task, no outputs, original unchanged). DBF unit set: 32 PASS; legacy: 14 PASS; source routes: 6 PASS; corrected quota adapter fixture: 3 PASS.
