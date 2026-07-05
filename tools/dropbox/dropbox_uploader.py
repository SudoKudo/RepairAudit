"""Automate file uploads to Dropbox participant folders.

Use this module when you want to push a file (e.g. a generated kit ZIP) into a
participant's Dropbox folder programmatically, without the participant initiating
the upload through the file-request link.

Credentials are read from the same environment variables as ``dropbox_api``:

  DROPBOX_ACCESS_TOKEN            — short-lived token
  DROPBOX_APP_KEY + DROPBOX_APP_SECRET + DROPBOX_REFRESH_TOKEN
                                  — long-lived OAuth2 refresh flow

Requires the 'dropbox' package:  pip install dropbox
"""

from __future__ import annotations

from pathlib import Path

from tools.dropbox.dropbox_client import _FOLDER_ROOT, get_client


_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB per upload session chunk


def upload_file(
    local_path: str | Path,
    dropbox_path: str,
    *,
    overwrite: bool = True,
) -> str:
    """Upload a local file to a Dropbox path.

    Files larger than 8 MB are sent via the chunked upload-session API so that
    network interruptions during large kit ZIPs do not require a full retry.

    Args:
        local_path: Path to the file on disk.
        dropbox_path: Destination path in Dropbox (e.g. ``/RepairAudit/participants/P001/kit.zip``).
            Must start with ``/``.
        overwrite: Replace an existing file at that path when ``True`` (default).
            When ``False`` Dropbox will autorename the file to avoid a conflict.

    Returns:
        The Dropbox path where the file was written.

    Raises:
        FileNotFoundError: If *local_path* does not exist.
        EnvironmentError: If Dropbox credentials are not set.
        dropbox.exceptions.ApiError: If the Dropbox API call fails.
    """
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
                    # Last (possibly empty) chunk — finalize the session.
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
    folder_root: str = _FOLDER_ROOT,
    overwrite: bool = True,
) -> str:
    """Upload a participant kit ZIP into the participant's Dropbox folder.

    The destination path is ``<folder_root>/<participant_id>/<zip filename>``.
    The folder must already exist in Dropbox (created by
    :func:`tools.dropbox.dropbox_api.create_participant_folder`).

    Args:
        local_zip_path: Path to the kit ZIP on disk.
        participant_id: Participant identifier — used to construct the Dropbox path.
        folder_root: Root Dropbox path (default: ``/RepairAudit/participants``).
        overwrite: Replace an existing file with the same name when ``True`` (default).

    Returns:
        The Dropbox path where the ZIP was written.
    """
    zip_path = Path(local_zip_path)
    dropbox_path = f"{folder_root.rstrip('/')}/{participant_id.strip()}/{zip_path.name}"
    return upload_file(zip_path, dropbox_path, overwrite=overwrite)
