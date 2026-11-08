# HTML deep inspection — current patch continuation

Pre-change source: `660b47002672a3ad86e4ce8b1cd7c514df178295`.

## Confirmed validator gaps and fixes

1. **CSS `image-set()` / `-webkit-image-set()`** can refer to an image through a quoted string without `url()`. The prior scanner ignored it and returned `localResourcesChecked=True` with the image absent. A real Qt WebEngine local-file request interceptor confirmed that this syntax requests `photo.png`. The bounded fix explicitly returns incomplete inspection for the unsupported function grammar, including escaped spelling and linked stylesheets. It does not invent a partial CSS parser or claim successful conversion. Quoted CSS content strings that merely name the function remain supported.
2. **`iframe srcdoc`** contains a second HTML document; its relative resources use the containing document's base. The prior scanner ignored those attributes. Qt WebEngine confirmed the resource request. The fix recursively applies the existing resolver with maximum nesting depth four and a bounded inline document size. Valid local companions still pass. Truncated output `srcdoc` attributes now remain incomplete, as `style`/`srcset` already did. An iframe-only document is considered unknown, rather than falsely certified empty by the outer body scanner.
3. **Existing but invalid raster companions** passed the old existence-only resource check. Empty, truncated or non-image `photo.png` now fails `ENGINE_OUTPUT_RESOURCE_INVALID`. The same applies to recognized raster magic under a renamed extension. Validation is independent of source preflight, takes place only for exported results and uses the existing streaming container inspector. Other local files such as fonts and CSS are not mislabeled as raster images. A bounded 128-entry LRU deduplicates work; eviction causes a later reference to be inspected again, not accepted unchecked. A valid HTML containing 150 distinct images still passes. Cancellation preserves its cause and validation does not rewrite source/output bytes.

These are reproducible result-validator defects with generated/fault-injected exports. They do **not** establish that current LibreOffice itself emits each corrupted example.

## Rejected candidate / uncertainty

The earlier inline SVG `xml:base="assets/"` example was **not** confirmed as a visual loss in the tested Chromium: it requested the document-relative image and ignored that inline base. An SVG embedded as an image made no external image request. Browser/SVG-client semantics vary; this finding is not counted as a proven quality defect and production handling of `xml:base` was not changed.

## Evidence

- `browser_resource_probe.py`, `browser-fixtures-escalated/evidence.json`, `browser-metadata.json`: real Qt WebEngine URL interception. Initial default sandbox launch failed with Chromium `EPERM`; escalated local-file run completed. Its ordinary offscreen/GPU/DBus warnings are not application conversion errors.
- `run_baseline_regressions.py`, `exact-baseline-regressions.log`: exact prior `660b470` application code imported from `git archive`; 13 new test methods, 18 failing subcases and two missing-new-receipt-key errors. This is intentional reproduction evidence, not final acceptance status.
- `final-html-regressions.log`: new HTML regression test results.
- `final-targeted.log`: combined focused compatibility run after the shared text allocator had stabilized.
- `transient-shared-allocator-targeted.log`: an earlier concurrent run hit the other agent's in-progress embedded allocator import; retained as intermediate evidence, not a remaining HTML failure.

One existing positive presentation fixture used arbitrary text bytes under `image.png`. Its `setUp` asset now uses a real 1×1 PNG, preserving all original assertions and hash-equality semantics. No tests were skipped or weakened.

## Limits

- `image-set()` is now an explicit incomplete check, even when its candidate assets exist. Correct interpretation of the full CSS grammar is not implemented here.
- Resource checks prove closure/existence and complete supported raster containers, not pixels, colors, visual layout, font integrity, external networking, JavaScript-generated content, or all media codecs.
- Supported recognized raster containers are PNG/APNG, JPEG, GIF, WebP, TIFF and BMP. Unsupported resources retain only the existing dependency/existence evidence.
- Large or deeply nested `srcdoc` remains explicitly incomplete; no browser or JavaScript is executed in production validation.
- The LRU counts validation passes; the diagnostic raster counter can include a repeated resource after eviction. It is not a quality score.
- XML base semantics remain a documented unverified area.

Source files changed: `_LocalHtmlResources` in `core.py`, `html_resources.py`, HTML-related parts of `postconditions.py`; new `tests/test_deep_html_resources.py`, and the valid PNG fixture in `tests/test_guard_presentation_v20.py`. No commits or workflow changes made by this agent.
