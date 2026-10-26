# Windows acceptance transport diagnosis — v21

First Windows run `40306383288`, job `121516413994` passed source unit checks, native v20, queue/save v08 and v19 native recipes, then `guard_v19_comparison_acceptance.py` failed with `TimeoutError: timed out` before it recorded preflight evidence. The old script did not report which HTTP call failed. We cannot establish from that evidence whether prepare, upload, preflight or system proxy caused it. Joined original log: `windows-first-full.log`.

Confirmed harness contract mismatch: every urllib call had a fixed 15-second transport timeout. `/preflight` is a synchronous production endpoint whose native budget is normally 120 seconds followed by the route matrix. The existing `--seconds 180` acceptance budget applied only to later status polling, not this blocking operation.

Changes restricted to the acceptance script and its new transport tests:

- Its loopback client explicitly uses `ProxyHandler({})`. Neither environment proxies nor Windows machine proxy settings can route this local-only QA traffic elsewhere.
- Ordinary prepare/upload/status/control requests still have a 15-second socket timeout.
- The synchronous preflight request alone uses the explicit `--seconds` acceptance deadline. No production/native timeout, filter, quality assertion or expected result has changed.
- Evidence records stage, request count, bounded last-32 endpoint/method/status/duration/timeout trace, and traceback on error. Token/body/query values are not recorded.
- Preflight duration and `preflightExceededLegacy15SecondCutoff` are retained separately even when polling evicts its trace entry.

Three tests pass: actual loopback request despite intentionally invalid machine HTTP proxy; explicit long preflight timeout leaves following ordinary calls at 15 seconds; a failed upload request retains exact stage and propagates TimeoutError without exposing its bytes.

Actual Linux LibreOffice/HTTP/isolated-analysis/automatic-retry scenario after the change: **PASS**, 22.879 seconds total, preflight 0.484 seconds, `preflightExceededLegacy15SecondCutoff=false`; confirmed initial precision loss, verified improvement and original results preserved. Evidence: `before-after-transport-final/evidence.json`.

The original Windows root cause remains unconfirmed until the next instrumented Windows run. The changed timeout is a documented test-operation deadline alignment, not proof of better native performance and not permission to accept a hang or failed conversion.
