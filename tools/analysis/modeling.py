"""Build de-identified snippet-level model datasets and fit local logit models.

The current implementation uses participant/snippet fixed effects so the
analysis can run locally with the repository's existing scientific stack.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm


_TRUTHY = {"1", "true", "t", "yes", "y"}
_MODEL_BASE_COLUMNS = [
    "llm_turns",
    "llm_applied_ratio",
    "llm_confidence_1to5",
]


def _resolve_run_dir(run_dir: Path) -> Path:
    """Resolve one extra nested extracted run directory when present."""
    run_dir = Path(run_dir)
    if not run_dir.exists():
        return run_dir

    top_level_markers = ["edits", "logs", "analysis", "condition.txt", "start_end_times.json"]
    if any((run_dir / marker).exists() for marker in top_level_markers):
        return run_dir

    nested_candidates = [
        p for p in run_dir.iterdir() if p.is_dir() and (p / "edits").exists() and (p / "logs").exists()
    ]
    if len(nested_candidates) == 1:
        return nested_candidates[0]
    return run_dir


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk and return {} on failure."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Load CSV rows from disk."""
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _load_condition(run_dir: Path) -> str:
    """Read the stored condition label for one run."""
    path = run_dir / "condition.txt"
    return path.read_text(encoding="utf-8").strip().lower() if path.exists() else "unknown"


def _is_truthy(value: Any) -> bool:
    """Return True for common truthy text values."""
    return str(value or "").strip().lower() in _TRUTHY


def _safe_int(value: Any, default: int = 0) -> int:
    """Parse integers from CSV-style scalar values."""
    try:
        return int(float(value))
    except Exception:
        return default


def _safe_float(value: Any, default: float = np.nan) -> float:
    """Parse floats from CSV-style scalar values."""
    try:
        return float(value)
    except Exception:
        return default


def _primary_outcome(row: dict[str, str], primary_source: str) -> str:
    """Resolve the run's primary outcome label for one snippet row."""
    if primary_source == "judge":
        verdict = str(row.get("judge_verdict", "") or "").strip().lower()
        if verdict == "absent":
            return "Mitigated"
        if verdict == "present":
            return "Preserved"
        if verdict == "uncertain":
            return "UNKNOWN"

    outcome = str(row.get("outcome", "") or "").strip()
    if outcome:
        return outcome
    return "UNKNOWN"


def build_snippet_model_dataset(runs_root: str | Path) -> pd.DataFrame:
    """Build a de-identified snippet-level modeling dataset from analyzed runs."""
    runs_path = Path(runs_root)
    rows_out: list[dict[str, Any]] = []

    for participant_index, participant_dir in enumerate(
        sorted([p for p in runs_path.iterdir() if p.is_dir()] if runs_path.exists() else []),
        start=1,
    ):
        run_dir = _resolve_run_dir(participant_dir)
        results_csv = run_dir / "analysis" / "results.csv"
        summary_json = run_dir / "analysis" / "summary.json"
        if not results_csv.exists() or not summary_json.exists():
            continue

        summary = _read_json(summary_json)
        primary_source = str(summary.get("primary_source", "") or "").strip() or "detector"
        condition = _load_condition(run_dir)
        profile = summary.get("participant_profile", {}) if isinstance(summary.get("participant_profile"), dict) else {}
        participant_code = f"participant_{participant_index:03d}"

        for row in _read_csv(results_csv):
            if str(row.get("status", "") or "").strip().lower() != "ok":
                continue

            primary = _primary_outcome(row, primary_source)
            rows_out.append(
                {
                    "run_id": participant_code,
                    "participant_id": participant_code,
                    "snippet_id": str(row.get("snippet_id", "") or "").strip(),
                    "condition": condition,
                    "primary_source": primary_source,
                    "primary_outcome": primary,
                    "primary_mitigated": 1 if primary == "Mitigated" else 0,
                    "primary_unknown": 1 if primary == "UNKNOWN" else 0,
                    "detector_outcome": str(row.get("outcome", "") or "").strip(),
                    "judge_verdict": str(row.get("judge_verdict", "") or "").strip().lower(),
                    "judge_enabled": 1 if _is_truthy(row.get("judge_enabled")) else 0,
                    "judge_scored": 1 if str(row.get("judge_verdict", "") or "").strip() else 0,
                    "language": str(row.get("language", "") or "").strip().lower() or "unknown",
                    "vuln_type": str(row.get("vuln_type", "") or "").strip(),
                    "cwe": str(row.get("cwe", "") or "").strip(),
                    "llm_turns": _safe_int(row.get("llm_turns"), 0),
                    "llm_applied_turns": _safe_int(row.get("llm_applied_turns"), 0),
                    "llm_applied_ratio": _safe_float(row.get("llm_applied_ratio"), np.nan),
                    "llm_confidence_1to5": _safe_float(row.get("llm_confidence_1to5"), np.nan),
                    "llm_strategy_primary": str(row.get("llm_strategy_primary", "") or "").strip(),
                    "participant_programming_experience": str(profile.get("programming_experience", "") or "").strip(),
                    "participant_language_experience": str(profile.get("language_experience", "") or "").strip(),
                    "participant_llm_coding_experience": str(profile.get("llm_coding_experience", "") or "").strip(),
                    "participant_security_experience": str(profile.get("security_experience", "") or "").strip(),
                }
            )

    return pd.DataFrame(rows_out)


