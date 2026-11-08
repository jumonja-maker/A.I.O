Media completion patch evidence (base b7216d1210489a5fd1eadb21daf2e3b3849e30f4)

Baseline reproduced from an isolated `git archive` of app_backend, not a mixed
module checkout. `baseline-raster.json` records the loaded baseline module path:

- PCX binary header 100x100 DPI -> Qt PNG 96.012x96.012 DPI; mandatory check
  accepted it with source=null and expectedLosses=[].
- TGA 37x19 and PCD 768x512 had no Qt/Pillow raster route.
- `probe/FINDINGS.md`, raw outputs and JSON record real LibreOffice CLI image
  exports: PCD 768x512 and PICT 40x20 become 794x1123 Draw canvases; an 800-color
  PICT becomes a 249-color GIF. Guard's existing CLI all-page contract blocks
  these native exports when no loaded-model receipt exists. These bare LO
  defects do not constitute a baseline Guard false PASS on this Linux host.

Implemented:

- Preserve both PCX DPI header fields in its source profile and owned QImage.
  Validate physical resolution for PNG/APNG/TIFF/BMP/JPEG and JPEG aliases.
  Read real BMP/JFIF DPI; permit JPEG integer-density rounding. Results that
  cannot carry the known scale explicitly list physical_resolution loss.
- Admit TGA through declared binary layout plus installed Qt decoder agreement;
  preserve native pixel canvas/alpha through Qt image writers.
- Add bounded, header-confirmed Pillow PhotoCD Base decode (768x512 or its
  90/270-degree orientation), recording decodedResolution=PhotoCD_Base.
  Unknown 180-degree orientation and truncated Base rasters remain on the
  original native route; higher PhotoCD resolutions are not claimed.
  User-visible MEDIA_PHOTOCD_BASE_ONLY explains Base's 768x512 scope and that
  the presence/preservation of 4Base/16Base/higher layers is not checked.
  It is a scope warning, not a claim of demonstrated resolution loss, and
  remains visible when technical result metadata/quality details are hidden.
- For the actual `PCT - Mac Pict` native importer only: require one page, one
  unrotated/unsheared/uncropped graphic and a QuickDraw v1/v2 frame; serialize
  that graphic through native GraphicProvider with typed pixel dimensions.
  Normal Draw documents keep their page behavior. Mandatory PICT bounds checks
  reject a reintroduced A4 canvas. This native API path requires Windows QA.
- Native GIF targets render native PNG pages first, then use the existing exact
  Qt GIF encoder. >256 colors, partial alpha and unrepresentable profiles fail
  with the same error codes as raster routes. Encode all pages before publishing
  a GIF set, update its page manifest, and record limited native-raster scope
  instead of claiming original/visual equivalence.

Validation:

- `final-unit.log`: 112 tests, all passed, including the real pinned DOM runner.
  Includes 14 new regressions, the Qt frame suite, DPI/ICC suite, legacy/Picture models,
  TIFF bounds/container checks and presentation-text preservation checks.
- `final-native-cached/completion-media-report.json`: 10 PASS, 1
  PASS_SAFE_REFUSAL, 4 BLOCKED, 0 FAIL; driver correctly exited 1. Generated
  PCX/TGA/PCD all pass PNG/APNG/TIFF with exact decoded RGB and geometry;
  PCX scale is 99.9998 DPI (PNG/TIFF representational rounding). Rich raster
  GIF is refused, limited raster GIF is exact. All original source hashes
  remained unchanged.
- PCT PNG/TIFF and rich/limited native Draw GIF cases are BLOCKED on the Linux
  installation: no bundled python.exe/PyUNO; original all-page contract refuses
  CLI without a native model receipt. They are not reported as passing.
- Shell wrapper executable also cannot discover the installation registry;
  `final-native/` records that separate honest BLOCKED result. The final native
  run above uses the installed real program/soffice executable.
- Python compile and `git diff --check` passed for changed media code.
- Independent review caught two issues and both were corrected: static JPEG/BMP
  DPI checks now compare the represented first page while retaining explicit
  page-loss receipts; GIF manifest conversion requires schema 1, exact pageCount,
  sequential integer page ordinals and matching filenames before publication.

Reusable Windows command (requires application's Python, bundled LO and Qt):

    python tools/qa/completion_media_native.py --executable PATH_TO_SOFFICE --output NEW_EVIDENCE_DIR

The driver writes completion-media-report.json and fails if any required case
is BLOCKED/FAIL. Optional `--fixture-dir DIR` adds original PCD/PCT specimens.
Installed Windows GUI, historical FULLTEST corpus and higher PhotoCD resolutions
remain unverified; this evidence does not close those gates.
