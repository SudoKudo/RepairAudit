"""Researcher-side Dropbox helpers for file requests and shared links."""

from __future__ import annotations

import re
from typing import Any

from tools.dropbox.dropbox_client import (
    DEFAULT_SUBMISSIONS_ROOT,
    dropbox_path_join,
    get_client,
)


def _is_path_conflict(exc: Exception) -> bool:
    """Treat Dropbox folder path conflicts as a normal already-exists case."""
    message = str(exc).casefold()
    return "path/conflict" in message or "conflict" in message


def _extract_missing_scope(exc: Exception) -> str:
    """Return the missing Dropbox scope name when the API error reports one."""
    match = re.search(r"required scope `([^`]+)`", str(exc))
    if not match:
        return ""
    return match.group(1).strip()


def _raise_actionable_scope_error(exc: Exception, *, action: str) -> None:
    """Raise a clearer permissions error when the Dropbox app is missing a scope."""
    scope = _extract_missing_scope(exc)
    if not scope:
        raise exc
    raise PermissionError(
        f"Dropbox permission error during {action}. "
        f"Enable the '{scope}' scope in the Dropbox App Console Permissions tab, "
        "then generate a new refresh token and update the local .env file."
    ) from exc


def ensure_folder(folder_path: str) -> str:
    """Create a Dropbox folder when it does not exist yet."""
    dbx = get_client()
    try:
        dbx.files_create_folder_v2(folder_path, autorename=False)
    except Exception as exc:
        if not _is_path_conflict(exc):
            _raise_actionable_scope_error(exc, action="folder creation")
    return folder_path


def _iter_file_requests(dbx: Any) -> list[Any]:
    """Return every visible Dropbox file request for the current account."""
    result = dbx.file_requests_list_v2(limit=1000)
    requests = list(getattr(result, "file_requests", []) or [])
    while getattr(result, "has_more", False):
        result = dbx.file_requests_list_continue(result.cursor)
        requests.extend(list(getattr(result, "file_requests", []) or []))
    return requests


def _find_open_file_request(dbx: Any, destination: str) -> Any | None:
    """Reuse an open file request when it already points at the same folder."""
    for request in _iter_file_requests(dbx):
        if str(getattr(request, "destination", "") or "") != destination:
            continue
        if bool(getattr(request, "is_open", False)):
            return request
    return None


def _shared_link_url(url: str, *, direct_download: bool) -> str:
    """Flip Dropbox shared links to direct-download form when requested."""
    if not direct_download:
        return url
    if "dl=0" in url:
        return url.replace("dl=0", "dl=1")
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}dl=1"


def create_shared_link(dropbox_path: str, *, direct_download: bool = True) -> dict[str, str]:
    """Create or reuse a shared link for one Dropbox path."""
    dbx = get_client()
    try:
        result = dbx.sharing_create_shared_link_with_settings(dropbox_path)
    except Exception as exc:
        if _extract_missing_scope(exc):
            _raise_actionable_scope_error(exc, action="shared-link creation")
        result = None

    if result is None:
        try:
            listing = dbx.sharing_list_shared_links(path=dropbox_path, direct_only=True)
            links = list(getattr(listing, "links", []) or [])
            if not links:
                raise RuntimeError(f"Could not create a shared link for {dropbox_path}.")
            result = links[0]
        except Exception as exc:
            _raise_actionable_scope_error(exc, action="shared-link lookup")
            raise

    url = str(result.url)
    return {
        "path": dropbox_path,
        "url": _shared_link_url(url, direct_download=direct_download),
    }


def create_participant_folder(
    participant_id: str,
    *,
    folder_root: str = DEFAULT_SUBMISSIONS_ROOT,
    file_request_title: str | None = None,
) -> dict[str, str]:
    """Create a Dropbox submission folder and file-request link for one participant."""
    if not participant_id or not participant_id.strip():
        raise ValueError("participant_id must not be blank.")

    pid = participant_id.strip()
    folder_path = dropbox_path_join(folder_root, pid)
    title = file_request_title or f"RepairAudit submission - {pid}"

    dbx = get_client()
    ensure_folder(folder_path)
    result = _find_open_file_request(dbx, folder_path)
    if result is None:
        try:
            result = dbx.file_requests_create(
                title=title,
                destination=folder_path,
                open=True,
            )
        except Exception as exc:
            _raise_actionable_scope_error(exc, action="file-request creation")
            raise

    return {
        "participant_id": pid,
        "folder_path": folder_path,
        "file_request_id": result.id,
        "file_request_url": result.url,
    }


def get_file_request(file_request_id: str) -> dict[str, Any]:
    """Return the current state of a Dropbox file request."""
    dbx = get_client()
    try:
        result = dbx.file_requests_get(file_request_id)
    except Exception as exc:
        _raise_actionable_scope_error(exc, action="file-request lookup")
        raise

    return {
        "id": result.id,
        "title": result.title,
        "destination": result.destination,
        "url": result.url,
        "is_open": result.is_open,
        "file_count": result.file_count,
    }
