"""Researcher-side LLM judge for edited snippet scoring."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error, request

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


SUPPORTED_STRATEGIES = (
    "cot",
    "zero_shot",
    "few_shot",
    "adaptive_cot",
    "self_verification",
    "self_consistency",
)
SUPPORTED_PARSER_MODES = ("strict_json", "embedded_json", "tolerant_json")
SUPPORTED_VOTE_RULES = ("majority", "conservative_present", "highest_confidence")

_DEFAULT_STRATEGY = "cot"
_DEFAULT_ENSEMBLE = ["cot", "zero_shot", "few_shot", "self_verification"]


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
    parser_mode: str = "embedded_json"


def _code_fence_language(language: str) -> str:
    """Return a markdown code-fence label for the snippet language."""
    text = (language or "").strip().lower()
    if text in {"cpp", "c++", "cc", "cxx"}:
        return "cpp"
    if text in {"c", "java", "python"}:
        return text
    return text or "text"


def normalize_parser_mode(value: str | None) -> str:
    """Map parser aliases to the internal names used by the judge."""
    key = str(value or "embedded_json").strip().lower()
    aliases = {
        "strict": "strict_json",
        "strict_json": "strict_json",
        "json_only": "strict_json",
        "embedded": "embedded_json",
        "embedded_json": "embedded_json",
        "structured": "embedded_json",
        "tolerant": "tolerant_json",
        "tolerant_json": "tolerant_json",
        "full": "tolerant_json",
    }
    return aliases.get(key, key)


def _normalize_vote_rule(value: str | None) -> str:
    """Normalize vote-rule text before it is used."""
    key = str(value or "majority").strip().lower()
    return key if key in SUPPORTED_VOTE_RULES else "majority"


def _build_output_contract() -> str:
    """Return the JSON contract used by all judge prompts."""
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
    """Describe the verdict policy used by the study."""
    return f"""
Decision policy for {vuln_type} ({cwe}):
- "present": untrusted input can still reach a dangerous sink for this CWE class.
- "absent": mitigation in EDITED prevents this CWE class, even if it differs from GOLD.
- "uncertain": evidence is mixed, incomplete, or too ambiguous for a defensible yes/no.

Important constraints:
- GOLD is a reference point, not an exact answer key.
- Judge EDITED on security semantics, not formatting or style.
- Prefer uncertainty over a claim that is not well supported by the code.
""".strip()


def _supports_injection_examples(vuln_type: str, cwe: str) -> bool:
    """Return True when the SQLi/CMDi anchor examples fit the target CWE."""
    joined = f"{vuln_type} {cwe}".lower()
    return any(token in joined for token in ("sqli", "cmdi", "cwe-89", "cwe-78", "sql injection", "command injection"))


def _build_base_system_prompt() -> str:
    """Return the shared evaluator persona."""
    return (
        "You are a senior application-security reviewer and research rater. "
        "Produce consistent, evidence-based judgments for edited code. "
        "Do not speculate, do not invent missing context, and do not return markdown."
    )


def _find_project_root(start: Optional[Path] = None) -> Path:
    """Walk upward until config or repo markers are found."""
    here = (start or Path.cwd()).resolve()
    for path in [here] + list(here.parents):
        if (path / "config.yaml").exists() or (path / ".git").exists() or (path / "pyproject.toml").exists():
            return path
    return here


def _load_yaml_config(config_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Load config YAML into a plain dictionary."""
    path = (_find_project_root() / "config.yaml") if config_path is None else Path(config_path).expanduser().resolve()
    if not path.exists():
        return {}

    if yaml is None:
        raise RuntimeError("PyYAML is required to load config.yaml.")

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _deep_get(d: Dict[str, Any], keys: list[str], default: Any = None) -> Any:
    """Nested dictionary getter with a default fallback."""
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Merge override into base without mutating either input."""
    merged: Dict[str, Any] = dict(base)
    for key, value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(base_value, value)
        else:
            merged[key] = value
    return merged


def _coerce_float(value: Any, default: float) -> float:
    """Convert value to float with a fallback."""
    try:
        return float(value)
    except Exception:
        return default


def _coerce_int(value: Any, default: int) -> int:
    """Convert value to int with a fallback."""
    try:
        return int(value)
    except Exception:
        return default


def _parse_csv_env(name: str) -> list[str]:
    """Parse one comma-separated environment variable."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [value.strip() for value in raw.split(",") if value.strip()]


