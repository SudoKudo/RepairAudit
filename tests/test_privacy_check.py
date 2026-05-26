from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.privacy_check import _scan_secret_patterns


class PrivacyCheckTests(unittest.TestCase):
    def test_secret_scan_covers_java_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            java_file = repo_root / "Example.java"
            java_file.write_text('String token = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456";\n', encoding="utf-8")

            findings = _scan_secret_patterns(repo_root, [java_file])

            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path, "Example.java")
            self.assertEqual(findings[0].category, "secret_pattern")


if __name__ == "__main__":
    unittest.main()
