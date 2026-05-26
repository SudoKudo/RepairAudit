from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from tools.analysis.metrics import summarize_participant_results


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class MetricsFallbackTests(unittest.TestCase):
    def test_detector_stays_primary_when_judge_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results_csv = Path(tmp) / "results.csv"
            _write_csv(
                results_csv,
                [
                    {
                        "snippet_id": "SQLi_01",
                        "outcome": "Mitigated",
                        "judge_enabled": "False",
                        "judge_verdict": "",
                        "status": "ok",
                        "vuln_type": "SQLi",
                    },
                    {
                        "snippet_id": "CMDi_01",
                        "outcome": "Preserved",
                        "judge_enabled": "False",
                        "judge_verdict": "",
                        "status": "ok",
                        "vuln_type": "CMDi",
                    },
                ],
            )

            summary = summarize_participant_results(str(results_csv))

            self.assertEqual(summary.primary_source, "detector")
            self.assertEqual(summary.primary_scored, 2)
            self.assertEqual(summary.primary_counts["Mitigated"], 1)
            self.assertEqual(summary.primary_counts["Preserved"], 1)
            self.assertEqual(summary.primary_counts["UNKNOWN"], 0)
            self.assertEqual(summary.judge_scored, 0)


if __name__ == "__main__":
    unittest.main()