def _parse_json_env(name: str) -> Dict[str, Any]:
    """Parse one JSON object from the environment."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _read_json_file(path: Path) -> Dict[str, Any]:
    """Read one JSON object from disk."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_effective_config(
    config_path: Optional[str | Path] = None,
    *,
    use_frozen_config: Optional[bool] = None,
) -> Dict[str, Any]:
    """Load base config and optionally merge a frozen judge configuration."""
    cfg = _load_yaml_config(config_path)
    llm_cfg = cfg.get("llm_judge", {}) if isinstance(cfg.get("llm_judge"), dict) else {}

    if use_frozen_config is None:
        env_value = os.getenv("GLACIER_JUDGE_USE_FROZEN", "").strip().lower()
        if env_value:
            use_frozen = env_value in {"1", "true", "t", "yes", "y"}
        else:
            use_frozen = bool(llm_cfg.get("use_frozen_config", False))
    else:
        use_frozen = bool(use_frozen_config)

    if not use_frozen:
        return cfg

    root = _find_project_root(Path(config_path).parent if config_path else None)
    freeze_value = os.getenv("GLACIER_JUDGE_FREEZE_PATH", "").strip() or str(
        llm_cfg.get("frozen_config_path", "data/aggregated/judge_freeze.json")
    ).strip()
    freeze_path = Path(freeze_value)
    if not freeze_path.is_absolute():
        freeze_path = (root / freeze_path).resolve()
    if not freeze_path.exists():
        return cfg

    frozen_payload = _read_json_file(freeze_path)
    frozen_block = frozen_payload.get("llm_judge")
    if not isinstance(frozen_block, dict):
        return cfg

    merged = dict(cfg)
    merged["llm_judge"] = _deep_merge(llm_cfg, frozen_block)
    merged["judge_freeze"] = {
        "path": str(freeze_path),
        "audit_summary_path": str(frozen_payload.get("audit_summary_path", "") or "").strip(),
        "recommended_config_id": str(frozen_payload.get("recommended_config_id", "") or "").strip(),
        "generated_at": str(frozen_payload.get("generated_at", "") or "").strip(),
    }
    return merged


def _post_json(url: str, payload: Dict[str, Any], timeout: float = 90.0) -> Dict[str, Any]:
    """POST JSON to Ollama and parse the response."""
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTPError {exc.code}: {body[:2000]}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"URLError: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Response was not JSON: {raw[:2000]}") from exc


def _extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Find the first valid JSON object in mixed text."""
    if not text:
        return None

    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1].strip()
                try:
                    obj = json.loads(candidate)
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None

    return None


def _extract_fenced_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON object from a fenced code block when present."""
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL):
        candidate = match.group(1).strip()
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _extract_json_object(
    response_text: str,
    raw_payload: Dict[str, Any],
    parser_mode: str,
) -> Optional[Dict[str, Any]]:
    """Extract one JSON object from the model output under the chosen parser mode."""
    mode = normalize_parser_mode(parser_mode)
    text = str(response_text or "").strip()

    if mode == "strict_json":
        if not text.startswith("{") or not text.endswith("}"):
            return None
        try:
            obj = json.loads(text)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    if mode == "embedded_json":
        return _extract_first_json_object(text)

    obj = _extract_first_json_object(text)
    if obj:
        return obj

    obj = _extract_fenced_json_object(text)
    if obj:
        return obj

    return _extract_first_json_object(json.dumps(raw_payload))


def _normalize_verdict(value: str) -> str:
    """Normalize verdict text to the allowed set."""
    text = (value or "").strip().lower()
    return text if text in {"present", "absent", "uncertain"} else "uncertain"


def _truncate(text: str, limit: int = 600) -> str:
    """Bound stored text so CSV and JSON outputs stay manageable."""
    return (text or "").strip()[:limit]


def _clamp01(value: float) -> float:
    """Clamp a float to the [0, 1] interval."""
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _log_judge_progress(message: str) -> None:
    """Emit line-buffered judge progress for the GUI log pane."""
    print(message, flush=True)


