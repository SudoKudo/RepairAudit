"""Download participant return archives from Dropbox into local runs folders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from tools.dropbox.dropbox_api import ensure_folder
from tools.dropbox.dropbox_client import DEFAULT_SUBMISSIONS_ROOT, dropbox_path_join, get_client


@dataclass
class ImportedSubmission:
    """One imported Dropbox file and the local run folder created from it."""

    participant_id: str
    remote_name: str
    remote_id: str
    downloaded_zip: Path
    extracted_run_dir: Path


def _list_folder_entries(folder_path: str) -> list[Any]:
    """List every entry under one Dropbox folder."""
    dbx = get_client()
    result = dbx.files_list_folder(folder_path)
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return entries


def list_participant_submission_files(
    participant_id: str,
    *,
    folder_root: str = DEFAULT_SUBMISSIONS_ROOT,
) -> list[dict[str, Any]]:
    """Return Dropbox file metadata for one participant submission folder."""
    folder_path = dropbox_path_join(folder_root, participant_id.strip())
    ensure_folder(folder_path)
    files: list[dict[str, Any]] = []
    for entry in _list_folder_entries(folder_path):
        path_display = str(getattr(entry, "path_display", "") or "")
        if not path_display:
            continue
        files.append(
            {
                "id": str(getattr(entry, "id", "") or ""),
                "name": str(getattr(entry, "name", "") or ""),
                "path_display": path_display,
                "path_lower": str(getattr(entry, "path_lower", "") or ""),
                "server_modified": getattr(entry, "server_modified", None),
                "size": int(getattr(entry, "size", 0) or 0),
            }
        )
    return files


def _timestamp_fragment(server_modified: Any) -> str:
    """Build a stable timestamp suffix from Dropbox metadata when available."""
    if hasattr(server_modified, "strftime"):
        return server_modified.strftime("%Y%m%dT%H%M%SZ")
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_extract_zip(archive: ZipFile, destination: Path) -> None:
    """Extract a ZIP only when every member stays inside the target directory."""
    destination_resolved = destination.resolve()
    for member in archive.infolist():
        member_path = destination / member.filename
        resolved_member = member_path.resolve()
        if not str(resolved_member).startswith(str(destination_resolved)):
            raise ValueError(f"Unsafe ZIP member path: {member.filename}")
    archive.extractall(destination)


def import_participant_submission_archives(
    participant_id: str,
    *,
    phase: str,
    runs_root: str | Path,
    download_root: str | Path | None = None,
    folder_root: str = DEFAULT_SUBMISSIONS_ROOT,
) -> list[ImportedSubmission]:
    """Download every ZIP in one participant Dropbox folder and extract it into runs/<phase>/."""
    pid = participant_id.strip()
    if not pid:
        raise ValueError("participant_id must not be blank.")

    dbx = get_client()
    files = list_participant_submission_files(pid, folder_root=folder_root)
    zip_files = [row for row in files if row["name"].casefold().endswith(".zip")]
    if not zip_files:
        return []

    runs_root_path = Path(runs_root)
    if download_root is None:
        download_root_path = runs_root_path / "_dropbox_downloads" / pid
    else:
        download_root_path = Path(download_root)
    download_root_path.mkdir(parents=True, exist_ok=True)

    imported: list[ImportedSubmission] = []
    for row in zip_files:
        remote_name = row["name"]
        remote_id = row["id"]
        downloaded_zip = download_root_path / remote_name
        dbx.files_download_to_file(str(downloaded_zip), row["path_display"])

        stamp = _timestamp_fragment(row["server_modified"])
        extracted_root = runs_root_path / f"submission_{phase}_{pid}_{stamp}"
        suffix = 1
        while extracted_root.exists():
            suffix += 1
            extracted_root = runs_root_path / f"submission_{phase}_{pid}_{stamp}_{suffix:02d}"
        extracted_run_dir = extracted_root / pid
        extracted_run_dir.mkdir(parents=True, exist_ok=True)

        with ZipFile(downloaded_zip, "r") as archive:
            _safe_extract_zip(archive, extracted_run_dir)

        manifest = {
            "participant_id": pid,
            "phase": phase,
            "remote_name": remote_name,
            "remote_id": remote_id,
            "downloaded_zip": str(downloaded_zip),
            "extracted_run_dir": str(extracted_run_dir),
            "imported_utc": datetime.now(timezone.utc).isoformat(),
        }
        (extracted_root / "dropbox_import.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        imported.append(
            ImportedSubmission(
                participant_id=pid,
                remote_name=remote_name,
                remote_id=remote_id,
                downloaded_zip=downloaded_zip,
                extracted_run_dir=extracted_run_dir,
            )
        )

    return imported
