"""Build judge calibration sets and run judge audit sweeps."""

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from tools.analysis import llm_judge


DEFAULT_AUDIT_ROOT = Path("data") / "aggregated" / "judge_audit"
DEFAULT_CALIBRATION_CSV = Path("data") / "aggregated" / "judge_calibration.csv"
DEFAULT_FREEZE_PATH = Path("data") / "aggregated" / "judge_freeze.json"


@dataclass(frozen=True)
class CalibrationCase:
    """One judge calibration case."""

    case_id: str
    snippet_id: str
    vuln_type: str
    cwe: str
    language: str
    baseline_relpath: str
    gold_relpath: str
    edited_relpath: str
    expected_verdict: str
    source_case: str
    notes: str = ""


def _repo_root() -> Path:
    """Return the repository root."""
    return Path(__file__).resolve().parents[2]


def _now_stamp() -> str:
    """Return a compact UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_text(path: Path) -> str:
    """Read text and return an empty string when the file cannot be read."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows into dictionaries."""
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write row dictionaries to CSV."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _repo_relative(path: Path) -> str:
    """Return a repo-relative path when possible."""
    root = _repo_root().resolve()
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(resolved)


def _normalize_expected_verdict(value: str) -> str:
    """Normalize expected-verdict text."""
    text = (value or "").strip().lower()
    return text if text in {"present", "absent", "uncertain"} else ""


def build_control_calibration_dataset(
    *,
    metadata_csv: Path,
    out_csv: Path,
    manual_cases_csv: Path | None = None,
) -> dict[str, Any]:
    """Build a baseline/gold control set for judge calibration."""
    if not metadata_csv.exists():
        raise FileNotFoundError(f"Missing metadata CSV: {metadata_csv}")

    rows = _read_csv_rows(metadata_csv)
    out_rows: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    repo_root = _repo_root()
    auto_rows = 0
    manual_rows = 0

    for row in rows:
        snippet_id = str(row.get("snippet_id") or "").strip()
        baseline_rel = str(row.get("baseline_relpath") or "").strip()
        gold_rel = str(row.get("gold_relpath") or "").strip()
        vuln_type = str(row.get("vuln_type") or row.get("vulnerability_type") or "").strip()
        cwe = str(row.get("cwe") or row.get("cwe_primary") or "").strip()
        language = str(row.get("language") or "").strip()
        if not snippet_id or not baseline_rel:
            continue

        baseline_path = Path(baseline_rel)
        if not baseline_path.is_absolute():
            baseline_path = repo_root / baseline_path
        if baseline_path.exists():
            case_id = f"{snippet_id}__baseline_present"
            out_rows.append(
                {
                    "case_id": case_id,
                    "snippet_id": snippet_id,
                    "vuln_type": vuln_type,
                    "cwe": cwe,
                    "language": language,
                    "baseline_relpath": _repo_relative(baseline_path),
                    "gold_relpath": gold_rel,
                    "edited_relpath": _repo_relative(baseline_path),
                    "expected_verdict": "present",
                    "source_case": "baseline_control",
                    "notes": "Baseline replay control.",
                }
            )
            seen_case_ids.add(case_id)
            auto_rows += 1

        if gold_rel:
            gold_path = Path(gold_rel)
            if not gold_path.is_absolute():
                gold_path = repo_root / gold_path
            if gold_path.exists():
                case_id = f"{snippet_id}__gold_absent"
                out_rows.append(
                    {
                        "case_id": case_id,
                        "snippet_id": snippet_id,
                        "vuln_type": vuln_type,
                        "cwe": cwe,
                        "language": language,
                        "baseline_relpath": _repo_relative(baseline_path),
                        "gold_relpath": _repo_relative(gold_path),
                        "edited_relpath": _repo_relative(gold_path),
                        "expected_verdict": "absent",
                        "source_case": "gold_control",
                        "notes": "Gold replay control.",
                    }
                )
                seen_case_ids.add(case_id)
                auto_rows += 1

    if manual_cases_csv and manual_cases_csv.exists():
        for row in _read_csv_rows(manual_cases_csv):
            case_id = str(row.get("case_id") or "").strip()
            expected_verdict = _normalize_expected_verdict(str(row.get("expected_verdict") or ""))
            if not case_id or not expected_verdict:
                continue
            if case_id in seen_case_ids:
                raise ValueError(f"Duplicate calibration case_id: {case_id}")
            out_rows.append(
                {
                    "case_id": case_id,
                    "snippet_id": str(row.get("snippet_id") or "").strip(),
                    "vuln_type": str(row.get("vuln_type") or "").strip(),
                    "cwe": str(row.get("cwe") or "").strip(),
                    "language": str(row.get("language") or "").strip(),
                    "baseline_relpath": str(row.get("baseline_relpath") or "").strip(),
                    "gold_relpath": str(row.get("gold_relpath") or "").strip(),
                    "edited_relpath": str(row.get("edited_relpath") or "").strip(),
                    "expected_verdict": expected_verdict,
                    "source_case": str(row.get("source_case") or "manual").strip() or "manual",
                    "notes": str(row.get("notes") or "").strip(),
                }
            )
            seen_case_ids.add(case_id)
            manual_rows += 1

    if not out_rows:
        raise ValueError("No calibration rows were generated.")

    _write_csv_rows(out_csv, out_rows)
    return {
        "out_csv": str(out_csv),
        "rows": len(out_rows),
        "auto_rows": auto_rows,
        "manual_rows": manual_rows,
    }


