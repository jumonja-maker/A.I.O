# A.I.O Deep Telemetry v2 — compatibility patch for BLACK.BOX stress audit v0.4

Base: Draft A.I.O PR #15, not main. This is a passive QA patch; no converter policy changes.

## Extended schema

- Each event includes producer_id, schema_version=2, monotonic_ns relative to this emitter, UTC timestamp, seq.
- Safe module_id, task_id, attempt_id and result_id identifiers are one-session keyed hashes. Paths, tokens and raw document names are not exported.
- Guard task events report actual execution_mode (native_uno/native_cli where available), reason_code, stage, reason_family, duration, result size and output identity; source digest is reused from existing upload SHA-256, not recomputed.
- Native stages import/export are emitted when *started*. Neither import_finished nor export_finished is invented where native callbacks cannot distinguish them.
- Native successful completion is separate from output_contract_status and fidelity_status. Fidelity is always not_checked in this patch.
- Zero-byte result artifacts produce output_contract_warning (OUTPUT_EMPTY) without changing Guard success/failure decisions.
- Folder-save emits save_file_written after destination rename using size and digest already computed during copy; this is NOT independent readback verification. It does not assert the complete batch was saved until Guard's existing terminal save.
- Direct legacy converter receives module identity and stable task/attempt IDs; it still reports fidelity not_checked and makes no claim of format preservation.
- Process PID of individual LibreOffice workers and native exit codes still require nonintrusive hooks in the native launcher; current libreoffice_started is NOT proof a new OS process spawned.

## Acceptance

1. Run both test_blackbox_telemetry.py and test_blackbox_audit_v02.py.
2. Build a QA A.I.O EXE with this branch over PR #15, not an older main EXE.
3. Start A.I.O using BLACK.BOX v0.4 (one-click), check the file under BLACK.BOX/data/.
4. Test normal and failed conversion, cancellation, folder and single-file saves; compare the JSONL task, artifact and save lifecycle with existing GUARD report and disk results.
5. Record overhead compared with telemetry disabled and verify crash/queue-overflow behavior.
6. Keep Draft until real Windows, LibreOffice/UNO/CLI integration passes.

No independent fidelity comparison, OCR, required cloud service or new mandatory A.I.O dependency is introduced.