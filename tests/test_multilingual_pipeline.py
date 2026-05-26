from __future__ import annotations

import csv
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from scripts.participant_kit import build_participant_kit
from scripts.participant_web_app_template import StudyStore
from tools.analysis.analyze_edits import analyze_participant
from tools.analysis.detectors import detect_cmdi, detect_sqli
from tools.reporting.html_report import _snippet_paths


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class MultilingualPipelineTests(unittest.TestCase):
    def test_build_participant_kit_preserves_original_extension_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            metadata_csv = tmp_root / "snippet_metadata.csv"
            baseline = REPO_ROOT / "snippets" / "baseline" / "SQLi" / "SQLi_Java_01.java"
            gold = REPO_ROOT / "snippets" / "gold" / "SQLi" / "SQLi_Java_01.java"
            _write_csv(
                metadata_csv,
                [
                    {
                        "snippet_id": "SQLi_Java_01",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "language": "java",
                        "baseline_relpath": str(baseline),
                        "gold_relpath": str(gold),
                        "task_short": "Java SQLi sample",
                        "notes": "",
                    }
                ],
            )

            args = Namespace(
                participant_id="P900",
                condition="security",
                phase="pilot",
                metadata_csv=str(metadata_csv),
                out_root=str(tmp_root / "participant_kits"),
                study_id="repairaudit-test",
                llm_provider="ollama",
                llm_model="qwen2.5-coder:7b-instruct",
                temperature=0.2,
                top_p=0.9,
                top_k=40,
                num_predict=512,
                seed=42,
                overwrite=False,
            )

            build_participant_kit(args)

            kit_dir = tmp_root / "participant_kits" / "P900"
            run_dir = kit_dir / "run_pilot_P900"
            self.assertEqual((run_dir / "edits" / "SQLi_Java_01.java").read_text(encoding="utf-8"), "")
            self.assertEqual(
                (run_dir / "baseline" / "SQLi_Java_01.java").read_text(encoding="utf-8"),
                baseline.read_text(encoding="utf-8"),
            )

            lock_payload = json.loads((kit_dir / "study_config.lock.json").read_text(encoding="utf-8"))
            manifest = json.loads((kit_dir / "kit_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(lock_payload["snippet_files"]["SQLi_Java_01"], "SQLi_Java_01.java")
            self.assertEqual(manifest["snippet_files"]["SQLi_Java_01"], "SQLi_Java_01.java")

            readme = (kit_dir / "README.md").read_text(encoding="utf-8")
            self.assertIn("Final Submitted Code", readme)
            self.assertIn("reference-only", readme)

    def test_study_store_loads_and_saves_non_python_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kit_root = Path(tmp)
            run_dir = kit_root / "run_pilot_P901"
            baseline_dir = run_dir / "baseline"
            edits_dir = run_dir / "edits"
            logs_dir = run_dir / "logs"
            baseline_dir.mkdir(parents=True, exist_ok=True)
            edits_dir.mkdir(parents=True, exist_ok=True)
            logs_dir.mkdir(parents=True, exist_ok=True)

            (kit_root / "study_config.lock.json").write_text(
                json.dumps({"snippet_files": {"SQLi_CPP_01": "SQLi_CPP_01.cpp"}}, indent=2),
                encoding="utf-8",
            )
            (baseline_dir / "SQLi_CPP_01.cpp").write_text("int baseline = 1;\n", encoding="utf-8")
            (edits_dir / "SQLi_CPP_01.cpp").write_text("", encoding="utf-8")
            _write_csv(
                logs_dir / "snippet_log.csv",
                [
                    {
                        "snippet_id": "SQLi_CPP_01",
                        "tool": "LLM",
                        "model": "qwen2.5-coder:7b-instruct",
                        "turns": "0",
                        "applied_turns": "0",
                        "strategy_primary": "",
                        "confidence_1to5": "",
                        "first_prompt": "",
                        "final_prompt": "",
                        "notes": "",
                    }
                ],
            )

            store = StudyStore(kit_root)
            self.assertEqual(store.load_baseline_snippet("SQLi_CPP_01"), "int baseline = 1;\n")
            self.assertEqual(store.load_snippet("SQLi_CPP_01"), "")

            summary = store.get_row("SQLi_CPP_01")
            summary.update(
                {
                    "turns": "1",
                    "applied_turns": "1",
                    "strategy_primary": "zero_shot",
                    "confidence_1to5": "4",
                }
            )
            store.save_snippet_and_summary("SQLi_CPP_01", "int safe_fix = 2;\n", summary)

            self.assertEqual((edits_dir / "SQLi_CPP_01.cpp").read_text(encoding="utf-8"), "int safe_fix = 2;\n")
            self.assertFalse((edits_dir / "SQLi_CPP_01.py").exists())

    def test_multilingual_detectors_classify_sample_fixtures(self) -> None:
        java_baseline = (REPO_ROOT / "snippets" / "baseline" / "SQLi" / "SQLi_Java_01.java").read_text(encoding="utf-8")
        java_gold = (REPO_ROOT / "snippets" / "gold" / "SQLi" / "SQLi_Java_01.java").read_text(encoding="utf-8")
        c_baseline = (REPO_ROOT / "snippets" / "baseline" / "CMDi" / "CMDi_C_01.c").read_text(encoding="utf-8")
        c_gold = (REPO_ROOT / "snippets" / "gold" / "CMDi" / "CMDi_C_01.c").read_text(encoding="utf-8")
        cpp_baseline = (REPO_ROOT / "snippets" / "baseline" / "SQLi" / "SQLi_CPP_01.cpp").read_text(encoding="utf-8")
        cpp_gold = (REPO_ROOT / "snippets" / "gold" / "SQLi" / "SQLi_CPP_01.cpp").read_text(encoding="utf-8")

        self.assertEqual(detect_sqli(java_baseline, language="java").verdict, "present")
        self.assertEqual(detect_sqli(java_gold, language="java").verdict, "absent")
        self.assertEqual(detect_cmdi(c_baseline, language="c").verdict, "present")
        self.assertEqual(detect_cmdi(c_gold, language="c").verdict, "absent")
        self.assertEqual(detect_sqli(cpp_baseline, language="cpp").verdict, "present")
        self.assertEqual(detect_sqli(cpp_gold, language="cpp").verdict, "absent")

    def test_analyze_participant_writes_language_and_edited_filename_for_java(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = tmp_root / "runs" / "pilot" / "P902"
            edits_dir = run_dir / "edits"
            edits_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "analysis").mkdir(parents=True, exist_ok=True)
            (run_dir / "diffs").mkdir(parents=True, exist_ok=True)
            (run_dir / "condition.txt").write_text("security\n", encoding="utf-8")

            baseline = REPO_ROOT / "snippets" / "baseline" / "SQLi" / "SQLi_Java_01.java"
            gold = REPO_ROOT / "snippets" / "gold" / "SQLi" / "SQLi_Java_01.java"
            edited = edits_dir / "SQLi_Java_01.java"
            edited.write_text(gold.read_text(encoding="utf-8"), encoding="utf-8")

            metadata_csv = tmp_root / "snippet_metadata.csv"
            _write_csv(
                metadata_csv,
                [
                    {
                        "snippet_id": "SQLi_Java_01",
                        "vuln_type": "SQLi",
                        "cwe": "CWE-89",
                        "language": "java",
                        "baseline_relpath": str(baseline),
                        "gold_relpath": str(gold),
                    }
                ],
            )

            with patch("tools.analysis.analyze_edits._judge_enabled_from_config", return_value=False):
                analyze_participant(str(run_dir), str(metadata_csv))

            with (run_dir / "analysis" / "results.csv").open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["language"], "java")
            self.assertEqual(rows[0]["edited_filename"], "SQLi_Java_01.java")
            self.assertEqual(rows[0]["outcome"], "Mitigated")

    def test_report_path_resolution_uses_edited_filename(self) -> None:
        repo_root = REPO_ROOT
        edits_dir = REPO_ROOT / "snippets" / "baseline" / "SQLi"
        row = {
            "snippet_id": "SQLi_Java_01",
            "baseline_relpath": "snippets/baseline/SQLi/SQLi_Java_01.java",
            "gold_relpath": "snippets/gold/SQLi/SQLi_Java_01.java",
            "edited_filename": "SQLi_Java_01.java",
        }

        baseline, gold, edited = _snippet_paths(repo_root, row, edits_dir)
        self.assertEqual(baseline.name, "SQLi_Java_01.java")
        self.assertEqual(gold.name, "SQLi_Java_01.java")
        self.assertEqual(edited.name, "SQLi_Java_01.java")


if __name__ == "__main__":
    unittest.main()
