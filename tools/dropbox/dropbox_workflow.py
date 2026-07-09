"""Researcher-side Dropbox workflow helpers used by the desktop GUI."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from tools.dropbox.dropbox_api import create_participant_folder, create_shared_link
from tools.dropbox.dropbox_client import get_client
from tools.dropbox.dropbox_submissions import import_participant_submission_archives
from tools.dropbox.dropbox_uploader import upload_participant_kit


def _now_utc() -> str:
    """Return the current UTC timestamp in ISO form."""
    return datetime.now(timezone.utc).isoformat()


def _resolve_under_repo(repo_root: Path, value: str | Path) -> Path:
    """Resolve a path relative to the repo root when needed."""
    raw = Path(value)
    return raw if raw.is_absolute() else (repo_root / raw)


def researcher_map_path(repo_root: Path, *, phase: str, participant_id: str) -> Path:
    """Return the researcher map path for one participant kit."""
    return repo_root / "participant_kits" / "_researcher_maps" / f"{phase}__{participant_id}.json"


def share_zip_path(repo_root: Path, *, out_root: str | Path, phase: str, participant_id: str) -> Path:
    """Return the local share ZIP path for one participant kit."""
    root = _resolve_under_repo(repo_root, out_root)
    return root / "_share_zips" / f"participant_kit_{phase}_{participant_id}.zip"


def local_delivery_state(
    repo_root: Path,
    *,
    out_root: str | Path,
    phase: str,
    participant_id: str,
) -> dict[str, Any]:
    """Describe the local kit artifacts that Dropbox publish depends on."""
    root = _resolve_under_repo(repo_root, out_root)
    kit_dir = root / participant_id
    zip_path = share_zip_path(repo_root, out_root=out_root, phase=phase, participant_id=participant_id)
    return {
        "out_root": str(root),
        "kit_dir": kit_dir,
        "share_zip_path": zip_path,
        "kit_dir_exists": kit_dir.exists(),
        "share_zip_exists": zip_path.exists(),
    }


def load_researcher_map(repo_root: Path, *, phase: str, participant_id: str) -> tuple[Path, dict[str, Any]]:
    """Load one researcher map when present, otherwise return a minimal payload."""
    path = researcher_map_path(repo_root, phase=phase, participant_id=participant_id)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return path, payload
        except Exception:
            pass
    return (
        path,
        {
            "generated_utc": _now_utc(),
            "participant_id": participant_id,
            "phase": phase,
            "out_root": "participant_kits",
        },
    )


def write_researcher_map(path: Path, payload: dict[str, Any]) -> None:
    """Write one researcher map JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def list_researcher_maps(repo_root: Path) -> list[dict[str, Any]]:
    """List known researcher maps so the GUI can offer quick selection."""
    root = repo_root / "participant_kits" / "_researcher_maps"
    if not root.exists():
        return []

    items: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        participant_id = str(payload.get("participant_id") or "").strip()
        phase = str(payload.get("phase") or "").strip()
        if not participant_id or not phase:
            continue
        items.append(
            {
                "participant_id": participant_id,
                "phase": phase,
                "path": path,
                "payload": payload,
            }
        )
    return items


def verify_dropbox_access() -> dict[str, str]:
    """Verify Dropbox credentials and return a short account summary."""
    dbx = get_client()
    account = dbx.users_get_current_account()
    name = getattr(getattr(account, "name", None), "display_name", "") or ""
    email = str(getattr(account, "email", "") or "")
    account_id = str(getattr(account, "account_id", "") or "")
    return {
        "display_name": str(name),
        "email": email,
        "account_id": account_id,
    }


def publish_share_zip_artifacts(*, participant_id: str, share_zip_path: Path) -> dict[str, Any]:
    """Upload one share ZIP and return the Dropbox links used by the researcher tools."""
    pid = participant_id.strip()
    if not pid:
        raise ValueError("participant_id must not be blank.")
    if not share_zip_path.exists():
        raise FileNotFoundError(f"Share ZIP not found: {share_zip_path}")

    submission_info = create_participant_folder(pid)
    kit_dropbox_path = upload_participant_kit(share_zip_path, pid)
    kit_link = create_shared_link(kit_dropbox_path, direct_download=True)
    return {
        "published_utc": _now_utc(),
        "kit_dropbox_path": kit_dropbox_path,
        "kit_shared_url": kit_link["url"],
        "submission_folder_path": submission_info["folder_path"],
        "submission_file_request_id": submission_info["file_request_id"],
        "submission_file_request_url": submission_info["file_request_url"],
    }


