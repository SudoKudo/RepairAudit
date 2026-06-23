"""RepairAudit participant kit generation utilities.

This module creates participant-facing packages that contain only the files
needed to perform code edits and return structured workflow data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zipfile import ZIP_DEFLATED, ZipFile

from tools.domain_classification.participant_ready import is_participant_ready_row

HARDNESS_BUCKETS = ("low", "medium", "high")
HARDNESS_ALIASES = {
    "low": "low",
    "med": "medium",
    "mid": "medium",
    "medium": "medium",
    "high": "high",
}
LANGUAGE_EXTENSIONS = {
    "c": ".c",
    "c#": ".cs",
    "cpp": ".cpp",
    "c++": ".cpp",
    "go": ".go",
    "java": ".java",
    "javascript": ".js",
    "php": ".php",
    "python": ".py",
    "ruby": ".rb",
    "typescript": ".ts",
}
PARTICIPANT_OS_ALIASES = {
    "windows": "windows",
    "win": "windows",
    "mac": "macos",
    "macos": "macos",
    "darwin": "macos",
    "osx": "macos",
    "linux": "linux",
    "ubuntu": "linux",
}
PARTICIPANT_OS_LABELS = {
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
}
PARTICIPANT_OS_LAUNCHERS = {
    "windows": "Launch_Study_Web_App.bat",
    "macos": "Launch_Study_Web_App.sh",
    "linux": "Launch_Study_Web_App.sh",
}
PARTICIPANT_SUPPORT_DIR_NAME = ".repairaudit"


def _configure_csv_field_limit() -> None:
    """Raise the CSV field limit so large code-sample cells can be read."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


_configure_csv_field_limit()


def _dataset_identifier(row: dict[str, str]) -> str:
    """Return the stable source identifier for one dataset row."""
    for key in ("sample_id", "row_uid", "source_row_id"):
        value = _clean_text(row.get(key, ""))
        if value:
            return value
    return "snippet"


def _participant_timestamp() -> str:
    """Return UTC timestamp text used in generated participant artifacts."""
    return datetime.now(timezone.utc).isoformat()


def _participant_log_fieldnames() -> list[str]:
    """Return snippet interaction columns expected by merge-interaction."""
    return [
        "snippet_id",
        "tool",
        "model",
        "turns",
        "applied_turns",
        "strategy_primary",
        "strategy_other_text",
        "confidence_1to5",
        "first_prompt",
        "final_prompt",
        "notes",
    ]


def _clean_text(value: Any) -> str:
    """Normalize nullable values into stripped text."""
    return str(value or "").strip()


def _normalize_hardness(value: Any) -> str:
    """Map dataset hardness text onto the three study buckets."""
    text = _clean_text(value).casefold()
    normalized = HARDNESS_ALIASES.get(text)
    if not normalized:
        raise ValueError(f"Unsupported hardness_strict value: {value!r}")
    return normalized


def _normalize_participant_os(value: Any) -> str:
    """Map CLI/GUI OS text onto the supported launcher targets."""
    text = _clean_text(value).casefold()
    normalized = PARTICIPANT_OS_ALIASES.get(text)
    if not normalized:
        raise ValueError(
            "Unsupported participant_os value: "
            f"{value!r}. Use one of {sorted(PARTICIPANT_OS_LABELS)}."
        )
    return normalized


def _parse_secondary_expertise(raw_value: Any) -> list[str]:
    """Parse the serialized secondary expertise list from the dataset."""
    text = _clean_text(raw_value)
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    return [_clean_text(item) for item in payload if _clean_text(item)]


def _safe_name_fragment(value: str) -> str:
    """Collapse a filename fragment to alnum, dash, and underscore."""
    fragment = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return fragment.strip("_")


def _dataset_extension(row: dict[str, str]) -> str:
    """Pick a file extension for a dataset-backed snippet."""
    file_path = Path(_clean_text(row.get("file_path", "")))
    if file_path.suffix:
        return file_path.suffix
    language = _clean_text(row.get("language", "")).casefold()
    return LANGUAGE_EXTENSIONS.get(language, ".txt")


def _dataset_output_name(row: dict[str, str]) -> str:
    """Build a stable participant-visible filename for one dataset row."""
    sample_id = _safe_name_fragment(_dataset_identifier(row)) or "snippet"
    file_name = Path(_clean_text(row.get("file_path", ""))).stem
    hint = _safe_name_fragment(file_name)[:40]
    suffix = _dataset_extension(row)
    if hint:
        return f"{sample_id}_{hint}{suffix}"
    return f"{sample_id}{suffix}"


def _snippet_extension(row: dict[str, str]) -> str:
    """Resolve the source-language file extension used for a participant snippet."""
    source_kind = _clean_text(row.get("source_kind", "")).casefold()
    if source_kind == "dataset":
        return _dataset_extension(row)
    baseline_path = Path(_clean_text(row.get("baseline_relpath", "")))
    if baseline_path.suffix:
        return baseline_path.suffix
    return ".txt"


def _build_participant_snippet_mappings(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, str], dict[str, str]]:
    """Assign participant-safe IDs, file names, and labels without exposing source identifiers."""
    participant_rows: list[dict[str, str]] = []
    snippet_files: dict[str, str] = {}
    snippet_labels: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        source_snippet_id = row["snippet_id"]
        participant_snippet_id = f"S{index:02d}"
        extension = _snippet_extension(row)
        participant_filename = f"snippet_{index:02d}{extension}"
        participant_label = f"Snippet {index}"

        copied = dict(row)
        copied["source_snippet_id"] = source_snippet_id
        copied["snippet_id"] = participant_snippet_id
        copied["participant_filename"] = participant_filename
        copied["participant_label"] = participant_label
        participant_rows.append(copied)

        snippet_files[participant_snippet_id] = participant_filename
        snippet_labels[participant_snippet_id] = participant_label
    return participant_rows, snippet_files, snippet_labels


def _decode_escaped_newlines(text: str) -> str:
    """Expand literal newline and tab escape sequences found in flattened dataset rows."""
    if "\n" in text or "\\n" not in text:
        return text
    return (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\t", "    ")
    )


