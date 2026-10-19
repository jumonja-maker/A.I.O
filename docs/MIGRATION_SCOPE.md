# Migration scope — new A.I.O repository, 19.10.2026

Source: private archived `jumonja-maker/All-in-One-` commit `f342a28086e4f9b59dc086a9cd52d4875d9a7cb8`.

## Retained
- Original main A.I.O shell and design: `index.html`, `src/styles.css`, registered tools, navigation, theme logic and current visible converter chrome
- Owner-approved converter designer snapshot (compressed, SHA-verified, UI-only patches); no live legacy routes
- Excel UI and original functional `merge_xlsx` engine, with a **standalone** XLSX integrity validator
- Local-only Qt/WebEngine desktop shell and authenticated loopback Excel backend
- Branding assets: original SVGs, exact owner-approved 1254px icon/Elva PNG masters restored from old commit 7856c110b0898c578530bc0c72ae5e065b4f2cfd, derived multi-frame ICO and DPI-aware PNG resources; old thumbnail retained only for compatibility

## Intentionally not migrated
- `app_backend/converter_universal.py`, old DOCX/image/PDF converters, target catalogues, old allowlists, legacy retries, result spool/state machines
- `src/converter-bridge.js` old implementation (replaced with a clean "Guard not ready" boundary)
- All Advanced frontend/backend and workflow/PR history
- Old policy that could silently reject LibreOffice routes

## Known incomplete items
1. **Converter Guard is not integrated.** This branch now runs direct native LibreOffice through disk streaming. UI does not impose format/size/time policies. Enabled targets mean a native attempt is available (native_decision), not that every source/target pair is qualified. See [native UI handoff](review/NATIVE_LIBREOFFICE_UI_PATCH_2026-10-19.md).
2. High-resolution brand assets are restored. Both Git blob IDs and owner-approved SHA-256 digests match the archived originals; see assets/brand/BRAND_PROVENANCE.json. EXE/installer/window icon references and responsive UI sources are repaired. Installed Windows shortcut/cache behavior still requires Windows build-and-QA.
3. The new Windows installer/PyInstaller bundle and installed Windows GUI smoke require new build-and-QA work. A restored interface in source does not equal a passed installation.
4. Historical converter designer composition still consists of UI-only Python string transformations. Next step: materialize their output once as stable standalone assets; then retire this compatibility composer. No old backend is imported.

## Rules
- Never merge legacy conversion backend as a shortcut.
- Preserve Excel independently from Guard.
- Each Guard rejection must carry an explicit rule/code and a trace event stating whether LibreOffice actually ran.
- No unverified route should be shown as working.
- Protect security boundaries independently of optional diagnostic features.
- Test single-file convert → save → retry → reset, and real data fidelity before growing the route set.

Original repository remains unchanged. Old QA bag is not migrated.
