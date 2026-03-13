"""Lower-level statistical utilities used by research analysis code.

Includes chi-square and Welch t-test helpers plus result serialization
patterns for reproducible downstream reporting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional, cast

import pandas as pd
import numpy as np
from scipy.stats import chi2_contingency, ttest_ind


OUTCOME_ORDER = ["Mitigated", "Preserved", "Obfuscated", "Amplified"]
VALID_CONDITIONS = ["productivity", "security"]


@dataclass
class TestResult:
    """Standardized statistical-test result container."""
    name: str
    statistic: float
    p_value: float
    extra: Dict[str, Any]


def to_float(x: Any) -> float:
    """Coerce scipy/numpy scalar-ish outputs to a real Python float."""
    try:
        if hasattr(x, "item"):
            return float(x.item())
        return float(x)
    except Exception:
        return float(np.asarray(x).reshape(-1)[0])


def to_int(x: Any) -> int:
    """Coerce scipy/numpy scalar-ish outputs to a real Python int."""
    try:
        if hasattr(x, "item"):
            return int(x.item())
        return int(x)
    except Exception:
        return int(np.asarray(x).reshape(-1)[0])


def _coerce_numeric(series: pd.Series) -> pd.Series:
    """Convert a series to numeric values with NaN coercion."""
    return pd.to_numeric(series, errors="coerce")


def chi_square_condition_outcome(df: pd.DataFrame) -> TestResult:
    """Run chi-square test on condition x outcome contingency table."""
    tab = pd.crosstab(df["condition"], df["outcome"]).reindex(
        index=VALID_CONDITIONS, columns=OUTCOME_ORDER, fill_value=0
    )

    res = chi2_contingency(tab.to_numpy())

    # Support both tuple-returning and object-returning SciPy versions
    chi2 = getattr(res, "statistic", res[0])
    p = getattr(res, "pvalue", res[1])
    dof = getattr(res, "dof", res[2])
    expected = getattr(res, "expected_freq", res[3])

    return TestResult(
        name="chi_square_condition_x_outcome",
        statistic=to_float(chi2),
        p_value=to_float(p),
        extra={
            "dof": to_int(dof),
            "observed": tab.to_dict(),
            "expected": np.asarray(expected).tolist(),
        },
    )


def welch_t_test(df: pd.DataFrame, col: str) -> Optional[TestResult]:
    """Run Welch t-test between productivity and security for one numeric column."""
    if col not in df.columns:
        return None

    d = df[["condition", col]].copy()
    d[col] = _coerce_numeric(d[col])
    d = d.dropna()
    if d.empty:
        return None

    a = d[d["condition"] == "productivity"][col]
    b = d[d["condition"] == "security"][col]
    if a.empty or b.empty:
        return None

    res = ttest_ind(a.to_numpy(), b.to_numpy(), equal_var=False)
    t = getattr(res, "statistic", res[0])
    p = getattr(res, "pvalue", res[1])

    return TestResult(
        name=f"welch_t_{col}",
        statistic=to_float(t),
        p_value=to_float(p),
        extra={
            "n_productivity": to_int(a.shape[0]),
            "n_security": to_int(b.shape[0]),
            "mean_productivity": to_float(a.mean()),
            "mean_security": to_float(b.mean()),
            "std_productivity": to_float(a.std(ddof=1)) if a.shape[0] > 1 else 0.0,
            "std_security": to_float(b.std(ddof=1)) if b.shape[0] > 1 else 0.0,
        },
    )


def compute_all_stats(merged_csv: str) -> Dict[str, Any]:
    """Compute inferential statistics from merged snippet-level CSV."""
    p = Path(merged_csv)
    if not p.exists():
        raise FileNotFoundError(f"Missing merged CSV: {p}")

    df = pd.read_csv(p)
    df["condition"] = df["condition"].astype(str).str.strip().str.lower()
    df["outcome"] = df["outcome"].astype(str).str.strip()

    df = df[df["condition"].isin(VALID_CONDITIONS)].copy()
    df = df[df["outcome"].isin(OUTCOME_ORDER)].copy()

    tests: List[TestResult] = [chi_square_condition_outcome(df)]

    for col in ["confidence_overall_1to5", "total_seconds", "lines_changed", "lines_added", "lines_removed"]:
        r = welch_t_test(df, col)
        if r is not None:
            tests.append(r)

    return {
        "n_rows": to_int(df.shape[0]),
        "tests": [
            {"name": t.name, "statistic": t.statistic, "p_value": t.p_value, "extra": t.extra}
            for t in tests
        ],
    }


def write_stats(merged_csv: str, out_json: str) -> str:
    """Write inferential stats JSON file and return output path."""
    payload = compute_all_stats(merged_csv)
    Path(out_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_json


def _safe_float_for_summary(x: Any) -> float:
    """Coerce CSV-loaded values into floats while preserving NaN on bad inputs."""
    if x is None:
        return np.nan

    if isinstance(x, (tuple, list)):
        if not x:
            return np.nan
        x = x[0]

    if isinstance(x, str):
        s = x.strip()
        if s == "" or s.lower() in ("nan", "none", "null"):
            return np.nan
        try:
            return float(s)
        except Exception:
            return np.nan

    try:
        return float(x)
    except Exception:
        return np.nan


def _to_float_array_for_summary(series: pd.Series) -> np.ndarray:
    """Convert a pandas series to float numpy array with NaN coercion."""
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)


def _describe_series_for_summary(series: pd.Series) -> Dict[str, Any]:
    """Return n/mean/std summary for one numeric series."""
    arr = _to_float_array_for_summary(series)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        return {"n": 0, "mean": np.nan, "std": np.nan}
    if arr.size == 1:
        return {"n": 1, "mean": float(arr[0]), "std": 0.0}
    return {"n": int(arr.size), "mean": float(np.mean(arr)), "std": float(np.std(arr, ddof=1))}


def _welch_ttest_for_summary(a: pd.Series, b: pd.Series) -> Optional[tuple[float, float]]:
    """Run Welch t-test when both groups have at least 2 valid samples."""
    a_arr = _to_float_array_for_summary(a)
    b_arr = _to_float_array_for_summary(b)
    a_arr = a_arr[~np.isnan(a_arr)]
    b_arr = b_arr[~np.isnan(b_arr)]
    if a_arr.size < 2 or b_arr.size < 2:
        return None

    # SciPy typing can vary across versions/typeshed: normalize safely here.
    res = cast(Any, ttest_ind(a_arr, b_arr, equal_var=False))
    if hasattr(res, "statistic") and hasattr(res, "pvalue"):
        try:
            return float(res.statistic), float(res.pvalue)
        except Exception:
            return None
    try:
        seq = cast(tuple[Any, Any], res)
        return float(seq[0]), float(seq[1])
    except Exception:
        return None


def compute_pilot_summary_lines(in_csv: str) -> List[str]:
    """Build text report lines for pilot aggregate summary statistics."""
    base_metrics = [
        ("duration_seconds", "Duration (s)"),
        ("primary_mitigation_rate", "Mitigation rate (LLM judge)"),
        ("primary_persistence_rate", "Persistence rate (LLM judge)"),
        ("primary_abstention_rate", "Abstention rate (LLM judge)"),
    ]
    secondary_metrics = [
        ("mitigation_rate_detector", "Mitigation rate (detector)"),
        ("persistence_rate_detector", "Persistence rate (detector)"),
        ("amplification_rate_detector", "Amplification rate (detector)"),
        ("judge_detector_disagreement_rate", "Judge-detector disagreement rate"),
        ("mitigations_per_minute", "Mitigations per minute"),
        ("time_to_first_secure_fix_seconds", "Time to first secure fix (s)"),
        ("judge_strategy_variance", "Judge strategy variance (entropy)"),
    ]

    df = pd.read_csv(in_csv)
    for col, _ in (base_metrics + secondary_metrics):
        if col in df.columns:
            df[col] = df[col].apply(_safe_float_for_summary)

    # The aggregate layer uses -1 when first-fix timing is unavailable.
    if "time_to_first_secure_fix_seconds" in df.columns:
        df.loc[df["time_to_first_secure_fix_seconds"] < 0, "time_to_first_secure_fix_seconds"] = np.nan

    lines: List[str] = []
    lines.append("Pilot summary statistics")
    lines.append(f"Participants: {len(df)}")
    lines.append("")

    lines.append("Overall (primary = LLM judge):")
    for col, label in base_metrics:
        if col not in df.columns:
            continue
        d = _describe_series_for_summary(df[col])
        lines.append(f"- {label}: {d['mean']:.3f} +/- {d['std']:.3f} (n={d['n']})")
    lines.append("")

    if "condition" in df.columns:
        lines.append("By condition (primary = LLM judge):")
        for cond, group in df.groupby("condition"):
            lines.append(f"Condition: {cond} (n={len(group)})")
            for col, label in base_metrics:
                if col not in group.columns:
                    continue
                d = _describe_series_for_summary(group[col])
                lines.append(f"  - {label}: {d['mean']:.3f} +/- {d['std']:.3f} (n={d['n']})")
        lines.append("")

    lines.append("Secondary (diagnostics):")
    for col, label in secondary_metrics:
        if col not in df.columns:
            continue
        d = _describe_series_for_summary(df[col])
        lines.append(f"- {label}: {d['mean']:.3f} +/- {d['std']:.3f} (n={d['n']})")
    lines.append("")

    if "condition" in df.columns and df["condition"].nunique() == 2:
        conds = list(df["condition"].dropna().unique())
        a = df[df["condition"] == conds[0]]
        b = df[df["condition"] == conds[1]]
        lines.append("Welch t-tests (primary metrics):")
        lines.append(f"Comparing: {conds[0]} vs {conds[1]}")
        for col, label in base_metrics:
            if col not in df.columns:
                continue
            res = _welch_ttest_for_summary(a[col], b[col])
            if res is None:
                lines.append(f"- {label}: insufficient samples")
            else:
                t_val, p_val = res
                lines.append(f"- {label}: t={t_val:.3f}, p={p_val:.4f}")
        lines.append("")

    return lines


def write_pilot_stats_text(in_csv: str, out_txt: str) -> str:
    """Write pilot summary statistics text file and return its output path."""
    lines = compute_pilot_summary_lines(in_csv)
    Path(out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_txt
