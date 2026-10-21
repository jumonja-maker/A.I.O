"""Contract tests for opt-in A.I.O audit event v2 without native conversion."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from app_backend.blackbox_telemetry import TelemetryEmitter


class AuditV2Tests(unittest.TestCase):
    def test_stable_privacy_safe_ids_and_causality(self):
        with tempfile.TemporaryDirectory(prefix="bb-audit-", dir=Path.cwd()) as tmp:
            dest = Path(tmp) / "events.jsonl"
            emitter = TelemetryEmitter(dest)
            emitter.emit("task_started", request_id="private-name.docx",
                         file_id="my-secret.docx", target="pdf",
                         task_id="job:1", attempt_id="job:1:1",
                         module_id="guard", backend="native_cli",
                         stage="engine", native_status="completed")
            emitter.emit("task_succeeded", request_id="private-name.docx",
                         file_id="my-secret.docx", target="pdf",
                         task_id="job:1", attempt_id="job:1:1",
                         module_id="guard", backend="native_cli",
                         stage="result", native_status="completed",
                         output_contract_status="checked", fidelity_status="not_checked",
                         result_id="secret-token", result_size=82)
            emitter.close()
            data = [json.loads(s) for s in dest.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(data[0]["schema_version"], 2)
            self.assertEqual(len(data), 3)
            self.assertEqual(data[1]["task_id"], data[2]["task_id"])
            self.assertEqual(data[1]["attempt_id"], data[2]["attempt_id"])
            self.assertEqual(data[1]["file_id"], data[2]["file_id"])
            self.assertEqual(data[2]["backend"], "native_cli")
            self.assertEqual(data[2]["fidelity_status"], "not_checked")
            text = dest.read_text(encoding="utf-8")
            self.assertNotIn("private-name.docx", text)
            self.assertNotIn("my-secret.docx", text)
            self.assertNotIn("secret-token", text)

    def test_invalid_external_metadata_does_not_leak(self):
        with tempfile.TemporaryDirectory(prefix="bb-audit-", dir=Path.cwd()) as tmp:
            dest = Path(tmp) / "events.jsonl"
            emitter = TelemetryEmitter(dest)
            emitter.emit("task_failed", file_id="x", request_id="j",
                         target="pdf", module_id="private user name",
                         backend="username/abc", reason_family="ENGINE",
                         filter_name="username secret", reason_code="bad;secret",
                         source_sha256="not-a-digest", result_sha256="privatepath",
                         process_exit_code=-1)
            emitter.close()
            event = json.loads(dest.read_text(encoding="utf-8").splitlines()[-1])
            self.assertNotIn("module_id", event)
            self.assertNotIn("backend", event)
            self.assertNotIn("filter_name", event)
            self.assertNotIn("result_sha256", event)
            self.assertEqual(event["reason_code"], "REDACTED")
            self.assertEqual(event["process_exit_code"], -1)


if __name__ == "__main__":
    unittest.main()
