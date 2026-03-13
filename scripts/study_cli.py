"""Unified CLI for the RepairAudit workflow.

This module consolidates day-to-day command entrypoints so the team has one
stable command surface instead of many tiny wrappers.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.analysis.analyze_edits import analyze_participant
from tools.analysis.interaction import (
    interaction_features,
    load_snippet_log_csv,
    merge_interaction_into_results,
)
from tools.analysis.metrics import write_summary_files
from tools.analysis.stats import write_pilot_stats_text
from tools.instrumentation.capture_env import capture_env
from tools.instrumentation.diff_runner import make_diff
from tools.instrumentation.snippet_timer import mark as mark_snippet_time
from tools.instrumentation.start_timer import write_time
from tools.reporting.html_report import build_aggregated_report_offline
from tools.validators.bandit_runner import run_bandit
from scripts.participant_kit import build_participant_kit, clean_participant_kits
from scripts.privacy_check import run_prepublish_check

DEFAULT_METADATA_PATH = Path("data") / "metadata" / "snippet_metadata.csv"
REPO_ROOT = Path(__file__).resolve().parents[1]

# Synthetic run generation constants (used by make-test-runs).
_STRATEGY_PRIMARY = [
    "zero_shot",
    "few_shot",
    "chain_of_thought",
    "adaptive_chain_of_thought",
    "other",
]
_PARTICIPANT_PROFILE_FIELDS = [
    "programming_experience",
    "python_experience",
    "llm_coding_experience",
    "security_experience",
]
_TOOLS = ["None", "ChatGPT", "Copilot", "Claude", "Gemini"]
_MODELS = ["", "synthetic", "gpt-synth-1", "llm-synth-2", "llm-synth-3"]


def _load_metadata_rows(metadata_csv: Path) -> list[dict[str, str]]:
    """Read metadata rows and keep only records with snippet_id + baseline path."""
    with metadata_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows: list[dict[str, str]] = []
        for row in reader:
            sid = (row.get("snippet_id") or "").strip()
            base = (row.get("baseline_relpath") or "").strip()
            if sid and base:
                rows.append({"snippet_id": sid, "baseline_relpath": base})
    return rows


def _load_snippets(metadata_csv: Path) -> list[dict[str, str]]:
    """Validate and load run initialization snippet metadata."""
    if not metadata_csv.exists():
        raise FileNotFoundError(f"Missing metadata CSV: {metadata_csv}")

    with metadata_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"Metadata CSV has no header row: {metadata_csv}")

        required = {"snippet_id", "baseline_relpath"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"Metadata CSV missing required columns: {sorted(missing)}")

        rows: list[dict[str, str]] = []
        for row in reader:
            sid = (row.get("snippet_id") or "").strip()
            base = (row.get("baseline_relpath") or "").strip()
            if sid and base:
                rows.append({"snippet_id": sid, "baseline_relpath": base})

    if not rows:
        raise ValueError(f"No usable rows found in metadata CSV: {metadata_csv}")
    return rows


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


def _copy_baselines_to_edits(run_dir: Path, snippets: list[dict[str, str]]) -> None:
    """Copy baseline snippet files into the participant edits folder."""
    edits_dir = run_dir / "edits"
    edits_dir.mkdir(parents=True, exist_ok=True)

    for item in snippets:
        sid = item["snippet_id"]
        src = Path(item["baseline_relpath"])
        if not src.exists():
            raise FileNotFoundError(f"Missing baseline snippet: {src}")
        dst = edits_dir / f"{sid}.py"
        if not dst.exists():
            shutil.copyfile(src, dst)


def _write_condition(run_dir: Path, condition: str) -> None:
    """Persist participant condition assignment for downstream analysis."""
    (run_dir / "condition.txt").write_text(condition.strip() + "\n", encoding="utf-8")


def _write_survey_template(run_dir: Path, participant_id: str, condition: str) -> None:
    """Create a starter survey JSON so data collection has a consistent schema."""
    survey_dir = run_dir / "survey"
    survey_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "participant_id": participant_id,
        "condition": condition,
        "experience_years": None,
        "confidence_overall_1to5": None,
        "mode_self_report": None,
        "suspected_vulnerabilities": None,
        "notes": "",
    }
    (survey_dir / "survey.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _ensure_run_dirs(run_dir: Path) -> None:
    """Create standard run subfolders used throughout the pipeline."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "analysis").mkdir(exist_ok=True)
    (run_dir / "diffs").mkdir(exist_ok=True)
    (run_dir / "survey").mkdir(exist_ok=True)


