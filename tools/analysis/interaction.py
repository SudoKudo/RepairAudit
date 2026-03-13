"""Parse and aggregate snippet-level interaction self-logs.

This module normalizes participant log rows, computes run-level interaction
features, and merges snippet interaction columns into analysis results.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

ALLOWED_STRATEGIES = {
    "zero_shot",
    "few_shot",
    "chain_of_thought",
    "adaptive_chain_of_thought",
    "other",
}
STRATEGY_ALIASES = {
    "zero shot": "zero_shot",
    "zero-shot": "zero_shot",
    "zeroshot": "zero_shot",
    "few shot": "few_shot",
    "few-shot": "few_shot",
    "fewshot": "few_shot",
    "cot": "chain_of_thought",
    "chain of thought": "chain_of_thought",
    "chain-of-thought": "chain_of_thought",
    "adaptive cot": "adaptive_chain_of_thought",
    "adaptive chain of thought": "adaptive_chain_of_thought",
    "adaptive chain-of-thought": "adaptive_chain_of_thought",
}


@dataclass(frozen=True)
class InteractionRow:
    """Normalized interaction-log record for one snippet edit session."""

    snippet_id: str
    tool: str
    model: str
    turns: int
    applied_turns: int
    strategy_primary: str
    confidence_1to5: int
    first_prompt: str
    final_prompt: str
    notes: str


def _to_int(x: str, default: int = 0) -> int:
    """Parse an integer from text; return default when parsing fails."""
    try:
        return int(float((x or "").strip()))
    except Exception:
        return default


def _normalize_strategy(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return "other"
    normalized = STRATEGY_ALIASES.get(raw, raw.replace("-", "_").replace(" ", "_"))
    return normalized if normalized in ALLOWED_STRATEGIES else "other"


def load_snippet_log_csv(path: Path) -> List[InteractionRow]:
    """Load and normalize snippet_log.csv rows into InteractionRow records."""
    if not path.exists():
        return []

    rows: List[InteractionRow] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            snippet_id = (r.get("snippet_id") or "").strip()
            if not snippet_id:
                continue

            turns = _to_int(r.get("turns") or "", 0)
            applied = _to_int(r.get("applied_turns") or "", 0)
            conf = _to_int(r.get("confidence_1to5") or "", 0)
            conf = min(max(conf, 0), 5)

            rows.append(
                InteractionRow(
                    snippet_id=snippet_id,
                    tool=(r.get("tool") or "").strip(),
                    model=(r.get("model") or "").strip(),
                    turns=max(turns, 0),
                    applied_turns=max(min(applied, max(turns, 0)), 0),
                    strategy_primary=_normalize_strategy(r.get("strategy_primary") or "other"),
                    confidence_1to5=conf,
                    first_prompt=(r.get("first_prompt") or "").strip(),
                    final_prompt=(r.get("final_prompt") or "").strip(),
                    notes=(r.get("notes") or "").strip(),
                )
            )
    return rows


def interaction_features(rows: List[InteractionRow]) -> Dict[str, Any]:
    """Aggregate interaction metrics across snippets for a run."""
    if not rows:
        return {
            "interaction_logged_snippets": 0,
            "avg_turns": 0.0,
            "avg_applied_ratio": 0.0,
            "strategy_distribution": {},
            "avg_confidence_1to5": 0.0,
        }

    n = len(rows)
    turns_sum = sum(r.turns for r in rows)
    applied_sum = sum(r.applied_turns for r in rows)
    conf_sum = sum(r.confidence_1to5 for r in rows)

    strat: Dict[str, int] = {}
    for r in rows:
        strat[r.strategy_primary] = strat.get(r.strategy_primary, 0) + 1

    avg_turns = turns_sum / n if n else 0.0
    avg_applied_ratio = (applied_sum / turns_sum) if turns_sum else 0.0
    avg_conf = conf_sum / n if n else 0.0

    return {
        "interaction_logged_snippets": n,
        "avg_turns": float(avg_turns),
        "avg_applied_ratio": float(avg_applied_ratio),
        "strategy_distribution": strat,
        "avg_confidence_1to5": float(avg_conf),
    }


def merge_interaction_into_results(*, results_rows: List[Dict[str, str]], interaction_rows: List[InteractionRow], snippet_id_field: str = "snippet_id") -> List[Dict[str, str]]:
    """Attach interaction columns onto results rows by snippet_id."""
    by_id: Dict[str, InteractionRow] = {r.snippet_id: r for r in interaction_rows}

    out: List[Dict[str, str]] = []
    for r in results_rows:
        sid = (r.get(snippet_id_field) or "").strip()
        ir = by_id.get(sid)

        r2 = dict(r)
        if ir is None:
            r2.update({
                "llm_tool": "",
                "llm_model": "",
                "llm_turns": "",
                "llm_applied_turns": "",
                "llm_applied_ratio": "",
                "llm_strategy_primary": "",
                "llm_confidence_1to5": "",
            })
            r2.pop("llm_strategy_secondary", None)
        else:
            ratio = (ir.applied_turns / ir.turns) if ir.turns else 0.0
            r2.update({
                "llm_tool": ir.tool,
                "llm_model": ir.model,
                "llm_turns": str(ir.turns),
                "llm_applied_turns": str(ir.applied_turns),
                "llm_applied_ratio": f"{ratio:.3f}",
                "llm_strategy_primary": ir.strategy_primary,
                "llm_confidence_1to5": str(ir.confidence_1to5),
            })
            r2.pop("llm_strategy_secondary", None)
        out.append(r2)
    return out
