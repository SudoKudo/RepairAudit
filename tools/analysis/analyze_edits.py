"""Score and classify participant edits snippet-by-snippet.

For each snippet in metadata, this module:
- reads baseline, edited, and gold code
- runs local heuristic detectors
- optionally runs the LLM judge
- writes `analysis/results.json` and `analysis/results.csv`
"""

from __future__ import annotations

import csv
import difflib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.analysis.detectors import detect_cmdi, detect_sqli
from tools.analysis.llm_judge import judge_edited_code_with_ollama

_TRUTHY = {"1", "true", "t", "yes", "y"}


def load_metadata(csv_path: str) -> List[Dict[str, str]]:
    """Load snippet metadata rows as dictionaries."""
    rows: List[Dict[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def _similarity_ratio(a: str, b: str) -> float:
    """Cheap text similarity score in [0, 1] used for obfuscation heuristic."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def classify_outcome(before_present: bool, after_present: bool, before_text: str, after_text: str) -> str:
    """Convert before/after vulnerability presence into study outcome labels."""
    if before_present and not after_present:
        return "Mitigated"

    if before_present and after_present:
        # Vulnerability persists. If edit changed the text heavily, we classify it
        # as obfuscation rather than simple preservation.
        sim = _similarity_ratio(before_text, after_text)
        return "Obfuscated" if sim <= 0.70 else "Preserved"

    if (not before_present) and after_present:
        return "Amplified"

    return "Unchanged"


def _read_text_safe(path: Path) -> Optional[str]:
    """Read UTF-8 text safely and return None on IO errors."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def _read_json_safe(path: Path) -> Dict[str, Any]:
    """Read a JSON object from disk and return {} on failure."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_vuln_type(v: str) -> str:
    """Normalize metadata vulnerability labels to canonical short forms."""
    v_norm = (v or "").strip().upper()
    if v_norm.startswith("SQLI"):
        return "SQLI"
    if v_norm.startswith("CMDI"):
        return "CMDI"
    return v_norm


def _language_from_metadata(item: dict[str, str], baseline_path: Path | None) -> str:
    """Resolve snippet language from metadata or baseline-file extension."""
    declared = (item.get("language") or "").strip().lower()
    if declared:
        return declared
    suffix = (baseline_path.suffix.lower() if baseline_path else "")
    return {
        ".py": "python",
        ".java": "java",
        ".c": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
    }.get(suffix, suffix.lstrip(".") or "text")


def _judge_enabled_from_config() -> bool:
    """Read `llm_judge.enabled` from config with a safe fallback."""
    try:
        from tools.analysis import llm_judge as _lj

        cfg = _lj._load_yaml_config(None)  # type: ignore[attr-defined]
        enabled = _lj._deep_get(cfg, ["llm_judge", "enabled"], True)  # type: ignore[attr-defined]
        return bool(enabled)
    except Exception:
        # Conservative fallback: no judge if config cannot be loaded.
        return False


def _detector_enabled_from_env() -> bool:
    """Allow the researcher console to disable detector diagnostics explicitly."""
    value = os.getenv("GLACIER_ENABLE_DETECTOR", "").strip().lower()
    if not value:
        return True
    return value in _TRUTHY


def _repo_relative_str(path: Path, repo_root: Path) -> str:
    """Return a repo-relative path when possible and keep absolute form otherwise."""
    try:
        return str(path.resolve().relative_to(repo_root.resolve())).replace("\\", "/")
    except Exception:
        return str(path.resolve())


def _candidate_assignment_paths(run_path: Path) -> list[Path]:
    """Return likely assignment metadata files for one analyzed run."""
    repo_root = Path(__file__).resolve().parents[2]
    candidates: list[Path] = [run_path / "researcher_assignment.json"]

    study_assignment = _read_json_safe(run_path / "study_assignment.json")
    map_names = {f"{run_path.parent.name}__{run_path.name}.json"}

    phase = str(study_assignment.get("phase") or "").strip()
    participant_id = str(study_assignment.get("participant_id") or run_path.name).strip()
    if phase and participant_id:
        map_names.add(f"{phase}__{participant_id}.json")

    for map_name in sorted(map_names):
        candidates.extend(sorted(repo_root.glob(f"**/_researcher_maps/{map_name}")))

    candidates.append(run_path / "study_assignment.json")
    return candidates


def _load_run_assignment(run_path: Path) -> dict[str, Any]:
    """Load the first valid assignment payload available for one run."""
    for candidate in _candidate_assignment_paths(run_path):
        if not candidate.exists():
            continue
        payload = _read_json_safe(candidate)
        if payload:
            return payload
    return {}


def _assignment_metadata_rows(run_path: Path) -> List[Dict[str, str]]:
    """Build analysis-ready metadata rows from run-local assignment files."""
    repo_root = Path(__file__).resolve().parents[2]
    payload = _load_run_assignment(run_path)
    source_kind = str(payload.get("source_kind") or "").strip().lower()
    rows = payload.get("snippet_mappings", [])
    if not isinstance(rows, list):
        return []

    out: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        snippet_id = str(row.get("snippet_id") or "").strip()
        participant_filename = Path(
            str(
                row.get("participant_filename")
                or row.get("output_name")
                or row.get("participant_file")
                or ""
            ).strip()
        ).name
        if not snippet_id or not participant_filename:
            continue

        baseline_path = run_path / "baseline" / participant_filename
        if source_kind == "dataset" or str(row.get("source_kind") or "").strip().lower() == "dataset":
            vuln_type = (
                str(row.get("vuln_type") or "").strip()
                or str(row.get("vulnerability_type") or "").strip()
                or str(row.get("cwe_primary") or "").strip()
            )
            cwe = str(row.get("cwe") or "").strip() or str(row.get("cwe_primary") or "").strip()
            out.append(
                {
                    "snippet_id": snippet_id,
                    "vuln_type": vuln_type,
                    "cwe": cwe,
                    "language": str(row.get("language") or "").strip(),
                    "baseline_relpath": _repo_relative_str(baseline_path, repo_root),
                    "gold_relpath": "",
                    "participant_filename": participant_filename,
                    "is_vulnerable": str(row.get("is_vulnerable") or "1").strip() or "1",
                }
            )
            continue

        baseline_rel = str(row.get("baseline_relpath") or "").strip()
        if not baseline_rel:
            baseline_rel = _repo_relative_str(baseline_path, repo_root)
        out.append(
            {
                "snippet_id": snippet_id,
                "vuln_type": str(row.get("vuln_type") or row.get("vulnerability_type") or "").strip(),
                "cwe": str(row.get("cwe") or row.get("cwe_primary") or "").strip(),
                "language": str(row.get("language") or "").strip(),
                "baseline_relpath": baseline_rel,
                "gold_relpath": str(row.get("gold_relpath") or "").strip(),
                "participant_filename": participant_filename,
                "is_vulnerable": str(row.get("is_vulnerable") or "").strip(),
            }
        )
    return out


def _load_run_snippet_files(run_path: Path) -> dict[str, str]:
    """Read participant-side edited-file mapping from the run folder when available."""
    for candidate in _candidate_assignment_paths(run_path):
        if not candidate.exists():
            continue
        payload = _read_json_safe(candidate)
        if not payload:
            continue

        mapping: dict[str, str] = {}
        snippet_rows = payload.get("snippet_mappings", [])
        if isinstance(snippet_rows, list):
            for row in snippet_rows:
                if not isinstance(row, dict):
                    continue
                file_name = Path(
                    str(
                        row.get("participant_filename")
                        or row.get("output_name")
                        or ""
                    ).strip()
                ).name
                if not file_name:
                    continue
                for key in (
                    str(row.get("snippet_id") or "").strip(),
                    str(row.get("source_snippet_id") or "").strip(),
                ):
                    if key:
                        mapping[key] = file_name
        if mapping:
            return mapping

        raw_mapping = payload.get("snippet_files", {})
        if isinstance(raw_mapping, dict):
            for key, value in raw_mapping.items():
                snippet_id = str(key or "").strip()
                file_name = Path(str(value or "").strip()).name
                if snippet_id and file_name:
                    mapping[snippet_id] = file_name
        if mapping:
            return mapping

    return {}


def analyze_participant(run_dir: str, metadata_csv: str, save_csv: bool = True) -> Dict[str, Any]:
    """Analyze one participant run and persist per-snippet outputs."""
    run_path = Path(run_dir)
    edits_dir = run_path / "edits"
    analysis_dir = run_path / "analysis"
    diffs_dir = run_path / "diffs"

    analysis_dir.mkdir(parents=True, exist_ok=True)
    diffs_dir.mkdir(parents=True, exist_ok=True)

    metadata_rows = _assignment_metadata_rows(run_path)
    if not metadata_rows:
        metadata_rows = load_metadata(metadata_csv)

    assignment = _load_run_assignment(run_path)
    participant_id = str(assignment.get("participant_id") or run_path.name).strip() or run_path.name
    phase = str(assignment.get("phase") or run_path.parent.name).strip() or run_path.parent.name
    condition_path = run_path / "condition.txt"
    condition = condition_path.read_text(encoding="utf-8", errors="ignore").strip() if condition_path.exists() else ""
    snippet_files = _load_run_snippet_files(run_path)

    judge_enabled = _judge_enabled_from_config()
    detector_enabled = _detector_enabled_from_env()
    results: List[Dict[str, Any]] = []
    total_items = len(metadata_rows)
    print(f"[analyze] participant={participant_id} phase={phase} snippets={total_items}", flush=True)

    for idx, item in enumerate(metadata_rows, start=1):
        snippet_id = (item.get("snippet_id") or "").strip()
        vuln_type_raw = (item.get("vuln_type") or "").strip()
        vuln_type = _normalize_vuln_type(vuln_type_raw)

        baseline_rel = (item.get("baseline_relpath") or "").strip()
        gold_rel = (item.get("gold_relpath") or "").strip()
        cwe = (item.get("cwe") or "").strip()

        baseline_path = Path(baseline_rel) if baseline_rel else None
        gold_path = Path(gold_rel) if gold_rel else None
        edited_name = snippet_files.get(snippet_id)
        if not edited_name:
            edited_name = Path(item.get("participant_filename", "")).name
        if not edited_name:
            edited_name = baseline_path.name if baseline_path and baseline_path.name else f"{snippet_id}.txt"
        edited_path = edits_dir / edited_name
        language = _language_from_metadata(item, baseline_path)

        row_base: Dict[str, Any] = {
            "participant_id": participant_id,
            "phase": phase,
            "condition": condition,
            "snippet_id": snippet_id,
            "language": language,
            "vuln_type": vuln_type_raw,
            "cwe": cwe,
            "baseline_relpath": baseline_rel,
            "gold_relpath": gold_rel,
            "edited_filename": edited_path.name,
            "scoring_mode": "detector_supported",
        }

        print(
            f"[analyze] [{idx}/{total_items}] snippet={snippet_id or '<missing>'} file={edited_path.name}",
            flush=True,
        )

        if not snippet_id or not baseline_path:
            print(f"[analyze] [{idx}/{total_items}] status=bad_metadata_row", flush=True)
            results.append({**row_base, "status": "bad_metadata_row"})
            continue
        if not baseline_path.exists():
            print(f"[analyze] snippet={snippet_id} status=missing_baseline", flush=True)
            results.append({**row_base, "status": "missing_baseline"})
            continue
        if not edited_path.exists():
            print(f"[analyze] snippet={snippet_id} status=missing_edit", flush=True)
            results.append({**row_base, "status": "missing_edit"})
            continue

        baseline_text = _read_text_safe(baseline_path)
        edited_text = _read_text_safe(edited_path)
        gold_text = _read_text_safe(gold_path) if (gold_path and gold_path.exists()) else ""

        if baseline_text is None or edited_text is None:
            print(f"[analyze] snippet={snippet_id} status=read_error", flush=True)
            results.append({**row_base, "status": "read_error"})
            continue

        # Run the vulnerability-specific deterministic detector pair.
        if detector_enabled and vuln_type == "SQLI":
            before = detect_sqli(baseline_text, language=language)
            after = detect_sqli(edited_text, language=language)
            judge_vuln_type = "SQLi"
        elif detector_enabled and vuln_type == "CMDI":
            before = detect_cmdi(baseline_text, language=language)
            after = detect_cmdi(edited_text, language=language)
            judge_vuln_type = "CMDi"
        else:
            before_assumed_present = str(item.get("is_vulnerable") or "").strip().lower() in _TRUTHY
            if not before_assumed_present:
                before_assumed_present = True

            before = type("DetectorLike", (), {"verdict": "present" if before_assumed_present else "uncertain", "risky_hits": [], "safe_hits": []})()
            after = type("DetectorLike", (), {"verdict": "", "risky_hits": [], "safe_hits": []})()
            judge_vuln_type = vuln_type_raw or cwe or "target vulnerability"
            row_base["scoring_mode"] = "judge_only"
            if detector_enabled:
                print(f"[analyze] snippet={snippet_id} detector=judge_only vuln_type={judge_vuln_type}", flush=True)
            else:
                print(f"[analyze] snippet={snippet_id} detector=disabled judge_only vuln_type={judge_vuln_type}", flush=True)

        # Study policy: treat "uncertain" as present for conservative outcomes.
        before_present = before.verdict in ("present", "uncertain")
        after_present = after.verdict in ("present", "uncertain")
        outcome = classify_outcome(before_present, after_present, baseline_text, edited_text) if after.verdict else ""
        if after.verdict:
            print(
                f"[analyze] snippet={snippet_id} detector before={before.verdict} after={after.verdict} outcome={outcome}",
                flush=True,
            )

        judge_fields: Dict[str, Any] = {
            "judge_enabled": judge_enabled,
            "judge_verdict": "",
            "judge_confidence": "",
            "judge_rationale": "",
            "judge_evidence": "",
            "judge_strategy": "",
            "judge_parser_mode": "",
            "judge_vote_rule": "",
            "judge_strategy_results": "",
            "judge_freeze_path": "",
            "judge_freeze_config_id": "",
            "judge_status": "skipped",
        }

        if judge_enabled:
            print(f"[analyze] snippet={snippet_id} judge=starting", flush=True)
            jr = judge_edited_code_with_ollama(
                snippet_id=snippet_id,
                vuln_type=judge_vuln_type,
                cwe=cwe,
                language=language,
                baseline_code=baseline_text,
                edited_code=edited_text,
                gold_code=gold_text or "",
            )
            print(
                f"[analyze] snippet={snippet_id} judge verdict={jr.verdict} confidence={jr.confidence:.2f} strategy={jr.strategy_name}",
                flush=True,
            )
            judge_fields.update(
                {
                    "judge_verdict": jr.verdict,
                    "judge_confidence": jr.confidence,
                    "judge_rationale": jr.rationale,
                    "judge_evidence": jr.evidence,
                    "judge_strategy": jr.strategy_name,
                    "judge_parser_mode": jr.parser_mode,
                    "judge_vote_rule": jr.vote_rule,
                    "judge_strategy_results": json.dumps(jr.strategy_results or {}, ensure_ascii=False),
                    "judge_status": "ok" if jr.raw_json and "_error" not in jr.raw_json else "uncertain",
                    "judge_freeze_path": str(
                        ((jr.raw_json.get("final") or {}).get("freeze_path", "") if isinstance(jr.raw_json, dict) else "")
                        or ""
                    ),
                    "judge_freeze_config_id": str(
                        ((jr.raw_json.get("final") or {}).get("freeze_config_id", "") if isinstance(jr.raw_json, dict) else "")
                        or ""
                    ),
                    "judge_raw_json": json.dumps(jr.raw_json, ensure_ascii=False),
                }
            )
            if not after.verdict:
                after_verdict = judge_fields["judge_verdict"]
                after_present = after_verdict in ("present", "uncertain")
                after.verdict = after_verdict
                outcome = classify_outcome(before_present, after_present, baseline_text, edited_text)
                print(
                    f"[analyze] snippet={snippet_id} judge_only before={before.verdict} after={after_verdict} outcome={outcome}",
                    flush=True,
                )
        elif not after.verdict:
            print(f"[analyze] snippet={snippet_id} status=unsupported_without_judge", flush=True)
            results.append({**row_base, **judge_fields, "status": "unsupported_without_judge"})
            continue

        results.append(
            {
                **row_base,
                "before_verdict": before.verdict,
                "after_verdict": after.verdict,
                "outcome": outcome,
                "before_risky_hits": "|".join(before.risky_hits),
                "after_risky_hits": "|".join(after.risky_hits),
                "before_safe_hits": "|".join(before.safe_hits),
                "after_safe_hits": "|".join(after.safe_hits),
                **judge_fields,
                "status": "ok",
            }
        )
        print(f"[analyze] snippet={snippet_id} status=ok", flush=True)

    out_json = analysis_dir / "results.json"
    out_json.write_text(json.dumps(results, indent=2), encoding="utf-8")

    if save_csv:
        out_csv = analysis_dir / "results.csv"
        preferred = [
            "participant_id",
            "phase",
            "condition",
            "snippet_id",
            "language",
            "vuln_type",
            "cwe",
            "before_verdict",
            "after_verdict",
            "outcome",
            "judge_enabled",
            "judge_verdict",
            "judge_confidence",
            "judge_status",
            "judge_strategy",
            "judge_parser_mode",
            "judge_vote_rule",
            "judge_freeze_path",
            "judge_freeze_config_id",
            "status",
            "before_risky_hits",
            "after_risky_hits",
            "before_safe_hits",
            "after_safe_hits",
            "judge_evidence",
            "judge_rationale",
            "baseline_relpath",
            "gold_relpath",
            "edited_filename",
        ]
        all_keys = {k for r in results for k in r.keys()}
        fieldnames = preferred + [k for k in sorted(all_keys) if k not in preferred]

        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(results)

    return {"count": len(results), "results_path": str(out_json)}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--run_dir", required=True)
    ap.add_argument("--metadata_csv", required=True)
    args = ap.parse_args()
    analyze_participant(args.run_dir, args.metadata_csv)








