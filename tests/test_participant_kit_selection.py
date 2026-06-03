from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "participant_kit.py"
)
SPEC = importlib.util.spec_from_file_location("participant_kit_module", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load module from {MODULE_PATH}")
participant_kit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(participant_kit)


class ParticipantKitSelectionTests(unittest.TestCase):
    def test_dataset_sampling_balances_hardness_with_bucket_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dataset_csv = tmp_path / "classified_dataset.csv"
            out_root = tmp_path / "kits"

            rows = [
                {
                    "sample_id": "LOW001",
                    "language": "Python",
                    "hardness_strict": "low",
                    "code_sample": "print('low1')",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/low1.py",
                },
                {
                    "sample_id": "LOW002",
                    "language": "Python",
                    "hardness_strict": "low",
                    "code_sample": "print('low2')",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/low2.py",
                },
                {
                    "sample_id": "LOW003",
                    "language": "Python",
                    "hardness_strict": "low",
                    "code_sample": "print('low3')",
                    "primary_expertise_area": "General Software Utility",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/low3.py",
                },
                {
                    "sample_id": "LOW004",
                    "language": "Python",
                    "hardness_strict": "low",
                    "code_sample": "print('low4')",
                    "primary_expertise_area": "Security / Application Security",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/low4.py",
                },
                {
                    "sample_id": "MED001",
                    "language": "Java",
                    "hardness_strict": "medium",
                    "code_sample": "class Med1 {}",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/Med1.java",
                },
                {
                    "sample_id": "MED002",
                    "language": "Java",
                    "hardness_strict": "medium",
                    "code_sample": "class Med2 {}",
                    "primary_expertise_area": "Database / Persistence",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/Med2.java",
                },
                {
                    "sample_id": "MED003",
                    "language": "Java",
                    "hardness_strict": "medium",
                    "code_sample": "class Med3 {}",
                    "primary_expertise_area": "Frontend / UI Engineering",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/Med3.java",
                },
                {
                    "sample_id": "MED004",
                    "language": "Java",
                    "hardness_strict": "medium",
                    "code_sample": "class Med4 {}",
                    "primary_expertise_area": "General Software Utility",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/Med4.java",
                },
                {
                    "sample_id": "HIGH001",
                    "language": "C",
                    "hardness_strict": "high",
                    "code_sample": "int high1(void) { return 1; }",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/high1.c",
                },
                {
                    "sample_id": "HIGH002",
                    "language": "C",
                    "hardness_strict": "high",
                    "code_sample": "int high2(void) { return 2; }",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/high2.c",
                },
                {
                    "sample_id": "HIGH003",
                    "language": "C",
                    "hardness_strict": "high",
                    "code_sample": "int high3(void) { return 3; }",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/high3.c",
                },
                {
                    "sample_id": "HIGH004",
                    "language": "C",
                    "hardness_strict": "high",
                    "code_sample": "int high4(void) { return 4; }",
                    "primary_expertise_area": "Networking / Distributed Systems",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/high4.c",
                },
            ]

            with dataset_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)

            args = argparse.Namespace(
                participant_id="P777",
                condition="security",
                phase="pilot",
                metadata_csv=str(dataset_csv),
                expertise_areas="Backend / API Development",
                samples_per_hardness=3,
                selection_seed=42,
                out_root=str(out_root),
                study_id="repairaudit-v1",
                llm_provider="ollama",
                llm_model="qwen2.5-coder:7b-instruct",
                temperature=0.2,
                top_p=0.9,
                top_k=40,
                num_predict=1200,
                seed=42,
                overwrite=False,
            )

            participant_kit.build_participant_kit(args)

            run_dir = out_root / "P777" / "run_pilot_P777"
            selection_csv = run_dir / "snippet_source.csv"
            self.assertTrue(selection_csv.exists())

            with selection_csv.open("r", newline="", encoding="utf-8") as handle:
                selected = list(csv.DictReader(handle))

            self.assertEqual(len(selected), 9)
            bucket_counts = {"low": 0, "medium": 0, "high": 0}
            fallback_seen = False
            for row in selected:
                bucket_counts[row["selection_bucket"]] += 1
                if row["selection_source"] == "bucket_fallback":
                    fallback_seen = True
            self.assertEqual(bucket_counts, {"low": 3, "medium": 3, "high": 3})
            self.assertTrue(fallback_seen)

            baseline_files = sorted((run_dir / "baseline").iterdir())
            edit_files = sorted((run_dir / "edits").iterdir())
            self.assertEqual(len(baseline_files), 9)
            self.assertEqual(len(edit_files), 9)
            self.assertTrue(all(file.read_text(encoding="utf-8") == "" for file in edit_files))

            assignment = json.loads((run_dir / "study_assignment.json").read_text(encoding="utf-8"))
            self.assertEqual(assignment["expertise_areas"], ["Backend / API Development"])
            self.assertEqual(assignment["samples_per_hardness"], 3)
            self.assertEqual(assignment["source_kind"], "dataset")

    def test_dataset_sampling_accepts_row_uid_and_sanitizes_output_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dataset_csv = tmp_path / "classified_dataset.csv"
            out_root = tmp_path / "kits"

            rows = []
            for bucket in ("low", "medium", "high"):
                for idx in range(1, 4):
                    rows.append(
                        {
                            "row_uid": f"primevul:{bucket}:{idx}",
                            "language": "C/C++",
                            "hardness_strict": bucket,
                            "code_sample": f"int {bucket}_{idx}(void) {{ return {idx}; }}",
                            "primary_expertise_area": "Security / Application Security",
                            "secondary_expertise_areas": "[]",
                            "file_path": f"src/{bucket}_{idx}.c",
                        }
                    )

            with dataset_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)

            args = argparse.Namespace(
                participant_id="P888",
                condition="security",
                phase="pilot",
                metadata_csv=str(dataset_csv),
                expertise_areas="Security / Application Security",
                samples_per_hardness=3,
                selection_seed=7,
                out_root=str(out_root),
                study_id="repairaudit-v1",
                llm_provider="ollama",
                llm_model="qwen2.5-coder:7b-instruct",
                temperature=0.2,
                top_p=0.9,
                top_k=40,
                num_predict=1200,
                seed=42,
                overwrite=False,
            )

            participant_kit.build_participant_kit(args)

            run_dir = out_root / "P888" / "run_pilot_P888"
            with (run_dir / "snippet_source.csv").open("r", newline="", encoding="utf-8") as handle:
                selected = list(csv.DictReader(handle))

            self.assertEqual(len(selected), 9)
            self.assertTrue(all(row["snippet_id"].startswith("primevul:") for row in selected))

            output_names = [path.name for path in (run_dir / "baseline").iterdir()]
            self.assertTrue(all(":" not in name for name in output_names))
            self.assertTrue(all(name.endswith(".c") for name in output_names))


if __name__ == "__main__":
    unittest.main()
