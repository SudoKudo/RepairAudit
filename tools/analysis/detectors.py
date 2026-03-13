"""Heuristic detectors for study-scoped SQLi and CMDi patterns.

These detectors trade completeness for reproducibility and speed. They are
study instruments (not general-purpose static analyzers), so rules are explicit
and easy to audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List
import re


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

# Unsafe SQL string construction signals (Python DB-API style)
SQLI_RISKY_SQL_BUILD = [
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
SQLI_RISKY_EXECUTE = [
    # execute(one_argument) (very broad, but useful as a flag)
    r"(?is)\bexecute\s*\(\s*[^,\n)]+\s*\)",
]
SQLI_SAFE_EXECUTE = [
    # execute(sql, params) / execute(sql, {...})
    r"(?is)\bexecute\s*\(\s*[^,\n)]+\s*,\s*[^)]+\)",
    # named parameters in query text (:name or %(name)s)
    r"(?is)[:][a-zA-Z_]\w+",
    r"(?is)%\([a-zA-Z_]\w*\)s",
    # sqlite qmark placeholder
    r"(?s)\?",
]


def detect_sqli(text_or_path: str) -> DetectorResult:
    """
    Heuristic SQLi detector (Python DB-API).
    """
    text = _as_text(text_or_path)

    risky_hits = _match_any(SQLI_RISKY_SQL_BUILD, text) + _match_any(SQLI_RISKY_EXECUTE, text)
    safe_hits = _match_any(SQLI_SAFE_EXECUTE, text)

    has_param_execute = bool(re.search(SQLI_SAFE_EXECUTE[0], text, flags=re.MULTILINE))
    has_unsafe_build = bool(_match_any(SQLI_RISKY_SQL_BUILD, text))
    has_unparam_execute = bool(_match_any(SQLI_RISKY_EXECUTE, text))

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

CMDI_RISKY = [
    r"os\.system\s*\(",
    r"os\.popen\s*\(",
    r"subprocess\.(run|call|Popen)\s*\([^)]*shell\s*=\s*True",
]

# Study-specific "safe": subprocess used AND no shell=True anywhere in the file
CMDI_SUBPROCESS_ANY = r"subprocess\.(run|call|Popen)\s*\("


def detect_cmdi(text_or_path: str) -> DetectorResult:
    """
    Heuristic CMDi detector.
    """
    text = _as_text(text_or_path)

    risky_hits = _match_any(CMDI_RISKY, text)

    safe_hits: List[str] = []
    if re.search(CMDI_SUBPROCESS_ANY, text, flags=re.MULTILINE) and not re.search(r"shell\s*=\s*True", text):
        safe_hits = ["subprocess_without_shell_true"]

    if risky_hits:
        verdict = "present"
    elif safe_hits:
        verdict = "absent"
    else:
        verdict = "uncertain"

    return DetectorResult("CMDi", risky_hits, safe_hits, verdict)


