"""Static regression contract for isolated Inno Guard-QA uninstallation.

This is intentionally not an installation/uninstallation smoke test.
The real exit-state check runs on an owner-controlled Windows workstation.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLERS = ROOT / "installer" / "windows"


def section(text, heading):
    hit = re.search(r"(?ms)^\[" + re.escape(heading) + r"\]\s*(.*?)(?=^\[[A-Za-z]+\]|\Z)", text)
    return hit.group(1) if hit else ""


class GuardQaUninstallContract(unittest.TestCase):
    def test_cyrillic_inno_tasks_are_encoded_for_unicode_compiler(self):
        # UTF-8 BOM lets older Unicode Inno compilers decode user-facing
        # Russian strings without mojibake. This is a static contract:
        # a Windows installer screenshot is still required.
        for file in INSTALLERS.glob("*.iss"):
            with self.subTest(installer=file.name):
                contents = file.read_bytes()
                self.assertTrue(contents.startswith(b"\\xef\\xbb\\xbf"))
                decoded = contents.decode("utf-8-sig")
                self.assertIn('Описание', 'Описание')  # encoding of this test itself
                self.assertIn('Создать ярлык на рабочем столе', decoded)
                self.assertNotIn('РЎРѕР·', decoded)

    def test_qa_appid_is_unique_from_other_aio_installers(self):
        values = {}
        for file in INSTALLERS.glob("*.iss"):
            text = file.read_text(encoding="utf-8")
            match = re.search(r"(?m)^AppId=(.+)$", text)
            self.assertIsNotNone(match, file.name)
            values[file.name] = match.group(1).strip()
        self.assertGreaterEqual(len(values), 3)
        self.assertEqual(len(set(values.values())), len(values))

    def test_logged_payload_includes_complete_bundled_libreoffice(self):
        text = (INSTALLERS / "AIO-Guard-QA.iss").read_text(encoding="utf-8")
        files = section(text, "Files")
        self.assertIn('Source: "..\\..\\dist\\A.I.O\\*"', files)
        self.assertIn('DestDir: "{app}"', files)
        self.assertIn("recursesubdirs createallsubdirs", files)
        self.assertIn("DefaultDirName={localappdata}\\Programs\\A.I.O-Guard-QA", text)
        self.assertIn("PrivilegesRequired=lowest", text)

    def test_explicit_cleanup_is_limited_to_app_owned_runtime(self):
        text = (INSTALLERS / "AIO-Guard-QA.iss").read_text(encoding="utf-8")
        deletion = [line.strip() for line in section(text, "UninstallDelete").splitlines()
                    if line.strip() and not line.lstrip().startswith(";")]
        self.assertEqual(deletion, ['Type: filesandordirs; Name: "{app}\\_internal"'])
        self.assertNotIn("CloseApplications=force", text)
        self.assertIn("CloseApplications=yes", text)
        self.assertIn("RestartApplications=no", text)
        self.assertNotIn("UninstallRun", text)
        for prohibited in ("{userappdata}", "{commonappdata}", "{userdocs}", "{autodesktop}",
                           "{localappdata}\\Programs\\A.I.O", "{app}\\.."):
            self.assertNotIn(prohibited, section(text, "UninstallDelete"))

    def test_shortcut_targets_scoped_to_this_installation(self):
        text = (INSTALLERS / "AIO-Guard-QA.iss").read_text(encoding="utf-8")
        icons = section(text, "Icons")
        self.assertEqual(icons.count('Filename: "{app}\\A.I.O.exe"'), 2)
        self.assertIn('Name: "{autoprograms}\\A.I.O (Guard QA)"', icons)
        self.assertIn('Name: "{autodesktop}\\A.I.O (Guard QA)"', icons)


if __name__ == "__main__":
    unittest.main()
