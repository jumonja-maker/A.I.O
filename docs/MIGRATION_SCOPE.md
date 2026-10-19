# Migration scope — new A.I.O repository, 19.10.2026

Source: private archived `jumonja-maker/All-in-One-` commit `f342a28086e4f9b59dc086a9cd52d4875d9a7cb8`.

## Retained
- Original main A.I.O shell and design: `index.html`, `src/styles.css`, registered tools, navigation, theme logic and current visible converter chrome
- Owner-approved converter designer snapshot (compressed, SHA-verified, UI-only patches); no live legacy routes
- Excel UI and original functional `merge_xlsx` engine, with a **standalone** XLSX integrity validator
- Local-only Qt/WebEngine desktop shell and authenticated loopback Excel backend
- Branding assets: original SVGs and exact Elva V13 Happy 52px WEBP thumbnail

## Intentionally not migrated
- `app_backend/converter_universal.py`, old DOCX/image/PDF converters, target catalogues, old allowlists, legacy retries, result spool/state machines
- `src/converter-bridge.js` old implementation (replaced with a clean "Guard not ready" boundary)
- All Advanced frontend/backend and workflow/PR history
- Old policy that could silently reject LibreOffice routes

## Known incomplete items
1. **New Converter Guard and LibreOffice execution are not yet implemented**. The converter tool is present visually, but conversion routes explicitly return `GUARD_NOT_READY`; no misleading claims of conversion success.
2. Two high-resolution brand PNG assets (~1.7 MB each) are still in archived source. The GitHub connector cannot read their binary bytes at that size; the exact smaller Elva owner-thumbnail and SVGs are preserved in the new repo. Replace with original high-resolution assets only after a verified binary transfer.
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