def _format_flat_c_like_code(text: str) -> str:
    """Make one-line brace-and-semicolon code samples readable without changing their tokens."""
    raw_lines: list[str] = []
    token: list[str] = []
    in_string = ""
    escaping = False
    index = 0
    length = len(text)

    while index < length:
        ch = text[index]

        if not in_string and text.startswith("/*", index):
            piece = "".join(token).strip()
            if piece:
                raw_lines.append(piece)
            token = []
            end = text.find("*/", index + 2)
            if end == -1:
                raw_lines.append(text[index:].strip())
                break
            raw_lines.append(text[index : end + 2].strip())
            index = end + 2
            continue

        if not in_string and text.startswith("//", index):
            piece = "".join(token).strip()
            if piece:
                raw_lines.append(piece)
            token = []
            end = text.find("\n", index + 2)
            if end == -1:
                raw_lines.append(text[index:].strip())
                break
            raw_lines.append(text[index:end].strip())
            index = end
            continue

        token.append(ch)
        if in_string:
            if escaping:
                escaping = False
                index += 1
                continue
            if ch == "\\":
                escaping = True
                index += 1
                continue
            if ch == in_string:
                in_string = ""
            index += 1
            continue

        if ch in {'"', "'"}:
            in_string = ch
            index += 1
            continue

        if ch in {"{", "}", ";"}:
            piece = "".join(token).strip()
            if piece:
                raw_lines.append(piece)
            token = []
            index += 1
            continue

        index += 1

    tail = "".join(token).strip()
    if tail:
        raw_lines.append(tail)

    indent = 0
    lines: list[str] = []
    for raw_line in raw_lines:
        line = re.sub(r"\s+", " ", raw_line.strip())
        if not line:
            continue
        if line.startswith("}"):
            indent = max(0, indent - 1)
        lines.append(("    " * indent) + line)
        opens = line.count("{")
        closes = line.count("}")
        if opens > closes:
            indent += opens - closes
        elif closes > opens and not line.startswith("}"):
            indent = max(0, indent - (closes - opens))
    return "\n".join(lines)


def _python_line_opens_block(line: str) -> bool:
    """Return True when a flattened Python statement starts an indented block."""
    stripped = line.strip()
    if not stripped.endswith(":"):
        return False
    starters = (
        "def ",
        "class ",
        "if ",
        "elif ",
        "else:",
        "for ",
        "while ",
        "try:",
        "except",
        "finally:",
        "with ",
        "match ",
        "case ",
    )
    return any(stripped.startswith(prefix) for prefix in starters)


def _python_line_dedents_first(line: str) -> bool:
    """Return True when a Python line should reduce indent before rendering."""
    stripped = line.strip()
    return stripped.startswith(("elif ", "else:", "except", "finally:", "case "))


def _split_flat_python_segments(text: str) -> list[str]:
    """Split one-line Python text into readable statements without changing tokens."""
    segments: list[str] = []
    token: list[str] = []
    in_string = ""
    escaping = False
    bracket_depth = 0
    index = 0
    length = len(text)

    while index < length:
        ch = text[index]
        token.append(ch)

        if in_string:
            if escaping:
                escaping = False
                index += 1
                continue
            if ch == "\\":
                escaping = True
                index += 1
                continue
            if ch == in_string:
                in_string = ""
            index += 1
            continue

        if ch in {'"', "'"}:
            in_string = ch
            index += 1
            continue

        if ch in "([{":
            bracket_depth += 1
            index += 1
            continue
        if ch in ")]}":
            bracket_depth = max(0, bracket_depth - 1)
            index += 1
            continue

        if ch == "#" and bracket_depth == 0:
            segments.append("".join(token).strip())
            break

        if ch == ";" and bracket_depth == 0:
            piece = "".join(token[:-1]).strip()
            if piece:
                segments.append(piece)
            token = []
            index += 1
            continue

        if ch == ":" and bracket_depth == 0:
            piece = "".join(token).strip()
            tail = text[index + 1 :].strip()
            if piece and tail and _python_line_opens_block(piece):
                segments.append(piece)
                token = []
            index += 1
            continue

        index += 1

    tail = "".join(token).strip()
    if tail:
        segments.append(tail)
    return segments


def _format_flat_python_code(text: str) -> str:
    """Make flattened Python samples readable enough for participant review."""
    segments = _split_flat_python_segments(text)
    indent = 0
    lines: list[str] = []

    for raw_segment in segments:
        line = raw_segment.strip()
        if not line:
            continue
        if _python_line_dedents_first(line):
            indent = max(0, indent - 1)
        lines.append(("    " * indent) + line)
        if _python_line_opens_block(line):
            indent += 1

    return "\n".join(lines)


