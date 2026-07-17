from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import privacy_check
from scripts.privacy_check import _scan_secret_patterns


class PrivacyCheckTests(unittest.TestCase):
    def test_secret_scan_covers_java_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            java_file = repo_root / "Example.java"
            prefix = "sk-"
            suffix = "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
            java_file.write_text(f'String token = "{prefix}{suffix}";\n', encoding="utf-8")

            findings = _scan_secret_patterns(repo_root, [java_file])

            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path, "Example.java")
            self.assertEqual(findings[0].category, "secret_pattern")

    def test_main_accepts_hyphenated_repo_root_flag(self) -> None:
        with patch.object(privacy_check, "run_prepublish_check", return_value=(True, [], "workspace-scan")) as run_check:
            with patch.object(privacy_check, "_print_report") as print_report:
                with patch("sys.argv", ["privacy_check.py", "--repo-root", "sample-root"]):
                    with self.assertRaises(SystemExit) as exc:
                        privacy_check.main()

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(run_check.call_args[0][0], Path("sample-root"))
        print_report.assert_called_once()


if __name__ == "__main__":
    unittest.main()
