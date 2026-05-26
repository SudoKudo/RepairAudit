from __future__ import annotations

import unittest
from pathlib import Path

from gui.study_gui import _derived_model_paths, _derived_report_path, _derived_stats_path, _resolve_output_path


REPO_ROOT = Path(__file__).resolve().parents[1]


class StudyGuiPathTests(unittest.TestCase):
    def test_relative_output_paths_resolve_under_repo_root(self) -> None:
        resolved = _resolve_output_path(REPO_ROOT, "data/aggregated/main_summary.csv")
        self.assertEqual(resolved, (REPO_ROOT / "data" / "aggregated" / "main_summary.csv").resolve())

    def test_stats_path_tracks_summary_stem(self) -> None:
        summary = (REPO_ROOT / "data" / "aggregated" / "main_summary.csv").resolve()
        self.assertEqual(_derived_stats_path(summary), summary.with_name("main_stats.txt"))

    def test_report_path_preserves_legacy_pilot_default(self) -> None:
        summary = (REPO_ROOT / "data" / "aggregated" / "pilot_summary.csv").resolve()
        self.assertEqual(_derived_report_path(summary, REPO_ROOT), (REPO_ROOT / "data" / "aggregated" / "report.html").resolve())

    def test_report_path_tracks_non_pilot_summary_stem(self) -> None:
        summary = (REPO_ROOT / "data" / "aggregated" / "main_summary.csv").resolve()
        self.assertEqual(_derived_report_path(summary, REPO_ROOT), summary.with_name("main_report.html"))

    def test_model_paths_track_summary_stem(self) -> None:
        summary = (REPO_ROOT / "data" / "aggregated" / "main_summary.csv").resolve()
        self.assertEqual(
            _derived_model_paths(summary),
            (
                summary.with_name("main_model_data.csv"),
                summary.with_name("main_models.json"),
                summary.with_name("main_models.txt"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
