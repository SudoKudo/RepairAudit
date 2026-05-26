from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from tools.reporting import html_report


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class HtmlReportTests(unittest.TestCase):
    def test_primary_outcome_falls_back_to_detector_when_judge_is_blank(self) -> None:
        row = {"judge_verdict": "", "outcome": "Mitigated"}
        self.assertEqual(html_report._derive_primary_outcome(row), "Mitigated")

    def test_strategy_rows_skip_detector_only_records(self) -> None:
        rows = [
            {
                "run_id": "P001",
                "snippet_id": "SQLi_01",
                "vuln_type": "SQLi",
                "judge_verdict": "",
                "judge_strategy": "",
                "per_strategy_results": {},
            }
        ]
        self.assertEqual(html_report._collect_strategy_rows(rows), [])

    def test_run_duration_prefers_active_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "start_end_times.json").write_text(
                json.dumps(
                    {
                        "start": "2026-04-08T10:00:00Z",
                        "end": "2026-04-08T11:00:00Z",
                        "active_seconds": 42.5,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(html_report._run_duration_seconds(run_dir), 42.5)

    def test_generated_report_uses_per_strategy_tokens_for_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            runs_root = tmp_root / "runs"
            run_dir = runs_root / "P001"
            analysis_dir = run_dir / "analysis"
            edits_dir = run_dir / "edits"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            edits_dir.mkdir(parents=True, exist_ok=True)

            (run_dir / "condition.txt").write_text("security\n", encoding="utf-8")
            (run_dir / "start_end_times.json").write_text(
                json.dumps({"start": "2026-04-08T10:00:00Z", "end": "2026-04-08T10:10:00Z"}),
                encoding="utf-8",
            )
            (edits_dir / "SQLi_01.py").write_text("print('edited')\n", encoding="utf-8")
            (analysis_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "primary_source": "judge",
                        "primary_scored_snippets": 1,
                        "primary_counts": {"Mitigated": 1, "Preserved": 0, "UNKNOWN": 0},
                        "primary_rates": {"mitigation": 1.0, "persistence": 0.0, "abstention": 0.0},
                    }
                ),
                encoding="utf-8",
            )
            _write_csv(
                analysis_dir / "results.csv",
                [
                    {
                        "snippet_id": "SQLi_01",
                        "vuln_type": "SQLi",
                        "outcome": "Mitigated",
                        "judge_verdict": "absent",
                        "judge_confidence": "0.91",
                        "judge_strategy": "ensemble",
                        "judge_strategy_results": json.dumps(
                            {
                                "cot": {"verdict": "absent", "confidence": 0.91},
                                "few_shot": {"verdict": "present", "confidence": 0.40},
                            }
                        ),
                        "status": "ok",
                    }
                ],
            )

            out_html = tmp_root / "report.html"
            html_report.build_aggregated_report_offline(
                repo_root=REPO_ROOT,
                runs_root=runs_root,
                out_html=out_html,
                title="Report Test",
            )

            html = out_html.read_text(encoding="utf-8")
            self.assertIn('data-strategy="cot|few_shot"', html)
            self.assertIn('<option value="cot">cot</option>', html)


if __name__ == "__main__":
    unittest.main()