def _load_calibration_cases(calibration_csv: Path) -> list[CalibrationCase]:
    """Load calibration rows from CSV."""
    if not calibration_csv.exists():
        raise FileNotFoundError(f"Missing calibration CSV: {calibration_csv}")

    cases: list[CalibrationCase] = []
    for row in _read_csv_rows(calibration_csv):
        expected_verdict = _normalize_expected_verdict(str(row.get("expected_verdict") or ""))
        if not expected_verdict:
            continue
        case = CalibrationCase(
            case_id=str(row.get("case_id") or "").strip(),
            snippet_id=str(row.get("snippet_id") or "").strip(),
            vuln_type=str(row.get("vuln_type") or "").strip(),
            cwe=str(row.get("cwe") or "").strip(),
            language=str(row.get("language") or "").strip(),
            baseline_relpath=str(row.get("baseline_relpath") or "").strip(),
            gold_relpath=str(row.get("gold_relpath") or "").strip(),
            edited_relpath=str(row.get("edited_relpath") or "").strip(),
            expected_verdict=expected_verdict,
            source_case=str(row.get("source_case") or "").strip(),
            notes=str(row.get("notes") or "").strip(),
        )
        if not case.case_id or not case.edited_relpath:
            continue
        cases.append(case)

    if not cases:
        raise ValueError(f"No usable calibration rows found in {calibration_csv}")
    return cases


def _resolve_case_path(value: str) -> Path:
    """Resolve one calibration file path against the repo root when needed."""
    path = Path(value)
    if path.is_absolute():
        return path
    return (_repo_root() / path).resolve()


