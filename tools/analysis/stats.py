"""Aggregate summary statistics for the security-only study flow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


VALID_CONDITIONS = ["security"]


def to_float(x: Any) -> float:
    """Coerce scalar-ish values to a plain Python float."""
    try:
        if hasattr(x, "item"):
            return float(x.item())
        return float(x)
    except Exception:
        return float(np.asarray(x).reshape(-1)[0])


def to_int(x: Any) -> int:
    """Coerce scalar-ish values to a plain Python int."""
    try:
        if hasattr(x, "item"):
            return int(x.item())
        return int(x)
    except Exception:
        return int(np.asarray(x).reshape(-1)[0])


def compute_all_stats(merged_csv: str) -> dict[str, Any]:
    """Return the high-level stats payload used by the CLI JSON writer."""
    csv_path = Path(merged_csv)
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing merged CSV: {csv_path}")

    df = pd.read_csv(csv_path)
    if "condition" in df.columns:
        df["condition"] = df["condition"].astype(str).str.strip().str.lower()
        df = df[df["condition"].isin(VALID_CONDITIONS)].copy()

    return {
        "n_rows": to_int(df.shape[0]),
        "tests": [],
        "note": "Condition contrasts are disabled in security-only study mode.",
    }


def write_stats(merged_csv: str, out_json: str) -> str:
    """Write the stats payload to JSON and return the output path."""
    payload = compute_all_stats(merged_csv)
    Path(out_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_json


def _safe_float_for_summary(value: Any) -> float:
    """Coerce CSV-style values into floats while preserving NaN on bad inputs."""
    if value is None:
        return np.nan

    if isinstance(value, (tuple, list)):
        if not value:
            return np.nan
        value = value[0]

    if isinstance(value, str):
        text = value.strip()
        if text == "" or text.lower() in {"nan", "none", "null"}:
            return np.nan
        try:
            return float(text)
        except Exception:
            return np.nan

    try:
        return float(value)
    except Exception:
        return np.nan


def _to_float_array_for_summary(series: pd.Series) -> np.ndarray:
    """Convert a pandas series to a float array with NaN coercion."""
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)


def _describe_series_for_summary(series: pd.Series) -> dict[str, Any]:
    """Return n, mean, and standard deviation for one numeric series."""
    arr = _to_float_array_for_summary(series)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        return {"n": 0, "mean": np.nan, "std": np.nan}
    if arr.size == 1:
        return {"n": 1, "mean": float(arr[0]), "std": 0.0}
    return {"n": int(arr.size), "mean": float(np.mean(arr)), "std": float(np.std(arr, ddof=1))}


def compute_pilot_summary_lines(in_csv: str) -> list[str]:
    """Build the human-readable pilot summary text report."""
    base_metrics = [
        ("duration_seconds", "Duration (s)"),
        ("primary_mitigation_rate", "Mitigation rate (LLM judge)"),
        ("primary_persistence_rate", "Persistence rate (LLM judge)"),
        ("primary_abstention_rate", "Abstention rate (LLM judge)"),
    ]
    detector_metrics = [
        ("mitigation_rate_detector", "Mitigation rate (detector)"),
        ("persistence_rate_detector", "Persistence rate (detector)"),
        ("amplification_rate_detector", "Amplification rate (detector)"),
        ("judge_detector_disagreement_rate", "Judge-detector disagreement rate"),
    ]
    workflow_metrics = [
        ("mitigations_per_minute", "Mitigations per minute"),
        ("time_to_first_secure_fix_seconds", "Time to first secure fix (s)"),
        ("judge_strategy_variance", "Judge strategy variance (entropy)"),
    ]

    df = pd.read_csv(in_csv)
    if "condition" in df.columns:
        df["condition"] = df["condition"].astype(str).str.strip().str.lower()
        df = df[df["condition"].isin(VALID_CONDITIONS)].copy()

    for col, _ in (base_metrics + detector_metrics + workflow_metrics):
        if col in df.columns:
            df[col] = df[col].apply(_safe_float_for_summary)

    if "time_to_first_secure_fix_seconds" in df.columns:
        df.loc[df["time_to_first_secure_fix_seconds"] < 0, "time_to_first_secure_fix_seconds"] = np.nan

    lines: list[str] = []
    lines.append("Pilot summary statistics")
    lines.append(f"Participants: {len(df)}")
    lines.append("")

    lines.append("Overall (primary = LLM judge):")
    for col, label in base_metrics:
        if col not in df.columns:
            continue
        described = _describe_series_for_summary(df[col])
        lines.append(f"- {label}: {described['mean']:.3f} +/- {described['std']:.3f} (n={described['n']})")
    lines.append("")

    lines.append("Secondary (diagnostics):")
    detector_rows_scored = 0
    if "scored_snippets" in df.columns:
        scored_series = pd.to_numeric(df["scored_snippets"], errors="coerce").fillna(0.0)
        detector_rows_scored = int(scored_series.sum())

    if detector_rows_scored > 0:
        for col, label in detector_metrics:
            if col not in df.columns:
                continue
            described = _describe_series_for_summary(df[col])
            lines.append(f"- {label}: {described['mean']:.3f} +/- {described['std']:.3f} (n={described['n']})")
    else:
        lines.append("- Detector diagnostics: not enabled or no detector-supported rows were analyzed.")

    for col, label in workflow_metrics:
        if col not in df.columns:
            continue
        described = _describe_series_for_summary(df[col])
        lines.append(f"- {label}: {described['mean']:.3f} +/- {described['std']:.3f} (n={described['n']})")
    lines.append("")

    lines.append("Condition contrasts are disabled in security-only study mode.")
    lines.append("")
    return lines


def write_pilot_stats_text(in_csv: str, out_txt: str) -> str:
    """Write the pilot summary text file and return its output path."""
    lines = compute_pilot_summary_lines(in_csv)
    Path(out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_txt
