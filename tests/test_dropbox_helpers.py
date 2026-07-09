from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tools.dropbox import dropbox_api
from tools.dropbox.dropbox_client import DEFAULT_KITS_ROOT, dropbox_path_join
from tools.dropbox.dropbox_submissions import _safe_extract_zip, import_participant_submission_archives
from tools.dropbox.dropbox_uploader import upload_participant_kit


class FakeDropboxClient:
    def __init__(self, source_zip: Path) -> None:
        self.source_zip = source_zip
        self.downloads: list[tuple[str, str]] = []

    def files_download_to_file(self, local_path: str, remote_path: str) -> None:
        self.downloads.append((local_path, remote_path))
        shutil.copyfile(self.source_zip, local_path)


class FakeFileRequestListResult:
    def __init__(self, file_requests: list[object]) -> None:
        self.file_requests = file_requests
        self.has_more = False
        self.cursor = ""


class FakeDropboxApiClient:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, bool]] = []

    def file_requests_list_v2(self, limit: int = 1000) -> FakeFileRequestListResult:
        request = type(
            "Request",
            (),
            {
                "id": "fr_existing",
                "url": "https://example.test/request",
                "destination": "/RepairAudit/submissions/P001",
                "is_open": True,
            },
        )()
        return FakeFileRequestListResult([request])

    def file_requests_create(self, title: str, destination: str, open: bool) -> object:
        self.created.append((title, destination, open))
        return type(
            "Request",
            (),
            {
                "id": "fr_new",
                "url": "https://example.test/new",
                "destination": destination,
                "is_open": open,
            },
        )()


class DropboxHelperTests(unittest.TestCase):
    def test_dropbox_path_join_normalizes_slashes(self) -> None:
        self.assertEqual(
            dropbox_path_join("/RepairAudit/", "/kits/", "P001", "kit.zip"),
            "/RepairAudit/kits/P001/kit.zip",
        )

    def test_create_participant_folder_reuses_open_file_request(self) -> None:
        fake_client = FakeDropboxApiClient()

        with patch.object(dropbox_api, "get_client", return_value=fake_client):
            with patch.object(dropbox_api, "ensure_folder", return_value="/RepairAudit/submissions/P001"):
                result = dropbox_api.create_participant_folder("P001")

        self.assertEqual(result["file_request_id"], "fr_existing")
        self.assertEqual(result["file_request_url"], "https://example.test/request")
        self.assertEqual(fake_client.created, [])

    def test_raise_actionable_scope_error_mentions_scope_and_refresh_token(self) -> None:
        with self.assertRaises(PermissionError) as raised:
            dropbox_api._raise_actionable_scope_error(
                Exception(
                    "BadInputError('Error in call to API function \"sharing/list_shared_links\": "
                    "Your app is not permitted to access this endpoint because it does not have "
                    "the required scope `sharing.read`.')"
                ),
                action="shared-link lookup",
            )

        message = str(raised.exception)
        self.assertIn("sharing.read", message)
        self.assertIn("new refresh token", message)

    def test_upload_participant_kit_uses_kits_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            zip_path = Path(tmp_dir) / "participant_kit_pilot_P001.zip"
            zip_path.write_bytes(b"PK\x03\x04")

            with patch("tools.dropbox.dropbox_uploader.ensure_folder") as ensure_folder:
                with patch("tools.dropbox.dropbox_uploader.upload_file", return_value="ok") as upload_file:
                    result = upload_participant_kit(zip_path, "P001")

            self.assertEqual(result, "ok")
            ensure_folder.assert_called_once_with(f"{DEFAULT_KITS_ROOT}/P001")
            upload_file.assert_called_once_with(
                zip_path,
                f"{DEFAULT_KITS_ROOT}/P001/{zip_path.name}",
                overwrite=True,
            )

    def test_import_participant_submission_archives_extracts_zip_into_runs_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_zip = tmp_path / "P001.zip"
            with ZipFile(source_zip, "w") as archive:
                archive.writestr("logs/snippet_log.csv", "snippet_id,turns\nS01,1\n")
                archive.writestr("study_assignment.json", "{}")

            fake_client = FakeDropboxClient(source_zip)
            runs_root = tmp_path / "runs" / "pilot"
            metadata = [
                {
                    "id": "id:123",
                    "name": "P001.zip",
                    "path_display": "/RepairAudit/submissions/P001/P001.zip",
                    "path_lower": "/repairaudit/submissions/p001/p001.zip",
                    "server_modified": datetime(2026, 7, 8, 12, 30, tzinfo=timezone.utc),
                    "size": source_zip.stat().st_size,
                }
            ]

            with patch("tools.dropbox.dropbox_submissions.get_client", return_value=fake_client):
                with patch("tools.dropbox.dropbox_submissions.list_participant_submission_files", return_value=metadata):
                    imported = import_participant_submission_archives(
                        "P001",
                        phase="pilot",
                        runs_root=runs_root,
                    )

            self.assertEqual(len(imported), 1)
            extracted = imported[0].extracted_run_dir
            self.assertTrue((extracted / "logs" / "snippet_log.csv").exists())
            self.assertTrue((extracted.parent / "dropbox_import.json").exists())

    def test_safe_extract_zip_rejects_parent_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_zip = tmp_path / "bad.zip"
            with ZipFile(source_zip, "w") as archive:
                archive.writestr("../escape.txt", "bad")

            destination = tmp_path / "dest"
            destination.mkdir(parents=True, exist_ok=True)
            with ZipFile(source_zip, "r") as archive:
                with self.assertRaises(ValueError):
                    _safe_extract_zip(archive, destination)


if __name__ == "__main__":
    unittest.main()
