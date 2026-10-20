"""Windows NIKITA acceptance gate for Guard v0.7 with actual bundled LibreOffice.

Only synthetic bytes are inspected. No document exports, installation or network
actions are performed. The Python/UNO worker and LibreOffice are isolated.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app_backend.converter_guard import ConverterGuard, LibreOfficeAdapter
from app_backend.converter_guard.routes import registry
from guard_v06_windows_preflight_gate import bmp_fixture

CALC = "com.sun.star.sheet.SpreadsheetDocument"


def main():
    adapter = LibreOfficeAdapter()
    if not adapter.available:
        raise AssertionError("GUARD_V07_NIKITA: actual LibreOffice is required")
    reg = registry(adapter.executable)
    if not reg.valid:
        raise AssertionError("GUARD_V07_NIKITA: native LO filter registry missing")
    inputs = {
        "bmp": ("qa-photo.bmp", bmp_fixture()),
        "csv": ("qa-table.csv", b"name,amount\nalpha,1\nbeta,2\n"),
    }
    with tempfile.TemporaryDirectory(prefix="aio-guard-v07-native-") as temporary:
        guard = ConverterGuard(adapter=adapter, spool_parent=Path(temporary))
        try:
            descriptors = [{"id":key, "name":value[0], "size":len(value[1])}
                           for key, value in inputs.items()]
            identity = guard.create(descriptors, prepare_only=True)["jobId"]
            for key, (_, payload) in inputs.items():
                guard.upload(identity, key, io.BytesIO(payload), len(payload))
            initial = guard.status(identity)
            if initial["attempted"] != 0:
                raise AssertionError("Preflight ran an export before native validation")
            info = guard.preflight(identity)
            proof = {item["id"]:item["preflight"] for item in info["files"]}
            if any(proof[x].get("state") != "detected" for x in inputs):
                raise AssertionError("Native proof missing for BMP/CSV: " +
                                     json.dumps(proof, ensure_ascii=False))
            if proof["csv"].get("documentServices") != [CALC]:
                raise AssertionError("CSV classified as a non-Calc document: " +
                                     json.dumps(proof["csv"], ensure_ascii=False))
            if not proof["csv"].get("filterName"):
                raise AssertionError("Native CSV import FilterName was not retained")
            formats = {format_info["id"]:format_info for format_info in info["formats"]}
            for key in inputs:
                if key not in formats["pdf"]["verifiedFileIds"]:
                    raise AssertionError(key + "->PDF not natively verified")
            if "bmp" not in formats["dbf"]["incompatibleFileIds"]:
                raise AssertionError("BMP->DBF not blocked before conversion")
            if "csv" not in formats["docx"]["incompatibleFileIds"]:
                raise AssertionError("CSV->DOCX not blocked before conversion")
            if "csv" not in formats["dbf"]["verifiedFileIds"]:
                raise AssertionError("CSV->DBF not verified by Calc filter")
            if guard.status(identity)["attempted"] != 0:
                raise AssertionError("Native preflight requested an export")
            print("GUARD_V07_NIKITA_PREFLIGHT_PASS " + json.dumps({
                "csvFilter":proof["csv"]["filterName"],
                "csvServices":proof["csv"]["documentServices"],
                "preflightChecks":info.get("preflightChecks"),
                "exportsBeforeStart":guard.status(identity)["attempted"],
                "bmpPdf":True,"bmpDbfBlocked":True,"csvPdf":True,
                "csvDbf":True,"csvDocxBlocked":True
            }), flush=True)
        finally:
            guard.close()


if __name__ == "__main__":
    main()
