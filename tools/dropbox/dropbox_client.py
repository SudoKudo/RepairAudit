"""Shared Dropbox client factory for all Dropbox tool modules."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv  


_REPO_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(_REPO_ROOT / ".env")


def get_client():
    """Return an authenticated Dropbox client.

    Credentials are read from environment variables (populated from .env at repo root):

      DROPBOX_ACCESS_TOKEN            — short-lived token
      DROPBOX_APP_KEY + DROPBOX_APP_SECRET + DROPBOX_REFRESH_TOKEN
                                      — long-lived OAuth2 refresh flow

    Raises:
        ImportError: If the 'dropbox' package is not installed.
        EnvironmentError: If no valid credential set is found.
    """
    try:
        import dropbox  
    except ImportError as exc:
        raise ImportError("Install the 'dropbox' package: pip install dropbox") from exc

    import os

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
        "Set DROPBOX_ACCESS_TOKEN, or set DROPBOX_APP_KEY + DROPBOX_APP_SECRET + DROPBOX_REFRESH_TOKEN."
    )