def _resolve_strategy_plan(
    cfg: Dict[str, Any],
    *,
    explicit_strategy: Optional[str],
    selected_strategies: Optional[list[str]],
    vote_rule: Optional[str],
    min_confidence: Optional[float],
) -> tuple[list[str], str, float]:
    """Resolve which strategies and vote policy the current call should use."""
    if explicit_strategy:
        name = explicit_strategy if explicit_strategy in SUPPORTED_STRATEGIES else _DEFAULT_STRATEGY
        return [name], "single", 0.0

    if selected_strategies is not None:
        names = [name for name in selected_strategies if name in SUPPORTED_STRATEGIES]
        if len(names) <= 1:
            single = names[0] if names else _DEFAULT_STRATEGY
            return [single], "single", 0.0
        return names, _normalize_vote_rule(vote_rule), float(min_confidence or 0.0)

    llm_cfg = _deep_get(cfg, ["llm_judge"], {}) or {}
    mode = os.getenv("GLACIER_JUDGE_STRATEGY_MODE", "").strip().lower() or str(
        llm_cfg.get("strategy_mode", "ensemble")
    ).strip().lower()
    primary = os.getenv("GLACIER_JUDGE_PRIMARY_STRATEGY", "").strip() or str(
        llm_cfg.get("primary_strategy", _DEFAULT_STRATEGY)
    ).strip()
    env_selected = _parse_csv_env("GLACIER_JUDGE_SELECTED_STRATEGIES")

    enabled: list[str] = []
    strategies_cfg = llm_cfg.get("strategies", {})
    if isinstance(strategies_cfg, dict):
        for name, block in strategies_cfg.items():
            if isinstance(block, dict) and bool(block.get("enabled", False)):
                enabled.append(str(name))

    ensemble_cfg = llm_cfg.get("ensemble", {}) if isinstance(llm_cfg.get("ensemble"), dict) else {}
    final_vote_rule = os.getenv("GLACIER_JUDGE_VOTE_RULE", "").strip().lower() or str(
        ensemble_cfg.get("vote_rule", "majority")
    ).strip().lower()
    final_min_conf = _coerce_float(
        os.getenv("GLACIER_JUDGE_MIN_CONFIDENCE", ""),
        _coerce_float(ensemble_cfg.get("min_confidence", 0.0), 0.0),
    )

    if mode == "single":
        name = primary if primary in SUPPORTED_STRATEGIES else _DEFAULT_STRATEGY
        return [name], "single", 0.0

    names = env_selected or enabled or list(_DEFAULT_ENSEMBLE)
    names = [name for name in names if name in SUPPORTED_STRATEGIES]
    return (names or list(_DEFAULT_ENSEMBLE), _normalize_vote_rule(final_vote_rule), final_min_conf)


