"""LLM-as-a-judge client with prompt strategy controls.

Supported prompt strategies:
- cot
- zero_shot
- few_shot
- self_consistency
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error, request

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


@dataclass(frozen=True)
class JudgeResult:
    """Normalized judge output consumed by analysis code."""

    verdict: str
    confidence: float
    rationale: str
    evidence: str
    raw_json: Dict[str, Any]
    strategy_name: str = ""
    strategy_results: Dict[str, Any] | None = None
    vote_rule: str = ""


_ALLOWED_STYLES = {"cot", "zero_shot", "few_shot", "self_consistency"}
_DEFAULT_STRATEGY = "cot"
_DEFAULT_ENSEMBLE = ["cot", "zero_shot", "few_shot", "self_consistency"]


def _build_output_contract() -> str:
    """Return the strict JSON schema contract used by all prompt styles."""
    return """
Return ONLY valid JSON with this exact shape and no extra keys:
{
  "verdict": "present" | "absent" | "uncertain",
  "confidence": <float between 0.0 and 1.0>,
  "rationale": "<=2 short sentences; plain language",
  "evidence": "cite concrete code behavior or token pattern from EDITED"
}
""".strip()


def _build_decision_policy(vuln_type: str, cwe: str) -> str:
    """Return a study-consistent decision policy for binary vulnerability judgment."""
    return f"""
Decision policy for {vuln_type} ({cwe}):
- "present": untrusted input can still reach a dangerous sink for this CWE class.
- "absent": mitigation in EDITED prevents this CWE class (even if implementation differs from GOLD).
- "uncertain": evidence is mixed, incomplete, or too ambiguous for a defensible yes/no.

Important constraints:
- GOLD is an ideal reference, not an exact-answer key.
- Judge EDITED on security semantics, not style preferences.
- Prefer conservative uncertainty over unsupported certainty.
""".strip()


def _build_base_system_prompt() -> str:
    """Return the shared evaluator persona and quality constraints."""
    return (
        "You are a senior application-security reviewer and methodology-conscious research rater. "
        "Your job is to produce consistent, evidence-grounded judgments for CWE-focused code edits. "
        "Avoid speculation, do not invent facts, and do not include markdown."
    )


def _find_project_root(start: Optional[Path] = None) -> Path:
    """Walk upward until config or project markers are found."""
    here = (start or Path.cwd()).resolve()
    for p in [here] + list(here.parents):
        if (p / "config.yaml").exists() or (p / "pyproject.toml").exists() or (p / ".git").exists():
            return p
    return here


def _load_yaml_config(config_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Load config YAML into dictionary."""
    path = (_find_project_root() / "config.yaml") if config_path is None else Path(config_path).expanduser().resolve()
    if not path.exists():
        return {}

    if yaml is None:
        raise RuntimeError("PyYAML is required to load config.yaml.")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _deep_get(d: Dict[str, Any], keys: list[str], default: Any = None) -> Any:
    """Nested dictionary getter with fallback."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _coerce_float(x: Any, default: float) -> float:
    """Convert value to float with fallback."""
    try:
        return float(x)
    except Exception:
        return default


def _coerce_int(x: Any, default: int) -> int:
    """Convert value to int with fallback."""
    try:
        return int(x)
    except Exception:
        return default


def _parse_csv_env(name: str) -> list[str]:
    """Parse comma separated env string."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [v.strip() for v in raw.split(",") if v.strip()]


def _parse_json_env(name: str) -> Dict[str, Any]:
    """Parse JSON object from env var."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _post_json(url: str, payload: Dict[str, Any], timeout: float = 90.0) -> Dict[str, Any]:
    """POST JSON to Ollama and parse response."""
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTPError {e.code}: {body[:2000]}") from e
    except error.URLError as e:
        raise RuntimeError(f"URLError: {e}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Response was not JSON: {raw[:2000]}") from e


def _extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Find first valid JSON object in mixed text."""
    if not text:
        return None

    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_str = False
    esc = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1].strip()
                try:
                    obj = json.loads(candidate)
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None

    return None


def _normalize_verdict(v: str) -> str:
    """Normalize verdict values."""
    val = (v or "").strip().lower()
    return val if val in {"present", "absent", "uncertain"} else "uncertain"


def _truncate(text: str, n: int = 600) -> str:
    """Bound text output length for logs and CSV."""
    return (text or "").strip()[:n]


def _clamp01(x: float) -> float:
    """Clamp float to [0,1]."""
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _log_judge_progress(message: str) -> None:
    """Emit line-buffered judge progress for the GUI execution log."""
    print(message, flush=True)


