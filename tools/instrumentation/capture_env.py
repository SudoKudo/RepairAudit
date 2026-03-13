"""Capture privacy-safe runtime environment metadata.

Only public-safe fields are stored (python version, OS info, architecture).
Sensitive identifiers like usernames, hostnames, and env vars are excluded.
"""

import json
import platform
import sys
from pathlib import Path
from datetime import datetime, timezone

def _now_iso():
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()

def capture_env(out_path: str) -> None:
    """
    Public-safe environment capture.

    Intentionally excludes:
      - username
      - hostname
      - absolute paths
      - environment variables
      - installed package lists
      - IP / network info
    """

    payload = {
        "captured_at_utc": _now_iso(),
        "python_version": sys.version.split()[0],  # e.g., "3.11.6"
        "python_implementation": platform.python_implementation(),
        "os": platform.system(),                   # e.g., "Windows"
        "os_release": platform.release(),          # e.g., "10"
        "machine_arch": platform.machine(),        # e.g., "AMD64"
    }

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("out_path")
    args = ap.parse_args()
    capture_env(args.out_path)