def cmd_start_run(args: argparse.Namespace) -> None:
    """Initialize one participant run from metadata and baseline snippets."""
    run_dir = Path("runs") / args.phase / args.participant_id
    _ensure_run_dirs(run_dir)

    snippets = _load_snippets(Path(args.metadata_csv))
    _write_condition(run_dir, args.condition)
    capture_env(str(run_dir / "environment.json"))

    times_path = run_dir / "start_end_times.json"
    if not times_path.exists():
        write_time(str(times_path), "start")

    _copy_baselines_to_edits(run_dir, snippets)
    _write_survey_template(run_dir, args.participant_id, args.condition)

    print("Run initialized.")
    print(f"Run dir:    {run_dir}")
    print(f"Condition:  {args.condition}")
    print(f"Edits dir:  {run_dir / 'edits'}")
    print(f"Timer file: {times_path}")
    print(f"Snippets:   {len(snippets)} from {args.metadata_csv}")


def cmd_analyze_run(args: argparse.Namespace) -> None:
    """Analyze one participant run and emit all per-run analysis outputs."""
    requested_run_dir = Path("runs") / args.phase / args.participant_id
    if not requested_run_dir.exists():
        raise FileNotFoundError(f"Run dir not found: {requested_run_dir}")

    run_dir = _resolve_run_dir(requested_run_dir)
    if run_dir != requested_run_dir:
        print(f"Resolved nested run dir: {run_dir}")

    edits_dir = run_dir / "edits"
    if not edits_dir.exists():
        raise FileNotFoundError(f"Missing edits folder: {edits_dir}")

    (run_dir / "analysis").mkdir(exist_ok=True)
    (run_dir / "diffs").mkdir(exist_ok=True)

    metadata_csv = Path(args.metadata_csv)
    if not metadata_csv.exists():
        raise FileNotFoundError(f"Missing metadata CSV: {metadata_csv}")

    analyze_participant(str(run_dir), str(metadata_csv))

    rows = _load_metadata_rows(metadata_csv)
    diff_stats: dict[str, dict[str, int]] = {}
    for row in rows:
        sid = row["snippet_id"]
        baseline = Path(row["baseline_relpath"])
        edited = edits_dir / f"{sid}.py"
        outdiff = run_dir / "diffs" / f"{sid}.diff"
        if baseline.exists() and edited.exists():
            diff_stats[sid] = make_diff(str(baseline), str(edited), str(outdiff))

    bandit_out = run_dir / "analysis" / "bandit.json"
    bandit_result = run_bandit(str(edits_dir), str(bandit_out))
    if isinstance(bandit_result, dict) and "error" in bandit_result:
        print(f"Warning: Bandit issue: {bandit_result['error']}")

    results_csv = run_dir / "analysis" / "results.csv"
    if results_csv.exists() and diff_stats:
        tmp_csv = run_dir / "analysis" / "results.tmp.csv"
        with results_csv.open("r", newline="", encoding="utf-8") as fin, tmp_csv.open(
            "w", newline="", encoding="utf-8"
        ) as fout:
            reader = csv.DictReader(fin)
            fieldnames = list(reader.fieldnames) if reader.fieldnames else []
            for col in ["lines_added", "lines_removed", "lines_changed", "hunks", "diff_bytes"]:
                if col not in fieldnames:
                    fieldnames.append(col)

            writer = csv.DictWriter(fout, fieldnames=fieldnames)
            writer.writeheader()
            for row in reader:
                sid = row.get("snippet_id", "")
                stats = diff_stats.get(
                    sid,
                    {"lines_added": 0, "lines_removed": 0, "lines_changed": 0, "hunks": 0, "diff_bytes": 0},
                )
                row["lines_added"] = stats.get("lines_added", 0)
                row["lines_removed"] = stats.get("lines_removed", 0)
                row["lines_changed"] = stats.get("lines_changed", 0)
                row["hunks"] = stats.get("hunks", 0)
                row["diff_bytes"] = stats.get("diff_bytes", 0)
                writer.writerow(row)

        tmp_csv.replace(results_csv)

    write_summary_files(
        results_csv=str(run_dir / "analysis" / "results.csv"),
        out_json=str(run_dir / "analysis" / "summary.json"),
        out_txt=str(run_dir / "analysis" / "summary.txt"),
    )

    print("Analysis complete.")
    print(f"Results: {run_dir / 'analysis' / 'results.csv'}")
    print(f"Summary: {run_dir / 'analysis' / 'summary.txt'}")
    print(f"Diffs:   {run_dir / 'diffs'}")


