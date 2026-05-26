"""Heuristic detectors for study-scoped SQLi and CMDi patterns.

These detectors trade completeness for reproducibility and speed. They are
study instruments (not general-purpose static analyzers), so rules are explicit
and easy to audit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class DetectorResult:
    """Normalized detector output for one snippet and vulnerability class."""
    vuln_type: str                 # "SQLi" or "CMDi"
    risky_hits: List[str]          # regex patterns that matched
    safe_hits: List[str]           # regex patterns that matched
    verdict: str                   # "present" | "absent" | "uncertain"


def _as_text(text_or_path: str) -> str:
    """
    Accept either raw source text OR a filesystem path.
    If it's a path that exists, read it. Otherwise treat as text.
    """
    try:
        p = Path(text_or_path)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return str(text_or_path)


def _match_any(patterns: List[str], text: str) -> List[str]:
    """Return the subset of regex patterns that match the given text."""
    hits: List[str] = []
    for pat in patterns:
        if re.search(pat, text, flags=re.MULTILINE):
            hits.append(pat)
    return hits


# -------------------------
# SQLi (CWE-89) detector
# -------------------------

# Unsafe SQL string construction signals by language family.
SQLI_RISKY_SQL_BUILD_PYTHON = [
    # f-strings that look like SQL
    r"""(?is)f["']\s*(select|insert|update|delete)\b.*\{.*\}.*["']""",
    # .format(...) usage
    r"(?is)\.\s*format\s*\(",
    # "%" formatting with SQL keywords
    r"""(?is)["']\s*(select|insert|update|delete)\b.*%[sd].*["']\s*%""",
    # concatenation around SQL
    r"""(?is)["']\s*(select|insert|update|delete)\b.*["']\s*\+\s*""",
    r"""(?is)\+\s*["']\s*(from|where|and|or)\b""",
]

# execute(...) shape: unparameterized call vs parameterized call
SQLI_RISKY_EXECUTE_PYTHON = [
    # execute(one_argument) (very broad, but useful as a flag)
    r"(?is)\bexecute\s*\(\s*[^,\n)]+\s*\)",
]
SQLI_SAFE_EXECUTE_PYTHON = [
    # execute(sql, params) / execute(sql, {...})
    r"(?is)\bexecute\s*\(\s*[^,\n)]+\s*,\s*[^)]+\)",
    # named parameters in query text (:name or %(name)s)
    r"(?is)[:][a-zA-Z_]\w+",
    r"(?is)%\([a-zA-Z_]\w*\)s",
    # sqlite qmark placeholder
    r"(?s)\?",
]

SQLI_RISKY_SQL_BUILD_JAVA = [
    r'(?is)"\s*(select|insert|update|delete)\b[^"]*"\s*\+',
    r'(?is)\+\s*"[^"]*\b(from|where|and|or)\b',
]
SQLI_RISKY_EXECUTE_JAVA = [
    r"(?is)\bcreateStatement\s*\(",
    r"(?is)\bexecute(Query|Update)\s*\(\s*[^,]+\s*\)",
]
SQLI_SAFE_EXECUTE_JAVA = [
    r"(?is)\bprepareStatement\s*\(",
    r"(?is)\bset(String|Int|Long|Double|Float|Boolean|Object)\s*\(",
]

SQLI_RISKY_SQL_BUILD_NATIVE = [
    r"(?is)\b(snprintf|sprintf)\s*\([^;]*(select|insert|update|delete)\b",
    r'(?is)"\s*(select|insert|update|delete)\b[^"]*"\s*\+',
    r"(?is)\bstr(cat|ncat)\s*\(",
]
SQLI_RISKY_EXECUTE_NATIVE = [
    r"(?is)\bsqlite3_exec\s*\(",
    r"(?is)\bmysql_query\s*\(",
]
SQLI_SAFE_EXECUTE_NATIVE = [
    r"(?is)\bsqlite3_prepare_v2\s*\(",
    r"(?is)\bsqlite3_bind_[a-z_]+\s*\(",
    r"(?is)\bmysql_stmt_prepare\s*\(",
    r"(?is)\bmysql_stmt_bind_param\s*\(",
]


def _normalize_language(language: str) -> str:
    """Collapse language labels into detector rule families."""
    text = (language or "").strip().lower()
    if text in {"py", "python"}:
        return "python"
    if text in {"java"}:
        return "java"
    if text in {"c", "cpp", "c++", "cc", "cxx"}:
        return "native"
    return ""


