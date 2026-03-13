"""Create baseline-vs-edited unified diffs plus change statistics.

The returned line counts are lightweight edit-effort signals that can be
joined onto per-snippet scoring outputs.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Dict


def _count_unified_diff_lines(diff_text: str) -> Dict[str, int]:
    """Count added/removed lines and hunk metadata from unified diff text."""
    added = 0
    removed = 0
    hunks = 0

    for line in diff_text.splitlines():
        if not line:
            continue
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            hunks += 1
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1

    diff_bytes = len(diff_text.encode("utf-8", errors="replace"))

    return {
        "lines_added": added,
        "lines_removed": removed,
        "lines_changed": added + removed,
        "hunks": hunks,
        "diff_bytes": diff_bytes,
    }


def make_diff(baseline_path: str, edited_path: str, out_path: str) -> Dict[str, int]:
    """
    Write a unified diff file and return line-change stats.
    """
    base = Path(baseline_path)
    edited = Path(edited_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    base_text = base.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    edited_text = edited.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            base_text,
            edited_text,
            fromfile=str(base).replace("\\", "/"),
            tofile=str(edited).replace("\\", "/"),
            lineterm="",
        )
    )

    diff_text = "\n".join(diff_lines) + ("\n" if diff_lines else "")
    out.write_text(diff_text, encoding="utf-8")

    return _count_unified_diff_lines(diff_text)

