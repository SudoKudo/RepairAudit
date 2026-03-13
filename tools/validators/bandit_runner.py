"""Run Bandit and extract compact severity summaries.

Bandit here acts as an additional static-analysis signal alongside custom
study detectors and optional LLM judge outputs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any


def run_bandit(target_path: str, output_path: str) -> Dict[str, Any]:
    """
    Run Bandit on the target directory recursively.
    Writes JSON output to output_path and returns parsed results.
    """

    target = Path(target_path)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "bandit",
        "-r",
        str(target),
        "-f",
        "json",
        "-o",
        str(out),
    ]

    try:
        subprocess.run(cmd, check=False, capture_output=True)
    except Exception as e:
        return {"error": str(e)}

    if not out.exists():
        return {"error": "Bandit output file not created."}

    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        return {"error": "Failed to parse Bandit JSON output."}

    return data


def summarize_bandit(bandit_json: Dict[str, Any]) -> Dict[str, int]:
    """
    Extract summary metrics from Bandit JSON output.
    """

    summary = {
        "bandit_issues_total": 0,
        "bandit_high": 0,
        "bandit_medium": 0,
        "bandit_low": 0,
    }

    results = bandit_json.get("results", [])

    for issue in results:
        severity = issue.get("issue_severity", "").upper()
        summary["bandit_issues_total"] += 1

        if severity == "HIGH":
            summary["bandit_high"] += 1
        elif severity == "MEDIUM":
            summary["bandit_medium"] += 1
        elif severity == "LOW":
            summary["bandit_low"] += 1

    return summary