def _read_results_csv(path: Path) -> list[dict[str, str]]:
    """Read existing analysis results.csv rows."""
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_results_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """Write results rows back to CSV after enrichment."""
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def cmd_merge_interaction(args: argparse.Namespace) -> None:
    """Merge snippet interaction log data into run analysis outputs."""
    requested_run_dir = Path(args.run_dir)
    run_dir = _resolve_run_dir(requested_run_dir)
    if run_dir != requested_run_dir:
        print(f"Resolved nested run dir: {run_dir}")

    results_csv = run_dir / "analysis" / "results.csv"
    summary_json = run_dir / "analysis" / "summary.json"
    log_csv = run_dir / "logs" / "snippet_log.csv"

    if not results_csv.exists():
        raise SystemExit(f"Missing: {results_csv}")
    if not log_csv.exists():
        raise SystemExit(f"Missing: {log_csv}")

    rows = _read_results_csv(results_csv)
    interaction_rows = load_snippet_log_csv(log_csv)
    merged_rows = merge_interaction_into_results(results_rows=rows, interaction_rows=interaction_rows)
    _write_results_csv(results_csv, merged_rows)

    payload: dict[str, Any] = {}
    if summary_json.exists():
        payload = json.loads(summary_json.read_text(encoding="utf-8"))
    payload["interaction"] = interaction_features(interaction_rows)
    payload["participant_profile"] = _load_participant_profile(run_dir)
    summary_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Merged interaction into: {results_csv}")
    print(f"Updated: {summary_json}")


def _load_participant_profile(run_dir: Path) -> dict[str, str]:
    profile_path = run_dir / "logs" / "participant_profile.json"
    if not profile_path.exists():
        return {field: "" for field in _PARTICIPANT_PROFILE_FIELDS}
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return {field: "" for field in _PARTICIPANT_PROFILE_FIELDS}
    return {field: str(payload.get(field, "") or "") for field in _PARTICIPANT_PROFILE_FIELDS}


def _load_condition(run_dir: Path) -> str:
    """Read run condition; default to unknown when missing."""
    run_dir = _resolve_run_dir(run_dir)
    p = run_dir / "condition.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else "unknown"


