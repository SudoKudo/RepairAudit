"""Write whole-run start/end timestamps in UTC ISO format."""

import json
from pathlib import Path
from datetime import datetime, timezone

def write_time(out_path: str, key: str) -> None:
    """Write a UTC timestamp under key start or end in the timer JSON file."""
    path = Path(out_path)
    payload = {}
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    payload[key] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

if __name__ == "__main__":
    # Usage:
    # python tools/instrumentation/start_timer.py <json_path> start
    # python tools/instrumentation/start_timer.py <json_path> end
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("key", choices=["start", "end"])
    args = ap.parse_args()
    write_time(args.json_path, args.key)

