"""Regression tests for judge calibration and parser-mode handling."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.analysis import judge_audit, llm_judge


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class JudgeParserModeTests(unittest.TestCase):
    def test_strict_json_rejects_wrapped_output(self) -> None:
        text = 'Answer:\\n{"verdict":"present","confidence":0.9,"rationale":"r","evidence":"e"}'
        self.assertIsNone(llm_judge._extract_json_object(text, {"response": text}, "strict_json"))

    def test_embedded_json_accepts_wrapped_output(self) -> None:
        text = 'Answer:\\n{"verdict":"present","confidence":0.9,"rationale":"r","evidence":"e"}'
        obj = llm_judge._extract_json_object(text, {"response": text}, "embedded_json")
        self.assertIsNotNone(obj)
        self.assertEqual(obj["verdict"], "present")

    def test_tolerant_json_accepts_fenced_output(self) -> None:
        text = '```json\\n{"verdict":"absent","confidence":0.8,"rationale":"r","evidence":"e"}\\n```'
        obj = llm_judge._extract_json_object(text, {"response": text}, "tolerant_json")
        self.assertIsNotNone(obj)
        self.assertEqual(obj["verdict"], "absent")


class JudgeCalibrationBuilderTests(unittest.TestCase):
    def test_build_control_calibration_dataset_emits_baseline_and_gold_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            repo = tmp / "repo"
            repo.mkdir()
            baseline = repo / "baseline.py"
            gold = repo / "gold.py"
            baseline.write_text("unsafe_call(user_input)\n", encoding="utf-8")
            gold.write_text("safe_call(user_input)\n", encoding="utf-8")

            metadata_csv = tmp / "metadata.csv"
            _write_csv(
                metadata_csv,
                [
                    {
                        "snippet_id": "S01",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "language": "python",
                        "baseline_relpath": str(baseline),
                        "gold_relpath": str(gold),
                    }
                ],
            )

            out_csv = tmp / "judge_calibration.csv"
            summary = judge_audit.build_control_calibration_dataset(
                metadata_csv=metadata_csv,
                out_csv=out_csv,
            )

            with out_csv.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(summary["rows"], 2)
            self.assertEqual({row["expected_verdict"] for row in rows}, {"present", "absent"})
            self.assertEqual({row["source_case"] for row in rows}, {"baseline_control", "gold_control"})


class JudgeAuditRunnerTests(unittest.TestCase):
    def test_run_judge_audit_writes_freeze_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            baseline = tmp / "baseline.py"
            gold = tmp / "gold.py"
            baseline.write_text("unsafe_call(user_input)\n", encoding="utf-8")
            gold.write_text("safe_call(user_input)\n", encoding="utf-8")

            calibration_csv = tmp / "calibration.csv"
            _write_csv(
                calibration_csv,
                [
                    {
                        "case_id": "S01__baseline_present",
                        "snippet_id": "S01",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "language": "python",
                        "baseline_relpath": str(baseline),
                        "gold_relpath": str(gold),
                        "edited_relpath": str(baseline),
                        "expected_verdict": "present",
                        "source_case": "baseline_control",
                        "notes": "",
                    },
                    {
                        "case_id": "S01__gold_absent",
                        "snippet_id": "S01",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "language": "python",
                        "baseline_relpath": str(baseline),
                        "gold_relpath": str(gold),
                        "edited_relpath": str(gold),
                        "expected_verdict": "absent",
                        "source_case": "gold_control",
                        "notes": "",
                    },
                ],
            )

            def fake_judge(**kwargs):
                edited_code = str(kwargs.get("edited_code") or "")
                parser_mode = str(kwargs.get("parser_mode") or "")
                strategy = kwargs.get("strategy")
                selected = kwargs.get("selected_strategies")
                is_safe = "safe_call" in edited_code

                if parser_mode == "embedded_json":
                    verdict = "absent" if is_safe else "present"
                else:
                    verdict = "uncertain"

                strategy_name = str(strategy or ("ensemble" if selected else "cot"))
                vote_rule = str(kwargs.get("vote_rule") or ("majority" if selected else "single"))
                return llm_judge.JudgeResult(
                    verdict=verdict,
                    confidence=0.9 if verdict != "uncertain" else 0.1,
                    rationale="stub",
                    evidence="stub",
                    raw_json={"verdict": verdict},
                    strategy_name=strategy_name,
                    strategy_results=None,
                    vote_rule=vote_rule,
                    parser_mode=parser_mode,
                )

            out_root = tmp / "judge_audit"
            freeze_path = tmp / "judge_freeze.json"
            with patch.object(judge_audit.llm_judge, "judge_edited_code_with_ollama", side_effect=fake_judge):
                payload = judge_audit.run_judge_audit(
                    calibration_csv=calibration_csv,
                    out_root=out_root,
                    strategies=["cot", "zero_shot"],
                    parser_modes=["strict_json", "embedded_json"],
                    vote_rules=["majority"],
                    write_global_freeze=True,
                    global_freeze_path=freeze_path,
                )

            self.assertTrue(Path(payload["summary_json"]).exists())
            self.assertTrue(Path(payload["local_freeze_json"]).exists())
            self.assertTrue(freeze_path.exists())

            frozen = json.loads(freeze_path.read_text(encoding="utf-8"))
            self.assertEqual(frozen["llm_judge"]["parser_mode"], "embedded_json")
            self.assertIn("recommended_config_id", frozen)


if __name__ == "__main__":
    unittest.main()