def _parse_iso_ts(value: Any) -> datetime | None:
    """Parse ISO timestamps and gracefully handle trailing Z format."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _compute_duration_seconds(run_dir: Path) -> float:
    """Compute run duration, preferring active session seconds when available."""
    run_dir = _resolve_run_dir(run_dir)
    times_path = run_dir / "start_end_times.json"
    if not times_path.exists():
        return 0.0
    try:
        data = json.loads(times_path.read_text(encoding="utf-8"))

        # Participant web app writes active_seconds across multiple open/close sessions.
        active = data.get("active_seconds")
        if active is not None:
            return max(0.0, float(active))

        # Backward-compatible fallback for legacy runs with only start/end.
        start_dt = _parse_iso_ts(data.get("start"))
        end_dt = _parse_iso_ts(data.get("end"))
        if start_dt is None or end_dt is None:
            return 0.0
        return max(0.0, float((end_dt - start_dt).total_seconds()))
    except Exception:
        return 0.0


def _load_summary(run_dir: Path) -> dict[str, Any]:
    """Read run-level summary JSON if it exists."""
    run_dir = _resolve_run_dir(run_dir)
    p = run_dir / "analysis" / "summary.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _stable_json(obj: Any) -> str:
    """Serialize a nested object for safe storage in a CSV cell."""
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except Exception:
        return ""



def _safe_float(x: Any, default: float = 0.0) -> float:
    """Coerce numeric-like values to float with a default fallback."""
    try:
        return float(x)
    except Exception:
        return default


def _read_results_rows(run_dir: Path) -> list[dict[str, str]]:
    """Load per-snippet analysis rows for one run if available."""
    run_dir = _resolve_run_dir(run_dir)
    p = run_dir / "analysis" / "results.csv"
    if not p.exists():
        return []
    try:
        with p.open("r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _is_primary_mitigated_row(row: dict[str, str], primary_source: str) -> bool:
    """Determine whether one snippet row is mitigated under the current primary source."""
    if primary_source == "judge":
        enabled = str(row.get("judge_enabled", "")).strip().lower() in {"1", "true", "t", "yes", "y"}
        verdict = str(row.get("judge_verdict", "")).strip().lower()
        return bool(enabled and verdict == "absent")
    return str(row.get("outcome", "")).strip() == "Mitigated"


def _compute_time_to_first_secure_fix_seconds(run_dir: Path, primary_source: str) -> float:
    """Return seconds from run start to first mitigated snippet end timestamp.

    Returns -1.0 when insufficient timing data is available.
    """
    run_dir = _resolve_run_dir(run_dir)
    times_path = run_dir / "start_end_times.json"
    snippet_times_path = run_dir / "timings" / "snippet_times.json"
    if not times_path.exists() or not snippet_times_path.exists():
        return -1.0

    try:
        run_times = json.loads(times_path.read_text(encoding="utf-8"))
        snippet_times = json.loads(snippet_times_path.read_text(encoding="utf-8"))
    except Exception:
        return -1.0

    try:
        run_start = datetime.fromisoformat(str(run_times.get("start", "")))
    except Exception:
        return -1.0

    rows = _read_results_rows(run_dir)
    mitigated_ids = [
        str(r.get("snippet_id", "")).strip()
        for r in rows
        if str(r.get("snippet_id", "")).strip() and _is_primary_mitigated_row(r, primary_source)
    ]
    if not mitigated_ids:
        return -1.0

    end_times: list[datetime] = []
    for sid in mitigated_ids:
        entry = snippet_times.get(sid, {}) if isinstance(snippet_times, dict) else {}
        if not isinstance(entry, dict):
            continue
        end_text = str(entry.get("end", "") or "").strip()
        if not end_text:
            continue
        try:
            end_times.append(datetime.fromisoformat(end_text))
        except Exception:
            continue

    if not end_times:
        return -1.0

    return max(0.0, (min(end_times) - run_start).total_seconds())


def _compute_judge_strategy_variance(run_dir: Path) -> tuple[float, int]:
    """Compute mean normalized entropy of per-strategy verdicts in [0,1]."""
    rows = _read_results_rows(run_dir)
    if not rows:
        return 0.0, 0

    entropies: list[float] = []
    denom = math.log(3.0)

    for row in rows:
        raw = str(row.get("judge_strategy_results", "") or "").strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict) or len(obj) < 2:
            continue

        counts = {"present": 0, "absent": 0, "uncertain": 0}
        for details in obj.values():
            if not isinstance(details, dict):
                continue
            verdict = str(details.get("verdict", "")).strip().lower()
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

def cmd_aggregate_pilot(args: argparse.Namespace) -> None:
    """Build pilot_summary.csv from run-level summary artifacts."""
    runs_root = Path(args.runs_root)
    out_csv = Path(args.out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    skipped_not_judge = 0

    for run_dir in sorted([p for p in runs_root.iterdir() if p.is_dir()]):
        summary = _load_summary(run_dir)
        if not summary:
            continue

        condition = _load_condition(run_dir)
        duration = _compute_duration_seconds(run_dir)
        primary_rates = summary.get("primary_rates", {}) or {}
        primary_counts = summary.get("primary_counts", {}) or {}
        primary_source = summary.get("primary_source", "") or ""

        if args.require_judge_primary and primary_source != "judge":
            skipped_not_judge += 1
            continue

        interaction = summary.get("interaction", {}) or {}
        profile = summary.get("participant_profile", {}) or {}
        strat_dist = interaction.get("strategy_distribution", {}) or {}

        primary_mitigated_count = _safe_float(primary_counts.get("Mitigated", 0), 0.0)
        duration_minutes = duration / 60.0 if duration > 0 else 0.0
        mitigations_per_minute = (primary_mitigated_count / duration_minutes) if duration_minutes > 0 else 0.0
        time_to_first_secure_fix_seconds = _compute_time_to_first_secure_fix_seconds(run_dir, primary_source)
        judge_strategy_variance, judge_strategy_variance_snippets = _compute_judge_strategy_variance(run_dir)

        rows.append(
            {
                "run_id": run_dir.name,
                "condition": condition,
                "duration_seconds": duration,
                "primary_source": primary_source,
                "primary_scored_snippets": summary.get("primary_scored_snippets", ""),
                "primary_mitigation_rate": primary_rates.get("mitigation", ""),
                "primary_persistence_rate": primary_rates.get("persistence", ""),
                "primary_abstention_rate": primary_rates.get("abstention", ""),
                "primary_mitigated": primary_counts.get("Mitigated", ""),
                "primary_preserved": primary_counts.get("Preserved", ""),
                "primary_unknown": primary_counts.get("UNKNOWN", ""),
                "mitigations_per_minute": mitigations_per_minute,
                "time_to_first_secure_fix_seconds": time_to_first_secure_fix_seconds,
                "judge_strategy_variance": judge_strategy_variance,
                "judge_strategy_variance_snippets": judge_strategy_variance_snippets,
                "scored_snippets": summary.get("scored_snippets", ""),
                "mitigation_rate_detector": (summary.get("rates", {}) or {}).get("mitigation", ""),
                "persistence_rate_detector": (summary.get("rates", {}) or {}).get("persistence", ""),
                "amplification_rate_detector": (summary.get("rates", {}) or {}).get("amplification", ""),
                "judge_scored_snippets": summary.get("judge_scored_snippets", ""),
                "judge_detector_disagreement_rate": summary.get("judge_detector_disagreement_rate", ""),
                "interaction_logged_snippets": interaction.get("interaction_logged_snippets", ""),
                "interaction_avg_turns": interaction.get("avg_turns", ""),
                "interaction_avg_applied_ratio": interaction.get("avg_applied_ratio", ""),
                "interaction_avg_confidence_1to5": interaction.get("avg_confidence_1to5", ""),
                "interaction_strategy_distribution": _stable_json(strat_dist),
                "participant_profile_complete": all(str(profile.get(field, "")).strip() for field in _PARTICIPANT_PROFILE_FIELDS),
                "participant_programming_experience": profile.get("programming_experience", ""),
                "participant_python_experience": profile.get("python_experience", ""),
                "participant_llm_coding_experience": profile.get("llm_coding_experience", ""),
                "participant_security_experience": profile.get("security_experience", ""),
            }
        )
    fieldnames = [
        "run_id",
        "condition",
        "duration_seconds",
        "primary_source",
        "primary_scored_snippets",
        "primary_mitigation_rate",
        "primary_persistence_rate",
        "primary_abstention_rate",
        "primary_mitigated",
        "primary_preserved",
        "primary_unknown",
        "mitigations_per_minute",
        "time_to_first_secure_fix_seconds",
        "judge_strategy_variance",
        "judge_strategy_variance_snippets",
        "scored_snippets",
        "mitigation_rate_detector",
        "persistence_rate_detector",
        "amplification_rate_detector",
        "judge_scored_snippets",
        "judge_detector_disagreement_rate",
        "interaction_logged_snippets",
        "interaction_avg_turns",
        "interaction_avg_applied_ratio",
        "interaction_avg_confidence_1to5",
        "interaction_strategy_distribution",
        "participant_profile_complete",
        "participant_programming_experience",
        "participant_python_experience",
        "participant_llm_coding_experience",
        "participant_security_experience",
    ]

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    if skipped_not_judge:
        print(
            f"Skipped {skipped_not_judge} runs because primary_source != 'judge' "
            "(require_judge_primary set)."
        )
    print(f"Wrote: {out_csv}")


def cmd_compute_stats(args: argparse.Namespace) -> None:
    """Compute descriptive/inferential statistics from aggregated CSV output."""
    write_pilot_stats_text(in_csv=args.in_csv, out_txt=args.out_txt)
    print(f"Wrote: {args.out_txt}")


def cmd_build_report(args: argparse.Namespace) -> None:
    """Generate aggregated HTML report across runs for a phase/root."""
    repo_root = Path(__file__).resolve().parents[1]
    runs_root = Path(args.runs_root) if args.runs_root else (repo_root / "runs" / args.phase)
    out_html = repo_root / args.out_html

    build_aggregated_report_offline(
        repo_root=repo_root,
        runs_root=runs_root,
        out_html=out_html,
        title=args.title,
    )
    print(f"Wrote: {out_html}")


def cmd_aggregate_results(args: argparse.Namespace) -> None:
    """Recompute summary.json and summary.txt for one run from results.csv."""
    requested_run_dir = Path(args.run_dir)
    if not requested_run_dir.exists():
        raise FileNotFoundError(f"Run directory not found: {requested_run_dir}")

    run_dir = _resolve_run_dir(requested_run_dir)
    if run_dir != requested_run_dir:
        print(f"Resolved nested run dir: {run_dir}")

    analysis_dir = run_dir / "analysis"
    results_csv = analysis_dir / "results.csv"

    if not results_csv.exists():
        raise FileNotFoundError(f"Missing results CSV: {results_csv}")

    out_json = analysis_dir / "summary.json"
    out_txt = analysis_dir / "summary.txt"
    summary = write_summary_files(results_csv=str(results_csv), out_json=str(out_json), out_txt=str(out_txt))

    print("Summary rebuilt.")
    print(f"Run: {run_dir}")
    print(f"Primary source: {summary.primary_source}")
    print(f"Summary JSON: {out_json}")
    print(f"Summary TXT:  {out_txt}")


def cmd_mark_snippet(args: argparse.Namespace) -> None:
    """Record per-snippet timing event (start or end)."""
    mark_snippet_time(args.run_dir, args.snippet_id, args.event)
    print(f"Marked {args.event} for {args.snippet_id} in {args.run_dir}")


def cmd_end_timer(args: argparse.Namespace) -> None:
    """Write run-level end timestamp to start_end_times.json."""
    write_time(args.timer_json, "end")
    print(f"Wrote end timestamp: {args.timer_json}")



def cmd_build_participant_kit(args: argparse.Namespace) -> None:
    """Wrapper command that builds a participant-facing kit."""
    build_participant_kit(args)


def cmd_clean_participant_kits(args: argparse.Namespace) -> None:
    """Wrapper command that removes generated participant kits."""
    clean_participant_kits(args)


def cmd_privacy_check(args: argparse.Namespace) -> None:
    """Run pre-publish privacy checks and return non-zero on failures."""
    ok, findings, mode = run_prepublish_check(REPO_ROOT)
    print("Privacy Pre-Publish Check")
    print("=" * 32)
    print(f"Scan mode: {mode}")

    if findings:
        high = [f for f in findings if f.severity == "HIGH"]
        med = [f for f in findings if f.severity == "MEDIUM"]
        print(f"Findings: HIGH={len(high)} MEDIUM={len(med)}")
        for sev in ("HIGH", "MEDIUM"):
            rows = [f for f in findings if f.severity == sev]
            if not rows:
                continue
            print(f"\n{sev} findings")
            for i, f in enumerate(rows, start=1):
                print(f"  {i}. [{f.category}] {f.path}")
                print(f"     {f.detail}")
    else:
        print("No findings.")

    if not ok:
        raise SystemExit(1)


def _synthetic_now_iso() -> str:
    """Return current UTC timestamp as ISO text for synthetic fixture files."""
    return datetime.now(timezone.utc).isoformat()


def _list_snippet_triples() -> list[tuple[str, Path, Path]]:
    """Return (snippet_id, baseline_path, gold_path) for all SQLi/CMDi snippets."""
    out: list[tuple[str, Path, Path]] = []
    for vuln_type in ["SQLi", "CMDi"]:
        bdir = REPO_ROOT / "snippets" / "baseline" / vuln_type
        gdir = REPO_ROOT / "snippets" / "gold" / vuln_type
        for bpath in sorted(bdir.glob("*.py")):
            out.append((bpath.stem, bpath, gdir / bpath.name))
    return out


def _copy_synthetic_edits(run_dir: Path, mode: str) -> None:
    """Populate edits/ with baseline, gold, or mixed files for synthetic runs."""
    edits_dir = run_dir / "edits"
    if edits_dir.exists():
        shutil.rmtree(edits_dir)
    edits_dir.mkdir(parents=True, exist_ok=True)

    items = _list_snippet_triples()
    if mode == "baseline":
        for _, bpath, _ in items:
            shutil.copy2(bpath, edits_dir / bpath.name)
    elif mode == "gold":
        for _, _, gpath in items:
            shutil.copy2(gpath, edits_dir / gpath.name)
    elif mode == "mixed":
        half = len(items) // 2
        for idx, (_, bpath, gpath) in enumerate(items):
            src = bpath if idx < half else gpath
            shutil.copy2(src, edits_dir / src.name)
    else:
        raise ValueError(f"Unknown mode: {mode}")


def _clamp_int(x: int, lo: int, hi: int) -> int:
    """Clamp integer x to inclusive [lo, hi] range."""
    return max(lo, min(hi, x))


def _make_synthetic_run(participant_id: str, condition: str, mode: str, *, seed: int | None = None) -> Path:
    """Create one synthetic run folder plus interaction log rows for UI/pipeline testing."""
    if seed is not None:
        random.seed(seed)

    run_dir = REPO_ROOT / "runs" / "pilot" / participant_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "condition.txt").write_text(condition + "\n", encoding="utf-8")
    (run_dir / "start_end_times.json").write_text(
        json.dumps({"start": _synthetic_now_iso(), "end": _synthetic_now_iso()}, indent=2),
        encoding="utf-8",
    )

    _copy_synthetic_edits(run_dir, mode)

    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_csv = logs_dir / "snippet_log.csv"
    (logs_dir / "participant_profile.json").write_text(
        json.dumps({
            "programming_experience": random.choice(["<1 year", "1-2 years", "3-5 years", "6+ years"]),
            "python_experience": random.choice(["none", "basic", "intermediate", "advanced"]),
            "llm_coding_experience": random.choice(["never", "rarely", "monthly", "weekly", "daily"]),
            "security_experience": random.choice(["none", "self-taught", "coursework", "professional"]),
        }, indent=2),
        encoding="utf-8",
    )

    fieldnames = [
        "snippet_id",
        "tool",
        "model",
        "turns",
        "applied_turns",
        "strategy_primary",
        "confidence_1to5",
        "first_prompt",
        "final_prompt",
        "notes",
    ]

    rows: list[dict[str, Any]] = []
    for sid, _, _ in _list_snippet_triples():
        if mode == "baseline":
            turns = random.randint(0, 2)
            applied_turns = random.randint(0, turns) if turns else 0
            tool = "None" if turns == 0 else random.choice(_TOOLS[1:])
            model = "" if tool == "None" else random.choice(_MODELS[1:])
            conf = random.choice([1, 2, 2, 3])
        elif mode == "gold":
            turns = random.randint(1, 6)
            applied_turns = random.randint(max(0, turns - 2), turns)
            tool = random.choice(_TOOLS[1:])
            model = random.choice(_MODELS[1:])
            conf = random.choice([3, 4, 4, 5, 5])
        else:
            turns = random.randint(0, 8)
            applied_turns = random.randint(0, turns) if turns else 0
            tool = "None" if turns == 0 else random.choice(_TOOLS[1:])
            model = "" if tool == "None" else random.choice(_MODELS[1:])
            conf = random.choice([2, 3, 3, 4, 5])

        turns = _clamp_int(turns, 0, 12)
        applied_turns = _clamp_int(applied_turns, 0, turns)
        strat_primary = random.choice(_STRATEGY_PRIMARY)
        if random.random() < 0.08:
            strat_primary = "other"

        if mode == "baseline":
            first_prompt, final_prompt = "", ""
            notes = "Synthetic control run (baseline copied)."
        elif mode == "gold":
            first_prompt = f"Fix {sid}. Return code only."
            final_prompt = f"Verify {sid} edit is secure. If not, explain briefly."
            notes = "Synthetic control run (gold copied)."
        else:
            first_prompt = f"Fix {sid}. Return code only."
            final_prompt = f"Verify {sid} edit is secure. If not, explain briefly."
            notes = f"Synthetic mixed run for {sid}."

        if random.random() < 0.07:
            first_prompt, final_prompt = "", ""

        rows.append(
            {
                "snippet_id": sid,
                "tool": tool,
                "model": model,
                "turns": turns,
                "applied_turns": applied_turns,
                "strategy_primary": strat_primary,
                "confidence_1to5": _clamp_int(conf, 1, 5),
                "first_prompt": first_prompt,
                "final_prompt": final_prompt,
                "notes": notes,
            }
        )

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return run_dir


def cmd_make_test_runs(args: argparse.Namespace) -> None:
    """Create synthetic pilot runs for quick end-to-end testing and demos."""
    runs_root = REPO_ROOT / "runs" / "pilot"
    runs_root.mkdir(parents=True, exist_ok=True)

    _make_synthetic_run("TEST001", condition="productivity", mode="baseline", seed=1001)
    _make_synthetic_run("TEST002", condition="security", mode="gold", seed=2002)
    _make_synthetic_run("TEST003", condition="productivity", mode="mixed", seed=3003)

    if not args.core_only:
        _make_synthetic_run("TEST004", condition="security", mode="mixed", seed=4004)
        _make_synthetic_run("TEST005", condition="productivity", mode="gold", seed=5005)

    print(f"Created synthetic runs in: {runs_root}")

def build_parser() -> argparse.ArgumentParser:
    """Create top-level parser with subcommands for each workflow action."""
    ap = argparse.ArgumentParser(description="Unified workflow CLI for the study repository.")
    sub = ap.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start-run", help="Initialize a participant run.")
    p_start.add_argument("--participant_id", required=True)
    p_start.add_argument("--condition", required=True, choices=["productivity", "security"])
    p_start.add_argument("--phase", default="pilot", choices=["self_test", "pilot", "main"])
    p_start.add_argument("--metadata_csv", default=str(DEFAULT_METADATA_PATH))
    p_start.set_defaults(func=cmd_start_run)

    p_analyze = sub.add_parser("analyze-run", help="Analyze a participant run.")
    p_analyze.add_argument("--participant_id", required=True)
    p_analyze.add_argument("--phase", default="pilot", choices=["self_test", "pilot", "main"])
    p_analyze.add_argument("--metadata_csv", default=str(DEFAULT_METADATA_PATH))
    p_analyze.set_defaults(func=cmd_analyze_run)

    p_merge = sub.add_parser("merge-interaction", help="Merge snippet interaction log into run outputs.")
    p_merge.add_argument("--run_dir", required=True)
    p_merge.set_defaults(func=cmd_merge_interaction)

    p_agg = sub.add_parser("aggregate-pilot", help="Aggregate pilot run summaries into one CSV.")
    p_agg.add_argument("--runs_root", default="runs/pilot")
    p_agg.add_argument("--out_csv", default="data/aggregated/pilot_summary.csv")
    p_agg.add_argument("--require_judge_primary", action="store_true")
    p_agg.set_defaults(func=cmd_aggregate_pilot)

    p_stats = sub.add_parser("compute-stats", help="Compute pilot stats from aggregate CSV.")
    p_stats.add_argument("--in_csv", default="data/aggregated/pilot_summary.csv")
    p_stats.add_argument("--out_txt", default="data/aggregated/pilot_stats.txt")
    p_stats.set_defaults(func=cmd_compute_stats)

    p_report = sub.add_parser("build-report", help="Build offline HTML report.")
    p_report.add_argument("--phase", default="pilot")
    p_report.add_argument("--runs_root", default="")
    p_report.add_argument("--out_html", default="data/aggregated/report.html")
    p_report.add_argument("--title", default="RepairAudit Report")
    p_report.set_defaults(func=cmd_build_report)

    p_rebuild = sub.add_parser("aggregate-results", help="Rebuild run summary files from results.csv.")
    p_rebuild.add_argument("--run_dir", required=True)
    p_rebuild.set_defaults(func=cmd_aggregate_results)

    p_mark = sub.add_parser("mark-snippet", help="Mark per-snippet timing event.")
    p_mark.add_argument("--run_dir", required=True)
    p_mark.add_argument("--snippet_id", required=True)
    p_mark.add_argument("--event", required=True, choices=["start", "end"])
    p_mark.set_defaults(func=cmd_mark_snippet)

    p_make = sub.add_parser("make-test-runs", help="Create synthetic test runs for pipeline and UI checks.")
    p_make.add_argument("--core-only", action="store_true", help="Create TEST001-TEST003 only.")
    p_make.set_defaults(func=cmd_make_test_runs)

    p_kit = sub.add_parser(
        "build-participant-kit",
        help="Create a participant-facing kit with locked config and baseline snippets.",
    )
    p_kit.add_argument("--participant_id", required=True)
    p_kit.add_argument("--condition", default="security", choices=["productivity", "security"])
    p_kit.add_argument("--phase", default="pilot", choices=["self_test", "pilot", "main"])
    p_kit.add_argument("--metadata_csv", default=str(DEFAULT_METADATA_PATH))
    p_kit.add_argument("--out_root", default="participant_kits")
    p_kit.add_argument("--study_id", default="repairaudit-v1")
    p_kit.add_argument("--llm_provider", default="ollama")
    p_kit.add_argument("--llm_model", default="qwen2.5-coder:7b-instruct")
    p_kit.add_argument("--temperature", type=float, default=0.2)
    p_kit.add_argument("--top_p", type=float, default=0.9)
    p_kit.add_argument("--top_k", type=int, default=40)
    p_kit.add_argument("--num_predict", type=int, default=1200)
    p_kit.add_argument("--seed", type=int, default=42)
    p_kit.add_argument("--overwrite", action="store_true")
    p_kit.set_defaults(func=cmd_build_participant_kit)

    p_clean = sub.add_parser(
        "clean-participant-kits",
        help="Remove generated participant kits by ID or with explicit --all.",
    )
    p_clean.add_argument("--out_root", default="participant_kits")
    p_clean.add_argument("--participant_id", default="")
    p_clean.add_argument("--all", action="store_true")
    p_clean.add_argument("--dry_run", action="store_true")
    p_clean.set_defaults(func=cmd_clean_participant_kits)

    p_end = sub.add_parser("end-timer", help="Write run-level end timestamp.")
    p_end.add_argument("--timer_json", required=True)
    p_end.set_defaults(func=cmd_end_timer)

    p_priv = sub.add_parser(
        "privacy-check",
        help="Run pre-publish privacy checks to prevent data leaks.",
    )
    p_priv.set_defaults(func=cmd_privacy_check)

    return ap


def main() -> None:
    """Parse CLI args and dispatch to the selected command handler."""
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()





