def detect_sqli(text_or_path: str, *, language: str = "") -> DetectorResult:
    """
    Heuristic SQLi detector for the supported study language families.
    """
    text = _as_text(text_or_path)
    family = _normalize_language(language)

    if family == "java":
        risky_build = SQLI_RISKY_SQL_BUILD_JAVA
        risky_exec = SQLI_RISKY_EXECUTE_JAVA
        safe_exec = SQLI_SAFE_EXECUTE_JAVA
    elif family == "native":
        risky_build = SQLI_RISKY_SQL_BUILD_NATIVE
        risky_exec = SQLI_RISKY_EXECUTE_NATIVE
        safe_exec = SQLI_SAFE_EXECUTE_NATIVE
    else:
        risky_build = SQLI_RISKY_SQL_BUILD_PYTHON
        risky_exec = SQLI_RISKY_EXECUTE_PYTHON
        safe_exec = SQLI_SAFE_EXECUTE_PYTHON

    risky_hits = _match_any(risky_build, text) + _match_any(risky_exec, text)
    safe_hits = _match_any(safe_exec, text)

    has_param_execute = bool(safe_exec and re.search(safe_exec[0], text, flags=re.MULTILINE))
    has_unsafe_build = bool(_match_any(risky_build, text))
    has_unparam_execute = bool(_match_any(risky_exec, text))

    # Decision rule:
    # - Parameterized execute AND no unsafe-build => absent
    # - Unsafe-build OR unparameterized execute => present
    # - Otherwise uncertain (unless placeholders strongly imply safety)
    if has_param_execute and not has_unsafe_build:
        verdict = "absent"
    elif has_unsafe_build or has_unparam_execute:
        verdict = "present"
    elif safe_hits:
        verdict = "absent"
    else:
        verdict = "uncertain"

    return DetectorResult("SQLi", risky_hits, safe_hits, verdict)


# -------------------------
# CMDi (CWE-78) detector
# -------------------------

CMDI_RISKY_PYTHON = [
    r"os\.system\s*\(",
    r"os\.popen\s*\(",
    r"subprocess\.(run|call|Popen)\s*\([^)]*shell\s*=\s*True",
]

# Study-specific "safe": subprocess used AND no shell=True anywhere in the file
CMDI_SUBPROCESS_ANY = r"subprocess\.(run|call|Popen)\s*\("

CMDI_RISKY_JAVA = [
    r"(?is)\bRuntime\.getRuntime\s*\(\)\.exec\s*\(",
    r'(?is)\bnew\s+ProcessBuilder\s*\([^)]*("sh"|"/bin/sh"|"cmd"|\"cmd\.exe\"|"-c"|"/c")',
]
CMDI_PROCESS_BUILDER_ANY = r"(?is)\bnew\s+ProcessBuilder\s*\("

CMDI_RISKY_NATIVE = [
    r"(?is)\bsystem\s*\(",
    r"(?is)\b_popen\s*\(",
    r"(?is)\bpopen\s*\(",
]
CMDI_SAFE_NATIVE = [
    r"(?is)\bexecv(e|p)?\s*\(",
    r"(?is)\bposix_spawn(p)?\s*\(",
]


def detect_cmdi(text_or_path: str, *, language: str = "") -> DetectorResult:
    """
    Heuristic CMDi detector for the supported study language families.
    """
    text = _as_text(text_or_path)
    family = _normalize_language(language)

    risky_hits: List[str] = []
    safe_hits: List[str] = []

    if family == "java":
        risky_hits = _match_any(CMDI_RISKY_JAVA, text)
        if re.search(CMDI_PROCESS_BUILDER_ANY, text, flags=re.MULTILINE) and not risky_hits:
            safe_hits = ["process_builder_without_shell_wrapper"]
    elif family == "native":
        risky_hits = _match_any(CMDI_RISKY_NATIVE, text)
        safe_hits = _match_any(CMDI_SAFE_NATIVE, text)
    else:
        risky_hits = _match_any(CMDI_RISKY_PYTHON, text)
        if re.search(CMDI_SUBPROCESS_ANY, text, flags=re.MULTILINE) and not re.search(r"shell\s*=\s*True", text):
            safe_hits = ["subprocess_without_shell_true"]

    if risky_hits:
        verdict = "present"
    elif safe_hits:
        verdict = "absent"
    else:
        verdict = "uncertain"

    return DetectorResult("CMDi", risky_hits, safe_hits, verdict)