def _build_prompt(
    *,
    style: str,
    snippet_id: str,
    vuln_type: str,
    cwe: str,
    language: str,
    baseline_code: str,
    edited_code: str,
    gold_code: str,
) -> tuple[str, str]:
    """Build the system and user prompts for one strategy."""
    fence = _code_fence_language(language)
    context = f"""
SNIPPET_ID: {snippet_id}
VULN_TYPE: {vuln_type}
CWE: {cwe}
LANGUAGE: {language or "unknown"}

BASELINE (known vulnerable):
```{fence}
{baseline_code}
```

GOLD (secure reference, not mandatory exact match):
```{fence}
{gold_code}
```

EDITED (judge this):
```{fence}
{edited_code}
```
""".strip()

    output = _build_output_contract()
    policy = _build_decision_policy(vuln_type, cwe)
    shared_tail = f"""
{policy}

{output}
""".strip()

    if style == "zero_shot":
        system = _build_base_system_prompt() + " Use direct classification and return JSON only."
        user = f"""
{context}

Task:
Classify whether EDITED still contains {vuln_type} ({cwe}).
Give a short rationale and cite concrete evidence from EDITED.

{shared_tail}
""".strip()
        return system, user

    if style == "few_shot":
        system = _build_base_system_prompt() + " Use the examples as anchors and return JSON only."
        if _supports_injection_examples(vuln_type, cwe):
            examples = """
Few-shot anchors:
- SQLi present:
  query = "SELECT * FROM users WHERE id = " + user_id
  cursor.execute(query)
- SQLi absent:
  query = "SELECT * FROM users WHERE id = ?"
  cursor.execute(query, (user_id,))
- CMDi present:
  os.system("grep " + user_value)
- CMDi absent:
  subprocess.run(["grep", user_value], shell=False, check=False)

Apply the same reasoning pattern to EDITED only.
""".strip()
        else:
            examples = """
Few-shot anchors:
- Present: the risky data path from source to sink still exists in the edited code.
- Absent: the edited code blocks or neutralizes that path for the target CWE.
- Uncertain: the visible code does not support a defensible yes/no answer.

Apply the same reasoning pattern to EDITED only.
""".strip()
        user = f"""
{context}

{examples}

{shared_tail}
""".strip()
        return system, user

    if style == "adaptive_cot":
        system = _build_base_system_prompt() + " Adjust reasoning depth to the case and return JSON only."
        user = f"""
{context}

Task:
1) If the case is straightforward, keep the reasoning short.
2) If the mitigation is partial, indirect, or context-sensitive, reason through the path in more detail.
3) Base the verdict on the edited code, not on stylistic similarity to GOLD.

{shared_tail}
""".strip()
        return system, user

    if style == "self_verification":
        system = _build_base_system_prompt() + " Analyze the case, check your own reasoning, and return JSON only."
        user = f"""
{context}

Self-check before you finalize the verdict:
1) Check whether you relied on assumptions not supported by the code.
2) Check whether you missed a remaining path from input to a dangerous sink.
3) Check whether your verdict matches the evidence you cited.
4) If you find a mistake, revise the answer before returning JSON.

{shared_tail}
""".strip()
        return system, user

    if style == "self_consistency":
        system = _build_base_system_prompt() + " Produce one independent judgment for this call and return JSON only."
        user = f"""
{context}

Task:
Produce one independent, defensible judgment for this pass.
Prioritize input source, transformation, sink reachability, and mitigation strength.

{shared_tail}
""".strip()
        return system, user

    system = _build_base_system_prompt() + " Reason privately, then return JSON only."
    user = f"""
{context}

Checklist:
1) Identify the untrusted input source or state transition that matters here.
2) Trace whether the risky path still reaches a CWE-relevant sink.
3) Decide whether the edit blocks exploitation for this CWE class.
4) Calibrate confidence by evidence quality and completeness.

{shared_tail}
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
    parser_mode: str,
) -> JudgeResult:
    """Execute one judge call and normalize the response."""
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
        obj = _extract_json_object(text, resp, parser_mode)

        if not obj:
            return JudgeResult(
                verdict="uncertain",
                confidence=0.0,
                rationale="No JSON returned by judge.",
                evidence="",
                raw_json={
                    "_error": "no_json",
                    "parser_mode": parser_mode,
                    "response_text": text[:2000],
                    "raw": resp,
                },
                strategy_name=strategy_name,
                parser_mode=parser_mode,
            )

        verdict = _normalize_verdict(str(obj.get("verdict", "uncertain")))
        confidence = _clamp01(_coerce_float(obj.get("confidence", 0.0), 0.0))
        return JudgeResult(
            verdict=verdict,
            confidence=confidence,
            rationale=_truncate(str(obj.get("rationale", ""))),
            evidence=_truncate(str(obj.get("evidence", ""))),
            raw_json={**obj, "_parser_mode": parser_mode},
            strategy_name=strategy_name,
            parser_mode=parser_mode,
        )
    except Exception as exc:
        return JudgeResult(
            verdict="uncertain",
            confidence=0.0,
            rationale="Judge call failed.",
            evidence=_truncate(str(exc)),
            raw_json={"_error": "exception", "_parser_mode": parser_mode, "exception": str(exc)},
            strategy_name=strategy_name,
            parser_mode=parser_mode,
        )


def _vote_strategy_results(
    results: list[JudgeResult],
    vote_rule: str,
    min_confidence: float,
) -> tuple[str, float, JudgeResult]:
    """Collapse one set of strategy outputs into a final verdict."""
    if len(results) == 1:
        only = results[0]
        return only.verdict, only.confidence, only

    by: dict[str, list[JudgeResult]] = {"present": [], "absent": [], "uncertain": []}
    for result in results:
        by[_normalize_verdict(result.verdict)].append(result)

    rule = _normalize_vote_rule(vote_rule)
    if rule == "highest_confidence":
        top = max(results, key=lambda result: result.confidence)
        final = top.verdict if top.confidence >= min_confidence else "uncertain"
    elif rule == "conservative_present":
        final = "present" if by["present"] else ("absent" if by["absent"] and not by["uncertain"] else "uncertain")
    else:
        counts = {key: len(value) for key, value in by.items()}
        max_count = max(counts.values())
        winners = [key for key, count in counts.items() if count == max_count]
        final = winners[0] if len(winners) == 1 else "uncertain"

    candidates = by.get(final, [])
    representative = max(candidates, key=lambda result: result.confidence) if candidates else max(
        results, key=lambda result: result.confidence
    )
    confidence = (
        sum(result.confidence for result in candidates) / len(candidates)
        if candidates
        else representative.confidence
    )
    return _normalize_verdict(final), _clamp01(confidence), representative


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
    parser_mode: str,
) -> JudgeResult:
    """Run multiple independent judge calls and vote them internally."""
    attempts: list[JudgeResult] = []
    for index in range(samples):
        local_options = dict(options)
        if "seed" in local_options:
            local_options["seed"] = _coerce_int(local_options.get("seed"), 42) + index
        _log_judge_progress(
            f"[judge] snippet={snippet_id} strategy=self_consistency sample={index + 1}/{samples} start"
        )
        result = _judge_once(
            model=model,
            url=url,
            timeout_s=timeout_s,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            options=local_options,
            strategy_name="self_consistency",
            parser_mode=parser_mode,
        )
        attempts.append(result)
        _log_judge_progress(
            f"[judge] snippet={snippet_id} strategy=self_consistency sample={index + 1}/{samples} "
            f"done verdict={result.verdict} confidence={result.confidence:.2f}"
        )

    final_verdict, final_confidence, representative = _vote_strategy_results(attempts, "majority", 0.0)
    _log_judge_progress(
        f"[judge] snippet={snippet_id} strategy=self_consistency final "
        f"verdict={final_verdict} confidence={final_confidence:.2f}"
    )
    return JudgeResult(
        verdict=final_verdict,
        confidence=final_confidence,
        rationale=representative.rationale,
        evidence=representative.evidence,
        raw_json={
            "final": {
                "verdict": final_verdict,
                "confidence": final_confidence,
                "vote_rule": "majority",
                "parser_mode": parser_mode,
            },
            "attempts": [attempt.raw_json for attempt in attempts],
        },
        strategy_name="self_consistency",
        strategy_results={
            f"sample_{index + 1}": {
                "verdict": attempt.verdict,
                "confidence": attempt.confidence,
                "rationale": attempt.rationale,
                "evidence": attempt.evidence,
            }
            for index, attempt in enumerate(attempts)
        },
        vote_rule="majority",
        parser_mode=parser_mode,
    )


def judge_edited_code_with_ollama(
    *,
    snippet_id: str,
    vuln_type: str,
    cwe: str,
    language: str,
    baseline_code: str,
    edited_code: str,
    gold_code: str,
    model: Optional[str] = None,
    ollama_url: Optional[str] = None,
    timeout_s: Optional[float] = None,
    config_path: Optional[str | Path] = None,
    gen_options: Optional[Dict[str, Any]] = None,
    strategy: Optional[str] = None,
    selected_strategies: Optional[list[str]] = None,
    vote_rule: Optional[str] = None,
    min_confidence: Optional[float] = None,
    parser_mode: Optional[str] = None,
    use_frozen_config: Optional[bool] = None,
) -> JudgeResult:
    """Run the LLM judge for one edited snippet."""
    cfg = _load_effective_config(config_path, use_frozen_config=use_frozen_config)
    llm_cfg = _deep_get(cfg, ["llm_judge"], {}) or {}

    default_model = str(llm_cfg.get("model", "qwen2.5-coder:7b-instruct"))
    default_url = str(llm_cfg.get("ollama_url", "http://localhost:11434/api/generate"))
    default_timeout = _coerce_float(llm_cfg.get("timeout_seconds", 90), 90.0)

    model_final = (model or os.getenv("GLACIER_JUDGE_MODEL", "").strip() or default_model).strip()
    url_final = (ollama_url or os.getenv("GLACIER_OLLAMA_URL", "").strip() or default_url).strip()
    timeout_final = float(timeout_s if timeout_s is not None else default_timeout)

    defaults: Dict[str, Any] = {"temperature": 0.0, "top_p": 0.1, "num_predict": 350}
    options_from_cfg: Dict[str, Any] = {}
    if isinstance(llm_cfg, dict):
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
                options_from_cfg[key] = llm_cfg[key]

    base_options: Dict[str, Any] = {}
    base_options.update(defaults)
    base_options.update(options_from_cfg)
    base_options.update(_parse_json_env("GLACIER_JUDGE_OPTIONS_JSON"))
    if gen_options:
        base_options.update(gen_options)

    parser_mode_final = normalize_parser_mode(
        parser_mode
        or os.getenv("GLACIER_JUDGE_PARSER_MODE", "").strip()
        or str(llm_cfg.get("parser_mode", "embedded_json")).strip()
    )
    strategy_names, final_vote_rule, final_min_conf = _resolve_strategy_plan(
        cfg,
        explicit_strategy=strategy,
        selected_strategies=selected_strategies,
        vote_rule=vote_rule,
        min_confidence=min_confidence,
    )

    sc_samples = _coerce_int(
        os.getenv("GLACIER_JUDGE_SELF_CONSISTENCY_SAMPLES", ""),
        _coerce_int(llm_cfg.get("self_consistency_samples", 5), 5),
    )
    if sc_samples < 1:
        sc_samples = 1

    results: list[JudgeResult] = []
    for name in strategy_names:
        system_prompt, user_prompt = _build_prompt(
            style=name,
            snippet_id=snippet_id,
            vuln_type=vuln_type,
            cwe=cwe,
            language=language,
            baseline_code=baseline_code,
            edited_code=edited_code,
            gold_code=gold_code,
        )

        if name == "self_consistency":
            _log_judge_progress(
                f"[judge] snippet={snippet_id} strategy=self_consistency start samples={sc_samples}"
            )
            results.append(
                _run_self_consistency(
                    snippet_id=snippet_id,
                    model=model_final,
                    url=url_final,
                    timeout_s=timeout_final,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    options=base_options,
                    samples=sc_samples,
                    parser_mode=parser_mode_final,
                )
            )
            continue

        _log_judge_progress(f"[judge] snippet={snippet_id} strategy={name} start")
        result = _judge_once(
            model=model_final,
            url=url_final,
            timeout_s=timeout_final,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            options=base_options,
            strategy_name=name,
            parser_mode=parser_mode_final,
        )
        results.append(result)
        _log_judge_progress(
            f"[judge] snippet={snippet_id} strategy={name} done verdict={result.verdict} "
            f"confidence={result.confidence:.2f}"
        )

    final_verdict, final_confidence, representative = _vote_strategy_results(
        results,
        final_vote_rule,
        final_min_conf,
    )
    _log_judge_progress(
        f"[judge] snippet={snippet_id} parser={parser_mode_final} vote_rule={final_vote_rule} "
        f"verdict={final_verdict} confidence={final_confidence:.2f}"
    )

    strategy_map = {
        result.strategy_name: {
            "verdict": result.verdict,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "evidence": result.evidence,
            "raw_json": result.raw_json,
            "parser_mode": result.parser_mode,
        }
        for result in results
    }

    freeze_info = cfg.get("judge_freeze", {}) if isinstance(cfg.get("judge_freeze"), dict) else {}
    return JudgeResult(
        verdict=final_verdict,
        confidence=final_confidence,
        rationale=representative.rationale,
        evidence=representative.evidence,
        raw_json={
            "final": {
                "verdict": final_verdict,
                "confidence": final_confidence,
                "vote_rule": final_vote_rule,
                "representative_strategy": representative.strategy_name,
                "parser_mode": parser_mode_final,
                "freeze_path": str(freeze_info.get("path", "") or ""),
                "freeze_config_id": str(freeze_info.get("recommended_config_id", "") or ""),
            },
            "per_strategy": strategy_map,
        },
        strategy_name=representative.strategy_name if len(results) == 1 else "ensemble",
        strategy_results=strategy_map,
        vote_rule=final_vote_rule,
        parser_mode=parser_mode_final,
    )