def _one_hot_block(series: pd.Series, prefix: str) -> tuple[np.ndarray, list[str]]:
    """Encode one categorical series as drop-first one-hot columns."""
    labels = sorted({str(x) for x in series.dropna().astype(str) if str(x)})
    if len(labels) <= 1:
        return np.empty((len(series), 0), dtype=float), []

    reference = labels[0]
    cols: list[np.ndarray] = []
    names: list[str] = []
    values = series.fillna("").astype(str)
    for label in labels[1:]:
        cols.append((values == label).astype(float).to_numpy())
        names.append(f"{prefix}[{label}]")
    return np.column_stack(cols), names


def _design_matrix(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Build the fixed-effects logit design matrix and binary target vector."""
    clean = df.copy()
    for col in _MODEL_BASE_COLUMNS:
        clean[col] = pd.to_numeric(clean[col], errors="coerce")

    X_blocks: list[np.ndarray] = [np.ones((len(clean), 1), dtype=float)]
    term_names: list[str] = ["intercept"]

    for col in _MODEL_BASE_COLUMNS:
        X_blocks.append(clean[[col]].to_numpy(dtype=float))
        term_names.append(col)

    for col in ["language", "vuln_type", "participant_id", "snippet_id"]:
        block, names = _one_hot_block(clean[col], col)
        if names:
            X_blocks.append(block)
            term_names.extend(names)

    X = np.column_stack(X_blocks)
    y = clean["primary_mitigated"].to_numpy(dtype=float)
    return X, y, term_names


def _fit_logit_irls(X: np.ndarray, y: np.ndarray, *, ridge: float = 1e-6, max_iter: int = 200, tol: float = 1e-8) -> dict[str, Any]:
    """Fit a logistic regression with light ridge regularization via IRLS."""
    n_features = X.shape[1]
    beta = np.zeros(n_features, dtype=float)
    penalty = np.eye(n_features, dtype=float) * ridge
    penalty[0, 0] = 0.0
    converged = False

    for iteration in range(1, max_iter + 1):
        eta = np.clip(X @ beta, -30.0, 30.0)
        p = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(p * (1.0 - p), 1e-6, None)
        z = eta + (y - p) / w

        xtwx = X.T @ (w[:, None] * X) + penalty
        xtwz = X.T @ (w * z)
        try:
            beta_new = np.linalg.solve(xtwx, xtwz)
        except np.linalg.LinAlgError:
            beta_new = np.linalg.pinv(xtwx) @ xtwz

        if np.max(np.abs(beta_new - beta)) < tol:
            beta = beta_new
            converged = True
            break
        beta = beta_new
    else:
        iteration = max_iter

    eta = np.clip(X @ beta, -30.0, 30.0)
    p = 1.0 / (1.0 + np.exp(-eta))
    w = np.clip(p * (1.0 - p), 1e-6, None)
    fisher = X.T @ (w[:, None] * X) + penalty
    cov = np.linalg.pinv(fisher)
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    z_scores = np.divide(beta, se, out=np.zeros_like(beta), where=se > 0)
    p_values = 2.0 * (1.0 - norm.cdf(np.abs(z_scores)))

    return {
        "beta": beta,
        "std_error": se,
        "z": z_scores,
        "p_value": p_values,
        "covariance": cov,
        "converged": converged,
        "iterations": iteration,
    }


def fit_primary_mitigation_model(df: pd.DataFrame) -> dict[str, Any]:
    """Fit a participant/snippet-adjusted logistic model for mitigation."""
    if df.empty:
        return {
            "method": "fixed_effects_logit",
            "status": "no_rows",
            "n_rows": 0,
            "n_modeled_rows": 0,
            "excluded_unknown": 0,
            "excluded_missing_predictors": 0,
            "coefficients": [],
        }

    excluded_unknown = int((df["primary_unknown"] == 1).sum()) if "primary_unknown" in df.columns else 0
    modeled = df[df["primary_unknown"] == 0].copy()

    required = ["primary_mitigated", *_MODEL_BASE_COLUMNS, "participant_id", "snippet_id", "language", "vuln_type"]
    for col in required:
        if col not in modeled.columns:
            modeled[col] = np.nan

    before_missing = len(modeled)
    modeled = modeled.dropna(subset=["primary_mitigated", *_MODEL_BASE_COLUMNS, "participant_id", "snippet_id"])
    excluded_missing = before_missing - len(modeled)

    if modeled.empty or modeled["primary_mitigated"].nunique() < 2:
        return {
            "method": "fixed_effects_logit",
            "status": "insufficient_variation",
            "n_rows": int(len(df)),
            "n_modeled_rows": int(len(modeled)),
            "excluded_unknown": excluded_unknown,
            "excluded_missing_predictors": excluded_missing,
            "coefficients": [],
        }

    X, y, term_names = _design_matrix(modeled)
    fit = _fit_logit_irls(X, y)

    coefficients: list[dict[str, Any]] = []
    for idx, name in enumerate(term_names):
        estimate = float(fit["beta"][idx])
        coefficients.append(
            {
                "term": name,
                "estimate": estimate,
                "std_error": float(fit["std_error"][idx]),
                "z_value": float(fit["z"][idx]),
                "p_value": float(fit["p_value"][idx]),
                "odds_ratio": float(np.exp(np.clip(estimate, -30.0, 30.0))),
            }
        )

    return {
        "method": "fixed_effects_logit",
        "status": "ok",
        "n_rows": int(len(df)),
        "n_modeled_rows": int(len(modeled)),
        "excluded_unknown": excluded_unknown,
        "excluded_missing_predictors": excluded_missing,
        "converged": bool(fit["converged"]),
        "iterations": int(fit["iterations"]),
        "coefficients": coefficients,
        "focal_terms": [name for name in term_names if name in {"llm_turns", "llm_applied_ratio", "llm_confidence_1to5"}],
    }


def _text_summary(model: dict[str, Any]) -> str:
    """Render a concise text summary for the fitted model."""
    lines = [
        "Snippet-level mitigation model",
        f"Method: {model.get('method', 'unknown')}",
        f"Status: {model.get('status', 'unknown')}",
        f"Rows total: {model.get('n_rows', 0)}",
        f"Rows modeled: {model.get('n_modeled_rows', 0)}",
        f"Excluded UNKNOWN: {model.get('excluded_unknown', 0)}",
        f"Excluded missing predictors: {model.get('excluded_missing_predictors', 0)}",
    ]
    if model.get("status") != "ok":
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            f"Converged: {model.get('converged', False)}",
            f"Iterations: {model.get('iterations', 0)}",
            "",
            "Focal coefficients:",
        ]
    )

    coeffs = {row["term"]: row for row in model.get("coefficients", []) if isinstance(row, dict)}
    for term in ["llm_turns", "llm_applied_ratio", "llm_confidence_1to5"]:
        row = coeffs.get(term)
        if not row:
            continue
        lines.append(
            f"- {term}: beta={row['estimate']:.4f}, se={row['std_error']:.4f}, "
            f"z={row['z_value']:.4f}, p={row['p_value']:.4f}, OR={row['odds_ratio']:.4f}"
        )

    lines.append("")
    lines.append("Note: exported participant IDs are de-identified local labels.")
    lines.append("Note: participant_id, snippet_id, language, and vuln_type fixed effects are included in the fitted design matrix.")
    return "\n".join(lines) + "\n"


def write_model_artifacts(*, runs_root: str | Path, out_csv: str | Path, out_json: str | Path, out_txt: str | Path) -> dict[str, Any]:
    """Build the snippet-level dataset, fit the local model, and write outputs."""
    dataset = build_snippet_model_dataset(runs_root)
    out_csv = Path(out_csv)
    out_json = Path(out_json)
    out_txt = Path(out_txt)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)

    if dataset.empty:
        dataset.to_csv(out_csv, index=False)
    else:
        dataset.to_csv(out_csv, index=False)

    model = fit_primary_mitigation_model(dataset)
    payload = {
        "dataset_rows": int(len(dataset)),
        "columns": list(dataset.columns),
        "model": model,
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    out_txt.write_text(_text_summary(model), encoding="utf-8")
    return payload
