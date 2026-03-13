"""Summarize per-snippet results into run-level metrics.

The module computes primary rates/counts and diagnostic agreement fields, then
writes summary.json and summary.txt in a consistent schema.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple


# Detector outcome labels used by analyze_edits.py
OUTCOME_ORDER = ["Mitigated", "Preserved", "Obfuscated", "Amplified", "Unchanged"]

# Judge verdict labels used by llm_judge.py
JUDGE_VERDICT_ORDER = ["absent", "present", "uncertain"]


@dataclass
class Summary:
    """Run-level metrics bundle written to summary JSON/TXT outputs."""
    # Primary (judge-driven) summary
    primary_source: str
    primary_scored: int
    primary_counts: Dict[str, int]
    primary_rates: Dict[str, float]

    # Secondary detector summary
    detector_scored: int
    detector_counts: Dict[str, int]
    detector_rates: Dict[str, float]
    detector_by_vuln_type: Dict[str, Dict[str, Any]]

    # Judge details (kept explicit for clarity)
    judge_scored: int
    judge_counts: Dict[str, int]
    judge_rates: Dict[str, float]
    judge_by_vuln_type: Dict[str, Dict[str, Any]]

    # Agreement diagnostics
    comparable_scored: int
    disagreement_count: int
    disagreement_rate: float


def _safe_div(num: float, den: float) -> float:
    """Divide safely and return 0.0 when the denominator is zero."""
    return float(num) / float(den) if den else 0.0


def load_results_csv(results_csv: str) -> List[Dict[str, str]]:
    """Load results.csv rows used for run-level metric computation."""
    p = Path(results_csv)
    if not p.exists():
        raise FileNotFoundError(f"Missing results CSV: {p}")

    with p.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader]
    return rows


def _has_judge_columns(rows: List[Dict[str, str]]) -> bool:
    """Return True when judge fields are present in results rows."""
    if not rows:
        return False
    sample = rows[0]
    return ("judge_verdict" in sample) or ("judge_enabled" in sample)


def _is_truthy(x: str) -> bool:
    """Return True for common truthy strings such as yes/true/1."""
    s = (x or "").strip().lower()
    return s in ("1", "true", "t", "yes", "y")


def _normalize_detector_outcome(outcome: str) -> str:
    """Normalize detector outcome text before counting."""
    o = (outcome or "").strip()
    return o


def compute_detector_counts(rows: List[Dict[str, str]]) -> Dict[str, int]:
    """Count detector outcomes across all scored snippet rows."""
    counts: Dict[str, int] = {k: 0 for k in OUTCOME_ORDER}
    for r in rows:
        outcome = _normalize_detector_outcome(r.get("outcome") or "")
        if outcome in counts:
            counts[outcome] += 1
        elif outcome:
            counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def compute_detector_rates(counts: Dict[str, int], total: int) -> Dict[str, float]:
    """Compute mitigation, persistence, and amplification detector rates."""
    mitigated = counts.get("Mitigated", 0)
    preserved = counts.get("Preserved", 0)
    obfuscated = counts.get("Obfuscated", 0)
    amplified = counts.get("Amplified", 0)

    return {
        "mitigation": _safe_div(mitigated, total),
        "persistence": _safe_div(preserved + obfuscated, total),
        "amplification": _safe_div(amplified, total),
    }


def compute_judge_counts(rows: List[Dict[str, str]]) -> Tuple[int, Dict[str, int]]:
    """Count judge verdicts and return the number of judge-scored rows."""
    counts: Dict[str, int] = {k: 0 for k in JUDGE_VERDICT_ORDER}
    scored = 0

    for r in rows:
        enabled = _is_truthy(r.get("judge_enabled", "")) if "judge_enabled" in r else True
        if not enabled:
            continue

        verdict = (r.get("judge_verdict") or "").strip().lower()
        if verdict not in counts:
            continue

        scored += 1
        counts[verdict] += 1

    return scored, counts


def compute_judge_rates(judge_counts: Dict[str, int], total: int) -> Dict[str, float]:
    """Compute mitigation, persistence, and abstention rates from judge counts."""
    absent = judge_counts.get("absent", 0)
    present = judge_counts.get("present", 0)
    unknown = judge_counts.get("uncertain", 0)  # treat as UNKNOWN/abstention metric

    return {
        "mitigation": _safe_div(absent, total),
        "persistence": _safe_div(present, total),
        "abstention": _safe_div(unknown, total),
    }


def breakdown_detector_by_vuln_type(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    """Compute detector counts and rates grouped by vulnerability type."""
    groups: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        vt = (r.get("vuln_type") or "").strip() or "UNKNOWN"
        groups.setdefault(vt, []).append(r)

    out: Dict[str, Dict[str, Any]] = {}
    for vt, g in groups.items():
        c = compute_detector_counts(g)
        total = len(g)
        out[vt] = {
            "total": total,
            "counts": c,
            "rates": compute_detector_rates(c, total),
        }
    return out


def breakdown_judge_by_vuln_type(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    """Compute judge counts and rates grouped by vulnerability type."""
    groups: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        vt = (r.get("vuln_type") or "").strip() or "UNKNOWN"
        groups.setdefault(vt, []).append(r)

    out: Dict[str, Dict[str, Any]] = {}
    for vt, g in groups.items():
        scored, c = compute_judge_counts(g)
        out[vt] = {
            "total": len(g),
            "judge_scored": scored,
            "judge_counts": c,
            "judge_rates": compute_judge_rates(c, scored) if scored else {"mitigation": 0.0, "persistence": 0.0, "abstention": 0.0},
        }
    return out


def _judge_outcome(verdict: str) -> str:
    """Map a judge verdict to simplified outcome labels for comparison."""
    v = (verdict or "").strip().lower()
    if v == "absent":
        return "Mitigated"
    if v == "present":
        return "Preserved"
    if v == "uncertain":
        return "UNKNOWN"
    return ""


def _detector_outcome_simple(outcome: str) -> str:
    """Map detector outcomes into simplified comparison categories."""
    o = (outcome or "").strip()
    if o == "Mitigated":
        return "Mitigated"
    if o in ("Preserved", "Obfuscated"):
        return "Preserved"
    if o == "Amplified":
        return "Amplified"
    if o == "Unchanged":
        return "Unchanged"
    return o or ""


def compute_disagreement(rows: List[Dict[str, str]]) -> Tuple[int, int, float]:
    """Compare judge outcome to a simplified detector outcome.
    Only score rows where judge is Mitigated/Preserved and detector is Mitigated/Preserved.
    Unknown/Amplified/Unchanged are excluded from comparables but still visible elsewhere.
    """
    comparable = 0
    disagree = 0

    for r in rows:
        j = _judge_outcome(r.get("judge_verdict") or "")
        d = _detector_outcome_simple(r.get("outcome") or "")

        if j not in ("Mitigated", "Preserved"):
            continue
        if d not in ("Mitigated", "Preserved"):
            continue

        comparable += 1
        if j != d:
            disagree += 1

    return comparable, disagree, _safe_div(disagree, comparable)


def summarize_participant_results(results_csv: str) -> Summary:
    """Compute complete run-level summaries from a participant results.csv file."""
    rows = load_results_csv(results_csv)
    rows_ok = [r for r in rows if (r.get("status") or "").strip().lower() == "ok"]

    detector_total = len(rows_ok)
    detector_counts = compute_detector_counts(rows_ok)
    detector_rates = compute_detector_rates(detector_counts, detector_total)
    detector_by_type = breakdown_detector_by_vuln_type(rows_ok)

    # Judge is primary. If judge columns are missing/disabled, fall back to detector.
    judge_scored = 0
    judge_counts = {k: 0 for k in JUDGE_VERDICT_ORDER}
    judge_rates = {"mitigation": 0.0, "persistence": 0.0, "abstention": 0.0}
    judge_by_type: Dict[str, Dict[str, Any]] = {}

    primary_source = "detector"
    primary_scored = detector_total
    primary_counts: Dict[str, int] = {
        "Mitigated": detector_counts.get("Mitigated", 0),
        "Preserved": detector_counts.get("Preserved", 0) + detector_counts.get("Obfuscated", 0),
        "UNKNOWN": 0,
    }
    primary_rates: Dict[str, float] = {
        "mitigation": detector_rates["mitigation"],
        "persistence": detector_rates["persistence"],
        "abstention": 0.0,
    }

    comparable_scored = 0
    disagreement_count = 0
    disagreement_rate = 0.0

    if _has_judge_columns(rows_ok):
        judge_scored, judge_counts = compute_judge_counts(rows_ok)
        judge_rates = compute_judge_rates(judge_counts, judge_scored) if judge_scored else {"mitigation": 0.0, "persistence": 0.0, "abstention": 0.0}
        judge_by_type = breakdown_judge_by_vuln_type(rows_ok)

        primary_source = "judge"
        primary_scored = judge_scored
        primary_counts = {
            "Mitigated": judge_counts.get("absent", 0),
            "Preserved": judge_counts.get("present", 0),
            "UNKNOWN": judge_counts.get("uncertain", 0),
        }
        primary_rates = judge_rates

        comparable_scored, disagreement_count, disagreement_rate = compute_disagreement(rows_ok)

    return Summary(
        primary_source=primary_source,
        primary_scored=primary_scored,
        primary_counts=primary_counts,
        primary_rates=primary_rates,
        detector_scored=detector_total,
        detector_counts=detector_counts,
        detector_rates=detector_rates,
        detector_by_vuln_type=detector_by_type,
        judge_scored=judge_scored,
        judge_counts=judge_counts,
        judge_rates=judge_rates,
        judge_by_vuln_type=judge_by_type,
        comparable_scored=comparable_scored,
        disagreement_count=disagreement_count,
        disagreement_rate=disagreement_rate,
    )


def write_summary_files(results_csv: str, out_json: str, out_txt: str) -> Summary:
    """Write summary JSON/TXT files and return the computed Summary object."""
    summary = summarize_participant_results(results_csv)

    payload: Dict[str, Any] = {
        "primary_source": summary.primary_source,
        "primary_scored_snippets": summary.primary_scored,
        "primary_counts": summary.primary_counts,
        "primary_rates": summary.primary_rates,
        "scored_snippets": summary.detector_scored,  # keep legacy name as detector-scored
        "counts": summary.detector_counts,
        "rates": summary.detector_rates,
        "by_vuln_type": summary.detector_by_vuln_type,
        "judge_scored_snippets": summary.judge_scored,
        "judge_counts": summary.judge_counts,
        "judge_rates": summary.judge_rates,
        "judge_by_vuln_type": summary.judge_by_vuln_type,
        "judge_detector_comparable": summary.comparable_scored,
        "judge_detector_disagreement_count": summary.disagreement_count,
        "judge_detector_disagreement_rate": summary.disagreement_rate,
    }

    Path(out_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines: List[str] = []
    lines.append("Run summary (auto-generated)")
    lines.append(f"Primary source: {summary.primary_source}")
    lines.append(f"Primary scored snippets: {summary.primary_scored}")
    lines.append(f"Primary counts: {summary.primary_counts}")
    lines.append(
        "Primary rates: Mitigation={:.3f}, Persistence={:.3f}, Abstention={:.3f}".format(
            summary.primary_rates["mitigation"], summary.primary_rates["persistence"], summary.primary_rates["abstention"]
        )
    )

    lines.append("")
    lines.append("Detector (secondary)")
    lines.append(f"Detector scored snippets: {summary.detector_scored}")
    lines.append(f"Detector counts: {summary.detector_counts}")
    lines.append(
        "Detector rates: Mitigation={:.3f}, Persistence={:.3f}, Amplification={:.3f}".format(
            summary.detector_rates["mitigation"], summary.detector_rates["persistence"], summary.detector_rates["amplification"]
        )
    )

    lines.append("")
    lines.append("LLM judge (primary)")
    lines.append(f"Judge scored snippets: {summary.judge_scored}")
    lines.append(f"Judge counts: {summary.judge_counts}")
    lines.append(
        "Judge rates: Mitigation={:.3f}, Persistence={:.3f}, Abstention={:.3f}".format(
            summary.judge_rates["mitigation"], summary.judge_rates["persistence"], summary.judge_rates["abstention"]
        )
    )

    lines.append("")
    lines.append(
        "Judge vs detector (diagnostic): comparable={}, disagreements={}, disagreement_rate={:.3f}".format(
            summary.comparable_scored, summary.disagreement_count, summary.disagreement_rate
        )
    )

    Path(out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary

