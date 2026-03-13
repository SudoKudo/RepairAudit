"""Record per-snippet start/end timestamps in a run-local JSON file."""

import json
from datetime import datetime, timezone
from pathlib import Path

def _now_iso():
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()

def mark(run_dir: str, snippet_id: str, event: str) -> None:
    """Record start or end time for one snippet in timings/snippet_times.json."""
    if event not in {"start", "end"}:
        raise ValueError("event must be start or end")

    run_path = Path(run_dir)
    out = run_path / "timings" / "snippet_times.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    if out.exists():
        data = json.loads(out.read_text(encoding="utf-8"))
    else:
        data = {}

    data.setdefault(snippet_id, {})
    data[snippet_id][event] = _now_iso()

    out.write_text(json.dumps(data, indent=2), encoding="utf-8")