def _resolve_strategy_plan(cfg: Dict[str, Any], explicit_strategy: Optional[str]) -> tuple[list[str], str, float]:
    """Resolve selected strategies and vote policy."""
    if explicit_strategy:
        name = explicit_strategy if explicit_strategy in _ALLOWED_STYLES else _DEFAULT_STRATEGY
        return [name], "single", 0.0

    llm_cfg = _deep_get(cfg, ["llm_judge"], {}) or {}

    mode = os.getenv("GLACIER_JUDGE_STRATEGY_MODE", "").strip().lower() or str(llm_cfg.get("strategy_mode", "ensemble")).strip().lower()
    primary = os.getenv("GLACIER_JUDGE_PRIMARY_STRATEGY", "").strip() or str(llm_cfg.get("primary_strategy", _DEFAULT_STRATEGY)).strip()
    selected = _parse_csv_env("GLACIER_JUDGE_SELECTED_STRATEGIES")

    enabled: list[str] = []
    strategies_cfg = llm_cfg.get("strategies", {})
    if isinstance(strategies_cfg, dict):
        for name, block in strategies_cfg.items():
            if isinstance(block, dict) and bool(block.get("enabled", False)):
                enabled.append(str(name))

    ensemble_cfg = llm_cfg.get("ensemble", {}) if isinstance(llm_cfg.get("ensemble"), dict) else {}
    vote_rule = os.getenv("GLACIER_JUDGE_VOTE_RULE", "").strip().lower() or str(ensemble_cfg.get("vote_rule", "majority")).strip().lower()
    min_conf = _coerce_float(os.getenv("GLACIER_JUDGE_MIN_CONFIDENCE", ""), _coerce_float(ensemble_cfg.get("min_confidence", 0.0), 0.0))

    if mode == "single":
        name = primary if primary in _ALLOWED_STYLES else _DEFAULT_STRATEGY
        return [name], "single", 0.0

    names = selected or enabled or list(_DEFAULT_ENSEMBLE)
    names = [n for n in names if n in _ALLOWED_STYLES]
    return (names or list(_DEFAULT_ENSEMBLE), vote_rule or "majority", min_conf)


def _build_prompt(
    *,
    style: str,
    snippet_id: str,
    vuln_type: str,
    cwe: str,
    baseline_code: str,
    edited_code: str,
    gold_code: str,
) -> tuple[str, str]:
    """Build strategy-specific system and user prompts."""
    context = f"""
SNIPPET_ID: {snippet_id}
VULN_TYPE: {vuln_type}
CWE: {cwe}

BASELINE (known vulnerable):
```python
{baseline_code}
```

GOLD (secure reference, not mandatory exact match):
```python
{gold_code}
```

EDITED (judge this):
```python
{edited_code}
```
""".strip()

    output = _build_output_contract()
    policy = _build_decision_policy(vuln_type, cwe)
    shared_user_tail = f"""
{policy}

{output}
""".strip()

    if style == "zero_shot":
        system = (
            _build_base_system_prompt()
            + " Use direct classification without hidden multi-step decomposition. Return JSON only."
        )
        user = f"""
{context}

Task:
Classify whether EDITED still contains {vuln_type} ({cwe}).
Give a terse rationale and concrete evidence from EDITED.

{shared_user_tail}
""".strip()
        return system, user

    if style == "few_shot":
        system = (
            _build_base_system_prompt()
            + " Use the examples as decision anchors, then apply the same logic to EDITED. Return JSON only."
        )
        user = f"""
{context}

Few-shot examples (security semantics over syntax):
- SQLi present example pattern:
  query = "SELECT * FROM users WHERE id = " + user_id
  cur.execute(query)
- SQLi absent example pattern:
  query = "SELECT * FROM users WHERE id = ?"
  cur.execute(query, (user_id,))
- CMDi present example pattern:
  os.system("grep " + user_value)
  subprocess.run("ls " + user_value, shell=True)
- CMDi absent example pattern:
  subprocess.run(["grep", user_value], shell=False, check=False)

Apply the same reasoning pattern to EDITED only.

{shared_user_tail}
""".strip()
        return system, user

    if style == "self_consistency":
        system = (
            _build_base_system_prompt()
            + " Produce one independent judgment per call. Do not assume prior attempts. Return JSON only."
        )
        user = f"""
{context}

Task:
Produce an independent, defensible judgment for this single pass.
Prioritize: input source -> transformation -> sink reachability -> mitigation strength.

{shared_user_tail}
""".strip()
        return system, user

    # cot
    system = (
        _build_base_system_prompt()
        + " Think step-by-step privately and do not reveal chain-of-thought. Return JSON only."
    )
    user = f"""
{context}

Checklist:
1) Identify the untrusted input source(s).
2) Trace whether input can reach a CWE-relevant sink.
3) Check whether mitigation blocks exploitation for this CWE class.
4) Calibrate confidence by evidence quality and completeness.

{shared_user_tail}
""".strip()
    return system, user

