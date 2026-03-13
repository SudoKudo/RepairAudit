"""Render an offline HTML report from analyzed runs.

The report is optimized for human review with:
- executive summary bullets
- strategy-level LLM judge analytics
- filterable/sortable snippet table
- expandable baseline/edited/gold code views
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from jinja2 import Template


@dataclass(frozen=True)
class RunPaths:
    """Container for resolved filesystem paths for one analyzed run."""
    run_id: str
    run_dir: Path
    analysis_dir: Path
    edits_dir: Path


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


def _read_text(path: Path) -> str:
    """Read UTF-8 text from a file and return empty text on failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _read_json(path: Path) -> Dict[str, Any]:
    """Read a JSON object from disk and return {} on failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_csv(path: Path) -> List[Dict[str, str]]:
    """Load a CSV file into a list of row dictionaries."""
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _sha12(text: str) -> str:
    """Return a short SHA-256 fingerprint for display/debugging."""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _as_int(x: Any, default: int = 0) -> int:
    """Safely coerce values to int with a default fallback."""
    try:
        return int(x)
    except Exception:
        return default


def _as_float(x: Any, default: float = 0.0) -> float:
    """Safely coerce values to float with a default fallback."""
    try:
        return float(x)
    except Exception:
        return default


def _safe_json_obj(raw: Any) -> Dict[str, Any]:
    """Parse JSON input and return a dict or {}."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _normalize_verdict(v: Any) -> str:
    """Normalize verdict strings to present/absent/uncertain."""
    text = str(v or "").strip().lower()
    return text if text in {"present", "absent", "uncertain"} else "uncertain"


def _outcome_from_verdict(v: str) -> str:
    """Map judge verdict labels into study outcome labels."""
    if v == "absent":
        return "Mitigated"
    if v == "present":
        return "Preserved"
    return "UNKNOWN"


def _derive_primary_outcome(row: Dict[str, Any]) -> str:
    """Determine primary outcome, preferring judge verdict when available."""
    verdict = _normalize_verdict(row.get("judge_verdict"))
    if verdict in {"absent", "present", "uncertain"}:
        return _outcome_from_verdict(verdict)

    detector_outcome = (row.get("outcome") or "").strip()
    if detector_outcome in {"Mitigated", "Preserved", "Obfuscated", "Amplified", "Unchanged"}:
        return detector_outcome
    return detector_outcome or "UNKNOWN"


