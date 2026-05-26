from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from tools.analysis.modeling import write_model_artifacts


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_run(
    runs_root: Path,
    run_id: str,
    condition: str,
    results_rows: list[dict[str, str]],
    participant_profile: dict[str, str],
) -> None:
    run_dir = runs_root / run_id
    analysis_dir = run_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "condition.txt").write_text(condition + "\n", encoding="utf-8")
    (analysis_dir / "summary.json").write_text(
        json.dumps(
            {
                "primary_source": "judge",
                "participant_profile": participant_profile,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_csv(analysis_dir / "results.csv", results_rows)


class ModelingTests(unittest.TestCase):
    def test_write_model_artifacts_builds_deidentified_dataset_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            runs_root = tmp_root / "runs"

            _write_run(
                runs_root,
                "P001",
                "security",
                [
                    {
                        "snippet_id": "SQLi_01",
                        "status": "ok",
                        "judge_verdict": "absent",
                        "judge_enabled": "True",
                        "outcome": "Mitigated",
                        "language": "python",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "llm_turns": "4",
                        "llm_applied_turns": "3",
                        "llm_applied_ratio": "0.75",
                        "llm_confidence_1to5": "5",
                        "llm_strategy_primary": "few_shot",
                    },
                    {
                        "snippet_id": "CMDi_01",
                        "status": "ok",
                        "judge_verdict": "present",
                        "judge_enabled": "True",
                        "outcome": "Preserved",
                        "language": "java",
                        "vuln_type": "CMDi",
                        "cwe": "CWE-78",
                        "llm_turns": "1",
                        "llm_applied_turns": "0",
                        "llm_applied_ratio": "0.00",
                        "llm_confidence_1to5": "2",
                        "llm_strategy_primary": "zero_shot",
                    },
                    {
                        "snippet_id": "SQLi_CPP_01",
                        "status": "ok",
                        "judge_verdict": "uncertain",
                        "judge_enabled": "True",
                        "outcome": "Obfuscated",
                        "language": "cpp",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "llm_turns": "2",
                        "llm_applied_turns": "1",
                        "llm_applied_ratio": "0.50",
                        "llm_confidence_1to5": "3",
                        "llm_strategy_primary": "other",
                    },
                ],
                {
                    "programming_experience": "3-5 years",
                    "language_experience": "advanced",
                    "llm_coding_experience": "weekly",
                    "security_experience": "coursework",
                },
            )

            _write_run(
                runs_root,
                "P002",
                "productivity",
                [
                    {
                        "snippet_id": "SQLi_01",
                        "status": "ok",
                        "judge_verdict": "present",
                        "judge_enabled": "True",
                        "outcome": "Preserved",
                        "language": "python",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "llm_turns": "2",
                        "llm_applied_turns": "1",
                        "llm_applied_ratio": "0.50",
                        "llm_confidence_1to5": "3",
                        "llm_strategy_primary": "few_shot",
                    },
                    {
                        "snippet_id": "CMDi_01",
                        "status": "ok",
                        "judge_verdict": "absent",
                        "judge_enabled": "True",
                        "outcome": "Mitigated",
                        "language": "java",
                        "vuln_type": "CMDi",
                        "cwe": "CWE-78",
                        "llm_turns": "5",
                        "llm_applied_turns": "4",
                        "llm_applied_ratio": "0.80",
                        "llm_confidence_1to5": "4",
                        "llm_strategy_primary": "zero_shot",
                    },
                    {
                        "snippet_id": "SQLi_CPP_01",
                        "status": "ok",
                        "judge_verdict": "present",
                        "judge_enabled": "True",
                        "outcome": "Preserved",
                        "language": "cpp",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "llm_turns": "1",
                        "llm_applied_turns": "0",
                        "llm_applied_ratio": "0.00",
                        "llm_confidence_1to5": "2",
                        "llm_strategy_primary": "other",
                    },
                ],
                {
                    "programming_experience": "1-2 years",
                    "language_experience": "intermediate",
                    "llm_coding_experience": "monthly",
                    "security_experience": "self-taught",
                },
            )

            out_csv = tmp_root / "pilot_model_data.csv"
            out_json = tmp_root / "pilot_models.json"
            out_txt = tmp_root / "pilot_models.txt"
            payload = write_model_artifacts(
                runs_root=runs_root,
                out_csv=out_csv,
                out_json=out_json,
                out_txt=out_txt,
            )

            self.assertEqual(payload["dataset_rows"], 6)
            self.assertEqual(payload["model"]["excluded_unknown"], 1)
            self.assertEqual(payload["model"]["status"], "ok")

            with out_csv.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            participant_ids = {row["participant_id"] for row in rows}
            self.assertEqual(participant_ids, {"participant_001", "participant_002"})
            self.assertNotIn("P001", participant_ids)
            self.assertNotIn("P002", participant_ids)

            text = out_txt.read_text(encoding="utf-8")
            self.assertIn("Snippet-level mitigation model", text)
            self.assertIn("de-identified local labels", text)

            payload_json = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertIn("condition_security", payload_json["model"]["focal_terms"])


if __name__ == "__main__":
    unittest.main()
