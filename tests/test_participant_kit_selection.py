from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile


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

    def test_python_dataset_formatter_breaks_flattened_statements(self) -> None:
        source = "def demo(user): query = user.strip(); if query: return query"

        formatted = participant_kit._normalize_code_sample(source, "Python")

        self.assertIn("def demo(user):\n", formatted)
        self.assertIn("    query = user.strip()\n", formatted)
        self.assertIn("    if query:\n", formatted)
        self.assertIn("        return query\n", formatted)

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
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-89",
                    "vulnerability_type": "Injection",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/low1.py",
                },
                {
                    "sample_id": "LOW002",
                    "language": "Python",
                    "hardness_strict": "low",
                    "code_sample": "print('low2')",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-89",
                    "vulnerability_type": "Injection",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/low2.py",
                },
                {
                    "sample_id": "LOW003",
                    "language": "Python",
                    "hardness_strict": "low",
                    "code_sample": "print('low3')",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-89",
                    "vulnerability_type": "Injection",
                    "primary_expertise_area": "General Software Utility",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/low3.py",
                },
                {
                    "sample_id": "LOW004",
                    "language": "Python",
                    "hardness_strict": "low",
                    "code_sample": "print('low4')",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-89",
                    "vulnerability_type": "Injection",
                    "primary_expertise_area": "Security / Application Security",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/low4.py",
                },
                {
                    "sample_id": "LOW_BAD",
                    "language": "Python",
                    "hardness_strict": "low",
                    "code_sample": "print('bad row')",
                    "is_vulnerable": "1",
                    "cwe_primary": "NVD-CWE-NOINFO",
                    "vulnerability_type": "Unknown/Unspecified",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/low_bad.py",
                },
                {
                    "sample_id": "MED001",
                    "language": "Java",
                    "hardness_strict": "medium",
                    "code_sample": "class Med1 {}",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-79",
                    "vulnerability_type": "Injection",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/Med1.java",
                },
                {
                    "sample_id": "MED002",
                    "language": "Java",
                    "hardness_strict": "medium",
                    "code_sample": "class Med2 {}",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-79",
                    "vulnerability_type": "Injection",
                    "primary_expertise_area": "Database / Persistence",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/Med2.java",
                },
                {
                    "sample_id": "MED003",
                    "language": "Java",
                    "hardness_strict": "medium",
                    "code_sample": "class Med3 {}",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-79",
                    "vulnerability_type": "Injection",
                    "primary_expertise_area": "Frontend / UI Engineering",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/Med3.java",
                },
                {
                    "sample_id": "MED004",
                    "language": "Java",
                    "hardness_strict": "medium",
                    "code_sample": "class Med4 {}",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-79",
                    "vulnerability_type": "Injection",
                    "primary_expertise_area": "General Software Utility",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/Med4.java",
                },
                {
                    "sample_id": "HIGH001",
                    "language": "C",
                    "hardness_strict": "high",
                    "code_sample": "int high1(void) { return 1; }",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-119",
                    "vulnerability_type": "Bounds Error",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/high1.c",
                },
                {
                    "sample_id": "HIGH002",
                    "language": "C",
                    "hardness_strict": "high",
                    "code_sample": "int high2(void) { return 2; }",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-119",
                    "vulnerability_type": "Bounds Error",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/high2.c",
                },
                {
                    "sample_id": "HIGH003",
                    "language": "C",
                    "hardness_strict": "high",
                    "code_sample": "int high3(void) { return 3; }",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-119",
                    "vulnerability_type": "Bounds Error",
                    "primary_expertise_area": "Backend / API Development",
                    "secondary_expertise_areas": "[]",
                    "file_path": "src/high3.c",
                },
                {
                    "sample_id": "HIGH004",
                    "language": "C",
                    "hardness_strict": "high",
                    "code_sample": "int high4(void) { return 4; }",
                    "is_vulnerable": "1",
                    "cwe_primary": "CWE-119",
                    "vulnerability_type": "Bounds Error",
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

                support_dir = out_root / "P777" / participant_kit.PARTICIPANT_SUPPORT_DIR_NAME
                run_dir = support_dir / "run_pilot_P777"
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
                self.assertNotIn("LOW_BAD", {row["source_snippet_id"] for row in selected})

                baseline_files = sorted((run_dir / "baseline").iterdir())
                edit_files = sorted((run_dir / "edits").iterdir())
                participant_readme = (out_root / "P777" / "README.md").read_text(encoding="utf-8")
                packager_text = (support_dir / "package_submission.py").read_text(encoding="utf-8")
                self.assertTrue(support_dir.exists())
                self.assertTrue((support_dir / "participant_web_app.py").exists())
                self.assertTrue((support_dir / "study_config.lock.json").exists())
                self.assertFalse((out_root / "P777" / "participant_web_app.py").exists())
                self.assertEqual(len(baseline_files), 9)
                self.assertEqual(len(edit_files), 9)
                self.assertTrue(all(file.read_text(encoding="utf-8") == "" for file in edit_files))
                self.assertTrue(all(path.name.startswith("snippet_") for path in baseline_files))
                self.assertTrue((out_root / "P777" / "Launch_Study_Web_App.bat").exists())
                self.assertFalse((out_root / "P777" / "Launch_Study_Web_App.sh").exists())
                self.assertIn("Launch_Study_Web_App.bat", participant_readme)
                self.assertIn("participant ID as the ZIP name", participant_readme)
                self.assertNotIn("Launch_Study_Web_App.sh", participant_readme)
                self.assertIn('zip_name = "P777.zip"', packager_text)
                self.assertIn("arc = str(p.relative_to(RUN_DIR)).replace", packager_text)
                self.assertNotIn('Path("P777") / p.relative_to(RUN_DIR)', packager_text)
                share_zip = out_root / "_share_zips" / "participant_kit_pilot_P777.zip"
                self.assertTrue(share_zip.exists())
                with ZipFile(share_zip, "r") as zf:
                    names = set(zf.namelist())
                self.assertIn("P777/Launch_Study_Web_App.bat", names)
                self.assertIn(f"P777/{participant_kit.PARTICIPANT_SUPPORT_DIR_NAME}/participant_web_app.py", names)

                self.assertEqual(assignment["participant_os"], "windows")
                self.assertEqual(assignment["source_kind"], "dataset")
                self.assertTrue(all(sid.startswith("S") for sid in assignment["snippet_ids"]))
                self.assertTrue(all(name.startswith("snippet_") for name in assignment["snippet_files"].values()))
                self.assertNotIn("snippet_mappings", assignment)
                self.assertNotIn("expertise_areas", assignment)
                self.assertNotIn("samples_per_hardness", assignment)
                self.assertEqual(json.loads(researcher_map.read_text(encoding="utf-8"))["out_root"], out_root.name)
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
                            "is_vulnerable": "1",
                            "cwe_primary": "CWE-119",
                            "vulnerability_type": "Bounds Error",
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

                support_dir = out_root / "P888" / participant_kit.PARTICIPANT_SUPPORT_DIR_NAME
                run_dir = support_dir / "run_pilot_P888"
                assignment = json.loads((run_dir / "study_assignment.json").read_text(encoding="utf-8"))
                selected = json.loads(researcher_map.read_text(encoding="utf-8"))["snippet_mappings"]
                manifest = json.loads((support_dir / "kit_manifest.json").read_text(encoding="utf-8"))
                participant_readme = (out_root / "P888" / "README.md").read_text(encoding="utf-8")

                self.assertEqual(len(selected), 9)
                self.assertTrue(all(row["source_snippet_id"].startswith("primevul:") for row in selected))
                self.assertTrue(all(sid.startswith("S") for sid in assignment["snippet_ids"]))
                self.assertTrue((support_dir / "participant_web_app.py").exists())
                self.assertTrue((support_dir / "package_submission.py").exists())
                self.assertFalse((out_root / "P888" / "package_submission.py").exists())

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
                launcher_bytes = (out_root / "P888" / "Launch_Study_Web_App.sh").read_bytes()
                self.assertNotIn(b"\r\n", launcher_bytes)
                self.assertNotIn("snippet_mappings", assignment)
                self.assertNotIn("expertise_areas", assignment)
                self.assertEqual(json.loads(researcher_map.read_text(encoding="utf-8"))["out_root"], out_root.name)
                share_zip = out_root / "_share_zips" / "participant_kit_pilot_P888.zip"
                self.assertTrue(share_zip.exists())
                with ZipFile(share_zip, "r") as zf:
                    names = set(zf.namelist())
                self.assertIn("P888/Launch_Study_Web_App.sh", names)
                self.assertIn(f"P888/{participant_kit.PARTICIPANT_SUPPORT_DIR_NAME}/study_config.lock.json", names)
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

    def test_clean_participant_kits_removes_matching_share_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            kits_root = Path(tmp_dir) / "participant_kits"
            participant_dir = kits_root / "P100"
            share_dir = kits_root / "_share_zips"
            participant_dir.mkdir(parents=True)
            share_dir.mkdir(parents=True)
            (participant_dir / "README.md").write_text("kit", encoding="utf-8")
            share_zip = share_dir / "participant_kit_pilot_P100.zip"
            share_zip.write_text("zip", encoding="utf-8")

            args = argparse.Namespace(
                out_root=str(kits_root),
                participant_id="P100",
                all=False,
                dry_run=False,
            )

            participant_kit.clean_participant_kits(args)

            self.assertFalse(participant_dir.exists())
            self.assertFalse(share_zip.exists())

    def test_build_participant_kit_records_dropbox_metadata_in_researcher_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dataset_csv = tmp_path / "classified_dataset.csv"
            out_root = tmp_path / "kits"
            researcher_map = (
                Path(__file__).resolve().parents[1]
                / "participant_kits"
                / "_researcher_maps"
                / "pilot__P909.json"
            )

            rows = []
            for bucket in ("low", "medium", "high"):
                for idx in range(1, 4):
                    rows.append(
                        {
                            "sample_id": f"{bucket.upper()}{idx:03d}",
                            "language": "Python",
                            "hardness_strict": bucket,
                            "code_sample": f"print('{bucket}-{idx}')",
                            "is_vulnerable": "1",
                            "cwe_primary": "CWE-89",
                            "vulnerability_type": "Injection",
                            "primary_expertise_area": "Backend / API Development",
                            "secondary_expertise_areas": "[]",
                            "file_path": f"src/{bucket}_{idx}.py",
                        }
                    )

            with dataset_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)

            args = argparse.Namespace(
                participant_id="P909",
                condition="security",
                phase="pilot",
                metadata_csv=str(dataset_csv),
                expertise_areas="Backend / API Development",
                samples_per_hardness=3,
                selection_seed=11,
                out_root=str(out_root),
                study_id="repairaudit-v1",
                participant_os="windows",
                llm_provider="ollama",
                llm_model="qwen3.6:27b",
                temperature=0.2,
                top_p=0.9,
                top_k=40,
                num_predict=1536,
                seed=42,
                dropbox_publish=True,
                overwrite=False,
            )

            try:
                with patch.object(
                    participant_kit,
                    "_publish_dropbox_artifacts",
                    return_value={
                        "published_utc": "2026-07-08T12:00:00+00:00",
                        "kit_dropbox_path": "/RepairAudit/kits/P909/participant_kit_pilot_P909.zip",
                        "kit_shared_url": "https://example.test/kit",
                        "submission_folder_path": "/RepairAudit/submissions/P909",
                        "submission_file_request_id": "fr_123",
                        "submission_file_request_url": "https://example.test/request",
                    },
                ) as publish_dropbox:
                    participant_kit.build_participant_kit(args)

                payload = json.loads(researcher_map.read_text(encoding="utf-8"))
                self.assertIn("dropbox", payload)
                self.assertEqual(payload["dropbox"]["kit_shared_url"], "https://example.test/kit")
                self.assertEqual(payload["dropbox"]["submission_file_request_url"], "https://example.test/request")
                publish_dropbox.assert_called_once()
            finally:
                researcher_map.unlink(missing_ok=True)

    @unittest.skipUnless(os.name == "nt", "Windows lock cleanup test is Windows-only.")
    def test_remove_tree_with_lock_cleanup_retries_after_stale_python_stop(self) -> None:
        target = Path("C:/temp/fake_kit")
        calls: list[str] = []

        def fake_rmtree(path: Path) -> None:
            calls.append(str(path))
            if len(calls) == 1:
                raise OSError(
                    "The process cannot access the file because it is being used by another process."
                )

        with patch.object(participant_kit.shutil, "rmtree", side_effect=fake_rmtree):
            with patch.object(
                participant_kit,
                "_terminate_windows_python_cwd_processes",
                return_value=[1234],
            ) as stop_mock:
                with patch.object(
                    participant_kit,
                    "_windows_directory_lock_candidates",
                    return_value=[],
                ):
                    participant_kit._remove_tree_with_lock_cleanup(target)

        stop_mock.assert_called_once_with(target)
        self.assertEqual(calls, [str(target), str(target)])


if __name__ == "__main__":
    unittest.main()
