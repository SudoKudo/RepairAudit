"""Shared Dropbox client setup used by the researcher-side Dropbox helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


_REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = _REPO_ROOT / ".env"
DEFAULT_DROPBOX_ROOT = "/RepairAudit"
DEFAULT_KITS_ROOT = f"{DEFAULT_DROPBOX_ROOT}/kits"
DEFAULT_SUBMISSIONS_ROOT = f"{DEFAULT_DROPBOX_ROOT}/submissions"

load_dotenv(ENV_PATH)


def dropbox_path_join(root: str, *parts: str) -> str:
    """Join Dropbox path fragments without duplicate slashes."""
    clean_root = "/" + root.strip().strip("/")
    clean_parts = [part.strip().strip("/") for part in parts if str(part).strip()]
    if not clean_parts:
        return clean_root
    return clean_root + "/" + "/".join(clean_parts)


def get_client() -> Any:
    """Return an authenticated Dropbox client built from local environment variables."""
    try:
        import dropbox
    except ImportError as exc:
        raise ImportError("Install the 'dropbox' package: pip install dropbox") from exc

    access_token = os.environ.get("DROPBOX_ACCESS_TOKEN", "").strip()
    app_key = os.environ.get("DROPBOX_APP_KEY", "").strip()
    app_secret = os.environ.get("DROPBOX_APP_SECRET", "").strip()
    refresh_token = os.environ.get("DROPBOX_REFRESH_TOKEN", "").strip()

    if access_token:
        return dropbox.Dropbox(oauth2_access_token=access_token)

    if app_key and app_secret and refresh_token:
        return dropbox.Dropbox(
            app_key=app_key,
            app_secret=app_secret,
            oauth2_refresh_token=refresh_token,
        )

    raise EnvironmentError(
        "Dropbox credentials not configured. "
        f"Checked: {ENV_PATH}. "
        "Set DROPBOX_ACCESS_TOKEN, or set DROPBOX_APP_KEY + DROPBOX_APP_SECRET + DROPBOX_REFRESH_TOKEN. "
        "If you created the file from Windows Explorer, confirm it is named '.env' and not '.env.txt'."
    )
