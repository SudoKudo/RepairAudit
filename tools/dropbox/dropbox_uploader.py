"""Researcher-side Dropbox uploads for generated participant kit archives."""

from __future__ import annotations

from pathlib import Path

from tools.dropbox.dropbox_api import ensure_folder
from tools.dropbox.dropbox_client import DEFAULT_KITS_ROOT, dropbox_path_join, get_client


_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB


def upload_file(
    local_path: str | Path,
    dropbox_path: str,
    *,
    overwrite: bool = True,
) -> str:
    """Upload one local file to Dropbox."""
    import dropbox.files as dbx_files  # type: ignore

    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(f"Local file not found: {path}")

    mode = dbx_files.WriteMode.overwrite if overwrite else dbx_files.WriteMode.add
    dbx = get_client()
    file_size = path.stat().st_size

    with path.open("rb") as handle:
        if file_size <= _CHUNK_SIZE:
            dbx.files_upload(handle.read(), dropbox_path, mode=mode, mute=True)
        else:
            session = dbx.files_upload_session_start(handle.read(_CHUNK_SIZE))
            cursor = dbx_files.UploadSessionCursor(
                session_id=session.session_id,
                offset=handle.tell(),
            )
            commit = dbx_files.CommitInfo(path=dropbox_path, mode=mode, mute=True)
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if len(chunk) < _CHUNK_SIZE:
                    dbx.files_upload_session_finish(chunk, cursor, commit)
                    break
                dbx.files_upload_session_append_v2(chunk, cursor)
                cursor = dbx_files.UploadSessionCursor(
                    session_id=cursor.session_id,
                    offset=handle.tell(),
                )

    return dropbox_path


def upload_participant_kit(
    local_zip_path: str | Path,
    participant_id: str,
    *,
    folder_root: str = DEFAULT_KITS_ROOT,
    overwrite: bool = True,
) -> str:
    """Upload one participant kit ZIP into the Dropbox kits area."""
    zip_path = Path(local_zip_path)
    participant_folder = dropbox_path_join(folder_root, participant_id.strip())
    ensure_folder(participant_folder)
    dropbox_path = dropbox_path_join(participant_folder, zip_path.name)
    return upload_file(zip_path, dropbox_path, overwrite=overwrite)
