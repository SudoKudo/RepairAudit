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
    def test_c_like_dataset_formatter_splits_inline_comments_into_readable_lines(self) -> None:
        source = (
            "int demo(void) { /* comment one */ int x = 1; "
            "if (x) { /* comment two */ x++; } return x; }"
        )

        formatted = participant_kit._normalize_code_sample(source, "C")

        self.assertIn("/* comment one */\n", formatted)
        self.assertIn("/* comment two */\n", formatted)
        self.assertIn("if (x) {", formatted)
        self.assertTrue(formatted.endswith("\n"))

    def test_dataset_sampling_balances_hardness_with_bucket_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dataset_csv = tmp_path / "classified_dataset.csv"
            out_root = tmp_path / "kits"
            researcher_map = (
                Path(__file__).resolve().parents[1]
                / "participant_kits"
                / "_researcher_maps"
                / "pilot__P777.json"
            )

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
                participant_os="windows",
                llm_provider="ollama",
                llm_model="qwen2.5-coder:7b-instruct",
                temperature=0.2,
                top_p=0.9,
                top_k=40,
                num_predict=1200,
                seed=42,
                overwrite=False,
            )

            try:
                participant_kit.build_participant_kit(args)

                run_dir = out_root / "P777" / "run_pilot_P777"
                self.assertTrue(researcher_map.exists())

                assignment = json.loads((run_dir / "study_assignment.json").read_text(encoding="utf-8"))
                selected = json.loads(researcher_map.read_text(encoding="utf-8"))["snippet_mappings"]

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
                participant_readme = (out_root / "P777" / "README.md").read_text(encoding="utf-8")
                self.assertEqual(len(baseline_files), 9)
                self.assertEqual(len(edit_files), 9)
                self.assertTrue(all(file.read_text(encoding="utf-8") == "" for file in edit_files))
                self.assertTrue(all(path.name.startswith("snippet_") for path in baseline_files))
                self.assertTrue((out_root / "P777" / "Launch_Study_Web_App.bat").exists())
                self.assertFalse((out_root / "P777" / "Launch_Study_Web_App.sh").exists())
                self.assertIn("Launch_Study_Web_App.bat", participant_readme)
                self.assertNotIn("Launch_Study_Web_App.sh", participant_readme)

                self.assertEqual(assignment["expertise_areas"], ["Backend / API Development"])
                self.assertEqual(assignment["samples_per_hardness"], 3)
                self.assertEqual(assignment["participant_os"], "windows")
                self.assertEqual(assignment["source_kind"], "dataset")
                self.assertTrue(all(sid.startswith("S") for sid in assignment["snippet_ids"]))
                self.assertTrue(all(name.startswith("snippet_") for name in assignment["snippet_files"].values()))
            finally:
                researcher_map.unlink(missing_ok=True)

    def test_dataset_sampling_accepts_row_uid_and_sanitizes_output_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dataset_csv = tmp_path / "classified_dataset.csv"
            out_root = tmp_path / "kits"
            researcher_map = (
                Path(__file__).resolve().parents[1]
                / "participant_kits"
                / "_researcher_maps"
                / "pilot__P888.json"
            )

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
                participant_os="linux",
                llm_provider="ollama",
                llm_model="qwen2.5-coder:7b-instruct",
                temperature=0.2,
                top_p=0.9,
                top_k=40,
                num_predict=1200,
                seed=42,
                overwrite=False,
            )

            try:
                participant_kit.build_participant_kit(args)

                run_dir = out_root / "P888" / "run_pilot_P888"
                assignment = json.loads((run_dir / "study_assignment.json").read_text(encoding="utf-8"))
                selected = json.loads(researcher_map.read_text(encoding="utf-8"))["snippet_mappings"]
                manifest = json.loads((out_root / "P888" / "kit_manifest.json").read_text(encoding="utf-8"))
                participant_readme = (out_root / "P888" / "README.md").read_text(encoding="utf-8")

                self.assertEqual(len(selected), 9)
                self.assertTrue(all(row["source_snippet_id"].startswith("primevul:") for row in selected))
                self.assertTrue(all(sid.startswith("S") for sid in assignment["snippet_ids"]))

                output_names = [path.name for path in (run_dir / "baseline").iterdir()]
                self.assertTrue(all(":" not in name for name in output_names))
                self.assertTrue(all(name.startswith("snippet_") for name in output_names))
                self.assertTrue(all(name.endswith(".c") for name in output_names))
                self.assertEqual(manifest["source_csv"], dataset_csv.name)
                self.assertEqual(manifest["participant_os"], "linux")
                self.assertEqual(manifest["launcher_file"], "Launch_Study_Web_App.sh")
                self.assertFalse((out_root / "P888" / "Launch_Study_Web_App.bat").exists())
                self.assertTrue((out_root / "P888" / "Launch_Study_Web_App.sh").exists())
                self.assertIn("Launch_Study_Web_App.sh", participant_readme)
                self.assertNotIn("Launch_Study_Web_App.bat", participant_readme)
            finally:
                researcher_map.unlink(missing_ok=True)

    def test_clean_participant_kits_preserves_researcher_maps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            kits_root = Path(tmp_dir) / "participant_kits"
            participant_dir = kits_root / "P100"
            reserved_dir = kits_root / "_researcher_maps"
            participant_dir.mkdir(parents=True)
            reserved_dir.mkdir(parents=True)
            (participant_dir / "README.md").write_text("kit", encoding="utf-8")
            (reserved_dir / "pilot__P100.json").write_text("{}", encoding="utf-8")

            args = argparse.Namespace(
                out_root=str(kits_root),
                participant_id="",
                all=True,
                dry_run=False,
            )

            participant_kit.clean_participant_kits(args)

            self.assertFalse(participant_dir.exists())
            self.assertTrue(reserved_dir.exists())


if __name__ == "__main__":
    unittest.main()
