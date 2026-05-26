from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.participant_web_app_template import PARTICIPANT_PROFILE_FIELDS, StudyStore
from scripts.study_cli import _compute_time_to_first_secure_fix_seconds, _load_participant_profile


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class ProfileAndTimingTests(unittest.TestCase):
    def test_profile_schema_uses_language_experience(self) -> None:
        self.assertIn("language_experience", PARTICIPANT_PROFILE_FIELDS)
        self.assertNotIn("python_experience", PARTICIPANT_PROFILE_FIELDS)

    def test_study_store_writes_language_experience_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kit_root = Path(tmp)
            run_dir = kit_root / "run_pilot_P001"
            logs_dir = run_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (kit_root / "study_config.lock.json").write_text("{}", encoding="utf-8")
            (logs_dir / "snippet_log.csv").write_text("snippet_id\nSQLi_01\n", encoding="utf-8")

            store = StudyStore(kit_root)
            profile = store.write_participant_profile(
                {
                    "programming_experience": "3-5 years",
                    "language_experience": "advanced",
                    "llm_coding_experience": "weekly",
                    "security_experience": "coursework",
                }
            )

            self.assertEqual(profile["language_experience"], "advanced")
            saved = json.loads((logs_dir / "participant_profile.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["language_experience"], "advanced")
            self.assertNotIn("python_experience", saved)

    def test_load_participant_profile_reads_language_experience(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            logs_dir = run_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / "participant_profile.json").write_text(
                json.dumps(
                    {
                        "programming_experience": "1-2 years",
                        "language_experience": "intermediate",
                        "llm_coding_experience": "monthly",
                        "security_experience": "self-taught",
                    }
                ),
                encoding="utf-8",
            )

            profile = _load_participant_profile(run_dir)

            self.assertEqual(profile["language_experience"], "intermediate")
            self.assertNotIn("python_experience", profile)

    def test_snippet_timing_records_first_view_and_last_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kit_root = Path(tmp)
            run_dir = kit_root / "run_pilot_P001"
            logs_dir = run_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (kit_root / "study_config.lock.json").write_text("{}", encoding="utf-8")
            (logs_dir / "snippet_log.csv").write_text("snippet_id\nSQLi_01\n", encoding="utf-8")

            store = StudyStore(kit_root)
            store.mark_snippet_started("SQLi_01")
            store.mark_snippet_saved("SQLi_01")

            timing_path = run_dir / "timings" / "snippet_times.json"
            payload = json.loads(timing_path.read_text(encoding="utf-8"))
            self.assertIn("start", payload["SQLi_01"])
            self.assertIn("end", payload["SQLi_01"])

    def test_time_to_first_secure_fix_handles_z_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            analysis_dir = run_dir / "analysis"
            timings_dir = run_dir / "timings"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            timings_dir.mkdir(parents=True, exist_ok=True)

            (run_dir / "start_end_times.json").write_text(
                json.dumps({"start": "2026-04-08T10:00:00Z", "end": "2026-04-08T10:20:00Z"}),
                encoding="utf-8",
            )
            (timings_dir / "snippet_times.json").write_text(
                json.dumps({"SQLi_01": {"start": "2026-04-08T10:01:00Z", "end": "2026-04-08T10:05:00Z"}}),
                encoding="utf-8",
            )
            _write_csv(
                analysis_dir / "results.csv",
                [{"snippet_id": "SQLi_01", "outcome": "Mitigated", "status": "ok"}],
            )

            seconds = _compute_time_to_first_secure_fix_seconds(run_dir, "detector")

            self.assertEqual(seconds, 300.0)


if __name__ == "__main__":
    unittest.main()