def _normalize_code_sample(text: str, language: str) -> str:
    """Clean and lightly format dataset-backed baseline code before it reaches participants."""
    normalized = _decode_escaped_newlines(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    language_key = language.casefold()
    c_like_languages = {
        "c",
        "c#",
        "cpp",
        "c++",
        "go",
        "java",
        "javascript",
        "php",
        "typescript",
    }
    if "\n" not in normalized and language_key in c_like_languages:
        normalized = _format_flat_c_like_code(normalized)

    if "\n" not in normalized and language_key == "python":
        normalized = _format_flat_python_code(normalized)

    return normalized + "\n"


def _selection_rng(participant_id: str, selection_seed: int) -> random.Random:
    """Create a deterministic RNG scoped to one participant kit."""
    digest = hashlib.sha256(f"{participant_id}:{selection_seed}".encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _load_dataset_rows(dataset_csv: Path) -> list[dict[str, str]]:
    """Load dataset rows used for expertise-aware kit generation."""
    with dataset_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"Dataset CSV has no header row: {dataset_csv}")

        required = {"language", "hardness_strict", "code_sample", "primary_expertise_area"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"Dataset CSV missing required columns: {sorted(missing)}")
        if not ({"sample_id"} <= set(reader.fieldnames) or {"row_uid"} <= set(reader.fieldnames)):
            raise ValueError("Dataset CSV must include either sample_id or row_uid.")

        rows: list[dict[str, str]] = []
        for row in reader:
            sample_id = _dataset_identifier({str(key): _clean_text(value) for key, value in row.items()})
            code_sample = _clean_text(row.get("code_sample"))
            if not sample_id or not code_sample:
                continue

            normalized_row = {str(key): _clean_text(value) for key, value in row.items()}
            if not is_participant_ready_row(normalized_row):
                continue
            normalized_row["source_kind"] = "dataset"
            normalized_row["snippet_id"] = sample_id
            normalized_row.setdefault("sample_id", sample_id)
            normalized_row["output_name"] = _dataset_output_name(normalized_row)
            normalized_row["hardness_bucket"] = _normalize_hardness(normalized_row.get("hardness_strict", ""))
            normalized_row["secondary_expertise_areas"] = json.dumps(
                _parse_secondary_expertise(normalized_row.get("secondary_expertise_areas", "")),
                ensure_ascii=False,
            )
            rows.append(normalized_row)

    if not rows:
        raise ValueError(
            f"No participant-ready dataset rows found in CSV: {dataset_csv}. "
            "Build a participant-ready dataset first or inspect the row filters."
        )
    return rows


def _row_matches_expertise(row: dict[str, str], expertise_labels: set[str]) -> bool:
    """Return True when the dataset row matches any selected expertise area."""
    if not expertise_labels:
        return True

    labels = {_clean_text(row.get("primary_expertise_area", "")).casefold()}
    labels.update(label.casefold() for label in _parse_secondary_expertise(row.get("secondary_expertise_areas", "")))
    labels.discard("")
    return bool(labels & expertise_labels)


def _choose_dataset_snippets(
    *,
    rows: list[dict[str, str]],
    expertise_areas: list[str],
    samples_per_hardness: int,
    participant_id: str,
    selection_seed: int,
) -> list[dict[str, str]]:
    """Select a balanced 9-snippet kit from the classified dataset."""
    if samples_per_hardness < 1:
        raise ValueError("samples_per_hardness must be at least 1.")

    expertise_labels = {label.casefold() for label in expertise_areas if label.strip()}
    rng = _selection_rng(participant_id, selection_seed)
    selected: list[dict[str, str]] = []

    for bucket in HARDNESS_BUCKETS:
        bucket_rows = [row for row in rows if row["hardness_bucket"] == bucket]
        if len(bucket_rows) < samples_per_hardness:
            raise ValueError(
                f"Dataset only has {len(bucket_rows)} {bucket} rows; need {samples_per_hardness}."
            )

        matching_rows = [row for row in bucket_rows if _row_matches_expertise(row, expertise_labels)]
        rng.shuffle(matching_rows)
        chosen = matching_rows[:samples_per_hardness]
        chosen_ids = {row["snippet_id"] for row in chosen}

        if len(chosen) < samples_per_hardness:
            fallback_rows = [row for row in bucket_rows if row["snippet_id"] not in chosen_ids]
            rng.shuffle(fallback_rows)
            chosen.extend(fallback_rows[: samples_per_hardness - len(chosen)])

        if len(chosen) < samples_per_hardness:
            raise ValueError(
                f"Could not assemble {samples_per_hardness} {bucket} snippets for participant {participant_id}."
            )

        for row in chosen:
            copied = dict(row)
            copied["selection_bucket"] = bucket
            copied["selection_source"] = (
                "expertise_match" if _row_matches_expertise(row, expertise_labels) else "bucket_fallback"
            )
            selected.append(copied)

    rng.shuffle(selected)
    return selected


def _load_metadata_rows(metadata_csv: Path) -> list[dict[str, str]]:
    """Load valid snippet rows from file-backed metadata CSV."""
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
            sid = _clean_text(row.get("snippet_id"))
            base = _clean_text(row.get("baseline_relpath"))
            if sid and base:
                rows.append(
                    {
                        "source_kind": "metadata",
                        "snippet_id": sid,
                        "baseline_relpath": base,
                        "language": _clean_text(row.get("language")),
                    }
                )

    if not rows:
        raise ValueError(f"No usable rows found in metadata CSV: {metadata_csv}")
    return rows


def _load_snippets(
    source_csv: Path,
    *,
    participant_id: str,
    expertise_areas: list[str],
    samples_per_hardness: int,
    selection_seed: int,
) -> list[dict[str, str]]:
    """Load either file-backed metadata rows or dataset-backed selection rows."""
    if not source_csv.exists():
        raise FileNotFoundError(f"Missing source CSV: {source_csv}")

    with source_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = set(reader.fieldnames or [])

    if {"snippet_id", "baseline_relpath"} <= fieldnames:
        return _load_metadata_rows(source_csv)

    if {"code_sample", "language", "hardness_strict", "primary_expertise_area"} <= fieldnames and (
        {"sample_id"} <= fieldnames or {"row_uid"} <= fieldnames
    ):
        dataset_rows = _load_dataset_rows(source_csv)
        return _choose_dataset_snippets(
            rows=dataset_rows,
            expertise_areas=expertise_areas,
            samples_per_hardness=samples_per_hardness,
            participant_id=participant_id,
            selection_seed=selection_seed,
        )

    raise ValueError(
        "Source CSV must either contain snippet_id/baseline_relpath metadata columns "
        "or dataset columns including code_sample/language/hardness_strict/primary_expertise_area "
        "plus either sample_id or row_uid."
    )


def _sha256_file(path: Path) -> str:
    """Return SHA-256 hash of a file for reproducibility manifests."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    """Return SHA-256 hash of a UTF-8 string payload."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _public_source_csv_ref(source_csv: Path, repo_root: Path) -> str:
    """Return a participant-safe source reference without leaking a local absolute path."""
    try:
        rel_path = source_csv.resolve().relative_to(repo_root.resolve())
        return rel_path.as_posix()
    except Exception:
        return source_csv.name


def _git_head_sha(repo_root: Path) -> str:
    """Return the current git commit SHA when available."""
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return output.strip()
    except Exception:
        return ""


def _write_kit_manifest(
    *,
    kit_dir: Path,
    source_csv: Path,
    lock_payload: dict[str, Any],
    snippet_files: dict[str, str],
    participant_os: str,
) -> None:
    """Write a local reproducibility manifest without participant results or logs."""
    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / "config.yaml"
    manifest = {
        "generated_utc": _participant_timestamp(),
        "repo_commit": _git_head_sha(repo_root),
        "participant_os": participant_os,
        "launcher_file": PARTICIPANT_OS_LAUNCHERS[participant_os],
        "source_csv": _public_source_csv_ref(source_csv, repo_root),
        "source_csv_sha256": _sha256_file(source_csv) if source_csv.exists() else "",
        "config_yaml_sha256": _sha256_file(config_path) if config_path.exists() else "",
        "study_config_lock_sha256": _sha256_text(json.dumps(lock_payload, sort_keys=True)),
        "snippet_files": snippet_files,
    }
    (kit_dir / "kit_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _hide_participant_support_dir(path: Path, participant_os: str) -> None:
    """Hide the support folder on Windows so participants only see the launcher and README."""
    if participant_os != "windows":
        return
    try:
        subprocess.run(
            ["attrib", "+h", str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _looks_like_windows_lock_error(exc: OSError) -> bool:
    """Recognize the Windows error text raised when a directory is someone's cwd."""
    text = str(exc or "").casefold()
    return "being used by another process" in text or "cannot access the file" in text


def _windows_process_snapshot(process_names: tuple[str, ...]) -> list[dict[str, str]]:
    """Read a small process snapshot through PowerShell so we can inspect likely lockers."""
    quoted = ",".join(f"'{name}'" for name in process_names)
    command = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.Name -in @({quoted}) }} | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Depth 3"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    payload = (proc.stdout or "").strip()
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except Exception:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    rows: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "pid": _clean_text(item.get("ProcessId", "")),
                "name": _clean_text(item.get("Name", "")),
                "command_line": _clean_text(item.get("CommandLine", "")),
            }
        )
    return rows


