"""Pre-publish privacy/safety gate for researcher workflows.

This checker helps prevent accidental publication of participant data and
credentials by scanning the repository for high-risk artifacts before push.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Finding:
    """One privacy or secret-scanning finding."""

    severity: str
    category: str
    path: str
    detail: str


BLOCKED_DIRS = [
    Path("runs"),
    Path("participant_kits"),
    Path("data") / "raw",
    Path("data") / "aggregated",
]

ALLOWED_PLACEHOLDER_NAMES = {".keep"}
ALLOWED_BLOCKED_FILE_PATHS = {"runs/_gui_session_state.json"}

# High-confidence credential patterns only (to limit false positives).
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{30,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("bearer_header", re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE)),
]

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".csv",
    ".yaml",
    ".yml",
    ".toml",
    ".bat",
    ".ps1",
    ".html",
    ".js",
    ".css",
    ".tex",
}

EXCLUDE_PARTS = {"venv", ".git", "__pycache__"}


def _is_git_repo(repo_root: Path) -> bool:
    """Return True when repo_root is inside a git working tree."""
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return cp.returncode == 0 and cp.stdout.strip().lower() == "true"
    except Exception:
        return False


def _tracked_files(repo_root: Path) -> list[Path]:
    """Return tracked files when inside git; empty list if unavailable."""
    cp = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        return []
    out: list[Path] = []
    for line in cp.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        p = repo_root / rel
        if p.exists() and p.is_file():
            out.append(p)
    return out


def _workspace_files(repo_root: Path) -> Iterable[Path]:
    """Yield source-like files for scanning when git metadata is unavailable."""
    for p in repo_root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in EXCLUDE_PARTS for part in p.parts):
            continue
        yield p


def _scan_blocked_directories(repo_root: Path) -> list[Finding]:
    """Flag files that exist under directories that should never be published."""
    findings: list[Finding] = []
    for rel in BLOCKED_DIRS:
        abs_dir = repo_root / rel
        if not abs_dir.exists():
            continue
        for p in abs_dir.rglob("*"):
            if not p.is_file():
                continue
            rel_path = str(p.relative_to(repo_root)).replace("\\", "/")
            if p.name in ALLOWED_PLACEHOLDER_NAMES:
                continue
            if rel_path in ALLOWED_BLOCKED_FILE_PATHS:
                continue
            findings.append(
                Finding(
                    severity="HIGH",
                    category="blocked_data_dir",
                    path=rel_path,
                    detail=f"File exists under blocked path '{rel.as_posix()}'.",
                )
            )
    return findings
def _scan_sensitive_filenames(repo_root: Path, files: Iterable[Path]) -> list[Finding]:
    """Flag suspicious file names that often indicate leaked participant artifacts."""
    findings: list[Finding] = []
    bad_name_tokens = [
        "chat_log.jsonl",
        "snippet_log.csv",
        "submission_",
        "manifest_hashes.json",
    ]
    for p in files:
        rel = str(p.relative_to(repo_root)).replace("\\", "/")
        lower = p.name.lower()
        if any(tok in lower for tok in bad_name_tokens):
            # Allowed inside blocked dirs because those are already checked above.
            if rel.startswith("runs/") or rel.startswith("participant_kits/"):
                continue
            findings.append(
                Finding(
                    severity="MEDIUM",
                    category="sensitive_filename",
                    path=rel,
                    detail="Potential participant-data artifact in publishable path.",
                )
            )
    return findings


def _scan_secret_patterns(repo_root: Path, files: Iterable[Path]) -> list[Finding]:
    """Scan text-like files for high-confidence credential signatures."""
    findings: list[Finding] = []
    for p in files:
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue

        rel = str(p.relative_to(repo_root)).replace("\\", "/")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(
                    Finding(
                        severity="HIGH",
                        category="secret_pattern",
                        path=rel,
                        detail=f"Matched secret signature: {label}",
                    )
                )
    return findings


def _scan_gitignore(repo_root: Path) -> list[Finding]:
    """Verify that blocked study-data paths are ignored by git."""
    findings: list[Finding] = []
    gi = repo_root / ".gitignore"
    if not gi.exists():
        return [
            Finding(
                severity="HIGH",
                category="gitignore",
                path=".gitignore",
                detail="Missing .gitignore file.",
            )
        ]

    text = gi.read_text(encoding="utf-8", errors="ignore")
    required_rules = ["runs/**", "participant_kits/**", "data/raw/**", "data/aggregated/**"]
    for rule in required_rules:
        if rule not in text:
            findings.append(
                Finding(
                    severity="HIGH",
                    category="gitignore",
                    path=".gitignore",
                    detail=f"Missing required ignore rule: {rule}",
                )
            )
    return findings


def run_prepublish_check(repo_root: Path) -> tuple[bool, list[Finding], str]:
    """Run all checks and return (ok, findings, scan_mode)."""
    repo_root = repo_root.resolve()
    mode = "git-tracked" if _is_git_repo(repo_root) else "workspace-scan"

    files = _tracked_files(repo_root) if mode == "git-tracked" else list(_workspace_files(repo_root))

    findings: list[Finding] = []
    findings.extend(_scan_gitignore(repo_root))
    findings.extend(_scan_blocked_directories(repo_root))
    findings.extend(_scan_sensitive_filenames(repo_root, files))
    findings.extend(_scan_secret_patterns(repo_root, files))

    ok = not any(f.severity == "HIGH" for f in findings)
    return ok, findings, mode


def _print_report(ok: bool, findings: list[Finding], mode: str) -> None:
    """Print a human-readable terminal report for the pre-publish scan."""
    print("Privacy Pre-Publish Check")
    print("=" * 32)
    print(f"Scan mode: {mode}")

    if not findings:
        print("No findings.")
        print("PASS: Repository is clear for publish review.")
        return

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

    if ok:
        print("\nPASS WITH WARNINGS: No HIGH findings.")
    else:
        print("\nFAIL: Resolve HIGH findings before publish.")


def main() -> None:
    """CLI entrypoint for one-command pre-publish privacy checks."""
    ap = argparse.ArgumentParser(description="Pre-publish privacy/safety checker.")
    ap.add_argument("--repo_root", default=".", help="Repository root to scan.")
    args = ap.parse_args()

    ok, findings, mode = run_prepublish_check(Path(args.repo_root))
    _print_report(ok, findings, mode)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

