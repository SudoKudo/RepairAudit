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
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.analysis.detectors import detect_cmdi, detect_sqli
from tools.analysis.llm_judge import judge_edited_code_with_ollama


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


def _normalize_vuln_type(v: str) -> str:
    """Normalize metadata vulnerability labels to canonical short forms."""
    v_norm = (v or "").strip().upper()
    if v_norm.startswith("SQLI"):
        return "SQLI"
    if v_norm.startswith("CMDI"):
        return "CMDI"
    return v_norm


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


def analyze_participant(run_dir: str, metadata_csv: str, save_csv: bool = True) -> Dict[str, Any]:
    """Analyze one participant run and persist per-snippet outputs."""
    run_path = Path(run_dir)
    edits_dir = run_path / "edits"
    analysis_dir = run_path / "analysis"
    diffs_dir = run_path / "diffs"

    analysis_dir.mkdir(parents=True, exist_ok=True)
    diffs_dir.mkdir(parents=True, exist_ok=True)

    metadata_rows = load_metadata(metadata_csv)

    participant_id = run_path.name
    phase = run_path.parent.name
    condition_path = run_path / "condition.txt"
    condition = condition_path.read_text(encoding="utf-8", errors="ignore").strip() if condition_path.exists() else ""

    judge_enabled = _judge_enabled_from_config()
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
        edited_path = edits_dir / f"{snippet_id}.py"

        row_base: Dict[str, Any] = {
            "participant_id": participant_id,
            "phase": phase,
            "condition": condition,
            "snippet_id": snippet_id,
            "vuln_type": vuln_type_raw,
            "cwe": cwe,
            "baseline_relpath": baseline_rel,
            "gold_relpath": gold_rel,
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
        if vuln_type == "SQLI":
            before = detect_sqli(baseline_text)
            after = detect_sqli(edited_text)
            judge_vuln_type = "SQLi"
        elif vuln_type == "CMDI":
            before = detect_cmdi(baseline_text)
            after = detect_cmdi(edited_text)
            judge_vuln_type = "CMDi"
        else:
            print(f"[analyze] snippet={snippet_id} status=unknown_vuln_type", flush=True)
            results.append({**row_base, "status": "unknown_vuln_type"})
            continue

        # Study policy: treat "uncertain" as present for conservative outcomes.
        before_present = before.verdict in ("present", "uncertain")
        after_present = after.verdict in ("present", "uncertain")
        outcome = classify_outcome(before_present, after_present, baseline_text, edited_text)
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
            "judge_vote_rule": "",
            "judge_strategy_results": "",
            "judge_status": "skipped",
        }

        if judge_enabled:
            print(f"[analyze] snippet={snippet_id} judge=starting", flush=True)
            jr = judge_edited_code_with_ollama(
                snippet_id=snippet_id,
                vuln_type=judge_vuln_type,
                cwe=cwe,
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
                    "judge_vote_rule": jr.vote_rule,
                    "judge_strategy_results": json.dumps(jr.strategy_results or {}, ensure_ascii=False),
                    "judge_status": "ok" if jr.raw_json and "_error" not in jr.raw_json else "uncertain",
                    "judge_raw_json": json.dumps(jr.raw_json, ensure_ascii=False),
                }
            )

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
            "judge_vote_rule",
            "status",
            "before_risky_hits",
            "after_risky_hits",
            "before_safe_hits",
            "after_safe_hits",
            "judge_evidence",
            "judge_rationale",
            "baseline_relpath",
            "gold_relpath",
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