def _judge_once(
    *,
    model: str,
    url: str,
    timeout_s: float,
    system_prompt: str,
    user_prompt: str,
    options: Dict[str, Any],
    strategy_name: str,
) -> JudgeResult:
    """Execute one judge call and normalize output."""
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": user_prompt,
        "system": system_prompt,
        "stream": False,
        "options": options,
    }

    try:
        resp = _post_json(url, payload, timeout=timeout_s)
        text = (resp.get("response") or "").strip()
        obj = _extract_first_json_object(text) or _extract_first_json_object(json.dumps(resp))

        if not obj:
            return JudgeResult(
                verdict="uncertain",
                confidence=0.0,
                rationale="No JSON returned by judge.",
                evidence="",
                raw_json={"_error": "no_json", "response_text": text[:2000], "raw": resp},
                strategy_name=strategy_name,
            )

        verdict = _normalize_verdict(str(obj.get("verdict", "uncertain")))
        conf = _clamp01(_coerce_float(obj.get("confidence", 0.0), 0.0))
        return JudgeResult(
            verdict=verdict,
            confidence=conf,
            rationale=_truncate(str(obj.get("rationale", ""))),
            evidence=_truncate(str(obj.get("evidence", ""))),
            raw_json=obj,
            strategy_name=strategy_name,
        )
    except Exception as exc:
        return JudgeResult(
            verdict="uncertain",
            confidence=0.0,
            rationale="Judge call failed.",
            evidence=_truncate(str(exc)),
            raw_json={"_error": "exception", "exception": str(exc)},
            strategy_name=strategy_name,
        )


def _vote_strategy_results(results: list[JudgeResult], vote_rule: str, min_confidence: float) -> tuple[str, float, JudgeResult]:
    """Vote strategy outputs into a final verdict."""
    if len(results) == 1:
        only = results[0]
        return only.verdict, only.confidence, only

    by: dict[str, list[JudgeResult]] = {"present": [], "absent": [], "uncertain": []}
    for r in results:
        by[_normalize_verdict(r.verdict)].append(r)

    # Vote policy defines how multiple strategy outputs collapse to one verdict.
    if vote_rule == "highest_confidence":
        top = max(results, key=lambda r: r.confidence)
        final = top.verdict if top.confidence >= min_confidence else "uncertain"
    elif vote_rule == "conservative_present":
        final = "present" if by["present"] else ("absent" if by["absent"] and not by["uncertain"] else "uncertain")
    else:
        # majority default
        counts = {k: len(v) for k, v in by.items()}
        max_count = max(counts.values())
        winners = [k for k, c in counts.items() if c == max_count]
        final = winners[0] if len(winners) == 1 else "uncertain"

    candidates = by.get(final, [])
    rep = max(candidates, key=lambda r: r.confidence) if candidates else max(results, key=lambda r: r.confidence)
    conf = sum(r.confidence for r in candidates) / len(candidates) if candidates else rep.confidence
    return _normalize_verdict(final), _clamp01(conf), rep


def _run_self_consistency(
    *,
    snippet_id: str,
    model: str,
    url: str,
    timeout_s: float,
    system_prompt: str,
    user_prompt: str,
    options: Dict[str, Any],
    samples: int,
) -> JudgeResult:
    """Run multiple independent samples and vote internally."""
    attempts: list[JudgeResult] = []
    for i in range(samples):
        # Adjust seed per sample so runs are not identical.
        local_opts = dict(options)
        if "seed" in local_opts:
            local_opts["seed"] = _coerce_int(local_opts.get("seed"), 42) + i
        _log_judge_progress(f"[judge] snippet={snippet_id} strategy=self_consistency sample={i + 1}/{samples} start")
        result = _judge_once(
            model=model,
            url=url,
            timeout_s=timeout_s,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            options=local_opts,
            strategy_name="self_consistency",
        )
        attempts.append(result)
        _log_judge_progress(
            f"[judge] snippet={snippet_id} strategy=self_consistency sample={i + 1}/{samples} done verdict={result.verdict} confidence={result.confidence:.2f}"
        )

    final_verdict, final_conf, rep = _vote_strategy_results(attempts, vote_rule="majority", min_confidence=0.0)
    _log_judge_progress(
        f"[judge] snippet={snippet_id} strategy=self_consistency final verdict={final_verdict} confidence={final_conf:.2f}"
    )
    return JudgeResult(
        verdict=final_verdict,
        confidence=final_conf,
        rationale=rep.rationale,
        evidence=rep.evidence,
        raw_json={
            "final": {"verdict": final_verdict, "confidence": final_conf, "vote_rule": "majority"},
            "attempts": [a.raw_json for a in attempts],
        },
        strategy_name="self_consistency",
        strategy_results={
            f"sample_{i+1}": {"verdict": a.verdict, "confidence": a.confidence, "rationale": a.rationale, "evidence": a.evidence}
            for i, a in enumerate(attempts)
        },
        vote_rule="majority",
    )