def _windows_process_current_directory(pid: int) -> str:
    """Read another process's current directory from the PEB."""
    import ctypes
    from ctypes import wintypes

    process_query_information = 0x0400
    process_vm_read = 0x0010

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.c_void_p),
        ]

    class CurDir(ctypes.Structure):
        _fields_ = [("DosPath", UnicodeString), ("Handle", wintypes.HANDLE)]

    class ProcessParameters(ctypes.Structure):
        _fields_ = [
            ("MaximumLength", wintypes.ULONG),
            ("Length", wintypes.ULONG),
            ("Flags", wintypes.ULONG),
            ("DebugFlags", wintypes.ULONG),
            ("ConsoleHandle", wintypes.HANDLE),
            ("ConsoleFlags", wintypes.ULONG),
            ("StandardInput", wintypes.HANDLE),
            ("StandardOutput", wintypes.HANDLE),
            ("StandardError", wintypes.HANDLE),
            ("CurrentDirectory", CurDir),
        ]

    class Peb(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_ubyte * 2),
            ("BeingDebugged", ctypes.c_ubyte),
            ("Reserved2", ctypes.c_ubyte * 1),
            ("Reserved3", ctypes.c_void_p * 2),
            ("Ldr", ctypes.c_void_p),
            ("ProcessParameters", ctypes.c_void_p),
        ]

    class ProcessBasicInformation(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2_0", ctypes.c_void_p),
            ("Reserved2_1", ctypes.c_void_p),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE

    read_process_memory = kernel32.ReadProcessMemory
    read_process_memory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    read_process_memory.restype = wintypes.BOOL

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    nt_query_information_process = ntdll.NtQueryInformationProcess
    nt_query_information_process.argtypes = [
        wintypes.HANDLE,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    ]
    nt_query_information_process.restype = wintypes.LONG

    def read_struct(handle: int, address: int, struct_type: type[ctypes.Structure]) -> ctypes.Structure:
        obj = struct_type()
        read = ctypes.c_size_t()
        ok = read_process_memory(handle, address, ctypes.byref(obj), ctypes.sizeof(obj), ctypes.byref(read))
        if not ok:
            raise OSError(ctypes.get_last_error())
        return obj

    handle = open_process(process_query_information | process_vm_read, False, int(pid))
    if not handle:
        raise OSError(ctypes.get_last_error())
    try:
        pbi = ProcessBasicInformation()
        result_length = wintypes.ULONG()
        status = nt_query_information_process(
            handle,
            0,
            ctypes.byref(pbi),
            ctypes.sizeof(pbi),
            ctypes.byref(result_length),
        )
        if status != 0:
            raise OSError(f"NtQueryInformationProcess={status}")

        peb = read_struct(handle, pbi.PebBaseAddress, Peb)
        params = read_struct(handle, peb.ProcessParameters, ProcessParameters)
        dos_path = params.CurrentDirectory.DosPath
        if not dos_path.Buffer or dos_path.Length == 0:
            return ""

        raw = ctypes.create_string_buffer(dos_path.Length)
        read = ctypes.c_size_t()
        ok = read_process_memory(handle, dos_path.Buffer, raw, dos_path.Length, ctypes.byref(read))
        if not ok:
            raise OSError(ctypes.get_last_error())
        return raw.raw.decode("utf-16-le", errors="ignore")
    finally:
        close_handle(handle)


def _windows_directory_lock_candidates(path: Path, process_names: tuple[str, ...]) -> list[dict[str, str]]:
    """Return processes whose current directory is the target kit path."""
    target = str(path.resolve()).rstrip("\\/").casefold()
    matches: list[dict[str, str]] = []
    for row in _windows_process_snapshot(process_names):
        pid_text = row.get("pid", "")
        if not pid_text.isdigit():
            continue
        try:
            cwd = _windows_process_current_directory(int(pid_text))
        except Exception:
            continue
        normalized = cwd.rstrip("\\/").casefold()
        if normalized == target or normalized.startswith(target + "\\"):
            copy = dict(row)
            copy["cwd"] = cwd
            matches.append(copy)
    return matches


def _terminate_windows_python_cwd_processes(path: Path) -> list[int]:
    """Stop stale Python processes whose current directory is the kit folder."""
    stopped: list[int] = []
    for row in _windows_directory_lock_candidates(path, ("python.exe", "py.exe")):
        pid_text = row.get("pid", "")
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"Stop-Process -Id {pid} -Force"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            stopped.append(pid)
        except Exception:
            continue
    return stopped


def _remove_tree_with_lock_cleanup(path: Path) -> None:
    """Remove a kit directory and handle the common stale-Python lock case on Windows."""
    try:
        shutil.rmtree(path)
        return
    except OSError as exc:
        if os.name != "nt" or not _looks_like_windows_lock_error(exc):
            raise

    stopped = _terminate_windows_python_cwd_processes(path)
    if stopped:
        time.sleep(0.2)
        try:
            shutil.rmtree(path)
            return
        except OSError:
            pass

    lockers = _windows_directory_lock_candidates(path, ("python.exe", "py.exe", "cmd.exe", "powershell.exe"))
    if lockers:
        details = ", ".join(
            f"{row['name']} pid {row['pid']} ({row.get('cwd', '')})"
            for row in lockers
        )
        raise OSError(f"Could not remove {path}; close these processes first: {details}")
    shutil.rmtree(path)


def _write_researcher_assignment(
    *,
    path: Path,
    participant_id: str,
    condition: str,
    phase: str,
    participant_os: str,
    source_csv: Path,
    source_kind: str,
    expertise_areas: list[str],
    samples_per_hardness: int,
    selection_seed: int,
    rows: list[dict[str, str]],
) -> None:
    """Write a local-only researcher map that links participant-safe IDs back to the source rows."""
    payload = {
        "generated_utc": _participant_timestamp(),
        "participant_id": participant_id,
        "condition": condition,
        "phase": phase,
        "participant_os": participant_os,
        "source_kind": source_kind,
        "source_csv": str(source_csv),
        "expertise_areas": expertise_areas,
        "samples_per_hardness": samples_per_hardness,
        "selection_seed": selection_seed,
        "snippet_mappings": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _researcher_map_name(phase: str, participant_id: str) -> str:
    """Return the stable local map filename used after submissions are imported into runs/<phase>/<participant_id>/."""
    return f"{phase}__{participant_id}.json"


def _write_participant_log_template(log_csv: Path, snippet_ids: list[str], model_name: str) -> None:
    """Create snippet_log.csv template with one row per snippet."""
    log_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = _participant_log_fieldnames()
    with log_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for sid in snippet_ids:
            writer.writerow(
                {
                    "snippet_id": sid,
                    "tool": "LLM",
                    "model": model_name,
                    "turns": "0",
                    "applied_turns": "0",
                    "strategy_primary": "",
                    "strategy_other_text": "",
                    "confidence_1to5": "",
                    "first_prompt": "",
                    "final_prompt": "",
                    "notes": "",
                }
            )


def _write_chat_log_template(chat_log_path: Path) -> None:
    """Create an empty JSONL chat log file with format hints."""
    chat_log_path.parent.mkdir(parents=True, exist_ok=True)
    if chat_log_path.exists():
        return
    chat_log_path.write_text(
        (
            "# chat_log.jsonl\n"
            "# One JSON object per line. The participant app appends entries.\n"
            "# Required: timestamp_utc, participant_id, snippet_id, turn_index, role, text, provider, model, session_id\n"
        ),
        encoding="utf-8",
    )



def _write_participant_profile_template(profile_path: Path) -> None:
    """Create a blank participant profile file for pre-task experience capture."""
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(
            {
                "programming_experience": "",
                "language_experience": "",
                "llm_coding_experience": "",
                "security_experience": "",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _parse_expertise_args(value: Any) -> list[str]:
    """Parse a comma-separated expertise list from CLI or GUI input."""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_piece in _clean_text(value).replace(";", ",").split(","):
        label = " ".join(raw_piece.split())
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(label)
    return ordered


def _llm_endpoint_is_local(base_url: str) -> bool:
    """Return True when the configured LLM endpoint resolves to localhost."""
    parsed = urlparse(_clean_text(base_url))
    host = (parsed.hostname or "").strip().lower()
    return host in {"", "127.0.0.1", "localhost", "::1"}


def _normalize_llm_provider(value: Any) -> str:
    """Collapse provider input to the supported Ollama-compatible label."""
    text = _clean_text(value).lower()
    if text == "ollama":
        return text
    return "ollama"


def _share_zip_path(out_root: Path, *, phase: str, participant_id: str) -> Path:
    """Return the researcher-side share zip path for one generated participant kit."""
    return out_root / "_share_zips" / f"participant_kit_{phase}_{participant_id}.zip"


def _write_share_zip(*, out_root: Path, kit_dir: Path, phase: str, participant_id: str) -> Path:
    """Write a share-ready archive so cloud drives do not need to zip the kit folder."""
    zip_path = _share_zip_path(out_root, phase=phase, participant_id=participant_id)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
        for path in sorted(kit_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = str(path.relative_to(out_root)).replace("\\", "/")
            zf.write(path, arcname=arcname)
    return zip_path


def _write_participant_readme(
    *,
    path: Path,
    model_name: str,
    llm_base_url: str,
    participant_os: str,
) -> None:
    """Write short launch/troubleshooting instructions for participants."""
    launcher_name = PARTICIPANT_OS_LAUNCHERS[participant_os]
    launcher_step = (
        f"1. On {PARTICIPANT_OS_LABELS[participant_os]}: double-click `{launcher_name}`."
        if participant_os == "windows"
        else f"1. On {PARTICIPANT_OS_LABELS[participant_os]}: run `bash {launcher_name}` from a terminal in this folder."
    )
    uses_local_ollama = _llm_endpoint_is_local(llm_base_url)
    if uses_local_ollama:
        setup_lines = "\n".join(
            [
                "1. Make sure Python is installed on the machine that will run this kit. The kit uses it only to start the app and package the return ZIP.",
                "2. Install Ollama from [https://ollama.com/download](https://ollama.com/download).",
                "3. Pull the assigned model once:",
                f"   - `ollama pull {model_name}`",
                "4. Start Ollama before opening the app:",
                "   - `ollama serve`",
                "5. Leave that terminal running while you work.",
            ]
        )
        troubleshooting_lines = "\n".join(
            [
                "- If the app cannot reach the assistant, make sure `ollama serve` is still running.",
                "- If the launcher says `py` or `python` is missing, install Python and run the launcher again.",
                "- If the browser does not open automatically, use the localhost URL shown in the launcher window.",
                "- If the app closes, reopen it from the same kit folder and continue there.",
            ]
        )
        runtime_lines = "\n".join(
            [
                "- Use only the assistant built into this kit.",
                f"- This kit is locked to `{model_name}`.",
                "- A snippet may or may not contain a vulnerability. You can use the chat to inspect the code, ask questions, or change it.",
                "- Larger pasted code blocks can take longer to answer. If a reply is slow, wait before retrying or send a smaller function/block.",
                "- If a reply stalls, use **Stop / Discard Reply** and then send a narrower follow-up question.",
                "- Do not edit the kit files or switch to another assistant unless the research team told you to do that.",
            ]
        )
    else:
        setup_lines = "\n".join(
            [
                "1. Make sure Python is installed on the machine that will run this kit. The kit uses it only to start the app and package the return ZIP.",
                "2. Make sure this machine can reach the assigned LLM endpoint before you begin.",
                "3. If your network setup requires VPN access, connect before launching the kit.",
                "4. You do not need to install a local model on this machine.",
                "5. Leave the endpoint settings in the kit as they are.",
            ]
        )
        troubleshooting_lines = "\n".join(
            [
                "- If the app cannot reach the assigned LLM endpoint, confirm that your network or VPN connection is active.",
                "- If the launcher says `py` or `python` is missing, install Python and run the launcher again.",
                "- If the browser does not open automatically, use the localhost URL shown in the launcher window.",
                "- If the app closes, reopen it from the same kit folder and continue there.",
            ]
        )
        runtime_lines = "\n".join(
            [
                "- Use only the assistant built into this kit.",
                "- You do not need to choose a model or edit the endpoint yourself.",
                "- A snippet may or may not contain a vulnerability. You can use the chat to inspect the code, ask questions, or change it.",
                "- Larger pasted code blocks can take longer to answer. If a reply is slow, wait before retrying or send a smaller function/block.",
                "- If a reply stalls, use **Stop / Discard Reply** and then send a narrower follow-up question.",
                "- Do not edit the kit files or switch to another assistant unless the research team told you to do that.",
            ]
        )
    content = f"""# RepairAudit Participant Kit

Use the launcher in this folder and do the rest of the work in the browser app. You should not need to open or edit any other files in the kit.

## 1) Before You Start
{setup_lines}

## 2) Start The App
{launcher_step}
2. This kit only includes the launcher for the machine it was prepared for.
3. Keep the command window or terminal open while you work.
4. The browser app should open on its own. If it does not, copy the localhost URL shown in the launcher window into your browser.
5. Use the onboarding window to complete the participant profile, then click **Begin Study**.
6. The visible study timer starts only after you click **Begin Study**.

## 3) In-App Assistant
{runtime_lines}

## 4) Finish And Return
1. Complete all assigned snippets in the app.
2. Each snippet needs final submitted code, at least one in-app LLM turn, and the required summary fields before the kit will finish cleanly.
3. Use **Finish (Build ZIP)** when you are done.
4. The kit writes the return ZIP into `exports/`.
5. Send that ZIP back to the research team.

## 5) Troubleshooting
{troubleshooting_lines}

## 6) Privacy Reminder
Do not put personal identifiers or sensitive account data into prompts, code comments, or notes.
"""
    path.write_text(content, encoding="utf-8")

def _write_submission_packager(
    *,
    path: Path,
    run_dir_name: str,
    participant_id: str,
    condition: str,
    phase: str,
) -> None:
    """Write a helper script that validates logs then zips participant outputs."""
    script = f'''from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

APP_ROOT = Path(__file__).resolve().parent
PUBLIC_ROOT = APP_ROOT.parent if (APP_ROOT.parent / "README.md").exists() else APP_ROOT
RUN_DIR = APP_ROOT / "{run_dir_name}"
EXPORTS = PUBLIC_ROOT / "exports"
LOG_CSV = RUN_DIR / "logs" / "snippet_log.csv"
CHAT_LOG = RUN_DIR / "logs" / "chat_log.jsonl"
PROFILE_JSON = RUN_DIR / "logs" / "participant_profile.json"
EDITS_DIR = RUN_DIR / "edits"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _to_int(x: str) -> int:
    try:
        return int(float((x or "").strip()))
    except Exception:
        return -1


def _validate_snippet_log() -> list[str]:
    errors: list[str] = []
    if not LOG_CSV.exists():
        return [f"Missing required log file: {{LOG_CSV}}"]

    required = [
        "snippet_id",
        "tool",
        "model",
        "turns",
        "applied_turns",
        "strategy_primary",
        "confidence_1to5",
    ]

    with LOG_CSV.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return ["snippet_log.csv has no rows."]

    seen_ids: set[str] = set()
    for idx, row in enumerate(rows, start=2):
        sid = (row.get("snippet_id") or "").strip()
        if not sid:
            errors.append(f"Line {{idx}}: snippet_id is blank.")
            continue

        if sid in seen_ids:
            errors.append(f"Line {{idx}}: duplicate snippet_id '{{sid}}'.")
        seen_ids.add(sid)

        for col in required[1:]:
            if not (row.get(col) or "").strip():
                errors.append(f"Line {{idx}} ({{sid}}): column '{{col}}' is blank.")

        turns = _to_int(row.get("turns") or "")
        applied = _to_int(row.get("applied_turns") or "")
        if turns < 1:
            errors.append(f"Line {{idx}} ({{sid}}): turns must be an integer >= 1 (at least one LLM turn required).")
        if applied < 0:
            errors.append(f"Line {{idx}} ({{sid}}): applied_turns must be a non-negative integer.")
        if turns >= 0 and applied >= 0 and applied > turns:
            errors.append(f"Line {{idx}} ({{sid}}): applied_turns cannot exceed turns.")

        strategy = (row.get("strategy_primary") or "").strip()
        if strategy not in {{"zero_shot", "few_shot", "chain_of_thought", "adaptive_chain_of_thought", "other"}}:
            errors.append(f"Line {{idx}} ({{sid}}): strategy_primary must be one of zero_shot, few_shot, chain_of_thought, adaptive_chain_of_thought, other.")
        if strategy == "other" and not (row.get("strategy_other_text") or "").strip():
            errors.append(f"Line {{idx}} ({{sid}}): strategy_other_text is required when strategy_primary is other.")

        conf = _to_int(row.get("confidence_1to5") or "")
        if conf < 1 or conf > 5:
            errors.append(f"Line {{idx}} ({{sid}}): confidence_1to5 must be an integer 1-5.")

    return errors


def _validate_chat_log() -> list[str]:
    errors: list[str] = []
    if not CHAT_LOG.exists():
        return [f"Missing required chat log: {{CHAT_LOG}}"]

    required = [
        "timestamp_utc",
        "participant_id",
        "snippet_id",
        "turn_index",
        "role",
        "text",
        "provider",
        "model",
        "session_id",
    ]

    rows = 0
    with CHAT_LOG.open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            rows += 1
            try:
                obj = json.loads(line)
            except Exception:
                errors.append(f"chat_log.jsonl line {{line_no}}: invalid JSON.")
                continue

            if not isinstance(obj, dict):
                errors.append(f"chat_log.jsonl line {{line_no}}: JSON must be an object.")
                continue

            for key in required:
                val = obj.get(key)
                if val is None or not str(val).strip():
                    errors.append(f"chat_log.jsonl line {{line_no}}: missing/blank '{{key}}'.")

            role = str(obj.get("role", "")).strip().lower()
            if role not in {{"user", "assistant"}}:
                errors.append(f"chat_log.jsonl line {{line_no}}: role must be user or assistant.")

            try:
                turn_index = int(obj.get("turn_index"))
                if turn_index < 1:
                    errors.append(f"chat_log.jsonl line {{line_no}}: turn_index must be >= 1.")
            except Exception:
                errors.append(f"chat_log.jsonl line {{line_no}}: turn_index must be an integer.")

    # Chat rows are validated for structure here. Per-snippet LLM-use minimum
    # is enforced by snippet_log.csv validation (turns >= 1).
    return errors


def _validate_participant_profile() -> list[str]:
    errors: list[str] = []
    if not PROFILE_JSON.exists():
        return [f"Missing required participant profile: {{PROFILE_JSON}}"]

    try:
        payload = json.loads(PROFILE_JSON.read_text(encoding="utf-8"))
    except Exception:
        return ["participant_profile.json must contain valid JSON."]

    if not isinstance(payload, dict):
        return ["participant_profile.json must contain a JSON object."]

    allowed = {{
        "programming_experience": {{"<1 year", "1-2 years", "3-5 years", "6+ years"}},
        "language_experience": {{"none", "basic", "intermediate", "advanced"}},
        "llm_coding_experience": {{"never", "rarely", "monthly", "weekly", "daily"}},
        "security_experience": {{"none", "self-taught", "coursework", "professional"}},
    }}
    for field, options in allowed.items():
        value = str(payload.get(field, "") or "").strip()
        if not value:
            errors.append(f"participant_profile.json: '{{field}}' is blank.")
        elif value not in options:
            errors.append(f"participant_profile.json: '{{field}}' must be one of {{sorted(options)}}.")
    return errors


def _validate_edited_outputs() -> list[str]:
    errors: list[str] = []
    if not EDITS_DIR.exists():
        return [f"Missing edits folder: {{EDITS_DIR}}"]

    blank_files: list[str] = []
    for path in sorted(EDITS_DIR.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            text = ""
        if not text.strip():
            blank_files.append(path.name)

    if blank_files:
        errors.append("Final Submitted Code is blank for: " + ", ".join(blank_files))
    return errors


def main() -> None:
    if not RUN_DIR.exists():
        raise FileNotFoundError(f"Missing run folder: {{RUN_DIR}}")

    log_errors = _validate_snippet_log()
    chat_errors = _validate_chat_log()
    profile_errors = _validate_participant_profile()
    code_errors = _validate_edited_outputs()
    all_errors = log_errors + chat_errors + profile_errors + code_errors
    if all_errors:
        print("Validation failed. Fix the following issues before packaging:")
        for msg in all_errors:
            print(f"- {{msg}}")
        raise SystemExit(1)

    EXPORTS.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = RUN_DIR / "manifest_hashes.json"

    files_for_manifest = []
    for p in sorted(RUN_DIR.rglob("*")):
        if p.is_file() and p.name != manifest_path.name:
            files_for_manifest.append(p)

    manifest = {{
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "participant_id": "{participant_id}",
        "condition": "{condition}",
        "phase": "{phase}",
        "files": [
            {{"path": str(p.relative_to(RUN_DIR)).replace('\\\\', '/'), "sha256": _sha256_file(p)}}
            for p in files_for_manifest
        ],
    }}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    zip_name = f"submission_{phase}_{participant_id}_{{ts}}.zip"
    zip_path = EXPORTS / zip_name
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
        for p in sorted(RUN_DIR.rglob("*")):
            if p.is_file():
                arc = str(Path("{participant_id}") / p.relative_to(RUN_DIR)).replace('\\\\', '/')
                zf.write(p, arcname=arc)

    print(f"Wrote: {{zip_path}}")


if __name__ == "__main__":
    main()
'''
    path.write_text(script, encoding="utf-8")


def _write_participant_launcher(*, kit_dir: Path, participant_os: str) -> None:
    """Write only the launcher needed for the participant's operating system."""
    launcher_bat = r"""@echo off
setlocal
set "APP_ROOT=%~dp0__APP_DIR__"
set PYTHONDONTWRITEBYTECODE=1

if not exist "%APP_ROOT%\participant_web_app.py" (
  echo Support files are missing from this kit.
  exit /b 1
)

REM Clear stale participant web-app Python processes so this kit always starts clean.
REM This avoids "port already in use" conflicts from previous kits/sessions.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$appRoot = [System.IO.Path]::GetFullPath('%APP_ROOT%'); $pidFile = Join-Path $appRoot 'participant_web_app.pid'; if (Test-Path $pidFile) { $rawPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1); if ($rawPid -match '^\d+$') { $targetPid = [int]$rawPid; $proc = Get-CimInstance Win32_Process -Filter \"ProcessId = $targetPid\" -ErrorAction SilentlyContinue; if ($proc -and $proc.Name -in @('python.exe','py.exe')) { try { Stop-Process -Id $targetPid -Force -ErrorAction Stop } catch {} } }; Remove-Item $pidFile -Force -ErrorAction SilentlyContinue }; Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','py.exe') -and $_.CommandLine -match 'participant_web_app\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }" >nul 2>&1

pushd "%APP_ROOT%"

if exist venv\Scripts\python.exe (
  venv\Scripts\python.exe participant_web_app.py
) else if exist C:\Windows\py.exe (
  py -3 participant_web_app.py
) else if exist C:\Windows\System32\py.exe (
  py -3 participant_web_app.py
) else (
  python participant_web_app.py
)
popd
"""
    launcher_bat = launcher_bat.replace("__APP_DIR__", PARTICIPANT_SUPPORT_DIR_NAME)
    launcher_sh = """#!/usr/bin/env bash
set -eu
APP_ROOT="$(dirname "$0")/__APP_DIR__"
export PYTHONDONTWRITEBYTECODE=1

if [ ! -f "$APP_ROOT/participant_web_app.py" ]; then
  echo "Support files are missing from this kit."
  exit 1
fi

cd "$APP_ROOT"

if [ -x "venv/bin/python" ]; then
  "venv/bin/python" participant_web_app.py
elif command -v python3 >/dev/null 2>&1; then
  python3 participant_web_app.py
else
  python participant_web_app.py
fi
"""
    launcher_sh = launcher_sh.replace("__APP_DIR__", PARTICIPANT_SUPPORT_DIR_NAME)
    if participant_os == "windows":
        (kit_dir / "Launch_Study_Web_App.bat").write_text(launcher_bat, encoding="utf-8")
        return
    with (kit_dir / "Launch_Study_Web_App.sh").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(launcher_sh)


def build_participant_kit(args: argparse.Namespace) -> None:
    """Build a locked participant kit with baseline snippets and submission helpers."""
    source_csv = Path(args.metadata_csv)
    participant_os = _normalize_participant_os(getattr(args, "participant_os", "windows"))
    expertise_areas = _parse_expertise_args(getattr(args, "expertise_areas", ""))
    samples_per_hardness = int(getattr(args, "samples_per_hardness", 3))
    selection_seed = int(getattr(args, "selection_seed", 42))

    snippets = _load_snippets(
        source_csv,
        participant_id=args.participant_id,
        expertise_areas=expertise_areas,
        samples_per_hardness=samples_per_hardness,
        selection_seed=selection_seed,
    )
    participant_rows, snippet_files, snippet_labels = _build_participant_snippet_mappings(snippets)
    snippets = participant_rows
    snippet_ids = [row["snippet_id"] for row in snippets]
    source_kind = _clean_text(snippets[0].get("source_kind", "metadata")) or "metadata"

    out_root = Path(args.out_root)
    kit_dir = out_root / args.participant_id
    support_dir = kit_dir / PARTICIPANT_SUPPORT_DIR_NAME
    run_dir_name = f"run_{args.phase}_{args.participant_id}"
    run_dir = support_dir / run_dir_name

    if kit_dir.exists():
        if not args.overwrite:
            raise FileExistsError(
                f"Kit already exists: {kit_dir}. Use --overwrite to replace it."
            )
        _remove_tree_with_lock_cleanup(kit_dir)

    (run_dir / "edits").mkdir(parents=True, exist_ok=True)
    # Keep an immutable baseline snapshot so participants can copy from it while
    # saving their final answer to the editable output file.
    (run_dir / "baseline").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)

    for row in snippets:
        sid = row["snippet_id"]
        output_name = snippet_files[sid]
        # Keep the editable answer file intentionally blank at start.
        (run_dir / "edits" / output_name).write_text("", encoding="utf-8")
        if source_kind == "dataset":
            (run_dir / "baseline" / output_name).write_text(
                _normalize_code_sample(
                    _clean_text(row.get("code_sample", "")),
                    _clean_text(row.get("language", "")),
                ),
                encoding="utf-8",
            )
        else:
            src = Path(row["baseline_relpath"])
            if not src.exists():
                raise FileNotFoundError(f"Missing baseline snippet: {src}")
            shutil.copy2(src, run_dir / "baseline" / output_name)

    (run_dir / "condition.txt").write_text(args.condition.strip() + "\n", encoding="utf-8")
    (run_dir / "study_assignment.json").write_text(
        json.dumps(
            {
                "generated_utc": _participant_timestamp(),
                "participant_id": args.participant_id,
                "condition": args.condition,
                "phase": args.phase,
                "participant_os": participant_os,
                "source_kind": source_kind,
                "expertise_areas": expertise_areas,
                "samples_per_hardness": samples_per_hardness,
                "selection_seed": selection_seed,
                "snippet_ids": snippet_ids,
                "snippet_files": snippet_files,
                "snippet_labels": snippet_labels,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "start_end_times.json").write_text(
        json.dumps(
            {
                "start": None,
                "end": None,
                "study_started": False,
                "study_started_utc": "",
                "active_seconds": 0.0,
                "sessions": [],
                "session_open_start": "",
                "last_heartbeat": "",
                "generated_utc": _participant_timestamp(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    _write_participant_profile_template(run_dir / "logs" / "participant_profile.json")
    _write_participant_log_template(
        run_dir / "logs" / "snippet_log.csv",
        snippet_ids=snippet_ids,
        model_name=args.llm_model,
    )
    _write_chat_log_template(run_dir / "logs" / "chat_log.jsonl")

    # The lock file is the participant-side contract for reproducible collection.
    locked_config: dict[str, Any] = {
        "study_id": args.study_id,
        "participant_id": args.participant_id,
        "condition": args.condition,
        "phase": args.phase,
        "generated_utc": _participant_timestamp(),
        "participant_os": participant_os,
        "snippet_ids": snippet_ids,
        "snippet_files": snippet_files,
        "snippet_labels": snippet_labels,
        "snippet_source": {
            "kind": source_kind,
            "expertise_areas": expertise_areas,
            "samples_per_hardness": samples_per_hardness,
            "selection_seed": selection_seed,
        },
        "llm": {
            "provider": _normalize_llm_provider(getattr(args, "llm_provider", "ollama")),
            "model": args.llm_model,
            "base_url": _clean_text(getattr(args, "llm_base_url", "http://127.0.0.1:11434")) or "http://127.0.0.1:11434",
            "temperature": args.temperature,
            "top_p": args.top_p,
            "top_k": args.top_k,
            "num_predict": args.num_predict,
            "seed": args.seed,
        },
        "rules": {
            "allow_model_change": False,
            "allow_setting_change": False,
            "analysis_in_kit": False,
        },
    }
    (support_dir / "study_config.lock.json").write_text(json.dumps(locked_config, indent=2), encoding="utf-8")
    _write_kit_manifest(
        kit_dir=support_dir,
        source_csv=source_csv,
        lock_payload=locked_config,
        snippet_files=snippet_files,
        participant_os=participant_os,
    )
    repo_root = Path(__file__).resolve().parents[1]
    _write_researcher_assignment(
        path=repo_root / "participant_kits" / "_researcher_maps" / _researcher_map_name(args.phase, args.participant_id),
        participant_id=args.participant_id,
        condition=args.condition,
        phase=args.phase,
        participant_os=participant_os,
        source_csv=source_csv,
        source_kind=source_kind,
        expertise_areas=expertise_areas,
        samples_per_hardness=samples_per_hardness,
        selection_seed=selection_seed,
        rows=snippets,
    )

    _write_participant_readme(
        path=kit_dir / "README.md",
        model_name=args.llm_model,
        llm_base_url=str(locked_config["llm"].get("base_url", "") or ""),
        participant_os=participant_os,
    )
    _write_submission_packager(
        path=support_dir / "package_submission.py",
        run_dir_name=run_dir_name,
        participant_id=args.participant_id,
        condition=args.condition,
        phase=args.phase,
    )
    template_app = Path(__file__).resolve().with_name("participant_web_app_template.py")
    if not template_app.exists():
        raise FileNotFoundError(f"Missing participant app template: {template_app}")
    shutil.copy2(template_app, support_dir / "participant_web_app.py")
    _write_participant_launcher(kit_dir=kit_dir, participant_os=participant_os)
    _hide_participant_support_dir(support_dir, participant_os)
    share_zip = _write_share_zip(
        out_root=out_root,
        kit_dir=kit_dir,
        phase=args.phase,
        participant_id=args.participant_id,
    )

    print("Participant kit created.")
    print(f"Kit: {kit_dir}")
    print(f"Share ZIP: {share_zip}")
    print(f"Support folder: {support_dir}")
    print(f"Snippets: {len(snippet_ids)}")


def clean_participant_kits(args: argparse.Namespace) -> None:
    """Remove generated participant kit folders with explicit safety guards.

    Safety model:
    - Use --participant_id to delete one kit.
    - Use --all to delete all kits under out_root.
    - Use --dry_run to preview deletions without changing files.
    """
    kits_root = Path(args.out_root)
    if not kits_root.exists():
        print(f"No kits root found: {kits_root}")
        return

    reserved_names = {"_researcher_maps", "__pycache__"}

    target_dirs: list[Path] = []
    participant_id = (args.participant_id or "").strip()

    if participant_id and args.all:
        raise ValueError("Use either --participant_id or --all, not both.")

    if participant_id:
        one = kits_root / participant_id
        if one.name in reserved_names:
            raise ValueError(f"Refusing to remove reserved internal folder: {one.name}")
        if one.exists() and one.is_dir():
            target_dirs = [one]
        else:
            print(f"No kit found for participant_id={participant_id} at {one}")
            return
    elif args.all:
        target_dirs = sorted(
            [d for d in kits_root.iterdir() if d.is_dir() and d.name not in reserved_names],
            key=lambda p: p.name,
        )
    else:
        raise ValueError("Specify --participant_id <id> or --all.")

    if not target_dirs:
        print("No kits to remove.")
        return

    print(f"Kits root: {kits_root}")
    print(f"Dry run: {bool(args.dry_run)}")

    removed = 0
    for d in target_dirs:
        if args.dry_run:
            print(f"[DRY RUN] would remove: {d}")
        else:
            _remove_tree_with_lock_cleanup(d)
            print(f"Removed: {d}")
            removed += 1

    if participant_id and not args.dry_run:
        share_zip_parent = kits_root / "_share_zips"
        for zip_path in share_zip_parent.glob(f"participant_kit_*_{participant_id}.zip"):
            zip_path.unlink(missing_ok=True)
            print(f"Removed share ZIP: {zip_path}")
        try:
            if share_zip_parent.exists() and not any(share_zip_parent.iterdir()):
                share_zip_parent.rmdir()
        except Exception:
            pass

    if args.dry_run:
        print(f"Preview complete. {len(target_dirs)} kit(s) would be removed.")
    else:
        print(f"Done. Removed {removed} kit(s).")
































