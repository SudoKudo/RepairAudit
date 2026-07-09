from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.dropbox import dropbox_workflow


class DropboxWorkflowTests(unittest.TestCase):
    def test_load_researcher_map_returns_minimal_payload_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            path, payload = dropbox_workflow.load_researcher_map(
                repo_root,
                phase="pilot",
                participant_id="P001",
            )

            self.assertFalse(path.exists())
            self.assertEqual(payload["participant_id"], "P001")
            self.assertEqual(payload["phase"], "pilot")
            self.assertEqual(payload["out_root"], "participant_kits")

    def test_local_delivery_state_reports_missing_and_present_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            state = dropbox_workflow.local_delivery_state(
                repo_root,
                out_root="participant_kits",
                phase="pilot",
                participant_id="P001",
            )
            self.assertFalse(state["kit_dir_exists"])
            self.assertFalse(state["share_zip_exists"])

            kit_dir = repo_root / "participant_kits" / "P001"
            share_zip = repo_root / "participant_kits" / "_share_zips" / "participant_kit_pilot_P001.zip"
            kit_dir.mkdir(parents=True, exist_ok=True)
            share_zip.parent.mkdir(parents=True, exist_ok=True)
            share_zip.write_bytes(b"PK\x03\x04")

            state = dropbox_workflow.local_delivery_state(
                repo_root,
                out_root="participant_kits",
                phase="pilot",
                participant_id="P001",
            )
            self.assertTrue(state["kit_dir_exists"])
            self.assertTrue(state["share_zip_exists"])

    def test_publish_existing_participant_kit_updates_researcher_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            share_zip = repo_root / "participant_kits" / "_share_zips" / "participant_kit_pilot_P001.zip"
            share_zip.parent.mkdir(parents=True, exist_ok=True)
            share_zip.write_bytes(b"PK\x03\x04")

            map_path = repo_root / "participant_kits" / "_researcher_maps" / "pilot__P001.json"
            map_path.parent.mkdir(parents=True, exist_ok=True)
            map_path.write_text(
                json.dumps({"participant_id": "P001", "phase": "pilot", "snippet_mappings": []}, indent=2),
                encoding="utf-8",
            )

            with patch.object(
                dropbox_workflow,
                "create_participant_folder",
                return_value={
                    "participant_id": "P001",
                    "folder_path": "/RepairAudit/submissions/P001",
                    "file_request_id": "fr_001",
                    "file_request_url": "https://example.test/request",
                },
            ):
                with patch.object(
                    dropbox_workflow,
                    "upload_participant_kit",
                    return_value="/RepairAudit/kits/P001/participant_kit_pilot_P001.zip",
                ):
                    with patch.object(
                        dropbox_workflow,
                        "create_shared_link",
                        return_value={
                            "path": "/RepairAudit/kits/P001/participant_kit_pilot_P001.zip",
                            "url": "https://example.test/kit",
                        },
                    ):
                        result = dropbox_workflow.publish_existing_participant_kit(
                            repo_root,
                            participant_id="P001",
                            phase="pilot",
                        )

            payload = json.loads(map_path.read_text(encoding="utf-8"))
            self.assertEqual(result["kit_shared_url"], "https://example.test/kit")
            self.assertIn("dropbox", payload)
            self.assertEqual(payload["dropbox"]["submission_file_request_url"], "https://example.test/request")

    def test_list_researcher_maps_reads_saved_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            map_root = repo_root / "participant_kits" / "_researcher_maps"
            map_root.mkdir(parents=True, exist_ok=True)
            (map_root / "pilot__P001.json").write_text(
                json.dumps({"participant_id": "P001", "phase": "pilot"}, indent=2),
                encoding="utf-8",
            )

            items = dropbox_workflow.list_researcher_maps(repo_root)

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["participant_id"], "P001")
            self.assertEqual(items[0]["phase"], "pilot")

    def test_publish_participant_kit_batch_collects_successes_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            participants = [
                {
                    "participant_id": "P001",
                    "phase": "pilot",
                    "payload": {"out_root": "participant_kits"},
                },
                {
                    "participant_id": "P002",
                    "phase": "pilot",
                    "payload": {"out_root": "participant_kits"},
                },
            ]

            def fake_publish(
                _repo_root: Path,
                *,
                participant_id: str,
                phase: str,
                out_root: str,
            ) -> dict[str, str]:
                if participant_id == "P002":
                    raise FileNotFoundError("Share ZIP not found")
                return {
                    "researcher_map_path": str(repo_root / "participant_kits" / "_researcher_maps" / f"{phase}__{participant_id}.json"),
                    "kit_shared_url": "https://example.test/kit",
                    "submission_file_request_url": "https://example.test/request",
                }

            with patch.object(dropbox_workflow, "publish_existing_participant_kit", side_effect=fake_publish):
                result = dropbox_workflow.publish_participant_kit_batch(
                    repo_root,
                    participants=participants,
                )

            self.assertEqual(len(result["successes"]), 1)
            self.assertEqual(result["successes"][0]["kit_shared_url"], "https://example.test/kit")
            self.assertEqual(len(result["failures"]), 1)
            self.assertEqual(result["failures"][0]["participant_id"], "P002")

    def test_import_participant_submissions_batch_collects_successes_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            participants = [
                {
                    "participant_id": "P001",
                    "phase": "pilot",
                },
                {
                    "participant_id": "P002",
                    "phase": "pilot",
                },
            ]

            def fake_import(
                _repo_root: Path,
                *,
                participant_id: str,
                phase: str,
                runs_root: str,
                download_root: str | None,
            ) -> dict[str, object]:
                if participant_id == "P002":
                    raise FileNotFoundError("No ZIP files found")
                return {
                    "runs_root": runs_root,
                    "download_root": download_root,
                    "imported": ["submission.zip"],
                }

            with patch.object(dropbox_workflow, "import_participant_submissions", side_effect=fake_import):
                result = dropbox_workflow.import_participant_submissions_batch(
                    repo_root,
                    participants=participants,
                    default_runs_root="runs/pilot",
                )

            self.assertEqual(len(result["successes"]), 1)
            self.assertEqual(result["successes"][0]["participant_id"], "P001")
            self.assertEqual(result["successes"][0]["imported"], ["submission.zip"])
            self.assertEqual(len(result["failures"]), 1)
            self.assertEqual(result["failures"][0]["participant_id"], "P002")


if __name__ == "__main__":
    unittest.main()