def judge_edited_code_with_ollama(
    *,
    snippet_id: str,
    vuln_type: str,
    cwe: str,
    baseline_code: str,
    edited_code: str,
    gold_code: str,
    model: Optional[str] = None,
    ollama_url: Optional[str] = None,
    timeout_s: Optional[float] = None,
    config_path: Optional[str | Path] = None,
    gen_options: Optional[Dict[str, Any]] = None,
    strategy: Optional[str] = None,
) -> JudgeResult:
    """Main judge entrypoint used by analyze_edits."""
    cfg = _load_yaml_config(config_path)

    default_model = str(_deep_get(cfg, ["llm_judge", "model"], "qwen2.5-coder:7b-instruct"))
    default_url = str(_deep_get(cfg, ["llm_judge", "ollama_url"], "http://localhost:11434/api/generate"))
    default_timeout = _coerce_float(_deep_get(cfg, ["llm_judge", "timeout_seconds"], 90), 90.0)

    model_final = (model or os.getenv("GLACIER_JUDGE_MODEL", "").strip() or default_model).strip()
    url_final = (ollama_url or os.getenv("GLACIER_OLLAMA_URL", "").strip() or default_url).strip()
    timeout_final = float(timeout_s if timeout_s is not None else default_timeout)

    # Combine defaults + config + env json + direct overrides.
    defaults: Dict[str, Any] = {"temperature": 0.0, "top_p": 0.1, "num_predict": 350}
    llm_cfg = _deep_get(cfg, ["llm_judge"], {}) or {}

    opts_cfg: Dict[str, Any] = {}
    if isinstance(llm_cfg, dict):
        for key in [
            "temperature",
            "top_p",
            "top_k",
            "num_predict",
            "repeat_penalty",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "num_ctx",
        ]:
            if key in llm_cfg:
                opts_cfg[key] = llm_cfg[key]

    env_opts = _parse_json_env("GLACIER_JUDGE_OPTIONS_JSON")

    base_opts: Dict[str, Any] = {}
    base_opts.update(defaults)
    base_opts.update(opts_cfg)
    base_opts.update(env_opts)
    if gen_options:
        base_opts.update(gen_options)

    # Resolve which strategy (or strategy set) to run for this snippet.
    strategy_names, vote_rule, min_conf = _resolve_strategy_plan(cfg, explicit_strategy=strategy)
    results: list[JudgeResult] = []

    sc_samples = _coerce_int(os.getenv("GLACIER_JUDGE_SELF_CONSISTENCY_SAMPLES", "5"), 5)
    if sc_samples < 1:
        sc_samples = 1

    for name in strategy_names:
        system_prompt, user_prompt = _build_prompt(
            style=name,
            snippet_id=snippet_id,
            vuln_type=vuln_type,
            cwe=cwe,
            baseline_code=baseline_code,
            edited_code=edited_code,
            gold_code=gold_code,
        )

        if name == "self_consistency":
            _log_judge_progress(f"[judge] snippet={snippet_id} strategy=self_consistency start samples={sc_samples}")
            results.append(
                _run_self_consistency(
                    snippet_id=snippet_id,
                    model=model_final,
                    url=url_final,
                    timeout_s=timeout_final,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    options=base_opts,
                    samples=sc_samples,
                )
            )
        else:
            _log_judge_progress(f"[judge] snippet={snippet_id} strategy={name} start")
            result = _judge_once(
                model=model_final,
                url=url_final,
                timeout_s=timeout_final,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                options=base_opts,
                strategy_name=name,
            )
            results.append(result)
            _log_judge_progress(
                f"[judge] snippet={snippet_id} strategy={name} done verdict={result.verdict} confidence={result.confidence:.2f}"
            )

    final_verdict, final_confidence, representative = _vote_strategy_results(results, vote_rule, min_conf)
    _log_judge_progress(
        f"[judge] snippet={snippet_id} ensemble vote_rule={vote_rule} verdict={final_verdict} confidence={final_confidence:.2f}"
    )

    strategy_map = {
        r.strategy_name: {
            "verdict": r.verdict,
            "confidence": r.confidence,
            "rationale": r.rationale,
            "evidence": r.evidence,
            "raw_json": r.raw_json,
        }
        for r in results
    }

    return JudgeResult(
        verdict=final_verdict,
        confidence=final_confidence,
        rationale=representative.rationale,
        evidence=representative.evidence,
        raw_json={
            "final": {
                "verdict": final_verdict,
                "confidence": final_confidence,
                "vote_rule": vote_rule,
                "representative_strategy": representative.strategy_name,
            },
            "per_strategy": strategy_map,
        },
        strategy_name=representative.strategy_name if len(results) == 1 else "ensemble",
        strategy_results=strategy_map,
        vote_rule=vote_rule,
    )



