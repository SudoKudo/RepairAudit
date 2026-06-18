from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "domain_classification"
    / "participant_ready.py"
)
SPEC = importlib.util.spec_from_file_location("participant_ready_module", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load module from {MODULE_PATH}")
participant_ready = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(participant_ready)


class ParticipantReadyDatasetTests(unittest.TestCase):
    def test_participant_ready_issues_reject_unknown_metadata(self) -> None:
        row = {
            "is_vulnerable": "1",
            "cwe_primary": "NVD-CWE-NOINFO",
            "vulnerability_type": "Unknown/Unspecified",
            "language": "C++",
            "code_sample": "int main(void) { return 0; }",
        }

        issues = participant_ready.participant_ready_issues(row)

        self.assertIn("missing_specific_cwe", issues)
        self.assertIn("missing_specific_vulnerability_type", issues)

    def test_participant_ready_issues_reject_flattened_line_comment(self) -> None:
        row = {
            "is_vulnerable": "1",
            "cwe_primary": "CWE-787",
            "vulnerability_type": "Bounds Error",
            "language": "C++",
            "code_sample": "int main(void) { ok(); // comment more(); return 0; }",
        }

        issues = participant_ready.participant_ready_issues(row)

        self.assertIn("flattened_line_comment", issues)

    def test_build_participant_ready_dataset_splits_ready_and_rejected_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_csv = tmp_path / "classified.csv"
            output_csv = tmp_path / "participant_ready.csv"
            rejected_csv = tmp_path / "participant_rejected.csv"

            rows = [
                {
                    "sample_id": "GOOD001",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-89",
                    "vulnerability_type": "Injection",
                    "language": "Python",
                    "code_sample": "cursor.execute(query)",
                },
                {
                    "sample_id": "BAD001",
                    "is_vulnerable": "1",
                    "cwe_primary": "NVD-CWE-NOINFO",
                    "vulnerability_type": "Unknown/Unspecified",
                    "language": "C++",
                    "code_sample": "int main(void) { ok(); // comment more(); return 0; }",
                },
            ]

            with input_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)

            summary = participant_ready.build_participant_ready_dataset(
                input_csv=input_csv,
                output_csv=output_csv,
                rejected_output_csv=rejected_csv,
            )

            self.assertEqual(summary["ready_rows"], 1)
            self.assertEqual(summary["rejected_rows"], 1)

            with output_csv.open("r", newline="", encoding="utf-8") as handle:
                ready_rows = list(csv.DictReader(handle))
            with rejected_csv.open("r", newline="", encoding="utf-8") as handle:
                rejected_rows = list(csv.DictReader(handle))

            self.assertEqual(ready_rows[0]["sample_id"], "GOOD001")
            self.assertEqual(ready_rows[0]["participant_ready"], "1")
            self.assertEqual(json.loads(ready_rows[0]["participant_ready_reasons"]), [])

            self.assertEqual(rejected_rows[0]["sample_id"], "BAD001")
            self.assertEqual(rejected_rows[0]["participant_ready"], "0")
            self.assertIn(
                "missing_specific_cwe",
                json.loads(rejected_rows[0]["participant_ready_reasons"]),
            )


if __name__ == "__main__":
    unittest.main()