def _confidence_by_verdict(rows: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Compute mean confidence grouped by predicted verdict."""
    buckets: dict[str, list[float]] = {"present": [], "absent": [], "uncertain": []}
    for row in rows:
        verdict = str(row.get("predicted_verdict") or "").strip().lower()
        if verdict not in buckets:
            continue
        try:
            buckets[verdict].append(float(row.get("confidence") or 0.0))
        except Exception:
            pass
    return {
        verdict: (sum(values) / len(values) if values else 0.0)
        for verdict, values in buckets.items()
    }


def _compute_config_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute accuracy, F1, abstention, and coverage for each audit config."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("config_id") or ""), []).append(row)

    out: list[dict[str, Any]] = []
    for config_id, group in sorted(buckets.items()):
        tp = tn = fp = fn = unknown_present = unknown_absent = exact = 0
        for row in group:
            expected = str(row.get("expected_verdict") or "").strip().lower()
            predicted = str(row.get("predicted_verdict") or "").strip().lower()
            if predicted == expected:
                exact += 1

            if expected == "present":
                if predicted == "present":
                    tp += 1
                elif predicted == "absent":
                    fn += 1
                else:
                    unknown_present += 1
            elif expected == "absent":
                if predicted == "absent":
                    tn += 1
                elif predicted == "present":
                    fp += 1
                else:
                    unknown_absent += 1

        total = len(group)
        answered = tp + tn + fp + fn
        abstentions = unknown_present + unknown_absent
        accuracy = exact / total if total else 0.0
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn + unknown_present) if (tp + fn + unknown_present) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        coverage = answered / total if total else 0.0
        abstention_rate = abstentions / total if total else 0.0
        effective_f1 = f1 * coverage

        sample = group[0]
        bundle = str(sample.get("strategy_bundle") or "").strip()
        out.append(
            {
                "config_id": config_id,
                "config_kind": str(sample.get("config_kind") or "").strip(),
                "strategy_bundle": bundle,
                "parser_mode": str(sample.get("parser_mode") or "").strip(),
                "vote_rule": str(sample.get("vote_rule") or "").strip(),
                "cases": total,
                "exact_match_rate": accuracy,
                "accuracy": accuracy,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "coverage": coverage,
                "abstention_rate": abstention_rate,
                "effective_f1": effective_f1,
                "tp": tp,
                "tn": tn,
                "fp": fp,
                "fn": fn,
                "unknown_present": unknown_present,
                "unknown_absent": unknown_absent,
                "mean_confidence": (
                    sum(float(row.get("confidence") or 0.0) for row in group) / total if total else 0.0
                ),
                "mean_confidence_by_verdict": _confidence_by_verdict(group),
            }
        )
    return out


def _compute_strategy_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Measure disagreement and entropy across single-strategy outputs."""
    buckets: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        if str(row.get("config_kind") or "") != "single":
            continue
        parser_mode = str(row.get("parser_mode") or "").strip()
        case_id = str(row.get("case_id") or "").strip()
        verdict = str(row.get("predicted_verdict") or "").strip().lower()
        if not parser_mode or not case_id or not verdict:
            continue
        buckets.setdefault(parser_mode, {}).setdefault(case_id, []).append(verdict)

    out: list[dict[str, Any]] = []
    denom = math.log(3.0)
    for parser_mode, case_map in sorted(buckets.items()):
        comparable = 0
        disagreements = 0
        entropy_values: list[float] = []
        for verdicts in case_map.values():
            if len(verdicts) < 2:
                continue
            comparable += 1
            counts = {
                "present": verdicts.count("present"),
                "absent": verdicts.count("absent"),
                "uncertain": verdicts.count("uncertain"),
            }
            if sum(1 for count in counts.values() if count > 0) > 1:
                disagreements += 1
            entropy = 0.0
            total = len(verdicts)
            for count in counts.values():
                if count <= 0:
                    continue
                prob = count / total
                entropy += -prob * math.log(prob)
            entropy_values.append(entropy / denom if denom > 0 else 0.0)

        out.append(
            {
                "parser_mode": parser_mode,
                "cases_compared": comparable,
                "strategy_disagreement_rate": (disagreements / comparable) if comparable else 0.0,
                "mean_strategy_entropy": (
                    sum(entropy_values) / len(entropy_values) if entropy_values else 0.0
                ),
            }
        )
    return out


def _compute_parser_fragility(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Measure how often one config family changes verdict across parser modes."""
    families: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        family_id = "|".join(
            [
                str(row.get("config_kind") or "").strip(),
                str(row.get("strategy_bundle") or "").strip(),
                str(row.get("vote_rule") or "").strip(),
            ]
        )
        case_id = str(row.get("case_id") or "").strip()
        verdict = str(row.get("predicted_verdict") or "").strip().lower()
        if not family_id or not case_id or not verdict:
            continue
        families.setdefault(family_id, {}).setdefault(case_id, []).append(verdict)

    out: dict[str, float] = {}
    for family_id, case_map in families.items():
        comparable = 0
        fragile = 0
        for verdicts in case_map.values():
            if len(verdicts) < 2:
                continue
            comparable += 1
            if len(set(verdicts)) > 1:
                fragile += 1
        out[family_id] = (fragile / comparable) if comparable else 0.0
    return out


def _attach_fragility(
    config_metrics: list[dict[str, Any]],
    fragility_map: dict[str, float],
) -> None:
    """Attach parser fragility rates to per-config metric rows."""
    for metric in config_metrics:
        family_id = "|".join(
            [
                str(metric.get("config_kind") or "").strip(),
                str(metric.get("strategy_bundle") or "").strip(),
                str(metric.get("vote_rule") or "").strip(),
            ]
        )
        metric["parser_fragility_rate"] = fragility_map.get(family_id, 0.0)


def _pick_recommended_config(config_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose one audited config to freeze for participant scoring."""
    if not config_metrics:
        raise ValueError("No config metrics available.")

    return max(
        config_metrics,
        key=lambda row: (
            float(row.get("effective_f1") or 0.0),
            -float(row.get("abstention_rate") or 0.0),
            -float(row.get("parser_fragility_rate") or 0.0),
            float(row.get("accuracy") or 0.0),
            -len(str(row.get("strategy_bundle") or "")),
        ),
    )


def _split_strategy_bundle(value: str) -> list[str]:
    """Split one strategy bundle into individual strategy names."""
    return [item for item in str(value or "").split("+") if item]


def _runtime_defaults_from_config(config_path: str | Path | None) -> dict[str, Any]:
    """Read the non-frozen judge runtime settings that back the audit."""
    cfg = llm_judge._load_effective_config(config_path, use_frozen_config=False)
    llm_cfg = llm_judge._deep_get(cfg, ["llm_judge"], {}) or {}
    generation: dict[str, Any] = {}
    for key in (
        "temperature",
        "top_p",
        "top_k",
        "num_predict",
        "repeat_penalty",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "num_ctx",
    ):
        if key in llm_cfg:
            generation[key] = llm_cfg[key]
    env_options = llm_judge._parse_json_env("GLACIER_JUDGE_OPTIONS_JSON")
    generation.update(env_options)
    return {
        "model": str(os.getenv("GLACIER_JUDGE_MODEL", "").strip() or llm_cfg.get("model", "")),
        "ollama_url": str(os.getenv("GLACIER_OLLAMA_URL", "").strip() or llm_cfg.get("ollama_url", "")),
        "timeout_seconds": llm_judge._coerce_float(llm_cfg.get("timeout_seconds", 90), 90.0),
        "self_consistency_samples": llm_judge._coerce_int(
            os.getenv("GLACIER_JUDGE_SELF_CONSISTENCY_SAMPLES", ""),
            llm_judge._coerce_int(llm_cfg.get("self_consistency_samples", 5), 5),
        ),
        "generation": generation,
    }


def _build_freeze_payload(
    *,
    recommended: dict[str, Any],
    audit_dir: Path,
    audit_summary_path: Path,
    runtime_defaults: dict[str, Any],
) -> dict[str, Any]:
    """Build the frozen judge artifact consumed by participant scoring."""
    strategies = _split_strategy_bundle(str(recommended.get("strategy_bundle") or ""))
    config_kind = str(recommended.get("config_kind") or "").strip()
    strategy_mode = "single" if config_kind == "single" and len(strategies) == 1 else "ensemble"
    primary_strategy = strategies[0] if strategies else llm_judge._DEFAULT_STRATEGY

    llm_block: dict[str, Any] = {
        "enabled": True,
        "model": runtime_defaults.get("model", ""),
        "ollama_url": runtime_defaults.get("ollama_url", ""),
        "timeout_seconds": runtime_defaults.get("timeout_seconds", 90.0),
        "strategy_mode": strategy_mode,
        "primary_strategy": primary_strategy,
        "parser_mode": str(recommended.get("parser_mode") or "embedded_json"),
        "use_frozen_config": True,
        "self_consistency_samples": int(runtime_defaults.get("self_consistency_samples", 5)),
        "ensemble": {
            "vote_rule": str(recommended.get("vote_rule") or "majority"),
            "min_confidence": 0.0,
        },
        "strategies": {
            name: {"enabled": name in strategies}
            for name in llm_judge.SUPPORTED_STRATEGIES
        },
    }
    llm_block.update(runtime_defaults.get("generation", {}))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_dir": str(audit_dir.resolve()),
        "audit_summary_path": str(audit_summary_path.resolve()),
        "recommended_config_id": str(recommended.get("config_id") or ""),
        "recommended_metrics": {
            "accuracy": float(recommended.get("accuracy") or 0.0),
            "precision": float(recommended.get("precision") or 0.0),
            "recall": float(recommended.get("recall") or 0.0),
            "f1": float(recommended.get("f1") or 0.0),
            "coverage": float(recommended.get("coverage") or 0.0),
            "abstention_rate": float(recommended.get("abstention_rate") or 0.0),
            "effective_f1": float(recommended.get("effective_f1") or 0.0),
            "parser_fragility_rate": float(recommended.get("parser_fragility_rate") or 0.0),
        },
        "llm_judge": llm_block,
    }


def _write_summary_text(
    *,
    out_txt: Path,
    config_metrics: list[dict[str, Any]],
    strategy_diagnostics: list[dict[str, Any]],
    recommended: dict[str, Any],
) -> None:
    """Write a plain-text audit summary."""
    lines: list[str] = []
    lines.append("Judge audit summary")
    lines.append(f"Recommended config: {recommended.get('config_id', '')}")
    lines.append(
        "Recommended metrics: accuracy={:.3f}, f1={:.3f}, coverage={:.3f}, abstention={:.3f}, effective_f1={:.3f}".format(
            float(recommended.get("accuracy") or 0.0),
            float(recommended.get("f1") or 0.0),
            float(recommended.get("coverage") or 0.0),
            float(recommended.get("abstention_rate") or 0.0),
            float(recommended.get("effective_f1") or 0.0),
        )
    )
    lines.append("")
    lines.append("Config metrics")
    for row in config_metrics:
        lines.append(
            "- {config_id}: acc={accuracy:.3f} f1={f1:.3f} coverage={coverage:.3f} abstention={abstention_rate:.3f} effective_f1={effective_f1:.3f} parser_fragility={parser_fragility_rate:.3f}".format(
                **{
                    "config_id": str(row.get("config_id") or ""),
                    "accuracy": float(row.get("accuracy") or 0.0),
                    "f1": float(row.get("f1") or 0.0),
                    "coverage": float(row.get("coverage") or 0.0),
                    "abstention_rate": float(row.get("abstention_rate") or 0.0),
                    "effective_f1": float(row.get("effective_f1") or 0.0),
                    "parser_fragility_rate": float(row.get("parser_fragility_rate") or 0.0),
                }
            )
        )
    lines.append("")
    lines.append("Strategy diagnostics by parser mode")
    for row in strategy_diagnostics:
        lines.append(
            "- {parser_mode}: disagreement={strategy_disagreement_rate:.3f} entropy={mean_strategy_entropy:.3f}".format(
                **{
                    "parser_mode": str(row.get("parser_mode") or ""),
                    "strategy_disagreement_rate": float(row.get("strategy_disagreement_rate") or 0.0),
                    "mean_strategy_entropy": float(row.get("mean_strategy_entropy") or 0.0),
                }
            )
        )
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_judge_audit(
    *,
    calibration_csv: Path,
    out_root: Path,
    strategies: list[str],
    parser_modes: list[str],
    vote_rules: list[str],
    config_path: str | Path | None = None,
    write_global_freeze: bool = False,
    global_freeze_path: Path | None = None,
) -> dict[str, Any]:
    """Run the judge calibration sweep."""
    cases = _load_calibration_cases(calibration_csv)
    clean_strategies = [name for name in strategies if name in llm_judge.SUPPORTED_STRATEGIES]
    if not clean_strategies:
        raise ValueError("Select at least one valid judge strategy.")

    clean_parsers = [llm_judge.normalize_parser_mode(mode) for mode in parser_modes]
    clean_parsers = [mode for mode in clean_parsers if mode in llm_judge.SUPPORTED_PARSER_MODES]
    if not clean_parsers:
        raise ValueError("Select at least one valid parser mode.")

    clean_vote_rules = [rule for rule in vote_rules if rule in llm_judge.SUPPORTED_VOTE_RULES]
    if not clean_vote_rules:
        clean_vote_rules = ["majority"]

    out_dir = out_root / f"audit_{_now_stamp()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    result_rows: list[dict[str, Any]] = []
    for case in cases:
        baseline_code = _safe_text(_resolve_case_path(case.baseline_relpath))
        gold_code = _safe_text(_resolve_case_path(case.gold_relpath)) if case.gold_relpath else ""
        edited_code = _safe_text(_resolve_case_path(case.edited_relpath))
        if not edited_code:
            continue

        for parser_mode in clean_parsers:
            for strategy in clean_strategies:
                result = llm_judge.judge_edited_code_with_ollama(
                    snippet_id=case.snippet_id,
                    vuln_type=case.vuln_type,
                    cwe=case.cwe,
                    language=case.language,
                    baseline_code=baseline_code,
                    edited_code=edited_code,
                    gold_code=gold_code,
                    strategy=strategy,
                    parser_mode=parser_mode,
                    config_path=config_path,
                    use_frozen_config=False,
                )
                result_rows.append(
                    {
                        "case_id": case.case_id,
                        "snippet_id": case.snippet_id,
                        "source_case": case.source_case,
                        "expected_verdict": case.expected_verdict,
                        "config_id": f"single::{strategy}::{parser_mode}",
                        "config_kind": "single",
                        "strategy_bundle": strategy,
                        "parser_mode": parser_mode,
                        "vote_rule": "single",
                        "predicted_verdict": result.verdict,
                        "confidence": result.confidence,
                        "correct": int(result.verdict == case.expected_verdict),
                    }
                )

            if len(clean_strategies) > 1:
                bundle = "+".join(clean_strategies)
                for vote_rule in clean_vote_rules:
                    result = llm_judge.judge_edited_code_with_ollama(
                        snippet_id=case.snippet_id,
                        vuln_type=case.vuln_type,
                        cwe=case.cwe,
                        language=case.language,
                        baseline_code=baseline_code,
                        edited_code=edited_code,
                        gold_code=gold_code,
                        selected_strategies=clean_strategies,
                        vote_rule=vote_rule,
                        parser_mode=parser_mode,
                        config_path=config_path,
                        use_frozen_config=False,
                    )
                    result_rows.append(
                        {
                            "case_id": case.case_id,
                            "snippet_id": case.snippet_id,
                            "source_case": case.source_case,
                            "expected_verdict": case.expected_verdict,
                            "config_id": f"ensemble::{bundle}::{vote_rule}::{parser_mode}",
                            "config_kind": "ensemble",
                            "strategy_bundle": bundle,
                            "parser_mode": parser_mode,
                            "vote_rule": vote_rule,
                            "predicted_verdict": result.verdict,
                            "confidence": result.confidence,
                            "correct": int(result.verdict == case.expected_verdict),
                        }
                    )

    if not result_rows:
        raise ValueError("The judge audit did not produce any result rows.")

    results_csv = out_dir / "results.csv"
    _write_csv_rows(results_csv, result_rows)

    config_metrics = _compute_config_metrics(result_rows)
    strategy_diagnostics = _compute_strategy_diagnostics(result_rows)
    fragility_map = _compute_parser_fragility(result_rows)
    _attach_fragility(config_metrics, fragility_map)
    recommended = _pick_recommended_config(config_metrics)

    metrics_csv = out_dir / "config_metrics.csv"
    strategy_csv = out_dir / "strategy_diagnostics.csv"
    _write_csv_rows(metrics_csv, config_metrics)
    _write_csv_rows(strategy_csv, strategy_diagnostics)

    runtime_defaults = _runtime_defaults_from_config(config_path)
    summary_json = out_dir / "summary.json"
    summary_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calibration_csv": str(calibration_csv.resolve()),
        "cases": len(cases),
        "strategies": clean_strategies,
        "parser_modes": clean_parsers,
        "vote_rules": clean_vote_rules,
        "recommended_config": recommended,
        "config_metrics": config_metrics,
        "strategy_diagnostics": strategy_diagnostics,
    }
    summary_json.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")

    summary_txt = out_dir / "summary.txt"
    _write_summary_text(
        out_txt=summary_txt,
        config_metrics=config_metrics,
        strategy_diagnostics=strategy_diagnostics,
        recommended=recommended,
    )

    freeze_payload = _build_freeze_payload(
        recommended=recommended,
        audit_dir=out_dir,
        audit_summary_path=summary_json,
        runtime_defaults=runtime_defaults,
    )
    local_freeze = out_dir / "recommended_frozen_judge.json"
    local_freeze.write_text(json.dumps(freeze_payload, indent=2), encoding="utf-8")

    written_global_freeze = ""
    if write_global_freeze:
        target = global_freeze_path or DEFAULT_FREEZE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(freeze_payload, indent=2), encoding="utf-8")
        written_global_freeze = str(target.resolve())

    return {
        "audit_dir": str(out_dir.resolve()),
        "results_csv": str(results_csv.resolve()),
        "config_metrics_csv": str(metrics_csv.resolve()),
        "strategy_diagnostics_csv": str(strategy_csv.resolve()),
        "summary_json": str(summary_json.resolve()),
        "summary_txt": str(summary_txt.resolve()),
        "local_freeze_json": str(local_freeze.resolve()),
        "global_freeze_json": written_global_freeze,
        "recommended_config_id": str(recommended.get("config_id") or ""),
    }
