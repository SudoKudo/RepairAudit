"""Create per-participant Dropbox folders and scoped file-request links.

Credentials are read from environment variables:

  DROPBOX_ACCESS_TOKEN            — short-lived token 
  DROPBOX_APP_KEY + DROPBOX_APP_SECRET + DROPBOX_REFRESH_TOKEN
                                  — long-lived OAuth2 refresh flow

"""

from __future__ import annotations

from typing import Any

from tools.dropbox.dropbox_client import _FOLDER_ROOT, get_client


def create_participant_folder(
    participant_id: str,
    *,
    folder_root: str = _FOLDER_ROOT,
    file_request_title: str | None = None,
) -> dict[str, str]:
    """Create a Dropbox folder and a scoped file-request link for one participant.

    The folder is created at ``<folder_root>/<participant_id>`` and a Dropbox
    file request pointing at that folder is opened so the participant (or anyone
    with the link) can upload files without a Dropbox account.


    Raises:
        ValueError: If *participant_id* is blank.
        EnvironmentError: If Dropbox credentials are not set.
        dropbox.exceptions.ApiError: If the Dropbox API call fails.
    """
    if not participant_id or not participant_id.strip():
        raise ValueError("participant_id must not be blank.")

    pid = participant_id.strip()
    folder_path = f"{folder_root.rstrip('/')}/{pid}"
    title = file_request_title or f"RepairAudit submission — {pid}"

    dbx = get_client()

    try:
        dbx.files_create_folder_v2(folder_path, autorename=False)
    except Exception as exc:
        # Folder already exists → ApiError with a conflict tag — that's fine.
        if "path/conflict" not in str(exc):
            raise

    result = dbx.file_requests_create(
        title=title,
        destination=folder_path,
        open=True,
    )

    return {
        "participant_id": pid,
        "folder_path": folder_path,
        "file_request_id": result.id,
        "file_request_url": result.url,
    }


def get_file_request(file_request_id: str) -> dict[str, Any]:
    """Return the current state of a Dropbox file request."""
    
    dbx = get_client()
    result = dbx.file_requests_get(file_request_id)
    
    return {
        "id": result.id,
        "title": result.title,
        "destination": result.destination,
        "url": result.url,
        "is_open": result.is_open,
        "file_count": result.file_count,
    }