def publish_existing_participant_kit(
    repo_root: Path,
    *,
    participant_id: str,
    phase: str,
    out_root: str | Path = "participant_kits",
) -> dict[str, Any]:
    """Upload an already-generated share ZIP and store the Dropbox URLs in the researcher map."""
    pid = participant_id.strip()
    if not pid:
        raise ValueError("participant_id must not be blank.")

    zip_path = share_zip_path(repo_root, out_root=out_root, phase=phase, participant_id=pid)
    dropbox_payload = publish_share_zip_artifacts(
        participant_id=pid,
        share_zip_path=zip_path,
    )

    map_path, payload = load_researcher_map(repo_root, phase=phase, participant_id=pid)
    payload["participant_id"] = pid
    payload["phase"] = phase
    payload["dropbox"] = dropbox_payload
    write_researcher_map(map_path, payload)

    return {
        "researcher_map_path": map_path,
        "share_zip_path": zip_path,
        **dropbox_payload,
    }


def publish_participant_kit_batch(
    repo_root: Path,
    *,
    participants: list[dict[str, Any]],
    default_out_root: str | Path = "participant_kits",
) -> dict[str, Any]:
    """Publish a batch of participant kits and collect per-participant results."""
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    for item in participants:
        participant_id = str(item.get("participant_id") or "").strip()
        phase = str(item.get("phase") or "").strip()
        payload = item.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}
        out_root = payload_dict.get("out_root") or default_out_root
        if not participant_id or not phase:
            failures.append(
                {
                    "participant_id": participant_id or "<missing>",
                    "phase": phase or "<missing>",
                    "error": "participant_id or phase missing from researcher map payload",
                }
            )
            continue
        try:
            result = publish_existing_participant_kit(
                repo_root,
                participant_id=participant_id,
                phase=phase,
                out_root=str(out_root),
            )
        except Exception as exc:
            failures.append(
                {
                    "participant_id": participant_id,
                    "phase": phase,
                    "error": str(exc),
                }
            )
            continue
        successes.append(result)

    return {
        "successes": successes,
        "failures": failures,
    }


def import_participant_submissions_batch(
    repo_root: Path,
    *,
    participants: list[dict[str, Any]],
    default_runs_root: str | Path = "runs/pilot",
    default_download_root: str | Path | None = None,
) -> dict[str, Any]:
    """Import Dropbox return ZIPs for several selected participants."""
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    for item in participants:
        participant_id = str(item.get("participant_id") or "").strip()
        phase = str(item.get("phase") or "").strip()
        if not participant_id or not phase:
            failures.append(
                {
                    "participant_id": participant_id or "<missing>",
                    "phase": phase or "<missing>",
                    "error": "participant_id or phase missing from researcher map payload",
                }
            )
            continue

        runs_root = default_runs_root or f"runs/{phase}"
        download_root = default_download_root or None
        try:
            result = import_participant_submissions(
                repo_root,
                participant_id=participant_id,
                phase=phase,
                runs_root=runs_root,
                download_root=download_root,
            )
        except Exception as exc:
            failures.append(
                {
                    "participant_id": participant_id,
                    "phase": phase,
                    "error": str(exc),
                }
            )
            continue

        successes.append(
            {
                "participant_id": participant_id,
                "phase": phase,
                **result,
            }
        )

    return {
        "successes": successes,
        "failures": failures,
    }


def import_participant_submissions(
    repo_root: Path,
    *,
    participant_id: str,
    phase: str,
    runs_root: str | Path = "runs/pilot",
    download_root: str | Path | None = None,
) -> dict[str, Any]:
    """Download participant return ZIPs from Dropbox into the local runs tree."""
    resolved_runs_root = _resolve_under_repo(repo_root, runs_root)
    resolved_download_root = None if download_root in {None, ""} else _resolve_under_repo(repo_root, download_root)
    imported = import_participant_submission_archives(
        participant_id,
        phase=phase,
        runs_root=resolved_runs_root,
        download_root=resolved_download_root,
    )
    return {
        "runs_root": resolved_runs_root,
        "download_root": resolved_download_root,
        "imported": imported,
    }
