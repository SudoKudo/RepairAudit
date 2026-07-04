"""Pipeline tests for run analysis, assignment handling, and aggregation."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import study_cli
from tools.analysis import analyze_edits
from tools.analysis.llm_judge import JudgeResult


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class RunAnalysisPipelineTests(unittest.TestCase):
    def test_find_phase_participant_run_dir_prefers_nested_imported_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            runs_root = tmp_path / "runs" / "pilot"
            nested_old = runs_root / "submission_pilot_M001_20260622T195510Z" / "M001"
            nested_new = runs_root / "submission_pilot_M001_20260623T195510Z" / "M001"
            nested_old.mkdir(parents=True)
            nested_new.mkdir(parents=True)

            with patch.object(study_cli, "Path", side_effect=lambda *parts: Path(tmp_path, *parts)):
                resolved = study_cli._find_phase_participant_run_dir("pilot", "M001")

            self.assertEqual(resolved, nested_new)

    def test_analyze_participant_prefers_run_assignment_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            run_dir = tmp_path / "runs" / "pilot" / "T001"
            (run_dir / "baseline").mkdir(parents=True)
            (run_dir / "edits").mkdir()

            baseline_name = "snippet_01.py"
            (run_dir / "baseline" / baseline_name).write_text(
                'query = "SELECT * FROM users WHERE id = " + user_id\ncursor.execute(query)\n',
                encoding="utf-8",
            )
            (run_dir / "edits" / baseline_name).write_text(
                'query = "SELECT * FROM users WHERE id = ?"\ncursor.execute(query, (user_id,))\n',
                encoding="utf-8",
            )
            (run_dir / "condition.txt").write_text("security\n", encoding="utf-8")
            (run_dir / "study_assignment.json").write_text(
                json.dumps(
                    {
                        "participant_id": "T001",
                        "phase": "pilot",
                        "source_kind": "dataset",
                        "snippet_mappings": [
                            {
                                "snippet_id": "S01",
                                "participant_filename": baseline_name,
                                "language": "python",
                                "vuln_type": "SQLi",
                                "cwe_primary": "CWE-89",
                                "is_vulnerable": "1",
                                "source_kind": "dataset",
                            }
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            legacy_metadata = tmp_path / "legacy_metadata.csv"
            _write_csv(
                legacy_metadata,
                [
                    {
                        "snippet_id": "SQLi_01",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "language": "python",
                        "baseline_relpath": "snippets/baseline/SQLi/SQLi_01.py",
                        "gold_relpath": "snippets/gold/SQLi/SQLi_01.py",
                    }
                ],
            )

            with patch.object(analyze_edits, "_judge_enabled_from_config", return_value=False):
                analyze_edits.analyze_participant(str(run_dir), str(legacy_metadata))

            results_csv = run_dir / "analysis" / "results.csv"
            with results_csv.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["snippet_id"], "S01")
            self.assertEqual(rows[0]["edited_filename"], baseline_name)
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["outcome"], "Mitigated")

    def test_analyze_participant_supports_judge_only_scoring_for_general_cwe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            run_dir = tmp_path / "runs" / "pilot" / "T002"
            (run_dir / "baseline").mkdir(parents=True)
            (run_dir / "edits").mkdir()

            snippet_name = "snippet_01.c"
            (run_dir / "baseline" / snippet_name).write_text(
                "void work(char *input) { use(input); }\n",
                encoding="utf-8",
            )
            (run_dir / "edits" / snippet_name).write_text(
                "void work(char *input) { if (input == NULL) return; use_safe(input); }\n",
                encoding="utf-8",
            )
            (run_dir / "condition.txt").write_text("security\n", encoding="utf-8")
            (run_dir / "study_assignment.json").write_text(
                json.dumps(
                    {
                        "participant_id": "T002",
                        "phase": "pilot",
                        "source_kind": "dataset",
                        "snippet_mappings": [
                            {
                                "snippet_id": "S01",
                                "participant_filename": snippet_name,
                                "language": "c",
                                "vulnerability_type": "Race Condition",
                                "cwe_primary": "CWE-362",
                                "is_vulnerable": "1",
                                "source_kind": "dataset",
                            }
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            legacy_metadata = tmp_path / "legacy_metadata.csv"
            _write_csv(
                legacy_metadata,
                [
                    {
                        "snippet_id": "IGNORED",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "language": "python",
                        "baseline_relpath": "snippets/baseline/SQLi/SQLi_01.py",
                        "gold_relpath": "snippets/gold/SQLi/SQLi_01.py",
                    }
                ],
            )

            judge_result = JudgeResult(
                verdict="absent",
                confidence=0.81,
                rationale="The edited code no longer exposes the original unsafe behavior.",
                evidence="The edited path routes work through use_safe after a guard check.",
                raw_json={"verdict": "absent"},
                strategy_name="zero_shot",
                strategy_results=None,
                vote_rule="single",
            )

            with patch.object(analyze_edits, "_judge_enabled_from_config", return_value=True), patch.object(
                analyze_edits,
                "judge_edited_code_with_ollama",
                return_value=judge_result,
            ):
                analyze_edits.analyze_participant(str(run_dir), str(legacy_metadata))

            with (run_dir / "analysis" / "results.csv").open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["judge_verdict"], "absent")
            self.assertEqual(rows[0]["outcome"], "Mitigated")

    def test_detector_can_be_disabled_even_for_supported_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            run_dir = tmp_path / "runs" / "pilot" / "T003"
            (run_dir / "baseline").mkdir(parents=True)
            (run_dir / "edits").mkdir()

            snippet_name = "snippet_01.py"
            (run_dir / "baseline" / snippet_name).write_text(
                'query = "SELECT * FROM users WHERE id = " + user_id\ncursor.execute(query)\n',
                encoding="utf-8",
            )
            (run_dir / "edits" / snippet_name).write_text(
                'query = "SELECT * FROM users WHERE id = ?"\ncursor.execute(query, (user_id,))\n',
                encoding="utf-8",
            )
            (run_dir / "condition.txt").write_text("security\n", encoding="utf-8")
            (run_dir / "study_assignment.json").write_text(
                json.dumps(
                    {
                        "participant_id": "T003",
                        "phase": "pilot",
                        "source_kind": "dataset",
                        "snippet_mappings": [
                            {
                                "snippet_id": "S01",
                                "participant_filename": snippet_name,
                                "language": "python",
                                "vuln_type": "SQLi",
                                "cwe_primary": "CWE-89",
                                "is_vulnerable": "1",
                                "source_kind": "dataset",
                            }
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            legacy_metadata = tmp_path / "legacy_metadata.csv"
            _write_csv(
                legacy_metadata,
                [
                    {
                        "snippet_id": "IGNORED",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "language": "python",
                        "baseline_relpath": "snippets/baseline/SQLi/SQLi_01.py",
                        "gold_relpath": "snippets/gold/SQLi/SQLi_01.py",
                    }
                ],
            )

            judge_result = JudgeResult(
                verdict="absent",
                confidence=0.95,
                rationale="The query now uses a bound parameter.",
                evidence="cursor.execute passes a placeholder and tuple args.",
                raw_json={"verdict": "absent"},
                strategy_name="zero_shot",
                strategy_results=None,
                vote_rule="single",
            )

            with patch.dict(os.environ, {"GLACIER_ENABLE_DETECTOR": "0"}, clear=False), patch.object(
                analyze_edits,
                "_judge_enabled_from_config",
                return_value=True,
            ), patch.object(
                analyze_edits,
                "judge_edited_code_with_ollama",
                return_value=judge_result,
            ):
                analyze_edits.analyze_participant(str(run_dir), str(legacy_metadata))

            with (run_dir / "analysis" / "results.csv").open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["scoring_mode"], "judge_only")
            self.assertEqual(rows[0]["judge_verdict"], "absent")
            self.assertEqual(rows[0]["outcome"], "Mitigated")

    def test_aggregate_pilot_uses_canonical_nested_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            runs_root = tmp_path / "runs" / "pilot"
            run_dir = runs_root / "submission_pilot_M001_20260622T195510Z" / "M001"
            analysis_dir = run_dir / "analysis"
            analysis_dir.mkdir(parents=True)
            (run_dir / "edits").mkdir()
            (run_dir / "logs").mkdir()

            (run_dir / "condition.txt").write_text("security\n", encoding="utf-8")
            (run_dir / "start_end_times.json").write_text(
                json.dumps({"start": "2026-06-22T10:00:00+00:00", "end": "2026-06-22T10:10:00+00:00"}, indent=2),
                encoding="utf-8",
            )
            (analysis_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "primary_source": "judge",
                        "primary_scored_snippets": 1,
                        "primary_counts": {"Mitigated": 1, "Preserved": 0, "UNKNOWN": 0},
                        "primary_rates": {"mitigation": 1.0, "persistence": 0.0, "abstention": 0.0},
                        "scored_snippets": 1,
                        "rates": {"mitigation": 1.0, "persistence": 0.0, "amplification": 0.0},
                        "judge_scored_snippets": 1,
                        "judge_detector_disagreement_rate": 0.0,
                        "interaction": {
                            "interaction_logged_snippets": 1,
                            "avg_turns": 2.0,
                            "avg_applied_ratio": 1.0,
                            "avg_confidence_1to5": 4.0,
                            "strategy_distribution": {"zero_shot": 1},
                        },
                        "participant_profile": {
                            "programming_experience": "3-5 years",
                            "language_experience": "advanced",
                            "llm_coding_experience": "daily",
                            "security_experience": "coursework",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            out_csv = tmp_path / "pilot_summary.csv"
            study_cli.cmd_aggregate_pilot(
                argparse.Namespace(
                    runs_root=str(runs_root),
                    out_csv=str(out_csv),
                    require_judge_primary=False,
                )
            )

            with out_csv.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_id"], "M001")


if __name__ == "__main__":
    unittest.main()