def _extract_per_strategy_results(row: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Extract per-strategy judge outputs from result-row fields."""
    parsed = _safe_json_obj(row.get("judge_strategy_results"))
    if not parsed:
        raw = _safe_json_obj(row.get("judge_raw_json"))
        per = raw.get("per_strategy")
        if isinstance(per, dict):
            parsed = per

    out: Dict[str, Dict[str, Any]] = {}
    for strategy, payload in parsed.items() if isinstance(parsed, dict) else []:
        if not isinstance(payload, dict):
            continue
        verdict = _normalize_verdict(payload.get("verdict"))
        conf = _as_float(payload.get("confidence"), 0.0)
        out[str(strategy)] = {
            "verdict": verdict,
            "confidence": conf,
            "outcome": _outcome_from_verdict(verdict),
        }
    return out


def _snippet_paths(repo_root: Path, snippet_id: str) -> Tuple[Path, Path]:
    """Resolve baseline and gold snippet paths from snippet_id."""
    prefix = snippet_id.split("_", 1)[0]
    baseline = repo_root / "snippets" / "baseline" / prefix / f"{snippet_id}.py"
    gold = repo_root / "snippets" / "gold" / prefix / f"{snippet_id}.py"
    return baseline, gold


def discover_runs(runs_root: Path) -> List[RunPaths]:
    """Discover run folders that have analysis outputs ready for reporting."""
    if not runs_root.exists():
        return []

    runs: List[RunPaths] = []
    for participant_dir in sorted([p for p in runs_root.iterdir() if p.is_dir()]):
        run_dir = _resolve_run_dir(participant_dir)
        analysis_dir = run_dir / "analysis"
        if (analysis_dir / "results.csv").exists() and (analysis_dir / "summary.json").exists():
            runs.append(
                RunPaths(
                    run_id=participant_dir.name,
                    run_dir=run_dir,
                    analysis_dir=analysis_dir,
                    edits_dir=run_dir / "edits",
                )
            )
    return runs


def _collect_strategy_rows(all_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten snippet rows into strategy-centric rows for metrics."""
    out: List[Dict[str, Any]] = []
    for row in all_rows:
        per = row.get("per_strategy_results") or {}
        if isinstance(per, dict) and per:
            for strategy, details in per.items():
                if not isinstance(details, dict):
                    continue
                verdict = _normalize_verdict(details.get("verdict"))
                out.append(
                    {
                        "strategy": str(strategy),
                        "verdict": verdict,
                        "confidence": _as_float(details.get("confidence"), 0.0),
                        "outcome": _outcome_from_verdict(verdict),
                        "run_id": row.get("run_id", ""),
                        "snippet_id": row.get("snippet_id", ""),
                        "vuln_type": row.get("vuln_type", ""),
                    }
                )
        else:
            strategy = (row.get("judge_strategy") or "single").strip() or "single"
            verdict = _normalize_verdict(row.get("judge_verdict"))
            out.append(
                {
                    "strategy": strategy,
                    "verdict": verdict,
                    "confidence": _as_float(row.get("judge_confidence"), 0.0),
                    "outcome": _outcome_from_verdict(verdict),
                    "run_id": row.get("run_id", ""),
                    "snippet_id": row.get("snippet_id", ""),
                    "vuln_type": row.get("vuln_type", ""),
                }
            )
    return out


def _compute_strategy_metrics(strategy_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate per-strategy counts, rates, and mean confidence."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in strategy_rows:
        key = (row.get("strategy") or "unknown").strip() or "unknown"
        b = buckets.setdefault(
            key,
            {
                "strategy": key,
                "decisions": 0,
                "absent": 0,
                "present": 0,
                "uncertain": 0,
                "confidence_sum": 0.0,
            },
        )

        verdict = _normalize_verdict(row.get("verdict"))
        b["decisions"] += 1
        b[verdict] += 1
        b["confidence_sum"] += _as_float(row.get("confidence"), 0.0)

    out: List[Dict[str, Any]] = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        n = max(1, _as_int(b.get("decisions"), 0))
        out.append(
            {
                "strategy": key,
                "decisions": b["decisions"],
                "absent": b["absent"],
                "present": b["present"],
                "uncertain": b["uncertain"],
                "mitigation_rate": b["absent"] / n,
                "persistence_rate": b["present"] / n,
                "abstention_rate": b["uncertain"] / n,
                "avg_confidence": b["confidence_sum"] / n,
            }
        )
    return out



def _run_duration_seconds(run_dir: Path) -> float:
    """Read run duration from start_end_times.json, returning 0 on missing data."""
    p = run_dir / "start_end_times.json"
    if not p.exists():
        return 0.0
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        start = str(obj.get("start", "") or "").strip()
        end = str(obj.get("end", "") or "").strip()
        if not start or not end:
            return 0.0
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except Exception:
        return 0.0


def _is_primary_mitigated_row(row: Dict[str, Any], primary_source: str) -> bool:
    """Check whether one snippet row is mitigated under the run's primary source."""
    if primary_source == "judge":
        verdict = _normalize_verdict(row.get("judge_verdict"))
        return verdict == "absent"
    return str(row.get("outcome", "")).strip() == "Mitigated"


def _time_to_first_secure_fix_seconds(run_dir: Path, rows: List[Dict[str, Any]], primary_source: str) -> float | None:
    """Compute seconds from run start to first mitigated snippet end-time."""
    times_path = run_dir / "start_end_times.json"
    snippet_path = run_dir / "timings" / "snippet_times.json"
    if not times_path.exists() or not snippet_path.exists():
        return None

    try:
        run_obj = json.loads(times_path.read_text(encoding="utf-8"))
        snippet_obj = json.loads(snippet_path.read_text(encoding="utf-8"))
        run_start = datetime.fromisoformat(str(run_obj.get("start", "") or ""))
    except Exception:
        return None

    mitigated_ids = [
        str(r.get("snippet_id", "")).strip()
        for r in rows
        if str(r.get("snippet_id", "")).strip() and _is_primary_mitigated_row(r, primary_source)
    ]
    if not mitigated_ids:
        return None

    end_times: List[datetime] = []
    for sid in mitigated_ids:
        item = snippet_obj.get(sid, {}) if isinstance(snippet_obj, dict) else {}
        if not isinstance(item, dict):
            continue
        end_raw = str(item.get("end", "") or "").strip()
        if not end_raw:
            continue
        try:
            end_times.append(datetime.fromisoformat(end_raw))
        except Exception:
            continue

    if not end_times:
        return None
    return max(0.0, (min(end_times) - run_start).total_seconds())


def _judge_strategy_variance_from_rows(rows: List[Dict[str, Any]]) -> tuple[float, int]:
    """Compute mean normalized entropy of per-strategy verdicts in [0,1]."""
    entropies: List[float] = []
    denom = math.log(3.0)

    for row in rows:
        per = row.get("per_strategy_results") or {}
        if not isinstance(per, dict) or len(per) < 2:
            continue

        counts = {"present": 0, "absent": 0, "uncertain": 0}
        for details in per.values():
            if not isinstance(details, dict):
                continue
            verdict = _normalize_verdict(details.get("verdict"))
            if verdict in counts:
                counts[verdict] += 1

        total = sum(counts.values())
        if total < 2:
            continue

        entropy = 0.0
        for c in counts.values():
            if c <= 0:
                continue
            p = c / total
            entropy += -p * math.log(p)

        entropies.append(entropy / denom if denom > 0 else 0.0)

    if not entropies:
        return 0.0, 0
    return float(sum(entropies) / len(entropies)), len(entropies)

def _compute_global_metrics(run_models: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute whole-study summary metrics across all runs."""
    mitigated = preserved = unknown = total_scored = 0
    turns_sum = applied_sum = conf_sum = 0.0
    interaction_runs = 0

    mpm_sum = 0.0
    mpm_n = 0
    first_fix_sum = 0.0
    first_fix_n = 0
    strat_var_sum = 0.0
    strat_var_n = 0

    for rm in run_models:
        summary = rm.get("summary") or {}
        pc = summary.get("primary_counts") or {}
        total_scored += _as_int(summary.get("primary_scored_snippets"), 0)
        mitigated += _as_int(pc.get("Mitigated"), 0)
        preserved += _as_int(pc.get("Preserved"), 0)
        unknown += _as_int(pc.get("UNKNOWN"), 0)

        inter = summary.get("interaction") or {}
        if inter:
            interaction_runs += 1
            turns_sum += _as_float(inter.get("avg_turns"), 0.0)
            applied_sum += _as_float(inter.get("avg_applied_ratio"), 0.0)
            conf_sum += _as_float(inter.get("avg_confidence_1to5"), 0.0)

        run_metrics = rm.get("run_metrics") or {}
        mpm = _as_float(run_metrics.get("mitigations_per_minute"), 0.0)
        mpm_sum += mpm
        mpm_n += 1

        ff = run_metrics.get("time_to_first_secure_fix_seconds")
        if ff is not None:
            first_fix_sum += _as_float(ff, 0.0)
            first_fix_n += 1

        sv = _as_float(run_metrics.get("judge_strategy_variance"), 0.0)
        if _as_int(run_metrics.get("judge_strategy_variance_snippets"), 0) > 0:
            strat_var_sum += sv
            strat_var_n += 1

    denom = float(total_scored) if total_scored else 1.0
    return {
        "runs": len(run_models),
        "primary_scored_snippets": total_scored,
        "primary_mitigated": mitigated,
        "primary_preserved": preserved,
        "primary_unknown": unknown,
        "primary_mitigation_rate": mitigated / denom,
        "primary_persistence_rate": preserved / denom,
        "primary_abstention_rate": unknown / denom,
        "interaction_runs": interaction_runs,
        "missing_interaction_runs": max(0, len(run_models) - interaction_runs),
        "interaction_avg_turns": (turns_sum / interaction_runs) if interaction_runs else 0.0,
        "interaction_avg_applied_ratio": (applied_sum / interaction_runs) if interaction_runs else 0.0,
        "interaction_avg_confidence_1to5": (conf_sum / interaction_runs) if interaction_runs else 0.0,
        "avg_mitigations_per_minute": (mpm_sum / mpm_n) if mpm_n else 0.0,
        "avg_time_to_first_secure_fix_seconds": (first_fix_sum / first_fix_n) if first_fix_n else 0.0,
        "avg_judge_strategy_variance": (strat_var_sum / strat_var_n) if strat_var_n else 0.0,
        "runs_with_first_fix_timing": first_fix_n,
        "runs_with_strategy_variance": strat_var_n,
    }


def _uniq(values: List[str]) -> List[str]:
    """Return sorted unique non-empty strings."""
    return sorted({(v or "").strip() for v in values if (v or "").strip()})


def _compute_filter_values(all_rows: List[Dict[str, Any]], strategy_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build filter option lists used by report dropdown controls."""
    return {
        "runs": _uniq([r.get("run_id", "") for r in all_rows]),
        "conditions": _uniq([r.get("condition", "") for r in all_rows]),
        "vuln_types": _uniq([r.get("vuln_type", "") for r in all_rows]),
        "primary_outcomes": _uniq([r.get("primary_outcome", "") for r in all_rows]),
        "judge_verdicts": _uniq([r.get("judge_verdict", "") for r in all_rows]),
        "judge_strategies": _uniq([r.get("strategy", "") for r in strategy_rows]),
    }


def _build_insights(global_metrics: Dict[str, Any], strategy_metrics: List[Dict[str, Any]]) -> List[str]:
    """Build short executive-summary insight bullet points."""
    insights: List[str] = []
    insights.append(
        f"Analyzed {global_metrics.get('runs', 0)} run(s) and {global_metrics.get('primary_scored_snippets', 0)} scored snippets."
    )
    insights.append(
        f"Missing interaction logs for {global_metrics.get('missing_interaction_runs', 0)} run(s)."
    )

    if strategy_metrics:
        best = max(strategy_metrics, key=lambda s: (_as_float(s.get("mitigation_rate"), 0.0), _as_int(s.get("decisions"), 0)))
        worst = max(strategy_metrics, key=lambda s: (_as_float(s.get("persistence_rate"), 0.0), _as_int(s.get("decisions"), 0)))
        insights.append(
            f"Best mitigation prompt strategy: {best.get('strategy')} ({_as_float(best.get('mitigation_rate')):.2f} mitigation rate)."
        )
        insights.append(
            f"Highest persistence prompt strategy: {worst.get('strategy')} ({_as_float(worst.get('persistence_rate')):.2f} persistence rate)."
        )

    return insights


def build_aggregated_report_offline(
    *,
    repo_root: Path,
    runs_root: Path,
    out_html: Path,
    title: str = "RepairAudit Report",
) -> None:
    """Assemble data and render the standalone HTML report."""
    out_html.parent.mkdir(parents=True, exist_ok=True)

    runs = discover_runs(runs_root)

    run_models: List[Dict[str, Any]] = []
    all_rows: List[Dict[str, Any]] = []

    for rp in runs:
        summary = _read_json(rp.analysis_dir / "summary.json")
        condition = _read_text(rp.run_dir / "condition.txt").strip() or "unknown"
        rows = _read_csv(rp.analysis_dir / "results.csv")

        enriched_rows: List[Dict[str, Any]] = []
        for row in rows:
            snippet_id = (row.get("snippet_id") or "").strip()
            if not snippet_id:
                continue

            baseline_path, gold_path = _snippet_paths(repo_root, snippet_id)
            edited_path = rp.edits_dir / f"{snippet_id}.py"

            baseline_code = _read_text(baseline_path)
            edited_code = _read_text(edited_path)
            gold_code = _read_text(gold_path)

            per_strategy = _extract_per_strategy_results(row)
            judge_strategy = (row.get("judge_strategy") or "").strip()
            judge_strategy_display = judge_strategy
            if judge_strategy == "ensemble" and per_strategy:
                judge_strategy_display = "ensemble (" + ", ".join(sorted(per_strategy.keys())) + ")"

            # Start from CSV row values, then attach richer typed fields used by the report.
            enriched: Dict[str, Any] = dict(row)
            enriched.update(
                {
                    "run_id": rp.run_id,
                    "condition": condition,
                    "primary_outcome": _derive_primary_outcome(row),
                    "judge_strategy_display": judge_strategy_display,
                    "per_strategy_results": per_strategy,
                    "baseline_path": str(baseline_path),
                    "edited_path": str(edited_path),
                    "gold_path": str(gold_path),
                    "baseline_sha256": _sha12(baseline_code),
                    "edited_sha256": _sha12(edited_code),
                    "gold_sha256": _sha12(gold_code),
                    "baseline_code": baseline_code,
                    "edited_code": edited_code,
                    "gold_code": gold_code,
                }
            )

            enriched_rows.append(enriched)
            all_rows.append(enriched)

        primary_source = (summary.get("primary_source") or "judge").strip() or "judge"
        duration_seconds = _run_duration_seconds(rp.run_dir)
        primary_counts = summary.get("primary_counts") or {}
        primary_mitigated = _as_int(primary_counts.get("Mitigated"), 0)
        mitigations_per_minute = (primary_mitigated / (duration_seconds / 60.0)) if duration_seconds > 0 else 0.0
        first_fix_seconds = _time_to_first_secure_fix_seconds(rp.run_dir, enriched_rows, primary_source)
        strat_var, strat_var_n = _judge_strategy_variance_from_rows(enriched_rows)

        run_models.append(
            {
                "run_id": rp.run_id,
                "condition": condition,
                "summary": summary,
                "rows": enriched_rows,
                "run_metrics": {
                    "duration_seconds": duration_seconds,
                    "mitigations_per_minute": mitigations_per_minute,
                    "time_to_first_secure_fix_seconds": first_fix_seconds,
                    "judge_strategy_variance": strat_var,
                    "judge_strategy_variance_snippets": strat_var_n,
                },
            }
        )

    strategy_rows = _collect_strategy_rows(all_rows)
    strategy_metrics = _compute_strategy_metrics(strategy_rows)
    global_metrics = _compute_global_metrics(run_models)
    filters = _compute_filter_values(all_rows, strategy_rows)
    insights = _build_insights(global_metrics, strategy_metrics)

    html = Template(_TEMPLATE).render(
        title=title,
        generated_at=datetime.now().astimezone().strftime("%B %d, %Y %I:%M %p %Z"),
        runs=run_models,
        all_rows=all_rows,
        global_metrics=global_metrics,
        strategy_metrics=strategy_metrics,
        filters=filters,
        insights=insights,
    )

    out_html.write_text(html, encoding="utf-8")


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{ title }}</title>
  <style>
    :root {
      --bg: #f4f8ff;
      --panel: #ffffff;
      --text: #0f172a;
      --muted: #4b5563;
      --border: #dbe7ff;
      --accent: #2563eb;
      --good: #059669;
      --bad: #dc2626;
      --warn: #b45309;
      --info: #0369a1;
      --panel-2: #f8fbff;
      --table-head: #f4f8ff;
      --table-hover: #f9fbff;
      --line: #e9f0ff;
      --btn-bg: #ffffff;
      --shadow: rgba(15,23,42,0.06);
    }
    [data-theme="dark"] {
      --bg: #0b1220;
      --panel: #111b2e;
      --text: #e6edf8;
      --muted: #99a9c1;
      --border: #223552;
      --accent: #7db0ff;
      --good: #34d399;
      --bad: #f87171;
      --warn: #f59e0b;
      --info: #38bdf8;
      --panel-2: #0f1729;
      --table-head: #16243d;
      --table-hover: #14233a;
      --line: #20314d;
      --btn-bg: #0f1a2f;
      --shadow: rgba(0,0,0,0.35);
    }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: "Segoe UI", system-ui, sans-serif; }
    .wrap { max-width: 1280px; margin: 0 auto; padding: 22px 16px 60px; }
    .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 16px; padding: 14px; box-shadow: 0 10px 24px var(--shadow); }
    .top { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
    h1 { margin: 0; font-size: 24px; }
    .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; }
    .chip { border: 1px solid var(--border); border-radius: 999px; padding: 6px 10px; font-size: 12px; color: var(--muted); background: var(--panel-2); }
    .kpis { margin-top: 12px; display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 10px; }
    .kpi { background: var(--panel-2); border: 1px solid var(--border); border-radius: 14px; padding: 12px; }
    .kpi h3 { margin: 0 0 6px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .4px; }
    .kpi .v { font-size: 24px; font-weight: 800; }
    .good{color:var(--good)} .bad{color:var(--bad)} .warn{color:var(--warn)} .info{color:var(--info)}
    .section-title { margin: 24px 0 10px; font-size: 16px; font-weight: 800; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 9px 8px; text-align: left; font-size: 12px; vertical-align: top; }
    th { cursor: pointer; background: var(--table-head); position: sticky; top: 0; }
    tbody tr:hover td { background: var(--table-hover); }
    .badge { display:inline-block; border-radius: 999px; padding: 2px 10px; font-weight: 700; font-size: 11px; }
    .b-good{background:#d1fae5;color:#065f46} .b-bad{background:#fee2e2;color:#7f1d1d} .b-warn{background:#fef3c7;color:#78350f} .b-info{background:#dbeafe;color:#1e3a8a}
    .pill { display:inline-block; border:1px solid var(--border); border-radius:999px; padding:3px 10px; background:var(--panel-2); color:var(--text); }
    [data-theme="dark"] .pill { background:#1a2942; color:#e6edf8; border-color:#34507a; }
    [data-theme="dark"] .b-good{background:#10392f;color:#86efcf}
    [data-theme="dark"] .b-bad{background:#4a1f24;color:#fecaca}
    [data-theme="dark"] .b-warn{background:#4b3313;color:#fcd34d}
    [data-theme="dark"] .b-info{background:#162f4f;color:#93c5fd}
    .toolbar { display:flex; justify-content:space-between; gap:10px; align-items:center; margin-bottom:10px; }
    .hint { color: var(--muted); font-size: 12px; }
    .btn { border:1px solid var(--border); background:var(--btn-bg); color:var(--text); border-radius:10px; padding:8px 12px; font-weight:700; cursor:pointer; }
    .filters { display:grid; grid-template-columns: 1.6fr repeat(6, minmax(120px, 1fr)); gap:8px; margin-bottom: 10px; }
    .behavior-col { display: none; }
    .field label { display:block; margin-bottom:4px; font-size:11px; color:var(--muted); }
    .field input,.field select { width:100%; box-sizing:border-box; padding:8px 9px; border:1px solid var(--border); border-radius:10px; }
    .expand-row { display:none; }
    pre { margin:8px 0 0; padding:10px; border:1px solid var(--border); border-radius:10px; background:#f8fbff; font-size:12px; max-height:280px; overflow:auto; }
    .triple { display:grid; grid-template-columns: repeat(3, minmax(220px, 1fr)); gap:8px; }
    .triple h4 { margin:0; font-size:12px; color:var(--muted); }
    .footer { margin-top: 16px; color:var(--muted); font-size:12px; }
    @media (max-width: 1100px) { .kpis{grid-template-columns:repeat(2,minmax(180px,1fr));} .filters{grid-template-columns:1fr 1fr 1fr;} .triple{grid-template-columns:1fr;} }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel top">
      <div>
        <h1>{{ title }}</h1>
        <div class="sub">Generated: {{ generated_at }}</div>
      </div>
      <div class="chips">
        <span class="chip">runs: {{ global_metrics.runs }}</span>
        <span class="chip">snippets scored: {{ global_metrics.primary_scored_snippets }}</span>
        <span class="chip">missing interaction runs: {{ global_metrics.missing_interaction_runs }}</span>
        <button class="btn" id="btnThemeTop" type="button">Toggle dark mode</button>
      </div>
    </div>

    <div class="kpis">
      <div class="kpi"><h3>Mitigation</h3><div class="v good">{{ '%.2f'|format(global_metrics.primary_mitigation_rate) }}</div></div>
      <div class="kpi"><h3>Persistence</h3><div class="v bad">{{ '%.2f'|format(global_metrics.primary_persistence_rate) }}</div></div>
      <div class="kpi"><h3>Abstention</h3><div class="v warn">{{ '%.2f'|format(global_metrics.primary_abstention_rate) }}</div></div>
      <div class="kpi"><h3>Interaction Avg Turns</h3><div class="v info">{{ '%.2f'|format(global_metrics.interaction_avg_turns) }}</div></div>
      <div class="kpi"><h3>Mitigations / Min</h3><div class="v good">{{ '%.3f'|format(global_metrics.avg_mitigations_per_minute) }}</div></div>
      <div class="kpi"><h3>Time To 1st Secure Fix (s)</h3><div class="v info">{% if global_metrics.runs_with_first_fix_timing > 0 %}{{ '%.1f'|format(global_metrics.avg_time_to_first_secure_fix_seconds) }}{% else %}N/A{% endif %}</div></div>
      <div class="kpi"><h3>Judge Strategy Variance</h3><div class="v warn">{% if global_metrics.runs_with_strategy_variance > 0 %}{{ '%.3f'|format(global_metrics.avg_judge_strategy_variance) }}{% else %}N/A{% endif %}</div></div>
    </div>

    <div class="section-title">Executive Summary</div>
    <div class="panel">
      <ul style="margin:0 0 0 18px; line-height:1.6;">
        {% for msg in insights %}<li>{{ msg }}</li>{% endfor %}
      </ul>
    </div>

    <div class="section-title">Prompt Strategy Analytics</div>
    <div class="panel">
      <div class="hint" style="margin-bottom:8px;">Sortable metrics from per-strategy LLM judge decisions.</div>
      <table id="strategyTable">
        <thead><tr>
          <th data-type="text">Prompt strategy</th>
          <th data-type="number">Decisions</th>
          <th data-type="number">Mitigated</th>
          <th data-type="number">Preserved</th>
          <th data-type="number">Uncertain</th>
          <th data-type="number">Mitigation rate</th>
          <th data-type="number">Persistence rate</th>
          <th data-type="number">Abstention rate</th>
          <th data-type="number">Avg confidence</th>
        </tr></thead>
        <tbody>
          {% for s in strategy_metrics %}
          <tr>
            <td><span class="pill">{{ s.strategy }}</span></td>
            <td>{{ s.decisions }}</td><td>{{ s.absent }}</td><td>{{ s.present }}</td><td>{{ s.uncertain }}</td>
            <td>{{ '%.2f'|format(s.mitigation_rate) }}</td>
            <td>{{ '%.2f'|format(s.persistence_rate) }}</td>
            <td>{{ '%.2f'|format(s.abstention_rate) }}</td>
            <td>{{ '%.2f'|format(s.avg_confidence) }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <div class="section-title">All Snippets</div>
    <div class="panel">
      <div class="toolbar">
        <div class="hint">Filter by participant, condition, vulnerability, outcome, verdict, and judge prompt strategy.</div>
        <div style="display:inline-flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <label style="font-size:12px; color:var(--muted); display:inline-flex; gap:6px; align-items:center;"><input id="toggleBehavior" type="checkbox" /> Show behavioral columns</label>
                    <button class="btn" id="btnReset" type="button">Reset filters</button>
        </div>
      </div>

      <div class="filters">
        <div class="field"><label for="q">Search</label><input id="q" placeholder="Search snippet id, notes, paths, evidence" /></div>
        <div class="field"><label for="fRun">Run</label><select id="fRun"><option value="">All</option>{% for v in filters.runs %}<option value="{{ v }}">{{ v }}</option>{% endfor %}</select></div>
        <div class="field"><label for="fCondition">Condition</label><select id="fCondition"><option value="">All</option>{% for v in filters.conditions %}<option value="{{ v }}">{{ v }}</option>{% endfor %}</select></div>
        <div class="field"><label for="fVuln">Vuln type</label><select id="fVuln"><option value="">All</option>{% for v in filters.vuln_types %}<option value="{{ v }}">{{ v }}</option>{% endfor %}</select></div>
        <div class="field"><label for="fOutcome">Primary outcome</label><select id="fOutcome"><option value="">All</option>{% for v in filters.primary_outcomes %}<option value="{{ v }}">{{ v }}</option>{% endfor %}</select></div>
        <div class="field"><label for="fVerdict">Judge verdict</label><select id="fVerdict"><option value="">All</option>{% for v in filters.judge_verdicts %}<option value="{{ v }}">{{ v }}</option>{% endfor %}</select></div>
        <div class="field"><label for="fStrategy">Judge strategy</label><select id="fStrategy"><option value="">All</option>{% for v in filters.judge_strategies %}<option value="{{ v }}">{{ v }}</option>{% endfor %}</select></div>
      </div>

      <table id="allTable">
        <thead><tr>
          <th data-type="text">Run</th>
          <th data-type="text">Condition</th>
          <th data-type="text">Snippet</th>
          <th data-type="text">Vuln</th>
          <th data-type="text">Primary</th>
          <th data-type="text">Judge</th>
          <th data-type="number">Judge conf</th>
          <th data-type="text">Judge strategy</th>
          <th data-type="number" class="behavior-col">Turns</th>
          <th data-type="number" class="behavior-col">Applied ratio</th>
          <th data-type="text">Code</th>
        </tr></thead>
        <tbody>
          {% for r in all_rows %}
          <tr data-run="{{ r.run_id }}" data-condition="{{ r.condition }}" data-vuln="{{ r.vuln_type }}" data-outcome="{{ r.primary_outcome }}" data-verdict="{{ r.judge_verdict }}" data-strategy="{{ r.judge_strategy }}">
            <td>{{ r.run_id }}</td>
            <td>{{ r.condition }}</td>
            <td><span class="pill">{{ r.snippet_id }}</span></td>
            <td>{{ r.vuln_type }}</td>
            <td>{% if r.primary_outcome == 'Mitigated' %}<span class="badge b-good">Mitigated</span>{% elif r.primary_outcome == 'Preserved' %}<span class="badge b-bad">Preserved</span>{% elif r.primary_outcome == 'UNKNOWN' %}<span class="badge b-warn">UNKNOWN</span>{% else %}<span class="badge b-info">{{ r.primary_outcome }}</span>{% endif %}</td>
            <td>{% if r.judge_verdict == 'absent' %}<span class="badge b-good">absent</span>{% elif r.judge_verdict == 'present' %}<span class="badge b-bad">present</span>{% else %}<span class="badge b-warn">{{ r.judge_verdict }}</span>{% endif %}</td>
            <td>{{ r.judge_confidence }}</td>
            <td>{{ r.judge_strategy_display }}</td>
            <td class="behavior-col">{{ r.llm_turns }}</td>
            <td class="behavior-col">{{ r.llm_applied_ratio }}</td>
            <td><button class="btn" type="button" data-expand="1">Triple view</button></td>
          </tr>
          <tr class="expand-row"><td colspan="11">
            <div class="triple">
              <div><h4>Baseline</h4><pre>{{ r.baseline_code }}</pre></div>
              <div><h4>Edited</h4><pre>{{ r.edited_code }}</pre></div>
              <div><h4>Gold</h4><pre>{{ r.gold_code }}</pre></div>
            </div>
          </td></tr>
          {% endfor %}
        </tbody>
      </table>
      <div class="footer" id="rowCount"></div>
    </div>

    <div class="section-title">Participant Overview</div>
    <div class="panel">
      <table id="runTable">
        <thead><tr>
          <th data-type="text">Run</th>
          <th data-type="text">Condition</th>
          <th data-type="number">Mitigation</th>
          <th data-type="number">Persistence</th>
          <th data-type="number">Abstention</th>
          <th data-type="number">Disagreement</th>
          <th data-type="number">Avg turns</th>
        </tr></thead>
        <tbody>
          {% for run in runs %}
          <tr>
            <td><span class="pill">{{ run.run_id }}</span></td>
            <td>{{ run.condition }}</td>
            <td>{{ '%.2f'|format(run.summary.primary_rates.mitigation if run.summary.primary_rates else 0.0) }}</td>
            <td>{{ '%.2f'|format(run.summary.primary_rates.persistence if run.summary.primary_rates else 0.0) }}</td>
            <td>{{ '%.2f'|format(run.summary.primary_rates.abstention if run.summary.primary_rates else 0.0) }}</td>
            <td>{{ '%.2f'|format(run.summary.judge_detector_disagreement_rate if run.summary.judge_detector_disagreement_rate else 0.0) }}</td>
            <td>{{ '%.2f'|format(run.summary.interaction.avg_turns if run.summary.interaction else 0.0) }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <div class="footer">Offline report generated from analyzed study runs.</div>
  </div>

  <script>
    function textCell(td){ return td && td.textContent ? td.textContent.trim() : ""; }
    function cmp(a,b,type,asc){
      if(type==="number"){
        const na = parseFloat(a), nb = parseFloat(b);
        const va = Number.isFinite(na) ? na : -Infinity;
        const vb = Number.isFinite(nb) ? nb : -Infinity;
        return asc ? va-vb : vb-va;
      }
      const sa=(a||"").toLowerCase(), sb=(b||"").toLowerCase();
      if(sa<sb) return asc?-1:1;
      if(sa>sb) return asc?1:-1;
      return 0;
    }
    function sortable(table){
      if(!table) return;
      const heads = Array.from(table.querySelectorAll("thead th"));
      heads.forEach((th,idx)=>{
        th.addEventListener("click", ()=>{
          const tb = table.querySelector("tbody");
          if(!tb) return;
          const type = th.getAttribute("data-type") || "text";
          const asc = th.getAttribute("data-sort") !== "asc";
          heads.forEach(h=>h.removeAttribute("data-sort"));
          th.setAttribute("data-sort", asc ? "asc" : "desc");
          const rows = Array.from(tb.querySelectorAll("tr")).filter(r=>!r.classList.contains("expand-row"));
          rows.sort((r1,r2)=>cmp(textCell(r1.children[idx]), textCell(r2.children[idx]), type, asc));
          rows.forEach(r=>{
            tb.appendChild(r);
            const next = r.nextElementSibling;
            if(next && next.classList.contains("expand-row")) tb.appendChild(next);
          });
          tb.querySelectorAll(".expand-row").forEach(r=>r.style.display="none");
        });
      });
    }
    function wireExpanders(table){
      if(!table) return;
      table.querySelectorAll('button[data-expand="1"]').forEach(btn=>{
        btn.addEventListener("click", ()=>{
          const tr = btn.closest("tr");
          if(!tr) return;
          const next = tr.nextElementSibling;
          if(!next || !next.classList.contains("expand-row")) return;
          table.querySelectorAll(".expand-row").forEach(r=>r.style.display="none");
          next.style.display = "table-row";
        });
      });
    }
    function applyFilters(){
      const q = document.getElementById("q").value.trim().toLowerCase();
      const fRun = document.getElementById("fRun").value;
      const fCondition = document.getElementById("fCondition").value;
      const fVuln = document.getElementById("fVuln").value;
      const fOutcome = document.getElementById("fOutcome").value;
      const fVerdict = document.getElementById("fVerdict").value;
      const fStrategy = document.getElementById("fStrategy").value;
      const tbody = document.querySelector("#allTable tbody");
      if(!tbody) return;
      const rows = Array.from(tbody.querySelectorAll("tr"));
      let visible = 0;
      for(let i=0;i<rows.length;i++){
        const tr = rows[i];
        if(tr.classList.contains("expand-row")){ tr.style.display="none"; continue; }
        const matches =
          (!fRun || (tr.getAttribute("data-run")||"")===fRun) &&
          (!fCondition || (tr.getAttribute("data-condition")||"")===fCondition) &&
          (!fVuln || (tr.getAttribute("data-vuln")||"")===fVuln) &&
          (!fOutcome || (tr.getAttribute("data-outcome")||"")===fOutcome) &&
          (!fVerdict || (tr.getAttribute("data-verdict")||"")===fVerdict) &&
          (!fStrategy || (tr.getAttribute("data-strategy")||"")===fStrategy) &&
          (!q || tr.textContent.toLowerCase().includes(q));
        tr.style.display = matches ? "" : "none";
        const next = tr.nextElementSibling;
        if(next && next.classList.contains("expand-row")) next.style.display="none";
        if(matches) visible += 1;
      }
      const rc = document.getElementById("rowCount");
      if(rc) rc.textContent = "Showing " + visible + " rows.";
    }
    function setBehaviorColumnsVisible(show){
      document.querySelectorAll("#allTable .behavior-col").forEach(el=>{
        el.style.display = show ? "table-cell" : "none";
      });
    }

    function resetFilters(){
      ["q","fRun","fCondition","fVuln","fOutcome","fVerdict","fStrategy"].forEach(id=>{ const el=document.getElementById(id); if(el) el.value=""; });
      applyFilters();
    }
    function setTheme(theme){
      const root = document.documentElement;
      if(!root) return;
      const normalized = theme === "dark" ? "dark" : "light";
      root.setAttribute("data-theme", normalized);
      try { window.localStorage.setItem("study_report_theme", normalized); } catch(e) {}
      const label = normalized === "dark" ? "Switch to light mode" : "Switch to dark mode";
      const btnThemeTop = document.getElementById("btnThemeTop");
      if(btnThemeTop) btnThemeTop.textContent = label;
    }
    function toggleTheme(){
      const root = document.documentElement;
      const isDark = root && root.getAttribute("data-theme") === "dark";
      setTheme(isDark ? "light" : "dark");
    }
    document.addEventListener("DOMContentLoaded", ()=>{
      const allTable = document.getElementById("allTable");
      sortable(allTable);
      wireExpanders(allTable);
      sortable(document.getElementById("strategyTable"));
      sortable(document.getElementById("runTable"));
      ["q","fRun","fCondition","fVuln","fOutcome","fVerdict","fStrategy"].forEach(id=>{
        const el = document.getElementById(id);
        if(!el) return;
        el.addEventListener("input", applyFilters);
        el.addEventListener("change", applyFilters);
      });
      const btn = document.getElementById("btnReset");
      if(btn) btn.addEventListener("click", resetFilters);

      let initialTheme = "light";
      try {
        const saved = window.localStorage.getItem("study_report_theme");
        if(saved === "dark" || saved === "light") initialTheme = saved;
      } catch(e) {}
      setTheme(initialTheme);
      const btnThemeTop = document.getElementById("btnThemeTop");
      if(btnThemeTop) btnThemeTop.addEventListener("click", toggleTheme);

      const toggleBehavior = document.getElementById("toggleBehavior");
      if(toggleBehavior){
        toggleBehavior.checked = false;
        setBehaviorColumnsVisible(false);
        toggleBehavior.addEventListener("change", ()=>setBehaviorColumnsVisible(toggleBehavior.checked));
      }
      applyFilters();
    });
  </script>
</body>
</html>
"""






















