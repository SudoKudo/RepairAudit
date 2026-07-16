"""Participant web app template stamped into each generated kit."""

from __future__ import annotations

import csv
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


def utc_now() -> str:
    """Return a stable UTC timestamp format used in participant logs."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse stored UTC timestamp text to datetime (supports trailing Z)."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _move_runtime_cwd_off_kit(kit_root: Path) -> None:
    """Move the process cwd off the participant kit so stale processes do not pin the folder."""
    try:
        current = Path.cwd().resolve()
        root = kit_root.resolve()
        if current == root or root in current.parents:
            os.chdir(tempfile.gettempdir())
    except Exception:
        pass


def _seconds_between(start_text: object, end_text: object) -> float:
    """Return non-negative elapsed seconds between two timestamp strings."""
    start_dt = _parse_utc_timestamp(start_text)
    end_dt = _parse_utc_timestamp(end_text)
    if start_dt is None or end_dt is None:
        return 0.0
    return max(0.0, (end_dt - start_dt).total_seconds())


def _to_float(value: object, default: float = 0.0) -> float:
    """Best-effort float coercion for dynamic JSON payload fields."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except Exception:
        return default


def _to_int(value: object, default: int = 0) -> int:
    """Best-effort int coercion for dynamic JSON payload fields."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(float(text))
    except Exception:
        return default


PROMPT_STRATEGY_OPTIONS = [
    ("zero_shot", "Zero-Shot"),
    ("few_shot", "Few-Shot"),
    ("chain_of_thought", "Chain-of-Thought"),
    ("adaptive_chain_of_thought", "Adaptive Chain-of-Thought"),
    ("other", "Other"),
]
PROMPT_STRATEGY_VALUES = {value for value, _label in PROMPT_STRATEGY_OPTIONS}
PARTICIPANT_PROFILE_OPTIONS = {
    "programming_experience": ["<1 year", "1-2 years", "3-5 years", "6+ years"],
    "language_experience": ["none", "basic", "intermediate", "advanced"],
    "llm_coding_experience": ["never", "rarely", "monthly", "weekly", "daily"],
    "security_experience": ["none", "self-taught", "coursework", "professional"],
}
PARTICIPANT_PROFILE_FIELDS = list(PARTICIPANT_PROFILE_OPTIONS.keys())


def participant_chat_system_prompt() -> str:
    """Return the neutral system prompt used for participant-side LLM chat."""
    return (
        "You are the assigned coding assistant for this study. "
        "Follow the participant's request exactly. "
        "Do not assume a task, intent, or output format beyond what the participant asked for. "
        "Keep responses straightforward and easy to use in the study app. "
        "Do not use markdown fences unless the participant asks for them. "
        "If you return code, preserve the original programming language and formatting style."
    )


class OllamaChatCancelled(RuntimeError):
    """Raised when a participant stops an in-flight Ollama chat request."""


class StudyStore:
    """Data layer for the participant web app."""

    def __init__(self, kit_root: Path) -> None:
        """Bind the participant kit paths and load the locked study configuration."""
        self.kit_root = kit_root
        self.public_root = kit_root.parent if (kit_root.parent / "README.md").exists() else kit_root
        self.lock_path = kit_root / "study_config.lock.json"
        self.readme_path = self.public_root / "README.md"
        self.packager_path = kit_root / "package_submission.py"

        self.lock_data = self._read_json(self.lock_path)
        self.run_dir = self._find_run_dir()
        self.edits_dir = self.run_dir / "edits"
        self.baseline_dir = self.run_dir / "baseline"
        self.log_csv = self.run_dir / "logs" / "snippet_log.csv"
        self.chat_log = self.run_dir / "logs" / "chat_log.jsonl"
        self.timer_path = self.run_dir / "start_end_times.json"
        self.snippet_times_path = self.run_dir / "timings" / "snippet_times.json"
        self.attestation_path = self.run_dir / "logs" / "llm_attestation.json"
        self.participant_profile_path = self.run_dir / "logs" / "participant_profile.json"

        self.fields = [
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

        # Guardrails to keep participant logs bounded and predictable.
        self.max_code_chars = 20000
        self.max_turn_text_chars = 12000
        self.max_field_chars = 2000
        self.max_chat_history_entries = 200
        self.max_chat_context_messages = 4
        self.max_chat_context_chars = 8000
        self.max_chat_request_chars = 16000

    def _find_run_dir(self) -> Path:
        """Locate the single run_* directory bundled inside this participant kit."""
        candidates = sorted([p for p in self.kit_root.glob("run_*") if p.is_dir()])
        if not candidates:
            raise FileNotFoundError("No run_* folder found in this participant kit.")
        return candidates[0]

    def _read_json(self, path: Path) -> dict[str, object]:
        """Read a JSON object from disk and return an empty mapping on failure."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _write_json(self, path: Path, data: dict[str, object]) -> None:
        """Write a JSON object using stable pretty-printed UTF-8 formatting."""
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _snippet_filename(self, snippet_id: str) -> str:
        """Resolve the locked filename for one snippet without trusting user input."""
        locked = self.lock_data.get("snippet_files", {}) if isinstance(self.lock_data, dict) else {}
        if isinstance(locked, dict):
            raw = str(locked.get(snippet_id, "") or "").strip()
            if raw:
                return Path(raw).name
        return ""

    def _snippet_path(self, root: Path, snippet_id: str) -> Path:
        """Resolve one snippet path within a kit folder, preserving its original extension."""
        self._assert_known_snippet(snippet_id)
        locked_name = self._snippet_filename(snippet_id)
        if locked_name:
            return root / locked_name

        matches = [p for p in sorted(root.glob(f"{snippet_id}.*")) if p.is_file()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return matches[0]
        return root / f"{snippet_id}.txt"

    def snippet_label(self, snippet_id: str) -> str:
        """Return the participant-facing label for one snippet without exposing source identifiers."""
        labels = self.lock_data.get("snippet_labels", {}) if isinstance(self.lock_data, dict) else {}
        if isinstance(labels, dict):
            raw = str(labels.get(snippet_id, "") or "").strip()
            if raw:
                return raw
        return snippet_id

    def _read_snippet_times(self) -> dict[str, dict[str, str]]:
        """Load snippet timing records and return an empty mapping on failure."""
        try:
            raw = json.loads(self.snippet_times_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for sid, payload in raw.items():
            if isinstance(payload, dict):
                out[str(sid)] = {str(k): str(v or "") for k, v in payload.items()}
        return out

    def _write_snippet_times(self, payload: dict[str, dict[str, str]]) -> None:
        """Persist snippet timing records using stable JSON formatting."""
        self.snippet_times_path.parent.mkdir(parents=True, exist_ok=True)
        self.snippet_times_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def mark_snippet_started(self, snippet_id: str) -> None:
        """Record the first observed edit-session entry time for one snippet."""
        self._assert_known_snippet(snippet_id)
        payload = self._read_snippet_times()
        entry = payload.setdefault(snippet_id, {})
        if not str(entry.get("start", "")).strip():
            entry["start"] = utc_now()
            self._write_snippet_times(payload)

    def mark_snippet_saved(self, snippet_id: str) -> None:
        """Record the latest save time for one snippet and backfill start if needed."""
        self._assert_known_snippet(snippet_id)
        payload = self._read_snippet_times()
        entry = payload.setdefault(snippet_id, {})
        now = utc_now()
        if not str(entry.get("start", "")).strip():
            entry["start"] = now
        entry["end"] = now
        self._write_snippet_times(payload)

    def _close_open_session(self, payload: dict[str, object], close_at: str) -> dict[str, object]:
        """Close any currently open session and accumulate active duration seconds."""
        open_start = str(payload.get("session_open_start", "") or "").strip()
        if not open_start:
            return payload

        # Prefer heartbeat timestamp so unexpected app closes do not include offline time.
        close_ts = str(payload.get("last_heartbeat", "") or "").strip() or close_at
        secs = _seconds_between(open_start, close_ts)

        sessions_obj = payload.get("sessions", [])
        sessions = sessions_obj if isinstance(sessions_obj, list) else []
        sessions.append({"start": open_start, "end": close_ts, "seconds": round(secs, 3)})

        payload["sessions"] = sessions
        payload["active_seconds"] = round(_to_float(payload.get("active_seconds", 0.0), 0.0) + secs, 3)
        payload["session_open_start"] = ""
        payload["last_heartbeat"] = ""
        return payload

    def study_started(self) -> bool:
        """Return True once the participant explicitly begins the timed study."""
        payload = self._read_json(self.timer_path)
        return bool(payload.get("study_started", False))

    def mark_onboarding_presented(self) -> None:
        """Record when the onboarding instructions were first shown before the timer starts."""
        payload = self._read_json(self.timer_path)
        if bool(payload.get("study_started", False)):
            return
        if str(payload.get("onboarding_opened_utc", "") or "").strip():
            return
        payload["onboarding_opened_utc"] = utc_now()
        self._write_json(self.timer_path, payload)

    def begin_study(self) -> dict[str, object]:
        """Start the timed study session after onboarding/profile review."""
        issues = self._participant_profile_issues()
        if issues:
            raise ValueError("Complete the Participant Profile first: " + "; ".join(issues))

        payload = self._read_json(self.timer_path)
        now = utc_now()
        if not payload.get("start"):
            payload["start"] = now
        payload["study_started"] = True
        payload["study_started_utc"] = str(payload.get("study_started_utc", "") or now)
        payload["end"] = ""
        payload["recovered_previous_session"] = False
        payload["recovered_at"] = ""
        onboarding_opened = str(payload.get("onboarding_opened_utc", "") or "").strip()
        if onboarding_opened:
            payload["instructions_seconds_before_start"] = round(_seconds_between(onboarding_opened, now), 3)
        payload["begin_study_clicked_utc"] = now
        payload["session_open_start"] = now
        payload["last_heartbeat"] = now
        payload["active_seconds"] = round(_to_float(payload.get("active_seconds", 0.0), 0.0), 3)
        self._write_json(self.timer_path, payload)
        return self.timer_status()

    def resume_session_if_started(self) -> None:
        """Resume a previously-started timed session when the app restarts."""
        payload = self._read_json(self.timer_path)
        if not bool(payload.get("study_started", False)):
            self._write_json(self.timer_path, payload)
            return

        now = utc_now()
        if not payload.get("start"):
            payload["start"] = str(payload.get("study_started_utc", "") or now)

        recovered = bool(str(payload.get("session_open_start", "") or "").strip())
        payload = self._close_open_session(payload, close_at=now)
        payload["recovered_previous_session"] = recovered
        if recovered:
            payload["recovered_at"] = now
        payload["session_open_start"] = now
        payload["last_heartbeat"] = now
        payload["active_seconds"] = round(_to_float(payload.get("active_seconds", 0.0), 0.0), 3)
        payload["end"] = ""
        self._write_json(self.timer_path, payload)

    def heartbeat(self) -> None:
        """Refresh heartbeat so active-time recovery stays accurate after crashes."""
        payload = self._read_json(self.timer_path)
        if not bool(payload.get("study_started", False)):
            return
        if str(payload.get("session_open_start", "") or "").strip():
            payload["last_heartbeat"] = utc_now()
            self._write_json(self.timer_path, payload)

    def seconds_since_last_heartbeat(self) -> float:
        """Return seconds since last heartbeat; large value when unavailable."""
        payload = self._read_json(self.timer_path)
        if not bool(payload.get("study_started", False)):
            return 0.0
        hb = _parse_utc_timestamp(payload.get("last_heartbeat", ""))
        if hb is None:
            return 10_000.0
        now = datetime.now(timezone.utc)
        return max(0.0, (now - hb).total_seconds())

    def mark_end(self) -> None:
        """Finalize timing by closing current session and writing end timestamp."""
        payload = self._read_json(self.timer_path)
        if not bool(payload.get("study_started", False)):
            self._write_json(self.timer_path, payload)
            return
        now = utc_now()
        payload = self._close_open_session(payload, close_at=now)
        payload["end"] = now
        self._write_json(self.timer_path, payload)

    def read_rows(self) -> list[dict[str, str]]:
        """Load snippet summary rows from the participant CSV log."""
        with self.log_csv.open("r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def write_rows(self, rows: list[dict[str, str]]) -> None:
        """Rewrite snippet_log.csv using the canonical study field order."""
        with self.log_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.fields)
            writer.writeheader()
            writer.writerows(rows)

    def get_snippet_ids(self) -> list[str]:
        """Return snippet IDs in the order stored in snippet_log.csv."""
        return [
            (row.get("snippet_id") or "").strip()
            for row in self.read_rows()
            if (row.get("snippet_id") or "").strip()
        ]


    def _assert_known_snippet(self, snippet_id: str) -> None:
        """Reject unknown snippet IDs so requests cannot escape kit scope."""
        if snippet_id not in set(self.get_snippet_ids()):
            raise ValueError(f"Unknown snippet_id: {snippet_id}")

    def load_snippet(self, snippet_id: str) -> str:
        """Load the participant-editable snippet text for one snippet ID."""
        path = self._snippet_path(self.edits_dir, snippet_id)
        if not path.exists():
            raise FileNotFoundError(f"Snippet file not found: {path}")
        return path.read_text(encoding="utf-8", errors="replace")

    def load_baseline_snippet(self, snippet_id: str) -> str:
        """Load read-only baseline text shown above the editable answer box."""
        path = self._snippet_path(self.baseline_dir, snippet_id)
        if not path.exists():
            # Backward compatibility: older kits may not contain baseline/.
            return self.load_snippet(snippet_id)
        return path.read_text(encoding="utf-8", errors="replace")

    def get_row(self, snippet_id: str) -> dict[str, str]:
        """Return the snippet_log.csv row associated with one snippet ID."""
        for row in self.read_rows():
            if (row.get("snippet_id") or "").strip() == snippet_id:
                return row
        raise KeyError(f"snippet_id not found in snippet_log.csv: {snippet_id}")

    def save_snippet_and_summary(
        self,
        snippet_id: str,
        code: str,
        summary: dict[str, str],
        *,
        validate_summary: bool = False,
    ) -> None:
        """Persist edited code plus summary fields for one snippet save action."""
        path = self._snippet_path(self.edits_dir, snippet_id)
        path.write_text(code, encoding="utf-8")

        rows = self.read_rows()
        found = False
        for row in rows:
            if (row.get("snippet_id") or "").strip() != snippet_id:
                continue
            # Only overwrite fields the browser actually sends. This prevents
            # hidden/autofilled fields from being blanked accidentally.
            for key in self.fields:
                if key == "snippet_id":
                    continue
                if key in summary:
                    row[key] = (summary.get(key) or "").strip()

            # Auto-fill from chat and write back into the CSV row object.
            normalized = self._auto_fill_row_from_chat(snippet_id, dict(row))
            row.clear()
            row.update(normalized)
            found = True
            break

        if not found:
            raise KeyError(f"snippet_id not found in snippet_log.csv: {snippet_id}")

        if validate_summary:
            if not code.strip():
                raise ValueError("Final Submitted Code cannot be blank.")
            self._validate_summary(normalized)
        self.write_rows(rows)

        # Track latest autosave/save so participants can see draft persistence feedback.
        timer_payload = self._read_json(self.timer_path)
        timer_payload["last_autosave_utc"] = utc_now()
        self._write_json(self.timer_path, timer_payload)

    def _validate_summary(self, summary: dict[str, str]) -> None:
        """Validate one normalized snippet-summary row before strict save or export."""
        try:
            turns = int((summary.get("turns") or "0").strip() or "0")
            applied = int((summary.get("applied_turns") or "").strip())
            confidence = int((summary.get("confidence_1to5") or "").strip())
        except Exception as exc:
            raise ValueError("Turns, Applied Turns, and Confidence must be integers.") from exc

        if turns < 0 or applied < 0 or applied > turns:
            raise ValueError("Turns/Applied Turns are invalid. Applied cannot exceed Turns.")
        if confidence < 1 or confidence > 5:
            raise ValueError("Confidence must be 1 to 5.")

        required_non_empty = ["tool", "model", "strategy_primary"]
        for key in required_non_empty:
            if not (summary.get(key) or "").strip():
                raise ValueError(f"{key} is required.")

        strategy_primary = (summary.get("strategy_primary") or "").strip()
        if strategy_primary not in PROMPT_STRATEGY_VALUES:
            raise ValueError("strategy_primary must be a valid prompt strategy.")
        if strategy_primary == "other" and not (summary.get("strategy_other_text") or "").strip():
            raise ValueError("Describe the primary strategy when Other is selected.")


    def _summary_issues(self, row: dict[str, str]) -> list[str]:
        """Return per-snippet summary issues used for UI lock/readiness display."""
        issues: list[str] = []

        required_non_empty = ["tool", "model", "strategy_primary"]
        for key in required_non_empty:
            if not (row.get(key) or "").strip():
                issues.append(f"{key} missing")

        strategy_primary = (row.get("strategy_primary") or "").strip()
        if strategy_primary and strategy_primary not in PROMPT_STRATEGY_VALUES:
            issues.append("strategy_primary must be a valid prompt strategy")
        if strategy_primary == "other" and not (row.get("strategy_other_text") or "").strip():
            issues.append("strategy_other_text is required when Primary Strategy is Other")


        try:
            turns = int((row.get("turns") or "").strip())
            applied = int((row.get("applied_turns") or "").strip())
            if turns < 1:
                issues.append("at least one in-app LLM turn is required")
            if applied < 0:
                issues.append("applied_turns must be >= 0")
            if applied > turns:
                issues.append("applied_turns cannot exceed turns")
        except Exception:
            issues.append("turn fields must be integers")

        try:
            conf = int((row.get("confidence_1to5") or "").strip())
            if conf < 1 or conf > 5:
                issues.append("confidence must be 1-5")
        except Exception:
            issues.append("confidence must be integer 1-5")

        return issues

    def _chat_turn_counts(self) -> dict[str, int]:
        """Count auto-logged chat turns per snippet from chat_log.jsonl."""
        counts: dict[str, int] = {}
        if not self.chat_log.exists():
            return counts

        with self.chat_log.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                sid = str(obj.get("snippet_id", "")).strip()
                if not sid:
                    continue
                counts[sid] = counts.get(sid, 0) + 1
        return counts

    def completion_status(self) -> dict[str, object]:
        """Summarize per-snippet readiness so the UI can drive progress state."""
        rows = self.read_rows()
        turn_counts = self._chat_turn_counts()

        by_snippet: list[dict[str, object]] = []
        for row in rows:
            sid = (row.get("snippet_id") or "").strip()
            if not sid:
                continue
            normalized_row = self._auto_fill_row_from_chat(sid, dict(row))
            summary_issues = self._summary_issues(normalized_row)
            try:
                code_complete = bool(self.load_snippet(sid).strip())
            except Exception:
                code_complete = False
            if not code_complete:
                summary_issues.append("final submitted code is blank")
            summary_complete = len(summary_issues) == 0
            turns = turn_counts.get(sid, 0)
            # Completion is gated by summary checks, including the
            # one-turn minimum requirement for in-app LLM usage.
            complete = summary_complete
            by_snippet.append(
                {
                    "snippet_id": sid,
                    "summary_complete": summary_complete,
                    "summary_issues": summary_issues,
                    "chat_turns": turns,
                    "complete": complete,
                }
            )

        completed_count = sum(1 for x in by_snippet if bool(x["complete"]))
        total = len(by_snippet)

        suggested_index = 0
        for i, x in enumerate(by_snippet):
            if not bool(x["complete"]):
                suggested_index = i
                break
        else:
            suggested_index = 0

        return {
            "snippets": by_snippet,
            "completed_count": completed_count,
            "total": total,
            "suggested_index": suggested_index,
        }

    def preflight_issues(self) -> list[str]:
        """Flatten readiness issues into user-facing export blockers."""
        status = self.completion_status()
        issues: list[str] = []
        for s in status["snippets"]:  # type: ignore[index]
            sid = str(s["snippet_id"])
            summary_issues = s.get("summary_issues", [])
            if isinstance(summary_issues, list) and summary_issues:
                issues.append(f"{sid}: " + ", ".join(str(x) for x in summary_issues))
        issues.extend(self._participant_profile_issues())
        if not self.study_started():
            issues.append("Review onboarding and click Begin Study before finishing the study.")
        return issues

    def timer_status(self) -> dict[str, object]:
        """Return timer/session state for resume and live timer rendering."""
        payload = self._read_json(self.timer_path)
        active_closed = _to_float(payload.get("active_seconds", 0.0), 0.0)
        open_start = str(payload.get("session_open_start", "") or "").strip()
        last_hb = str(payload.get("last_heartbeat", "") or "").strip()
        active_open = _seconds_between(open_start, last_hb) if (open_start and last_hb) else 0.0
        return {
            "start": payload.get("start", ""),
            "end": payload.get("end", ""),
            "study_started": bool(payload.get("study_started", False)),
            "study_started_utc": payload.get("study_started_utc", ""),
            "onboarding_opened_utc": payload.get("onboarding_opened_utc", ""),
            "instructions_seconds_before_start": payload.get("instructions_seconds_before_start", 0.0),
            "active_seconds": active_closed,
            "active_display_seconds": round(active_closed + active_open, 3),
            "session_open_start": payload.get("session_open_start", ""),
            "last_heartbeat": payload.get("last_heartbeat", ""),
            "last_autosave_utc": payload.get("last_autosave_utc", ""),
            "recovered_previous_session": bool(payload.get("recovered_previous_session", False)),
            "recovered_at": payload.get("recovered_at", ""),
        }

    def export_preview_files(self) -> list[str]:
        """Return a relative-file preview list for finish confirmation modal."""
        out: list[str] = []
        for fp in sorted(self.run_dir.rglob("*")):
            if fp.is_file():
                out.append(str(fp.relative_to(self.run_dir)).replace("\\", "/"))
        return out

    def read_participant_profile(self) -> dict[str, str]:
        """Load participant-level experience fields used as analysis covariates."""
        if not self.participant_profile_path.exists():
            return {field: "" for field in PARTICIPANT_PROFILE_FIELDS}
        try:
            payload = json.loads(self.participant_profile_path.read_text(encoding="utf-8"))
        except Exception:
            return {field: "" for field in PARTICIPANT_PROFILE_FIELDS}
        profile = {field: "" for field in PARTICIPANT_PROFILE_FIELDS}
        if isinstance(payload, dict):
            for field in PARTICIPANT_PROFILE_FIELDS:
                value = str(payload.get(field, "") or "").strip()
                profile[field] = value if value in PARTICIPANT_PROFILE_OPTIONS[field] else ""
        return profile

    def write_participant_profile(self, payload: dict[str, object]) -> dict[str, str]:
        """Persist participant-level experience selections."""
        profile = {field: "" for field in PARTICIPANT_PROFILE_FIELDS}
        for field in PARTICIPANT_PROFILE_FIELDS:
            value = str(payload.get(field, "") or "").strip()
            profile[field] = value if value in PARTICIPANT_PROFILE_OPTIONS[field] else ""
        self.participant_profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.participant_profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        return profile

    def _participant_profile_issues(self) -> list[str]:
        """Return missing participant profile requirements for export preflight."""
        profile = self.read_participant_profile()
        issues: list[str] = []
        for field in PARTICIPANT_PROFILE_FIELDS:
            if not profile.get(field, "").strip():
                issues.append(f"Participant Profile: {field.replace('_', ' ').title()} is required")
        return issues

    def write_finish_attestation(
        self,
        *,
        confirmed_assigned_profile: bool,
        deviation_note: str,
        provider: str,
        model: str,
    ) -> None:
        """Persist final model-profile attestation required at export time."""
        lock_llm = self.lock_data.get("llm", {}) if isinstance(self.lock_data, dict) else {}
        expected_provider = str(lock_llm.get("provider", "")) if isinstance(lock_llm, dict) else ""
        expected_model = str(lock_llm.get("model", "")) if isinstance(lock_llm, dict) else ""

        payload = {
            "timestamp_utc": utc_now(),
            "participant_id": str(self.lock_data.get("participant_id", "")).strip(),
            "confirmed_assigned_profile": bool(confirmed_assigned_profile),
            "deviation_note": (deviation_note or "").strip(),
            "reported_provider": (provider or "").strip(),
            "reported_model": (model or "").strip(),
            "expected_provider": expected_provider,
            "expected_model": expected_model,
        }
        self.attestation_path.parent.mkdir(parents=True, exist_ok=True)
        self.attestation_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def next_turn_index(self, snippet_id: str) -> int:
        """Return the next sequential turn index for a snippet chat session."""
        idx = 1
        if not self.chat_log.exists():
            return idx
        with self.chat_log.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if str(obj.get("snippet_id", "")).strip() != snippet_id:
                    continue
                try:
                    idx = max(idx, _to_int(obj.get("turn_index", 0), 0) + 1)
                except Exception:
                    continue
        return idx

    def append_turn(
        self,
        *,
        snippet_id: str,
        role: str,
        text: str,
        provider: str,
        model: str,
        session_id: str,
    ) -> dict[str, object]:
        """Append one user or assistant turn to the persistent chat log."""
        if role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        if not text.strip():
            raise ValueError("turn text cannot be empty")

        entry = {
            "timestamp_utc": utc_now(),
            "participant_id": str(self.lock_data.get("participant_id", "")).strip(),
            "snippet_id": snippet_id,
            "turn_index": self.next_turn_index(snippet_id),
            "role": role,
            "text": text,
            "provider": provider.strip(),
            "model": model.strip(),
            "session_id": session_id.strip(),
        }

        self.chat_log.parent.mkdir(parents=True, exist_ok=True)
        if not self.chat_log.exists():
            self.chat_log.write_text("", encoding="utf-8")
        with self.chat_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Keep snippet_log.csv synchronized with chat-derived fields so
        # export validation sees the same turns/model/prompt state as the UI.
        rows = self.read_rows()
        for row in rows:
            if (row.get("snippet_id") or "").strip() != snippet_id:
                continue
            normalized = self._auto_fill_row_from_chat(snippet_id, dict(row))
            row.clear()
            row.update(normalized)
            break
        self.write_rows(rows)
        return entry

    def read_chat_entries(self, snippet_id: str) -> list[dict[str, object]]:
        """Return chat log entries for one snippet in logged order."""
        self._assert_known_snippet(snippet_id)
        rows: list[dict[str, object]] = []
        if not self.chat_log.exists():
            return rows

        with self.chat_log.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                sid = str(obj.get("snippet_id", "")).strip()
                if sid != snippet_id:
                    continue

                role = str(obj.get("role", "")).strip().lower()
                if role not in {"user", "assistant"}:
                    continue

                rows.append(
                    {
                        "snippet_id": sid,
                        "turn_index": _to_int(obj.get("turn_index", 0), 0),
                        "role": role,
                        "text": str(obj.get("text", "")),
                        "timestamp_utc": str(obj.get("timestamp_utc", "")),
                        "provider": str(obj.get("provider", "")),
                        "model": str(obj.get("model", "")),
                        "session_id": str(obj.get("session_id", "")),
                    }
                )

        rows.sort(key=lambda x: _to_int(x.get("turn_index", 0), 0))
        if len(rows) > self.max_chat_history_entries:
            rows = rows[-self.max_chat_history_entries :]
        return rows

    def chat_messages_for_ollama(self, snippet_id: str, *, max_chars: int | None = None) -> list[dict[str, str]]:
        """Map snippet chat history to Ollama chat format while keeping context small."""
        entries = self.read_chat_entries(snippet_id)
        char_budget = self.max_chat_context_chars if max_chars is None else max(0, int(max_chars))
        if char_budget <= 0:
            return []

        msgs: list[dict[str, str]] = []
        used_chars = 0
        for entry in reversed(entries):
            role = str(entry.get("role", "")).strip().lower()
            txt = str(entry.get("text", ""))
            if role not in {"user", "assistant"} or not txt.strip():
                continue
            if len(txt) > self.max_turn_text_chars:
                txt = txt[-self.max_turn_text_chars :]
            if used_chars and (used_chars + len(txt)) > char_budget:
                break
            if not used_chars and len(txt) > char_budget:
                txt = txt[-char_budget:]
            msgs.append({"role": role, "content": txt})
            used_chars += len(txt)
            if len(msgs) >= self.max_chat_context_messages:
                break
        msgs.reverse()
        return msgs

    def _auto_fill_row_from_chat(self, snippet_id: str, row: dict[str, str]) -> dict[str, str]:
        """Fill turn/prompt/model fields from logged chat so participant cannot manually spoof them."""
        out = dict(row)

        llm = self.lock_data.get("llm", {}) if isinstance(self.lock_data, dict) else {}
        locked_provider = str(llm.get("provider", "ollama")) if isinstance(llm, dict) else "ollama"
        locked_model = str(llm.get("model", "")) if isinstance(llm, dict) else ""

        if not (out.get("tool") or "").strip():
            out["tool"] = locked_provider.capitalize() if locked_provider else "Ollama"
        if not (out.get("model") or "").strip() and locked_model:
            out["model"] = locked_model

        entries = self.read_chat_entries(snippet_id)
        if not entries:
            # Keep turns explicit and valid even when participant used no LLM
            # turns for this snippet.
            out["turns"] = str(int((out.get("turns") or "0").strip() or "0"))
            out["first_prompt"] = str(out.get("first_prompt") or "")
            out["final_prompt"] = str(out.get("final_prompt") or "")
            return out

        out["turns"] = str(len(entries))

        prompts = [
            str(e.get("text", "")).strip()
            for e in entries
            if str(e.get("role", "")).strip().lower() == "user" and str(e.get("text", "")).strip()
        ]
        if prompts:
            out["first_prompt"] = prompts[0][: self.max_field_chars]
            out["final_prompt"] = prompts[-1][: self.max_field_chars]

        latest = entries[-1]
        latest_model = str(latest.get("model", "")).strip()
        latest_provider = str(latest.get("provider", "")).strip()
        if latest_model:
            out["model"] = latest_model
        if latest_provider:
            out["tool"] = latest_provider.capitalize()

        try:
            applied = int((out.get("applied_turns") or "").strip())
            total_turns = int(out["turns"])
            if applied > total_turns:
                out["applied_turns"] = str(total_turns)
        except Exception:
            pass

        return out

    def build_submission_zip(self) -> tuple[int, str]:
        """Run the packaged ZIP builder and return its exit code plus console output."""
        self.mark_end()
        proc = subprocess.run(
            [sys.executable, str(self.packager_path)],
            cwd=str(self.kit_root),
            capture_output=True,
            text=True,
        )
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc.returncode, output

def html_page(csrf_token: str) -> str:
    """Return participant UI HTML for the in-kit web app."""
    return r"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>RepairAudit Participant App</title>
<style>
:root{--bg:#f4f8ff;--panel:#ffffff;--text:#0f2039;--muted:#5c6f8b;--line:#d5e3ff;--accent:#2d79ff;--accent-dark:#1f5fd1;--ok:#0a8f4e;--bad:#c53b32}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:linear-gradient(180deg,#f9fbff 0%,#eff5ff 100%);font-family:Segoe UI,Arial,sans-serif;color:var(--text)}
.wrap{width:100%;max-width:none;margin:0;padding:16px 18px}
.hdr{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px 16px;margin-bottom:12px;box-shadow:0 8px 20px rgba(30,74,138,.08)}
.hdr h1{margin:0;font-size:23px}
.sub{margin-top:5px;color:var(--muted);font-size:13px}
.conn{position:absolute;top:12px;right:12px;border:1px solid #c7dcff;background:#edf5ff;color:#275089;border-radius:999px;padding:6px 10px;font-size:12px;font-weight:700}
.conn.bad{border-color:#efb6b2;background:#ffecec;color:#8c2f2b}
.timer{margin-top:8px;display:inline-block;padding:7px 10px;border:1px solid #cfe0ff;border-radius:10px;background:#eef5ff;color:#2a4f87;font-size:12px;font-weight:600}
.notice{margin-top:8px;padding:9px 11px;border:1px solid #ffd4c2;background:#fff5f0;border-radius:10px;color:#8a3f20;font-size:12px}
.grid{display:grid;grid-template-columns:.52fr 1.1fr .95fr;gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:12px;box-shadow:0 7px 18px rgba(30,74,138,.06)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sp{display:flex;justify-content:space-between;align-items:center}
.lbl{font-size:12px;color:var(--muted)}
.prog{font-size:14px;font-weight:700}
.btn{border:none;border-radius:11px;padding:9px 12px;font-weight:700;cursor:pointer;background:var(--accent);color:#fff}
.btn:hover{background:var(--accent-dark)}
.btn.alt{background:#eef4ff;color:#214c8e;border:1px solid #ccddff}
.btn.ok{background:var(--ok);color:#ffffff}
.btn.tiny{padding:6px 9px;font-size:12px}
.btn.guideaction{min-width:132px;justify-content:center;text-align:center}
.msg{margin-top:8px;font-size:13px}
.msg.ok{color:var(--ok)}
.msg.err{color:var(--bad)}
.list{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.snip{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border:1px solid #d8e6ff;border-radius:10px;background:#fff;cursor:pointer}
.snip.active{border-color:#7fb0ff;background:#f2f8ff}
.tag{font-size:11px;padding:3px 7px;border-radius:999px;background:#ecf3ff;color:#2c558f}
.readme{max-height:220px;overflow:auto;font-size:13px;line-height:1.45;color:#334f76;border:1px solid #e1ebff;border-radius:10px;padding:10px 11px;background:#fbfdff}
.readme .guideintro{margin:0 0 10px;color:#214c8e;font-weight:700}
.readme h4{margin:0 0 6px;color:#214c8e;font-size:13px}
.readme p{margin:0 0 10px}
.readme ul{margin:0 0 10px 18px;padding:0}
.readme li{margin:0 0 6px}
.readme .guidenotice{margin-top:8px;padding:9px 11px;border:1px solid #ffd4c2;background:#fff5f0;border-radius:10px;color:#8a3f20;font-size:12px;font-weight:700}
textarea,input{width:100%;border:1px solid #ccddff;border-radius:10px;padding:8px 10px;background:#fff;color:var(--text)}
textarea{font-family:Consolas,monospace;font-size:13px;min-height:220px;line-height:1.5;tab-size:4}
#baseline_code{border:1px solid #ccddff;border-radius:10px;background:#f8fbff;min-height:170px;max-height:360px;overflow:auto;font-family:Consolas,monospace;font-size:13px;line-height:1.5;tab-size:4}
#baseline_code.expanded{min-height:520px;height:70vh;max-height:none}
.baselineempty{padding:10px 12px;color:#5c6f8b}
.baselineLine{display:grid;grid-template-columns:56px minmax(0,1fr);align-items:stretch;border-bottom:1px solid #edf3ff;cursor:pointer}
.baselineLine:last-child{border-bottom:none}
.baselineLine:hover{background:#eef5ff}
.baselineLine.anchor{background:#edf4ff}
.baselineLine.marked{background:#dfeeff}
.baselineNum{padding:0 10px;color:#6a7f9e;text-align:right;border-right:1px solid #e1ebff;user-select:none}
.baselineText{padding:0 12px;white-space:pre;overflow-x:auto}
#chat_prompt{min-height:105px}
.hint{font-size:12px;color:#496584}
/* Match dropdown styling with the rest of the UI. */
select{
  width:100%;
  border:1px solid #c7dbff;
  border-radius:10px;
  padding:8px 10px;
  color:var(--text);
  font-weight:600;
  background:linear-gradient(180deg,#ffffff 0%,#f6faff 100%);
}
select:focus{
  outline:none;
  border-color:#82adff;
  box-shadow:0 0 0 3px rgba(45,121,255,0.18);
}
.chatlog{margin-top:8px;border:1px solid #d8e6ff;border-radius:10px;padding:9px;background:#fbfdff}
.chatlog.expanded .chatdetail{max-height:680px}
.chatindex{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.chatindexbtn{border:1px solid #ccddff;background:#ffffff;color:#214c8e;border-radius:999px;padding:6px 10px;font-size:12px;font-weight:700;cursor:pointer}
.chatindexbtn.active{border-color:#7fb0ff;background:#eaf2ff}
.chatdetail{min-height:220px;max-height:420px;overflow:auto}
.chatrow{margin:0 0 8px 0;padding:8px 10px;border-radius:9px;white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.45}
.chatrow.user{background:#eaf2ff;border:1px solid #c9dcff}
.chatrow.assistant{background:#eef8f1;border:1px solid #cde7d2}
.chatmeta{font-size:11px;color:#5c6f8b;margin-bottom:3px}
.form{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.full{grid-column:1/-1}
.modalBack{position:fixed;inset:0;background:rgba(15,32,57,.52);display:none;align-items:center;justify-content:center;padding:20px;z-index:999}
.modalBack.show{display:flex}
.modalCard{width:min(840px,100%);max-height:88vh;overflow:auto;background:#fff;border:1px solid #cfe0ff;border-radius:18px;padding:18px 18px 14px 18px;box-shadow:0 24px 60px rgba(15,32,57,.24)}
.modalCard h2{margin:0 0 8px 0;font-size:22px}
.modalCard h3{margin:14px 0 6px 0;font-size:15px}
.modalCard p,.modalCard li{font-size:13px;line-height:1.5;color:#294564}
.modalCard ul{margin:6px 0 0 18px;padding:0}
.modalCard .example{margin-top:4px;padding:8px 10px;border:1px solid #d9e7ff;border-radius:10px;background:#f7faff;color:#1f426e;font-family:Consolas,monospace;font-size:12px}
@media (max-width:1280px){.grid{grid-template-columns:1fr}.form{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <div class="conn" id="connBadge" title="Shows whether the browser is currently connected to the local study server.">Backend: Connecting...</div>
    <h1>RepairAudit Participant App</h1>
    <div class="sub" id="meta">Loading study workspace...</div>
    <div class="timer" id="liveTimer">Session Time: 0h 0m 0s</div>
    <div class="notice">PRIVACY REMINDER: DO NOT INCLUDE PERSONAL IDENTIFIERS OR SENSITIVE ACCOUNT DATA IN PROMPTS, NOTES, OR CHAT TEXT.</div>
  </div>

  <div class="grid">
    <section class="card">
      <div class="sp"><strong title="Tracks snippet completion across this run.">Progress</strong><span class="tag" id="doneCount" title="Completed snippets out of total snippets.">0 / 0 complete</span></div>
      <div class="list" id="snippetList" title="Click any snippet to jump directly to it."></div>
      <hr style="border:none;border-top:1px solid #e5edff;margin:12px 0" />
      <button class="btn ok" id="zipBtn" style="width:100%" title="Validates required fields and builds your final submission ZIP.">Finish (Build ZIP)</button>
      <div class="lbl" style="margin-top:8px">Finish validates your files and creates the submission zip.</div>
      <div class="msg" id="msg"></div>
    </section>

    <section class="card">
      <div class="sp">
        <div class="prog" id="progress">Snippet 1 of 1</div>
        <div class="row">
          <button class="btn alt" id="prevBtn" title="Save current snippet, then move to previous snippet.">Previous</button>
          <button class="btn" id="saveBtn" title="Save current edited code and snippet summary fields.">Save</button>
          <button class="btn alt" id="nextBtn" title="Save current snippet, then move to next snippet.">Next</button>
        </div>
      </div>
      <div class="lbl" id="sidLbl" style="margin:8px 0" title="Current snippet identifier."></div>

      <div class="sp" style="margin:8px 0 4px 0">
        <div class="lbl" title="Original vulnerable code for this snippet.">Baseline (read-only)</div>
        <div class="row">
          <button class="btn alt tiny" id="toggleBaselineSizeBtn" type="button" title="Expand or collapse the baseline code pane.">Expand Baseline</button>
          <button class="btn alt tiny" id="copyBaselineBtn" title="Copy baseline code to clipboard.">Copy Baseline</button>
          <button class="btn alt tiny" id="copyMarkedBaselineBtn" type="button" title="Copy only the marked baseline line or line range.">Copy Marked Lines</button>
          <button class="btn alt tiny" id="clearBaselineSelectionBtn" type="button" title="Clear the current baseline line marks.">Clear Marks</button>
        </div>
      </div>
      <div class="lbl" id="baselineMeta" style="margin:0 0 4px 0" title="Language and file name for this baseline snippet."></div>
      <div id="baseline_code" title="Baseline snippet is read-only. Click one line, then another line, to mark a range."></div>
      <div class="lbl" id="baselineSelectionNote" style="margin:6px 0 0 0">Click one line, then another line, to mark a range. Use Copy Marked Lines when you only need part of the file.</div>
      <div class="lbl" style="margin:10px 0 4px 0" title="Paste and refine the final code you want to submit for this snippet.">Final Submitted Code</div>
      <textarea id="edited_code" wrap="off" spellcheck="false" title="Paste the final code here, then edit it until it matches what you want to submit."></textarea>

      <hr style="border:none;border-top:1px solid #e5edff;margin:12px 0" />
      <div class="sp">
        <strong title="Use this panel to chat with the LLM assigned to this kit. Prompts and replies are auto-logged to this snippet.">In-App LLM Chat</strong>
        <div class="row">
          <span class="tag" id="chatTurnCount" title="Auto-logged turns for the current snippet.">0 turns</span>
          <button class="btn alt tiny" id="toggleChatSizeBtn" type="button" title="Expand or collapse the visible chat history area.">Expand Chat</button>
        </div>
      </div>
      <div class="lbl" id="ollamaStatus" style="margin-top:6px" title="Connection/model status for the assigned LLM endpoint.">Checking assigned LLM connection...</div>
      <div class="chatlog" id="chatHistory" title="Chat history for the currently selected snippet.">
        <div class="chatindex" id="chatHistoryIndex" title="Select a numbered prompt/reply exchange to inspect."></div>
        <div class="chatdetail" id="chatHistoryDetail" title="Prompt and reply text for the selected exchange."></div>
      </div>
      <div class="full" style="margin-top:8px">
        <label class="lbl" title="Enter one prompt for the assigned LLM about the current snippet.">Chat Prompt</label>
        <div class="hint" style="margin-bottom:6px">Larger inputs can take longer to answer. If a reply is slow, wait before retrying or send a smaller function or block.</div>
        <textarea id="chat_prompt" placeholder="Chat with the LLM about this snippet as you would in a real life setting. Please type here to start your chat." title="Press Ctrl+Enter to send quickly."></textarea>
      </div>
      <div class="row" style="margin-top:8px">
        <button class="btn" id="sendChatBtn" title="Send prompt to the assigned LLM and auto-log both user and assistant turns.">Send To LLM</button>
        <button class="btn alt tiny" id="discardChatBtn" type="button" style="display:none" title="Stop waiting for the current reply and discard it.">Stop / Discard Reply</button>
      </div>
      <div class="lbl" style="margin-top:6px">Prompts and replies here are auto-logged for this snippet.</div>
    </section>

    <section class="card">
      <div class="sp"><strong title="Web app usage instructions for this study task.">Web App Guide</strong><div class="row"><button class="btn alt tiny guideaction" id="showOnboardingBtn" title="Open the short onboarding guide again.">Show Onboarding</button><button class="btn alt tiny guideaction" id="toggleReadme" title="Show or hide the guide panel.">Hide</button></div></div>
      <div class="readme" id="readme" style="margin-top:8px" title="Step-by-step instructions for completing this study inside the app."></div>

      <hr style="border:none;border-top:1px solid #e5edff;margin:12px 0" />
      <strong title="Required and optional metadata for this snippet.">Snippet Summary</strong>
      <div class="form" style="margin-top:8px">
        <div>
          <label class="lbl" title="How many logged turns directly influenced your final code for this snippet.">Applied Turns</label>
          <input id="applied_turns" placeholder="integer, <= total auto turns" title="Whole number. Must be less than or equal to auto-logged turns." />
          <div class="row" style="margin-top:6px">
            <button class="btn alt tiny" id="appliedZeroBtn" type="button" title="Set applied turns to zero.">Use 0</button>
            <button class="btn alt tiny" id="appliedOneBtn" type="button" title="Set applied turns to one.">Use 1</button>
            <button class="btn alt tiny" id="appliedAllBtn" type="button" title="Set applied turns equal to total auto-logged turns.">Use All</button>
          </div>
          <div class="lbl" id="autoTurnsNote" style="margin-top:6px">Auto-logged turns for this snippet: 0</div>
        </div>
        <div><label class="lbl" title="Main prompting approach used for this snippet.">Primary Strategy</label><select id="strategy_primary" title="Choose the main prompt strategy you used for this snippet."><option value="">Select...</option><option value="zero_shot">Zero-Shot</option><option value="few_shot">Few-Shot</option><option value="chain_of_thought">Chain-of-Thought</option><option value="adaptive_chain_of_thought">Adaptive Chain-of-Thought</option><option value="other">Other</option></select></div>
        <div class="full" id="strategyOtherWrap" style="display:none"><label class="lbl" title="Describe the strategy you used when Other is selected.">Other Strategy</label><input id="strategy_other_text" placeholder="brief strategy label or description" title="Required when Primary Strategy is Other." /></div>
        <div><label class="lbl" title="How confident you are that your final submitted code is secure.">Confidence (1-5)</label><select id="confidence_1to5" title="Rate how confident you are that your final submitted code is secure. 1 = low confidence, 5 = high confidence."><option value="">Select...</option><option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="4">4</option><option value="5">5</option></select></div>
        <div class="full"><label class="lbl" title="Optional factual notes about your process for this snippet.">Notes</label><input id="notes" placeholder="optional notes" title="Optional. Avoid personal or sensitive information." /></div>
      </div>


    </section>
  </div>
</div>

<div class="modalBack" id="onboardingBackdrop" aria-hidden="true">
  <div class="modalCard" role="dialog" aria-modal="true" aria-labelledby="onboardingTitle">
    <div class="sp">
      <h2 id="onboardingTitle">Before You Start</h2>
      <div class="row">
        <button class="btn ok" id="beginStudyBtn" type="button" title="Save the participant profile, close this popup, and start the study timer.">Begin Study</button>
        <button class="btn alt tiny" id="closeOnboardingBtn" type="button" title="Close the onboarding guide.">Close</button>
      </div>
    </div>
    <div id="onboardingBody"></div>
    <hr style="border:none;border-top:1px solid #e5edff;margin:12px 0" />
    <strong id="participantProfileHeading" title="Participant-level background information used in later aggregate analysis.">Participant Profile</strong>
    <div class="lbl" id="participantProfileNote" style="margin-top:6px">Complete this once before clicking Begin Study.</div>
    <div class="form" style="margin-top:8px">
      <div><label class="lbl" title="Your overall programming experience.">Programming Experience</label><select id="programming_experience"><option value="">Select...</option><option value="<1 year">&lt;1 year</option><option value="1-2 years">1-2 years</option><option value="3-5 years">3-5 years</option><option value="6+ years">6+ years</option></select></div>
      <div><label class="lbl" title="Your experience with the programming language or languages used in this study.">Language Experience</label><select id="language_experience"><option value="">Select...</option><option value="none">None</option><option value="basic">Basic</option><option value="intermediate">Intermediate</option><option value="advanced">Advanced</option></select></div>
      <div><label class="lbl" title="How often you use LLMs for coding.">LLM Coding Experience</label><select id="llm_coding_experience"><option value="">Select...</option><option value="never">Never</option><option value="rarely">Rarely</option><option value="monthly">Monthly</option><option value="weekly">Weekly</option><option value="daily">Daily</option></select></div>
      <div><label class="lbl" title="Your security training or practice background.">Security Experience</label><select id="security_experience"><option value="">Select...</option><option value="none">None</option><option value="self-taught">Self-taught</option><option value="coursework">Coursework</option><option value="professional">Professional</option></select></div>
    </div>
  </div>
</div>

<script>
var CSRF_TOKEN = "__CSRF_TOKEN__";
var state = null;
var idx = 0;
var currentSid = "";
var pingTimer = null;
var timerTick = null;
var timerBaseSeconds = 0;
var timerBaseMs = Date.now();
var onboardingPrompted = false;
var chatExpanded = false;
var onboardingStorageKey = "participant_console_onboarding_v4";
var backendConnected = true;
var timerFrozenSeconds = 0;
var activeChatXhr = null;
var activeChatRequestId = "";
var baselineExpanded = false;
var chatExchangesBySnippet = {};
var chatExchangeSelection = {};
var baselineTextBySnippet = {};
var baselineSelectionBySnippet = {};
var baselineAnchorLineBySnippet = {};

// Compatibility fallback for environments that do not provide Number.isFinite.
if(typeof Number.isFinite !== "function"){
  Number.isFinite = function(n){
    return typeof n === "number" && isFinite(n);
  };
}

function byId(id){ return document.getElementById(id); }

function addEvt(el, evt, fn){
  if(!el || !evt || !fn){ return; }
  if(el.addEventListener){
    el.addEventListener(evt, fn);
    return;
  }
  if(el.attachEvent){
    el.attachEvent("on" + evt, fn);
  }
}

function setMsg(text, ok){
  var el = byId("msg");
  if(!el){ return; }
  el.textContent = text || "";
  el.className = "msg " + (ok ? "ok" : "err");
}

function setConn(ok){
  var el = byId("connBadge");
  var wasConnected = backendConnected;
  backendConnected = !!ok;
  if(!backendConnected && wasConnected && studyStarted()){
    timerFrozenSeconds = currentTimerSeconds();
  }
  if(!el){ return; }
  if(ok){
    el.textContent = "Backend: Connected";
    el.className = "conn";
  } else {
    el.textContent = "Backend: Connection Issue";
    el.className = "conn bad";
  }
}

function formatSecs(total){
  var s = Math.max(0, Math.floor(Number(total || 0)));
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var r = s % 60;
  return h + "h " + m + "m " + r + "s";
}

function currentTimerSeconds(){
  var elapsed = Math.max(0, (Date.now() - timerBaseMs) / 1000);
  return Math.max(0, timerBaseSeconds + elapsed);
}

function updateTimerBase(totalSeconds){
  var next = Number(totalSeconds || 0);
  if(!Number.isFinite(next)){
    next = 0;
  }
  timerBaseSeconds = Math.max(next, currentTimerSeconds());
  timerFrozenSeconds = timerBaseSeconds;
  timerBaseMs = Date.now();
}

function renderLiveTimer(){
  var timer = byId("liveTimer");
  if(!timer){ return; }
  if(!studyStarted()){
    timer.textContent = "Session Time: starts when you click Begin Study";
    return;
  }
  if(!backendConnected){
    timer.textContent = "Session Time: " + formatSecs(timerFrozenSeconds) + " (paused - reconnecting)";
    return;
  }
  timer.textContent = "Session Time: " + formatSecs(currentTimerSeconds());
}

function api(path, method, body, onOk, onErr){
  var xhr = new XMLHttpRequest();
  xhr.open(method || "GET", path, true);
  xhr.setRequestHeader("Content-Type", "application/json");
  xhr.setRequestHeader("X-CSRF-Token", CSRF_TOKEN);
  xhr.onreadystatechange = function(){
    if(xhr.readyState !== 4 || xhr.__repairAuditAborted){ return; }
    var data = {};
    try { data = JSON.parse(xhr.responseText || "{}"); } catch(_e) { data = {}; }
    if(xhr.status >= 200 && xhr.status < 300){
      onOk && onOk(data);
    } else {
      onErr && onErr(data.error || data.message || ("Request failed: " + xhr.status));
    }
  };
  xhr.onabort = function(){
    xhr.__repairAuditAborted = true;
  };
  xhr.onerror = function(){
    if(xhr.__repairAuditAborted){ return; }
    onErr && onErr("Network request failed.");
  };
  xhr.send(body ? JSON.stringify(body) : null);
  return xhr;
}

function snippetStatusFor(sid){
  if(!state || !state.completion || !state.completion.snippets){ return null; }
  var arr = state.completion.snippets || [];
  for(var i=0;i<arr.length;i++){
    var row = arr[i] || {};
    if(String(row.snippet_id || "") === String(sid)){ return row; }
  }
  return null;
}

function snippetLabel(sid){
  if(state && state.snippet_labels && state.snippet_labels[sid]){
    return String(state.snippet_labels[sid]);
  }
  return String(sid || "");
}

function snippetFileName(sid){
  if(state && state.snippet_files && state.snippet_files[sid]){
    return String(state.snippet_files[sid]);
  }
  return "";
}

function snippetLanguageLabel(sid){
  var fileName = snippetFileName(sid);
  var dot = fileName.lastIndexOf(".");
  var ext = dot >= 0 ? fileName.slice(dot).toLowerCase() : "";
  var labels = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".cs": "C#",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".ts": "TypeScript"
  };
  return labels[ext] || (ext ? ext.slice(1).toUpperCase() : "Code");
}

function normalizeSnippetText(text){
  return String(text || "").replace(/\\r\\n/g, "\\n").replace(/\\r/g, "\\n");
}

function looksFlattenedCode(text){
  var lines = normalizeSnippetText(text).split("\\n").filter(function(line){
    return line.trim() !== "";
  });
  if(lines.length <= 1){
    return true;
  }
  var maxLen = 0;
  var longCount = 0;
  var totalLen = 0;
  for(var i = 0; i < lines.length; i++){
    var len = lines[i].length;
    totalLen += len;
    if(len > maxLen){ maxLen = len; }
    if(len > 180){ longCount += 1; }
  }
  var avgLen = totalLen / lines.length;
  return maxLen > 260 || avgLen > 160 || longCount >= Math.max(2, Math.floor(lines.length / 3));
}

function trimLeftText(text){
  return String(text || "").replace(/^\s+/, "");
}

function trimRightText(text){
  return String(text || "").replace(/\s+$/, "");
}

function splitDenseCommentLine(line){
  if(line.indexOf("//") !== 0){
    return [line];
  }
  var markers = [
    " if (",
    " for (",
    " while (",
    " switch (",
    " return ",
    " const ",
    " let ",
    " var ",
    " int ",
    " bool ",
    " char ",
    " void ",
    " static ",
    " class ",
    " try ",
    " catch ",
    " else ",
    " DBUG_",
    " /*"
  ];
  var splitAt = -1;
  for(var i = 0; i < markers.length; i++){
    var pos = line.indexOf(markers[i], 3);
    if(pos > 10 && (splitAt === -1 || pos < splitAt)){
      splitAt = pos;
    }
  }
  if(splitAt === -1){
    return [line];
  }
  return [
    trimRightText(line.slice(0, splitAt)),
    trimLeftText(line.slice(splitAt))
  ];
}

function splitDenseBlockCommentLine(line){
  var pos = line.indexOf("*/");
  if(pos === -1 || pos >= line.length - 2){
    return [line];
  }
  var tail = trimLeftText(line.slice(pos + 2));
  if(!tail){
    return [line];
  }
  return [
    trimRightText(line.slice(0, pos + 2)),
    tail
  ];
}

function formatFlatCStyleCode(text){
  var rawLines = [];
  var token = [];
  var inString = "";
  var escaping = false;
  var index = 0;
  var length = text.length;

  while(index < length){
    var ch = text.charAt(index);

    if(!inString && text.slice(index, index + 2) === "/*"){
      var blockPiece = trimRightText(token.join(""));
      if(blockPiece){
        rawLines.push(blockPiece);
      }
      token = [];
      var blockEnd = text.indexOf("*/", index + 2);
      if(blockEnd === -1){
        rawLines.push(text.slice(index).trim());
        break;
      }
      rawLines.push(text.slice(index, blockEnd + 2).trim());
      index = blockEnd + 2;
      continue;
    }

    if(!inString && text.slice(index, index + 2) === "//"){
      var commentPiece = trimRightText(token.join(""));
      if(commentPiece){
        rawLines.push(commentPiece);
      }
      token = [];
      var lineEnd = text.indexOf("\\n", index + 2);
      if(lineEnd === -1){
        var tail = text.slice(index).trim();
        var splitTail = splitDenseCommentLine(tail);
        for(var st = 0; st < splitTail.length; st++){
          if(splitTail[st]){
            rawLines.push(splitTail[st]);
          }
        }
        break;
      }
      rawLines.push(text.slice(index, lineEnd).trim());
      index = lineEnd;
      continue;
    }

    token.push(ch);
    if(inString){
      if(escaping){
        escaping = false;
        index += 1;
        continue;
      }
      if(ch === "\\\\"){
        escaping = true;
        index += 1;
        continue;
      }
      if(ch === inString){
        inString = "";
      }
      index += 1;
      continue;
    }

    if(ch === '"' || ch === "'"){
      inString = ch;
      index += 1;
      continue;
    }

    if(ch === "{" || ch === "}" || ch === ";"){
      var piece = token.join("").trim();
      if(piece){
        rawLines.push(piece);
      }
      token = [];
      index += 1;
      continue;
    }

    index += 1;
  }

  var tail = token.join("").trim();
  if(tail){
    rawLines.push(tail);
  }

  var indent = 0;
  var lines = [];
  for(var lineIndex = 0; lineIndex < rawLines.length; lineIndex++){
    var rawLine = rawLines[lineIndex];
    var blockParts = splitDenseBlockCommentLine(rawLine);
    for(var blockIndex = 0; blockIndex < blockParts.length; blockIndex++){
      var denseParts = splitDenseCommentLine(blockParts[blockIndex]);
      for(var denseIndex = 0; denseIndex < denseParts.length; denseIndex++){
        var line = denseParts[denseIndex].trim().replace(/\s+/g, " ");
        if(!line){
          continue;
        }
        if(line.indexOf("}") === 0){
          indent = Math.max(0, indent - 1);
        }
        lines.push(new Array(indent + 1).join("    ") + line);
        var opens = (line.match(/\{/g) || []).length;
        var closes = (line.match(/\}/g) || []).length;
        if(opens > closes){
          indent += (opens - closes);
        } else if(closes > opens && line.indexOf("}") !== 0){
          indent = Math.max(0, indent - (closes - opens));
        }
      }
    }
  }
  return lines.join("\\n");
}

function pythonLineOpensBlock(line){
  var stripped = String(line || "").trim();
  if(!stripped || stripped.charAt(stripped.length - 1) !== ":"){
    return false;
  }
  var starters = [
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
    "case "
  ];
  for(var i = 0; i < starters.length; i++){
    if(stripped.indexOf(starters[i]) === 0){
      return true;
    }
  }
  return false;
}

function pythonLineDedentsFirst(line){
  var stripped = String(line || "").trim();
  return (
    stripped.indexOf("elif ") === 0 ||
    stripped.indexOf("else:") === 0 ||
    stripped.indexOf("except") === 0 ||
    stripped.indexOf("finally:") === 0 ||
    stripped.indexOf("case ") === 0
  );
}

function splitFlatPythonSegments(text){
  var segments = [];
  var token = [];
  var inString = "";
  var escaping = false;
  var bracketDepth = 0;
  var index = 0;
  var length = text.length;

  while(index < length){
    var ch = text.charAt(index);
    token.push(ch);

    if(inString){
      if(escaping){
        escaping = false;
        index += 1;
        continue;
      }
      if(ch === "\\\\"){
        escaping = true;
        index += 1;
        continue;
      }
      if(ch === inString){
        inString = "";
      }
      index += 1;
      continue;
    }

    if(ch === '"' || ch === "'"){
      inString = ch;
      index += 1;
      continue;
    }

    if(ch === "(" || ch === "[" || ch === "{"){
      bracketDepth += 1;
      index += 1;
      continue;
    }
    if(ch === ")" || ch === "]" || ch === "}"){
      bracketDepth = Math.max(0, bracketDepth - 1);
      index += 1;
      continue;
    }

    if(ch === "#" && bracketDepth === 0){
      segments.push(token.join("").trim());
      break;
    }

    if(ch === ";" && bracketDepth === 0){
      var piece = token.slice(0, token.length - 1).join("").trim();
      if(piece){
        segments.push(piece);
      }
      token = [];
      index += 1;
      continue;
    }

    if(ch === ":" && bracketDepth === 0){
      var blockPiece = token.join("").trim();
      var tail = text.slice(index + 1).trim();
      if(blockPiece && tail && pythonLineOpensBlock(blockPiece)){
        segments.push(blockPiece);
        token = [];
      }
      index += 1;
      continue;
    }

    index += 1;
  }

  var tail = token.join("").trim();
  if(tail){
    segments.push(tail);
  }
  return segments;
}

function formatFlatPythonCode(text){
  var segments = splitFlatPythonSegments(text);
  var indent = 0;
  var lines = [];
  for(var i = 0; i < segments.length; i++){
    var line = segments[i].trim();
    if(!line){
      continue;
    }
    if(pythonLineDedentsFirst(line)){
      indent = Math.max(0, indent - 1);
    }
    lines.push(new Array(indent + 1).join("    ") + line);
    if(pythonLineOpensBlock(line)){
      indent += 1;
    }
  }
  return lines.join("\\n");
}

function formatBaselineForDisplay(text, sid){
  var normalized = normalizeSnippetText(text).trim();
  if(!normalized){
    return "";
  }
  var fileName = snippetFileName(sid).toLowerCase();
  var ext = fileName.slice(fileName.lastIndexOf("."));
  var cLike = {
    ".c": true,
    ".cc": true,
    ".cpp": true,
    ".cxx": true,
    ".cs": true,
    ".go": true,
    ".java": true,
    ".js": true,
    ".php": true,
    ".ts": true
  };
  if(cLike[ext] && looksFlattenedCode(normalized)){
      normalized = formatFlatCStyleCode(
        normalized
        .split("\\n")
        .map(function(line){ return line.trim(); })
        .filter(function(line){ return line !== ""; })
        .join(" ")
    );
  }
  if(ext === ".py" && looksFlattenedCode(normalized)){
    normalized = formatFlatPythonCode(
      normalized
      .split("\\n")
      .map(function(line){ return line.trim(); })
      .filter(function(line){ return line !== ""; })
      .join(" ")
    );
  }
  return normalized + "\\n";
}

function renderSidebar(){
  if(!state){ return; }
  var list = byId("snippetList");
  if(!list){ return; }
  list.innerHTML = "";
  var ids = state.snippet_ids || [];

  var done = 0;
  var total = ids.length;
  if(state.completion && typeof state.completion.completed_count !== "undefined"){
    done = Number(state.completion.completed_count || 0);
  }
  var doneEl = byId("doneCount");
  if(doneEl){ doneEl.textContent = done + " / " + total + " complete"; }

  for(var i=0;i<ids.length;i++){
    var sid = ids[i];
    var st = snippetStatusFor(sid);
    var complete = !!(st && st.complete);
    var row = document.createElement("div");
    row.className = "snip" + (i === idx ? " active" : "");
    row.title = complete ? "Snippet complete. Click to review." : "Snippet in progress. Click to continue.";
    var left = document.createElement("span");
    left.textContent = snippetLabel(sid);
    var badge = document.createElement("span");
    badge.className = "tag";
    badge.textContent = complete ? "Complete" : "In Progress";
    row.appendChild(left);
    row.appendChild(badge);
    row.onclick = (function(j){ return function(){ idx = j; loadSnippet(); }; })(i);
    list.appendChild(row);
  }
}

function fillProfile(profile){
  profile = profile || {};
  var fields = ["programming_experience","language_experience","llm_coding_experience","security_experience"];
  for(var i=0;i<fields.length;i++){
    var key = fields[i];
    var el = byId(key);
    if(el){ el.value = profile[key] || ""; }
  }
}

function collectProfile(){
  var out = {};
  var fields = ["programming_experience","language_experience","llm_coding_experience","security_experience"];
  for(var i=0;i<fields.length;i++){
    var key = fields[i];
    var el = byId(key);
    out[key] = el ? (el.value || "").trim() : "";
  }
  return out;
}


function saveProfile(onDone){
  api("/api/save_profile", "POST", collectProfile(), function(resp){
    if(!state){ state = {}; }
    state.participant_profile = (resp && resp.participant_profile) ? resp.participant_profile : collectProfile();
    if(onDone){ onDone(null, resp); }
    else { refreshState(); }
  }, function(msg){
    if(onDone){ onDone(msg); }
    else { setMsg("Could not save participant profile: " + msg, false); }
  });
}

function wireProfileInputs(){
  var profileInputs = ["programming_experience","language_experience","llm_coding_experience","security_experience"];
  for(var pi=0; pi<profileInputs.length; pi++){
    var pe = byId(profileInputs[pi]);
    if(pe && !pe.getAttribute("data-profile-wired")){
      pe.setAttribute("data-profile-wired", "1");
      addEvt(pe, "change", function(){ saveProfile(); });
    }
  }
}

function fillSummary(row){
  row = row || {};
  var fields = ["applied_turns","strategy_primary","strategy_other_text","confidence_1to5","notes"];
  for(var i=0;i<fields.length;i++){
    var key = fields[i];
    var el = byId(key);
    if(!el){ continue; }
    el.value = row[key] || "";
  }
  updateStrategyOtherField();
}

function collectSummary(){
  var out = {};
  out.tool = "";
  out.model = "";
  var fields = ["applied_turns","strategy_primary","strategy_other_text","confidence_1to5","notes"];
  for(var i=0;i<fields.length;i++){
    var key = fields[i];
    var el = byId(key);
    out[key] = el ? (el.value || "").trim() : "";
  }
  if(out.strategy_primary !== "other"){
    out.strategy_other_text = "";
  }
  return out;
}

function updateStrategyOtherField(){
  var wrap = byId("strategyOtherWrap");
  var input = byId("strategy_other_text");
  var strategy = byId("strategy_primary");
  var show = !!(strategy && strategy.value === "other");
  if(wrap){
    wrap.style.display = show ? "block" : "none";
  }
  if(input){
    input.disabled = !show || !studyStarted();
    if(!show){
      input.value = "";
    }
  }
}

function buildInAppGuide(data){
  return [
    "<div class='guideintro'>Quick reference for the app on this page.</div>",
    "<h4>Using This App</h4>",
    "<ul>",
    "<li>Use <strong>Show Onboarding</strong> any time you need to reopen the instructions or participant profile.</li>",
    "<li>The <strong>Baseline</strong> pane is reference-only. Only <strong>Final Submitted Code</strong> is exported.</li>",
    "<li>Click one baseline line, then another, to mark a range. Use <strong>Copy Marked Lines</strong> when you only want part of the file.</li>",
    "<li>Each snippet may or may not be vulnerable. Use the chat however you normally would to inspect or discuss it.</li>",
    "<li>If a snippet is long, send only the relevant function or block instead of the whole file.</li>",
    "<li>Save each snippet summary and final code before moving to the next one. When everything is done, click <strong>Finish (Build ZIP)</strong>.</li>",
    "</ul>",
    "<h4>Assistant Rules</h4>",
    "<ul>",
    "<li>Use the assistant that comes with this kit.</li>",
    "<li>Each snippet must include at least one in-app LLM turn.</li>",
    "<li>A turn is one logged message in the current snippet chat.</li>",
    "<li>Do not send study code to outside assistants or web services unless the research team told you to.</li>",
    "</ul>",
    "<h4>Snippet Summary</h4>",
    "<ul>",
    "<li><strong>Applied Turns</strong>: non-negative integer, must be less than or equal to total logged turns. Count only assistant turns that changed your final code.</li>",
    "<li><strong>Primary Strategy</strong>: pick the main prompt style you used for this snippet. If you choose <strong>Other</strong>, add a short note.</li>",
    "<li><strong>Confidence</strong>: choose a value from 1 to 5 based on how confident you are that your final submitted code is secure.</li>",
    "<li><strong>Notes</strong>: optional short factual notes.</li>",
    "</ul>",
    "<h4>If The Assistant Stalls</h4>",
    "<ul>",
    "<li>Larger pasted code blocks can take longer to answer.</li>",
    "<li>Retry once with a narrower question or code block.</li>",
    "<li>If it still fails, keep your current code changes and continue.</li>",
    "</ul>",
    "<div class='guidenotice'>PRIVACY REMINDER: DO NOT INCLUDE PERSONAL IDENTIFIERS OR SENSITIVE ACCOUNT DATA.</div>"
  ].join("");
}

function setChatTurnBadge(turns){
  var el = byId("chatTurnCount");
  if(el){
    var count = Math.max(0, Number(turns || 0));
    el.textContent = count + (count === 1 ? " turn" : " turns");
  }
}

function baselineLinesForText(text){
  var lines = String(text || "").replace(/\\r\\n?/g, "\\n").split("\\n");
  if(lines.length > 1 && lines[lines.length - 1] === ""){
    lines.pop();
  }
  return lines;
}

function updateBaselineSelectionNote(){
  var note = byId("baselineSelectionNote");
  if(!note){
    return;
  }
  var selection = baselineSelectionBySnippet[currentSid];
  var anchor = baselineAnchorLineBySnippet[currentSid];
  if(selection && Number.isFinite(selection.start) && Number.isFinite(selection.end)){
    if(selection.start === selection.end){
      note.textContent = "Marked line " + selection.start + ". Use Copy Marked Lines or click another line to start over.";
    } else {
      note.textContent = "Marked lines " + selection.start + "-" + selection.end + ". Use Copy Marked Lines when you only need that range.";
    }
    return;
  }
  if(Number.isFinite(anchor) && anchor > 0){
    note.textContent = "Start line set to " + anchor + ". Click another line to finish the range.";
    return;
  }
  note.textContent = "Click one line, then another line, to mark a range. Use Copy Marked Lines when you only need part of the file.";
}

function renderBaselineViewer(text, sid){
  var box = byId("baseline_code");
  baselineTextBySnippet[sid] = String(text || "");
  if(!box){
    return;
  }
  box.innerHTML = "";
  var lines = baselineLinesForText(text);
  if(!lines.length){
    box.innerHTML = "<div class='baselineempty'>No baseline code available for this snippet.</div>";
    updateBaselineSelectionNote();
    return;
  }
  var selection = baselineSelectionBySnippet[sid];
  var anchor = baselineAnchorLineBySnippet[sid];
  for(var i = 0; i < lines.length; i++){
    var lineNumber = i + 1;
    var row = document.createElement("div");
    var marked = false;
    if(selection && Number.isFinite(selection.start) && Number.isFinite(selection.end)){
      marked = lineNumber >= selection.start && lineNumber <= selection.end;
    } else if(Number.isFinite(anchor) && anchor === lineNumber){
      marked = true;
      row.className = "baselineLine anchor";
    }
    if(!row.className){
      row.className = "baselineLine" + (marked ? " marked" : "");
    } else if(marked){
      row.className += " marked";
    }
    row.title = "Click to mark this line or a line range for copying.";
    row.onclick = (function(targetLine){
      return function(){
        toggleBaselineLineSelection(targetLine);
      };
    })(lineNumber);

    var numberCell = document.createElement("div");
    numberCell.className = "baselineNum";
    numberCell.textContent = lineNumber;
    var textCell = document.createElement("div");
    textCell.className = "baselineText";
    textCell.textContent = lines[i] === "" ? " " : lines[i];

    row.appendChild(numberCell);
    row.appendChild(textCell);
    box.appendChild(row);
  }
  updateBaselineSelectionNote();
}

function toggleBaselineLineSelection(lineNumber){
  if(!currentSid || !Number.isFinite(lineNumber) || lineNumber < 1){
    return;
  }
  var existing = baselineSelectionBySnippet[currentSid];
  var anchor = baselineAnchorLineBySnippet[currentSid];
  if(existing && Number.isFinite(existing.start) && Number.isFinite(existing.end)){
    delete baselineSelectionBySnippet[currentSid];
    baselineAnchorLineBySnippet[currentSid] = lineNumber;
  } else if(Number.isFinite(anchor) && anchor > 0){
    baselineSelectionBySnippet[currentSid] = {
      start: Math.min(anchor, lineNumber),
      end: Math.max(anchor, lineNumber)
    };
    delete baselineAnchorLineBySnippet[currentSid];
  } else {
    baselineAnchorLineBySnippet[currentSid] = lineNumber;
  }
  renderBaselineViewer(baselineTextBySnippet[currentSid] || "", currentSid);
}

function clearBaselineSelection(){
  if(!currentSid){
    return;
  }
  delete baselineSelectionBySnippet[currentSid];
  delete baselineAnchorLineBySnippet[currentSid];
  renderBaselineViewer(baselineTextBySnippet[currentSid] || "", currentSid);
}

function copyTextToClipboard(text, okMessage, errMessage){
  if(!text){
    setMsg(errMessage, false);
    return;
  }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(function(){
      setMsg(okMessage, true);
    }, function(){
      setMsg(errMessage, false);
    });
    return;
  }
  var helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "readonly");
  helper.style.position = "absolute";
  helper.style.left = "-9999px";
  document.body.appendChild(helper);
  try{
    helper.focus();
    helper.select();
    var ok = document.execCommand && document.execCommand("copy");
    setMsg(ok ? okMessage : errMessage, !!ok);
  } catch(_err){
    setMsg(errMessage, false);
  } finally {
    document.body.removeChild(helper);
  }
}

function toggleBaselineSize(){
  baselineExpanded = !baselineExpanded;
  var box = byId("baseline_code");
  var btn = byId("toggleBaselineSizeBtn");
  if(box){
    if(baselineExpanded){
      box.classList.add("expanded");
    } else {
      box.classList.remove("expanded");
    }
  }
  if(btn){
    btn.textContent = baselineExpanded ? "Collapse Baseline" : "Expand Baseline";
  }
}

function toggleChatSize(){
  chatExpanded = !chatExpanded;
  var box = byId("chatHistory");
  var btn = byId("toggleChatSizeBtn");
  if(box){
    if(chatExpanded){
      box.classList.add("expanded");
    } else {
      box.classList.remove("expanded");
    }
  }
  if(btn){
    btn.textContent = chatExpanded ? "Collapse Chat" : "Expand Chat";
  }
}

function refreshCurrentSnippetTurnUI(){
  var autoTurns = getAutoTurnsForCurrent();
  var autoTurnsNote = byId("autoTurnsNote");
  if(autoTurnsNote){
    autoTurnsNote.textContent = "Auto-logged turns for this snippet: " + autoTurns;
  }
  setChatTurnBadge(autoTurns);
}

function copyBaseline(){
  var text = String(baselineTextBySnippet[currentSid] || "");
  if(!text){
    setMsg("No baseline code available to copy.", false);
    return;
  }
  copyTextToClipboard(text, "Baseline copied to clipboard.", "Could not copy baseline to clipboard.");
}

function copyMarkedBaseline(){
  if(!currentSid){
    setMsg("No snippet selected.", false);
    return;
  }
  var lines = baselineLinesForText(baselineTextBySnippet[currentSid] || "");
  if(!lines.length){
    setMsg("No baseline code available to copy.", false);
    return;
  }
  var selection = baselineSelectionBySnippet[currentSid];
  var anchor = baselineAnchorLineBySnippet[currentSid];
  var start = 0;
  var end = 0;
  if(selection && Number.isFinite(selection.start) && Number.isFinite(selection.end)){
    start = Math.max(1, selection.start);
    end = Math.min(lines.length, selection.end);
  } else if(Number.isFinite(anchor) && anchor > 0){
    start = anchor;
    end = anchor;
  } else {
    setMsg("Mark a baseline line or line range first.", false);
    return;
  }
  var snippet = lines.slice(start - 1, end).join("\\n");
  copyTextToClipboard(snippet, "Marked baseline lines copied to clipboard.", "Could not copy marked baseline lines.");
}

function toggleReadme(){
  var readme = byId("readme");
  var toggle = byId("toggleReadme");
  if(!readme || !toggle){ return; }
  var hidden = readme.style.display === "none";
  readme.style.display = hidden ? "block" : "none";
  toggle.textContent = hidden ? "Hide" : "Show";
}

function ensureGuideVisible(){
  var readme = byId("readme");
  var toggle = byId("toggleReadme");
  if(readme){ readme.style.display = "block"; }
  if(toggle){ toggle.textContent = "Hide"; }
}

function focusParticipantProfile(){
  showOnboarding(true);
  var target = byId("participantProfileHeading") || byId("programming_experience");
  if(target && target.scrollIntoView){
    target.scrollIntoView({behavior:"smooth", block:"center"});
  }
  var first = byId("programming_experience");
  if(first && first.focus){
    try{ first.focus(); } catch(_err) {}
  }
}

function buildOnboardingHtml(data){
  var started = !!(data && data.timer && data.timer.study_started);
  return [
    started
      ? "<p>Use this page as a reference while you work.</p>"
      : "<p>Complete the study inside this app. The README is only for setup and launch.</p>",
    "<h3>How To Complete Each Snippet</h3>",
    "<ul>",
    "<li>Select a snippet from the left list.</li>",
    "<li>Review the baseline code and decide whether you want to inspect it, ask questions, or change it.</li>",
    "<li>Click one baseline line, then another, if you want to mark and copy only part of the baseline into the chat.</li>",
    "<li>Use the in-app chat however you normally would. If you decide a change is needed, place your final answer in <strong>Final Submitted Code</strong>.</li>",
    "<li>The baseline pane is reference-only. Only <strong>Final Submitted Code</strong> is exported for analysis.</li>",
    started
      ? "<li>Your participant profile is saved below. Close this window when you are ready to return to the current snippet.</li>"
      : "<li>Complete the participant profile below, read this onboarding guide, and click <strong>Begin Study</strong>. The timer starts only then.</li>",
    "<li>Use the in-app chat that comes with this kit. At least one in-app turn is required for each snippet.</li>",
    "<li>If a snippet is long, send only the relevant function or code block instead of the whole file.</li>",
    "<li>Fill in the snippet summary and save before moving on.</li>",
    "<li>After all snippets are complete, click <strong>Finish (Build ZIP)</strong>.</li>",
    "</ul>",
    "<h3>Assigned Assistant</h3>",
    "<p>Use the assistant built into this kit. Do not switch to outside assistants or edit the kit files unless the research team told you to.</p>",
    "<h3>What Counts As A Turn</h3>",
    "<p>A turn is one logged message in the in-app chat. The app auto-logs total turns for each snippet.</p>",
    "<p><strong>Applied Turns</strong> means how many assistant turns directly changed your final code.</p>",
    "<h3>Prompt Strategies</h3>",
    "<ul>",
    "<li><strong>Zero-Shot</strong>: ask directly, with no example first.<div class='example'>Review this function and tell me if anything looks unsafe.</div></li>",
    "<li><strong>Few-Shot</strong>: give one or two short examples before asking your question.<div class='example'>Example risky pattern: ... Example safer pattern: ... Now review this function.</div></li>",
    "<li><strong>Chain-of-Thought</strong>: ask the model to explain its reasoning before it gives a final answer.<div class='example'>Walk through what this code is doing and whether it looks vulnerable.</div></li>",
    "<li><strong>Adaptive Chain-of-Thought</strong>: let the model decide whether a short answer is enough or whether step-by-step reasoning is needed.<div class='example'>If this is straightforward, answer briefly. If not, reason it out first.</div></li>",
    "<li><strong>Other</strong>: if your prompt style does not fit the listed categories, choose Other and add a short description.</li>",
    "</ul>",
    "<h3>If The Assistant Refuses Or Fails</h3>",
    "<ul>",
    "<li>Larger pasted code blocks can take longer to answer.</li>",
    "<li>Retry once with a narrower question or code block.</li>",
    "<li>If it still fails, keep your current code changes and move on.</li>",
    "</ul>",
    "<h3>Privacy</h3>",
    "<p>Do not place personal identifiers or sensitive account data in prompts, notes, or code comments.</p>"
  ].join("");
}

function showOnboarding(forceOpen){
  var back = byId("onboardingBackdrop");
  var body = byId("onboardingBody");
  if(!back || !body || !state){ return; }
  if(!forceOpen && studyStarted()){
    try{
      if(window.localStorage && window.localStorage.getItem(onboardingStorageKey) === "seen"){
        return;
      }
    } catch(_err) {}
  }
  body.innerHTML = buildOnboardingHtml(state);
  fillProfile((state && state.participant_profile) ? state.participant_profile : {});
  wireProfileInputs();
  updateOnboardingControls();
  back.classList.add("show");
  back.setAttribute("aria-hidden", "false");
}

function updateOnboardingControls(){
  var started = studyStarted();
  var title = byId("onboardingTitle");
  var beginBtn = byId("beginStudyBtn");
  var closeBtn = byId("closeOnboardingBtn");
  var profileNote = byId("participantProfileNote");
  if(title){
    title.textContent = started ? "Study Guide" : "Before You Start";
  }
  if(profileNote){
    profileNote.textContent = started
      ? "Your participant profile is already on file for this session."
      : "Complete this once before clicking Begin Study.";
  }
  if(beginBtn){
    beginBtn.disabled = started;
    beginBtn.style.display = started ? "none" : "inline-flex";
  }
  if(closeBtn){
    closeBtn.disabled = !started;
    closeBtn.style.opacity = started ? "1" : "0.5";
    closeBtn.textContent = started ? "Done" : "Close";
    closeBtn.title = started ? "Close the onboarding guide." : "Close the onboarding guide.";
  }
}

function hideOnboarding(markSeen){
  if(!studyStarted()){
    return;
  }
  var back = byId("onboardingBackdrop");
  if(back){
    back.classList.remove("show");
    back.setAttribute("aria-hidden", "true");
  }
  if(markSeen){
    try{
      if(window.localStorage){
        window.localStorage.setItem(onboardingStorageKey, "seen");
      }
    } catch(_err) {}
  }
}

function showOnboardingIfNeeded(){
  if(onboardingPrompted || !state){ return; }
  onboardingPrompted = true;
  showOnboarding(false);
}

function studyStarted(){
  return !!(state && state.timer && state.timer.study_started);
}

function setStudyStartedUI(){
  var locked = !studyStarted();
  var ids = ["prevBtn","saveBtn","nextBtn","zipBtn","copyBaselineBtn","copyMarkedBaselineBtn","clearBaselineSelectionBtn","sendChatBtn","discardChatBtn","applied_turns","strategy_primary","strategy_other_text","confidence_1to5","notes","appliedZeroBtn","appliedOneBtn","appliedAllBtn"];
  for(var i=0;i<ids.length;i++){
    var el = byId(ids[i]);
    if(el){ el.disabled = locked; }
  }
  var edited = byId("edited_code");
  if(edited){ edited.readOnly = locked; }
  var prompt = byId("chat_prompt");
  if(prompt){ prompt.disabled = locked; }
  var list = byId("snippetList");
  if(list){
    list.style.pointerEvents = locked ? "none" : "auto";
    list.style.opacity = locked ? "0.6" : "1";
  }
  updateStrategyOtherField();
  updateOnboardingControls();
}

function beginStudy(){
  saveProfile(function(profileErr){
    if(profileErr){
      focusParticipantProfile();
      setMsg("Could not save the Participant Profile: " + profileErr, false);
      return;
    }
    api("/api/begin_study", "POST", {}, function(resp){
      var nextTimer = (resp && resp.timer) ? resp.timer : {};
      if(!state){ state = {}; }
      state.timer = nextTimer;
      updateTimerBase(Number(nextTimer.active_display_seconds || nextTimer.active_seconds || 0));
      renderLiveTimer();
      setStudyStartedUI();
      hideOnboarding(true);
      setMsg("Study started. The timer is now running.", true);
    }, function(msg){
      var lower = String(msg || "").toLowerCase();
      if(lower.indexOf("participant profile") !== -1){
        focusParticipantProfile();
        setMsg("Complete the Participant Profile in this popup before starting the study. " + msg, false);
        return;
      }
      setMsg("Could not start study: " + msg, false);
    });
  });
}

function isRefusalLike(text){
  var msg = String(text || "").toLowerCase();
  var markers = [
    "i can't help",
    "i cannot help",
    "i can't assist",
    "i cannot assist",
    "i'm sorry, but i can't",
    "i am sorry, but i can't",
    "i'm sorry, but i cannot",
    "i am sorry, but i cannot",
    "cannot provide that",
    "can't provide that",
    "cannot comply"
  ];
  for(var i=0;i<markers.length;i++){
    if(msg.indexOf(markers[i]) !== -1){
      return true;
    }
  }
  return false;
}

function formatChatFailure(msg){
  var raw = String(msg || "");
  var lower = raw.toLowerCase();
  if(lower.indexOf("empty response") !== -1){
    return "The assigned LLM returned an empty response. Retry once with a narrower request.";
  }
  if(
    lower.indexOf("context window") !== -1 ||
    lower.indexOf("maximum context length") !== -1 ||
    lower.indexOf("prompt is too large") !== -1 ||
    lower.indexOf("prompt too large") !== -1 ||
    lower.indexOf("too many tokens") !== -1 ||
    lower.indexOf("input is too long") !== -1 ||
    lower.indexOf("input too large") !== -1
  ){
    return "The pasted request is too large for one LLM turn. Send only the relevant function or block, then retry.";
  }
  if(lower.indexOf("timed out") !== -1 || lower.indexOf("timeout") !== -1){
    return "The assigned LLM timed out before returning a response. Retry once or shorten the request.";
  }
  if(lower.indexOf("connection") !== -1 || lower.indexOf("refused") !== -1 || lower.indexOf("unavailable") !== -1){
    return "Could not reach the assigned LLM endpoint. Confirm it is available, then retry.";
  }
  return "LLM request failed: " + raw;
}

function getAutoTurnsForCurrent(){
  var st = snippetStatusFor(currentSid || "");
  if(st && typeof st.chat_turns !== "undefined"){
    return Math.max(0, Number(st.chat_turns || 0));
  }
  return 0;
}

function validateSummaryInputs(showPopup){
  var summary = collectSummary();
  var autoTurns = getAutoTurnsForCurrent();
  var codeEl = byId("edited_code");
  var code = codeEl ? String(codeEl.value || "").trim() : "";
  if(!code){
    return {ok:false, message:"Final Submitted Code is required before you can save or move on."};
  }
  if(autoTurns < 1){
    return {ok:false, message:"At least one in-app LLM turn is required for this snippet."};
  }

  var appliedRaw = String(summary.applied_turns || "").trim();
  var applied = /^\\d+$/.test(appliedRaw) ? Number(appliedRaw) : NaN;
  if(!Number.isFinite(applied) || applied < 0){
    return {ok:false, message:"Applied Turns must be a non-negative integer."};
  }
  if(applied > autoTurns){
    return {ok:false, message:"Applied Turns cannot be greater than total turns (" + autoTurns + ")."};
  }

  var confRaw = String(summary.confidence_1to5 || "").trim();
  var conf = /^\\d+$/.test(confRaw) ? Number(confRaw) : NaN;
  if(!Number.isFinite(conf) || conf < 1 || conf > 5){
    return {ok:false, message:"Confidence must be an integer from 1 to 5."};
  }

  var strategy = String(summary.strategy_primary || "").trim();
  var allowed = {"zero_shot":true,"few_shot":true,"chain_of_thought":true,"adaptive_chain_of_thought":true,"other":true};
  if(!allowed[strategy]){
    return {ok:false, message:"Primary Strategy is required."};
  }
  if(strategy === "other" && !String(summary.strategy_other_text || "").trim()){
    return {ok:false, message:"Describe the primary strategy when Other is selected."};
  }

  return {ok:true, message:""};
}

function updateNavButtons(){
  var next = byId("nextBtn");
  if(!next || !state || !state.snippet_ids){ return; }
  var last = state.snippet_ids.length - 1;
  if(idx >= last){
    next.textContent = "Finish";
    next.title = "You are on the last snippet. Click to finish and build ZIP.";
  } else {
    next.textContent = "Next";
    next.title = "Save current snippet, then move to next snippet.";
  }
}

function refreshState(cb){
  api("/api/state", "GET", null, function(d){
    state = d;
    setConn(true);
    var meta = byId("meta");
    if(meta){
      meta.textContent = "Assigned snippets loaded. Use Onboarding for instructions.";
    }
    var readme = byId("readme");
    if(readme){ readme.innerHTML = buildInAppGuide(d); }
    var t = d.timer || {};
    updateTimerBase(Number(t.active_display_seconds || t.active_seconds || 0));
    renderLiveTimer();
    fillProfile(d.participant_profile || {});
    renderSidebar();
    updateNavButtons();
    validateSummaryInputs(false);
    setStudyStartedUI();
    showOnboardingIfNeeded();
    if(cb){ cb(); }
  }, function(msg){
    setConn(false);
    setMsg(msg, false);
  });
}

function loadSnippet(){
  if(!state || !state.snippet_ids || !state.snippet_ids.length){
    setMsg("No snippets found in kit.", false);
    return;
  }
  currentSid = state.snippet_ids[idx];
  api("/api/snippet?snippet_id=" + encodeURIComponent(currentSid), "GET", null, function(d){
    setConn(true);
    var progress = byId("progress");
    if(progress){ progress.textContent = "Snippet " + (idx + 1) + " of " + state.snippet_ids.length; }
    var sidLbl = byId("sidLbl");
    if(sidLbl){ sidLbl.textContent = snippetLabel(currentSid); }
    var baselineMeta = byId("baselineMeta");
    if(baselineMeta){
      var fileName = snippetFileName(currentSid);
      var language = snippetLanguageLabel(currentSid);
      baselineMeta.textContent = fileName ? (language + " | " + fileName) : language;
    }
    renderBaselineViewer(formatBaselineForDisplay(d.baseline_code || "", currentSid), currentSid);
    var edited = byId("edited_code");
    if(edited){
      var editedText = d.edited_code || "";
      edited.value = editedText;
    }
    fillSummary(d.summary || {});
    var tool = byId("tool");
    if(tool){ tool.value = "Auto-recorded"; }
    var model = byId("model");
    if(model){ model.value = "Auto-recorded"; }
    refreshCurrentSnippetTurnUI();
    loadChatHistory();
    refreshOllamaStatus();
    renderSidebar();
    updateNavButtons();
    validateSummaryInputs(false);
  }, function(msg){
    setConn(false);
    setMsg(msg, false);
  });
}

function saveDraftCurrent(onOk, onErr){
  if(!currentSid){
    onOk && onOk();
    return;
  }
  var code = byId("edited_code") ? byId("edited_code").value : "";
  var payload = {snippet_id: currentSid, code: code, summary: collectSummary(), strict_summary: false};
  api("/api/save_snippet", "POST", payload, function(resp){
    onOk && onOk(resp || {});
  }, function(msg){
    onErr && onErr(msg);
  });
}

function saveCurrent(nextFn){
  if(!currentSid){
    setMsg("No snippet selected.", false);
    return;
  }
  if(!studyStarted()){
    setMsg("Review the onboarding guide and click Begin Study before editing snippets.", false);
    return;
  }

  var validation = validateSummaryInputs(true);
  if(!validation.ok){
    setMsg(validation.message, false);
    return;
  }

  var code = byId("edited_code") ? byId("edited_code").value : "";
  var payload = {snippet_id: currentSid, code: code, summary: collectSummary(), strict_summary: true};
  api("/api/save_snippet", "POST", payload, function(){
    setMsg("Saved " + snippetLabel(currentSid) + ".", true);
    refreshState(function(){
      if(nextFn){ nextFn(); }
    });
  }, function(msg){
    setConn(false);
    setMsg(msg, false);
  });
}

function move(delta){
  var ids = state && state.snippet_ids ? state.snippet_ids : [];
  var last = ids.length - 1;

  if(delta > 0 && idx >= last){
    var goFinish = window.confirm("You are on the last snippet. Do you want to build your submission ZIP now?");
    if(goFinish){
      buildZip();
    }
    return;
  }

  saveCurrent(function(){
    idx = Math.max(0, Math.min(ids.length - 1, idx + delta));
    loadSnippet();
  });
}

function escHtml(text){
  return String(text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function buildChatExchanges(entries){
  var exchanges = [];
  var current = null;
  for(var i=0;i<entries.length;i++){
    var row = entries[i] || {};
    var role = String(row.role || "").toLowerCase();
    var turn = String(row.turn_index || "");
    var text = String(row.text || "");
    if(!text.trim()){
      continue;
    }
    if(role === "user"){
      if(current){
        exchanges.push(current);
      }
      current = {prompt: text, promptTurn: turn, replies: []};
      continue;
    }
    if(role === "assistant"){
      if(!current){
        current = {prompt: "", promptTurn: "", replies: []};
      }
      current.replies.push({turn: turn, text: text});
    }
  }
  if(current){
    exchanges.push(current);
  }
  return exchanges;
}

function renderChatExchangeSelection(){
  var indexBox = byId("chatHistoryIndex");
  var detailBox = byId("chatHistoryDetail");
  var exchanges = chatExchangesBySnippet[currentSid] || [];
  if(!indexBox || !detailBox){
    return;
  }
  if(!exchanges.length){
    indexBox.innerHTML = "";
    detailBox.innerHTML = "<div class='lbl'>No turns logged yet for this snippet.</div>";
    return;
  }
  var selected = chatExchangeSelection[currentSid];
  if(!Number.isFinite(selected) || selected < 0 || selected >= exchanges.length){
    selected = exchanges.length - 1;
    chatExchangeSelection[currentSid] = selected;
  }
  var buttons = indexBox.getElementsByTagName("button");
  for(var bi=0; bi<buttons.length; bi++){
    buttons[bi].className = "chatindexbtn" + (bi === selected ? " active" : "");
  }
  var exchange = exchanges[selected];
  var html = "<div class='chatmeta'>Exchange " + (selected + 1) + " of " + exchanges.length + "</div>";
  if(exchange.prompt){
    html += "<div class='chatrow user'><div class='chatmeta'>Prompt - Turn " + escHtml(exchange.promptTurn) + "</div><div>" + escHtml(exchange.prompt) + "</div></div>";
  } else {
    html += "<div class='chatrow user'><div class='chatmeta'>Prompt</div><div>No prompt text was recorded for this exchange.</div></div>";
  }
  if(exchange.replies.length){
    for(var ri=0; ri<exchange.replies.length; ri++){
      var reply = exchange.replies[ri];
      html += "<div class='chatrow assistant'><div class='chatmeta'>Assistant - Turn " + escHtml(reply.turn) + "</div><div>" + escHtml(reply.text) + "</div></div>";
    }
  } else {
    html += "<div class='chatrow assistant'><div class='chatmeta'>Assistant</div><div>No assistant reply was recorded for this exchange.</div></div>";
  }
  detailBox.innerHTML = html;
  detailBox.scrollTop = 0;
}

function renderChatHistory(entries){
  var indexBox = byId("chatHistoryIndex");
  var detailBox = byId("chatHistoryDetail");
  if(!indexBox || !detailBox){
    return;
  }
  var exchanges = buildChatExchanges(entries || []);
  chatExchangesBySnippet[currentSid] = exchanges;
  indexBox.innerHTML = "";
  if(!exchanges.length){
    detailBox.innerHTML = "<div class='lbl'>No turns logged yet for this snippet.</div>";
    return;
  }
  var selected = chatExchangeSelection[currentSid];
  if(!Number.isFinite(selected) || selected < 0 || selected >= exchanges.length){
    selected = exchanges.length - 1;
    chatExchangeSelection[currentSid] = selected;
  }
  for(var i=0;i<exchanges.length;i++){
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chatindexbtn" + (i === selected ? " active" : "");
    btn.textContent = "Exchange " + (i + 1);
    btn.title = exchanges[i].prompt ? exchanges[i].prompt.slice(0, 160) : "Assistant-only exchange";
    btn.onclick = (function(j){
      return function(){
        chatExchangeSelection[currentSid] = j;
        renderChatExchangeSelection();
      };
    })(i);
    indexBox.appendChild(btn);
  }
  renderChatExchangeSelection();
}

function refreshCurrentSnippetAfterChat(){
  refreshCurrentSnippetTurnUI();
  loadChatHistory();
  renderSidebar();
  updateNavButtons();
  validateSummaryInputs(false);
}

function loadChatHistory(){
  var indexBox = byId("chatHistoryIndex");
  var detailBox = byId("chatHistoryDetail");
  if(!indexBox || !detailBox || !currentSid){ return; }
  api("/api/chat_history?snippet_id=" + encodeURIComponent(currentSid), "GET", null, function(d){
    var entries = (d && d.entries) ? d.entries : [];
    refreshCurrentSnippetTurnUI();
    renderChatHistory(entries);
  }, function(msg){
    indexBox.innerHTML = "";
    detailBox.innerHTML = "<div class='msg err'>Could not load chat history: " + escHtml(msg) + "</div>";
  });
}

function refreshOllamaStatus(){
  var el = byId("ollamaStatus");
  if(!el){ return; }
  api("/api/ollama_status", "GET", null, function(d){
    if(d && d.ok){
      if(d.model_found){
        el.textContent = "Assigned LLM connected and ready.";
      } else {
        el.textContent = "Assigned LLM connected, but the expected runtime profile was not reported.";
      }
    } else {
      el.textContent = "Assigned LLM status unavailable.";
    }
  }, function(msg){
    el.textContent = "Assigned LLM unavailable: " + msg;
  });
}

function setChatRequestUI(inFlight){
  var sendBtn = byId("sendChatBtn");
  var discardBtn = byId("discardChatBtn");
  if(sendBtn){
    sendBtn.disabled = !!inFlight;
    sendBtn.textContent = inFlight ? "Sending..." : "Send To LLM";
  }
  if(discardBtn){
    discardBtn.style.display = inFlight ? "inline-flex" : "none";
    discardBtn.disabled = !inFlight;
  }
}

function discardActiveChat(){
  if(!activeChatXhr || !activeChatRequestId){
    return;
  }
  var requestId = activeChatRequestId;
  api("/api/cancel_chat", "POST", {request_id: requestId}, function(){}, function(){});
  try{
    activeChatXhr.abort();
  } catch(_err){}
  activeChatXhr = null;
  activeChatRequestId = "";
  setChatRequestUI(false);
  setMsg("Stopped waiting for the current reply. The app will discard it if it finishes in the background.", false);
}

function sendChat(){
  if(!studyStarted()){
    setMsg("Review the onboarding guide and click Begin Study before using chat.", false);
    return;
  }
  if(!currentSid){
    setMsg("No snippet selected.", false);
    return;
  }
  var promptEl = byId("chat_prompt");
  var prompt = promptEl ? (promptEl.value || "").trim() : "";
  if(!prompt){
    setMsg("Chat prompt is required.", false);
    return;
  }
  if(prompt.length > 12000){
    setMsg("Chat prompt is too large for one request. Paste only the relevant function or block, then retry.", false);
    return;
  }
  if(activeChatXhr){
    setMsg("A reply is already in progress. Wait for it or stop and discard it first.", false);
    return;
  }
  setChatRequestUI(true);
  var requestId = "chat_" + Date.now() + "_" + Math.floor(Math.random() * 1000000);
  activeChatRequestId = requestId;

  saveDraftCurrent(function(){
    activeChatXhr = api("/api/ollama_chat", "POST", {
      snippet_id: currentSid,
      prompt: prompt,
      provider: "",
      model: "",
      session_id: "session_1",
      request_id: requestId
    }, function(resp){
      if(activeChatRequestId !== requestId){
        return;
      }
      activeChatXhr = null;
      activeChatRequestId = "";
      setChatRequestUI(false);
      if(promptEl){ promptEl.value = ""; }
      if(resp && resp.discarded){
        setMsg("The current reply was discarded.", false);
        return;
      }
      var assistantText = (resp && resp.assistant_text) ? String(resp.assistant_text) : "";
      if(resp && resp.truncated){
        setMsg("The LLM response was cut off before it finished. Send a smaller code block or retry after narrowing the request.", false);
      } else if(isRefusalLike(assistantText)){
        setMsg("The LLM response was logged, but it looks like a refusal or non-answer. Retry once with a narrower request if needed.", false);
      } else {
        setMsg("LLM response logged for " + snippetLabel(currentSid) + ".", true);
      }
      delete chatExchangeSelection[currentSid];
      refreshState(function(){
        refreshCurrentSnippetAfterChat();
      });
    }, function(msg){
      if(activeChatRequestId !== requestId){
        return;
      }
      activeChatXhr = null;
      activeChatRequestId = "";
      setChatRequestUI(false);
      setMsg(formatChatFailure(msg), false);
    });
  }, function(msg){
    activeChatXhr = null;
    activeChatRequestId = "";
    setChatRequestUI(false);
    setMsg("Could not save the current draft before chat: " + msg, false);
  });
}

function collectAttestationForFinish(){
  var usedAssignedProfile = window.confirm(
    "Final Attestation:\\nClick OK if you used the assigned model/profile.\\nClick Cancel if you deviated."
  );

  var deviationNote = "";
  if(!usedAssignedProfile){
    deviationNote = window.prompt(
      "You selected deviation. Briefly describe the model/profile you used instead:",
      ""
    ) || "";
    deviationNote = deviationNote.trim();
    if(!deviationNote){
      return {ok:false, message:"Finish blocked: deviation note required."};
    }
  }

  var preview = usedAssignedProfile
    ? "Assigned model/profile confirmed."
    : "Deviation noted: " + deviationNote;

  if(!window.confirm(preview + "\\n\\nContinue and build submission ZIP?")){
    return {ok:false, message:"Finish cancelled."};
  }

  return {ok:true, confirmed: usedAssignedProfile, note: deviationNote};
}

function buildZip(){
  if(!studyStarted()){
    setMsg("Review the onboarding guide and click Begin Study before finishing the study.", false);
    return;
  }
  saveCurrent(function(){
    api("/api/preflight", "POST", {}, function(pre){
      if(!pre || !pre.ok){
        var issues = (pre && pre.issues && pre.issues.length)
          ? pre.issues.join("\\n- ")
          : "Unknown validation issues.";
        setMsg("Finish blocked: " + issues, false);
        return;
      }

      var att = collectAttestationForFinish();
      if(!att.ok){
        setMsg(att.message, false);
        return;
      }

      api("/api/build_zip", "POST", {
        confirmed_assigned_profile: att.confirmed,
        deviation_note: att.note,
        provider: "",
        model: ""
      }, function(resp){
        var zipPath = resp && resp.zip_path ? String(resp.zip_path) : "";
        setMsg(zipPath ? ("ZIP created: " + zipPath) : "ZIP created.", true);
      }, function(msg){
        setMsg("ZIP build failed: " + msg, false);
      });
    }, function(msg){
      setMsg("Preflight failed: " + msg, false);
    });
  });
}

function setAppliedTurnPreset(mode){
  var el = byId("applied_turns");
  if(!el){ return; }
  var autoTurns = getAutoTurnsForCurrent();
  if(mode === "zero"){
    el.value = "0";
  } else if(mode === "one"){
    el.value = autoTurns >= 1 ? "1" : "0";
  } else if(mode === "all"){
    el.value = String(autoTurns);
  }
  validateSummaryInputs(false);
}

function wire(){
  var prev = byId("prevBtn");
  if(prev){ prev.onclick = function(){ move(-1); }; }
  var next = byId("nextBtn");
  if(next){ next.onclick = function(){ move(1); }; }
  var save = byId("saveBtn");
  if(save){ save.onclick = function(){ saveCurrent(); }; }
  var zip = byId("zipBtn");
  if(zip){ zip.onclick = buildZip; }
  var send = byId("sendChatBtn");
  if(send){ send.onclick = sendChat; }
  var discard = byId("discardChatBtn");
  if(discard){ discard.onclick = discardActiveChat; }
  var baselineSizeBtn = byId("toggleBaselineSizeBtn");
  if(baselineSizeBtn){ baselineSizeBtn.onclick = toggleBaselineSize; }
  var copy = byId("copyBaselineBtn");
  if(copy){ copy.onclick = copyBaseline; }
  var copyMarked = byId("copyMarkedBaselineBtn");
  if(copyMarked){ copyMarked.onclick = copyMarkedBaseline; }
  var clearMarked = byId("clearBaselineSelectionBtn");
  if(clearMarked){ clearMarked.onclick = clearBaselineSelection; }
  var toggle = byId("toggleReadme");
  if(toggle){ toggle.onclick = toggleReadme; }
  var showOnboardingBtn = byId("showOnboardingBtn");
  if(showOnboardingBtn){ showOnboardingBtn.onclick = function(){ showOnboarding(true); }; }
  var beginStudyBtn = byId("beginStudyBtn");
  if(beginStudyBtn){ beginStudyBtn.onclick = beginStudy; }
  var closeOnboardingBtn = byId("closeOnboardingBtn");
  if(closeOnboardingBtn){ closeOnboardingBtn.onclick = function(){ hideOnboarding(true); }; }
  var onboardingBackdrop = byId("onboardingBackdrop");
  if(onboardingBackdrop){
    addEvt(onboardingBackdrop, "click", function(ev){
      if(ev && ev.target === onboardingBackdrop){
        hideOnboarding(true);
      }
    });
  }
  var chatSizeBtn = byId("toggleChatSizeBtn");
  if(chatSizeBtn){ chatSizeBtn.onclick = toggleChatSize; }
  var appliedZero = byId("appliedZeroBtn");
  if(appliedZero){ appliedZero.onclick = function(){ setAppliedTurnPreset("zero"); }; }
  var appliedOne = byId("appliedOneBtn");
  if(appliedOne){ appliedOne.onclick = function(){ setAppliedTurnPreset("one"); }; }
  var appliedAll = byId("appliedAllBtn");
  if(appliedAll){ appliedAll.onclick = function(){ setAppliedTurnPreset("all"); }; }

  var summaryInputs = ["applied_turns","strategy_primary","strategy_other_text","confidence_1to5"];
  for(var si=0; si<summaryInputs.length; si++){
    var se = byId(summaryInputs[si]);
    if(se){
      addEvt(se, "change", function(){ validateSummaryInputs(false); });
      addEvt(se, "blur", function(){ validateSummaryInputs(false); });
    }
  }
  var strategyPrimary = byId("strategy_primary");
  if(strategyPrimary){
    addEvt(strategyPrimary, "change", function(){
      updateStrategyOtherField();
      validateSummaryInputs(false);
    });
  }
  wireProfileInputs();

  var prompt = byId("chat_prompt");
  if(prompt){
    addEvt(prompt, "keydown", function(ev){
      ev = ev || window.event;
      if((ev.ctrlKey || ev.metaKey) && ev.key === "Enter"){
        ev.preventDefault();
        sendChat();
      }
    });
  }
}
function checkBackend(){
  api("/api/ping", "GET", null, function(d){
    setConn(true);
    var timerObj = d.timer || {};
    updateTimerBase(Number(timerObj.active_display_seconds || timerObj.active_seconds || 0));
    renderLiveTimer();
    if(timerObj.study_started){
      api("/api/heartbeat", "POST", {}, function(hb){
        var hbTimer = (hb && hb.timer) ? hb.timer : {};
        updateTimerBase(Number(hbTimer.active_display_seconds || hbTimer.active_seconds || 0));
        renderLiveTimer();
      }, function(){});
    }
  }, function(){
    setConn(false);
  });
}

function notifyClientClosing(){
  if(window.__repairAuditClosingSent){ return; }
  window.__repairAuditClosingSent = true;
  try{
    if(navigator.sendBeacon){
      var payload = new Blob([JSON.stringify({reason:"pagehide"})], {type:"application/json"});
      navigator.sendBeacon("/api/client-closing", payload);
      return;
    }
  } catch(_beaconErr) {}
  try{
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/client-closing", false);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.send("{}");
  } catch(_xhrErr) {}
}

function boot(){
  wire();
  refreshState(function(){
    loadSnippet();
  });
  refreshOllamaStatus();
  renderLiveTimer();
  timerTick = setInterval(renderLiveTimer, 1000);
  pingTimer = setInterval(checkBackend, 2000);
  if(window.addEventListener){
    window.addEventListener("pagehide", notifyClientClosing);
    window.addEventListener("beforeunload", notifyClientClosing);
  }
  checkBackend();
}

// Attach global error listener when available, but do not block app startup.
try{
  if(window.addEventListener){
    window.addEventListener("error", function(ev){
      var msg = (ev && ev.message) ? ev.message : "Unexpected script error";
      setConn(false);
      setMsg("Frontend error: " + msg, false);
    });
  }
} catch(_err) {}

try{
  boot();
} catch(e){
  setConn(false);
  setMsg("Frontend startup error: " + (e && e.message ? e.message : "unknown error"), false);
}
</script>
</body>
</html>""".replace("__CSRF_TOKEN__", csrf_token)

class AppHandler(BaseHTTPRequestHandler):
    """HTTP request handler serving the participant web app and API endpoints."""

    store: StudyStore
    csrf_token: str = ""
    allowed_origin: str = ""
    shutdown_now: bool = False
    client_seen: bool = False
    heartbeat_seen: bool = False
    close_requested_at: float | None = None
    last_client_poll_at: float | None = None
    cancelled_chat_request_ids: set[str] = set()
    cancelled_chat_lock = threading.Lock()
    quiet_paths = {"/api/ping", "/api/heartbeat", "/api/save_snippet"}

    def log_message(self, format: str, *args: object) -> None:
        """Hide routine health-check and autosave lines from the console window."""
        try:
            parts = str(getattr(self, "requestline", "") or "").split()
            path = parts[1] if len(parts) >= 2 else ""
            status_text = str(args[1]) if len(args) >= 2 else ""
            if path.split("?", 1)[0] in self.quiet_paths and status_text.startswith(("2", "3")):
                return
        except Exception:
            pass
        super().log_message(format, *args)

    def _shutdown_server_soon(self, delay_seconds: float = 0.35) -> None:
        """Stop the local app shortly after the current response is flushed."""
        if self.shutdown_now:
            return

        AppHandler.shutdown_now = True
        server = self.server
        store = self.store

        def _stop() -> None:
            time.sleep(max(0.0, float(delay_seconds)))
            try:
                store.mark_end()
            except Exception:
                pass
            try:
                server.shutdown()
            except Exception:
                pass

        threading.Thread(target=_stop, daemon=True).start()

    @classmethod
    def touch_client_poll(cls) -> None:
        """Record the latest browser poll so idle windows can be cleaned up."""
        cls.last_client_poll_at = time.monotonic()

    def _post_security_ok(self) -> tuple[bool, str]:
        """Require same-origin + CSRF token for state-changing requests."""
        origin = (self.headers.get("Origin") or "").strip()
        referer = (self.headers.get("Referer") or "").strip()
        token = (self.headers.get("X-CSRF-Token") or "").strip()

        origin_ok = (origin == self.allowed_origin) or (not origin and referer.startswith(self.allowed_origin))
        if not origin_ok:
            return False, "Request origin is not allowed."
        if not self.csrf_token or token != self.csrf_token:
            return False, "Missing or invalid CSRF token."
        return True, ""

    def _json(self, payload: dict[str, object], status: int = HTTPStatus.OK) -> None:
        """Write a no-cache JSON response back to the browser client."""
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _html(self, text: str, status: int = HTTPStatus.OK) -> None:
        """Write a no-cache HTML response back to the browser client."""
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    @classmethod
    def mark_chat_request_cancelled(cls, request_id: str) -> None:
        """Remember that a chat reply should be discarded before it is logged."""
        if not request_id:
            return
        with cls.cancelled_chat_lock:
            cls.cancelled_chat_request_ids.add(request_id)

    @classmethod
    def consume_chat_request_cancelled(cls, request_id: str) -> bool:
        """Return True once for a cancelled chat request and clear its marker."""
        if not request_id:
            return False
        with cls.cancelled_chat_lock:
            if request_id not in cls.cancelled_chat_request_ids:
                return False
            cls.cancelled_chat_request_ids.remove(request_id)
            return True

    @classmethod
    def clear_chat_request_cancelled(cls, request_id: str) -> None:
        """Drop a stale cancellation marker once a request finishes normally."""
        if not request_id:
            return
        with cls.cancelled_chat_lock:
            cls.cancelled_chat_request_ids.discard(request_id)


    def _ollama_request(self, path: str, payload: dict[str, object] | None = None, *, timeout: float = 90.0) -> dict[str, object]:
        """Call the configured Ollama-compatible HTTP API and return parsed JSON."""
        url = self._ollama_url(path)
        data = None
        method = "GET"
        headers = {
            "Content-Type": "application/json",
            "ngrok-skip-browser-warning": "true",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            method = "POST"

        req = Request(url=url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = raw.strip()
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    detail = str(parsed.get("error") or parsed.get("message") or detail).strip()
            except Exception:
                pass
            if detail:
                raise RuntimeError(f"LLM endpoint rejected the request ({exc.code}): {detail}") from exc
            raise RuntimeError(f"LLM endpoint rejected the request ({exc.code}).") from exc
        except URLError as exc:
            raise RuntimeError(
                f"Could not reach the configured LLM endpoint at {self._ollama_base_url()} ({exc})."
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

        try:
            obj = json.loads(raw)
        except Exception as exc:
            raise RuntimeError("LLM endpoint returned non-JSON response.") from exc

        if not isinstance(obj, dict):
            raise RuntimeError("LLM endpoint returned unexpected response format.")
        return obj

    def _ollama_base_url(self) -> str:
        """Return the configured participant-side Ollama-compatible endpoint base URL."""
        llm = self.store.lock_data.get("llm", {})
        if isinstance(llm, dict):
            raw = str(llm.get("base_url", "") or "").strip()
            if raw:
                return raw.rstrip("/")
        return "http://127.0.0.1:11434"

    def _ollama_url(self, path: str) -> str:
        """Join a configured base URL with an Ollama API path."""
        base = self._ollama_base_url()
        path = path if path.startswith("/") else f"/{path}"
        if base.lower().endswith("/api") and path.startswith("/api/"):
            return base + path[4:]
        return base + path

    def _ollama_endpoint_label(self) -> str:
        """Return short endpoint text for participant-facing status messages."""
        base = self._ollama_base_url()
        parsed = urlparse(base)
        host = (parsed.hostname or "").strip().lower()
        if host in {"", "127.0.0.1", "localhost", "::1"}:
            return "local Ollama"
        return base

    def _ollama_assigned_model(self) -> str:
        """Return the locked model name assigned to the participant kit."""
        llm = self.store.lock_data.get("llm", {})
        if isinstance(llm, dict):
            return str(llm.get("model", "") or "").strip()
        return ""

    def _ollama_options_from_lock(self) -> dict[str, object]:
        """Return generation options that the kit lock file exposes to the UI."""
        llm = self.store.lock_data.get("llm", {})
        if not isinstance(llm, dict):
            return {}

        opts: dict[str, object] = {}
        for key in ["temperature", "top_p", "top_k", "num_predict", "seed"]:
            if key in llm and llm.get(key) is not None and str(llm.get(key)).strip() != "":
                opts[key] = llm.get(key)
        return opts

    def _participant_chat_system_prompt(self) -> str:
        """Return the participant-side system prompt used for chat requests."""
        return participant_chat_system_prompt()

    def _ollama_stream_chat(
        self,
        path: str,
        payload: dict[str, object],
        *,
        timeout: float = 240.0,
        request_id: str = "",
    ) -> dict[str, object]:
        """Stream one Ollama chat reply internally so the upstream request can be cancelled."""
        url = self._ollama_url(path)
        parsed = urlparse(url)
        scheme = (parsed.scheme or "http").lower()
        connection_cls = HTTPSConnection if scheme == "https" else HTTPConnection
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        request_path = parsed.path or "/"
        if parsed.query:
            request_path += "?" + parsed.query

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson, application/json",
            "ngrok-skip-browser-warning": "true",
        }
        body = json.dumps(payload).encode("utf-8")
        conn = connection_cls(host, port, timeout=timeout)

        try:
            conn.request("POST", request_path, body=body, headers=headers)
            resp = conn.getresponse()
            if resp.status < 200 or resp.status >= 300:
                raw = resp.read().decode("utf-8", errors="replace")
                detail = raw.strip()
                try:
                    parsed_error = json.loads(raw)
                    if isinstance(parsed_error, dict):
                        detail = str(parsed_error.get("error") or parsed_error.get("message") or detail).strip()
                except Exception:
                    pass
                if detail:
                    raise RuntimeError(f"LLM endpoint rejected the request ({resp.status}): {detail}")
                raise RuntimeError(f"LLM endpoint rejected the request ({resp.status}).")

            try:
                raw_socket = getattr(getattr(resp, "fp", None), "raw", None)
                sock = getattr(raw_socket, "_sock", None)
                if sock is not None:
                    sock.settimeout(0.5)
            except Exception:
                pass

            assistant_parts: list[str] = []
            final_obj: dict[str, object] = {}

            while True:
                if request_id and AppHandler.consume_chat_request_cancelled(request_id):
                    raise OllamaChatCancelled("Participant cancelled the in-flight reply.")
                try:
                    raw_line = resp.readline()
                except socket.timeout:
                    continue
                if not raw_line:
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    obj = json.loads(line)
                except Exception as exc:
                    raise RuntimeError("LLM endpoint returned invalid streamed JSON.") from exc
                if not isinstance(obj, dict):
                    raise RuntimeError("LLM endpoint returned unexpected streamed response format.")

                final_obj = obj
                chunk = self._ollama_message_text(obj)
                if chunk:
                    assistant_parts.append(chunk)
                if bool(obj.get("done", False)):
                    break

            assistant_text = "".join(assistant_parts)
            if not final_obj:
                raise RuntimeError("LLM endpoint returned an empty streamed response.")

            merged = dict(final_obj)
            message_obj = merged.get("message", {})
            if isinstance(message_obj, dict):
                merged["message"] = {
                    **message_obj,
                    "content": assistant_text,
                }
            else:
                merged["message"] = {"role": "assistant", "content": assistant_text}
            if "response" in merged:
                merged["response"] = assistant_text
            return merged
        except OllamaChatCancelled:
            raise
        except URLError as exc:
            raise RuntimeError(
                f"Could not reach the configured LLM endpoint at {self._ollama_base_url()} ({exc})."
            ) from exc
        except socket.timeout as exc:
            raise RuntimeError("LLM streaming request timed out.") from exc
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"LLM request failed: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _ollama_message_text(self, resp: dict[str, object]) -> str:
        """Extract assistant text from either Ollama chat or generate style responses."""
        msg_obj = resp.get("message", {})
        assistant_text = ""
        if isinstance(msg_obj, dict):
            assistant_text = str(msg_obj.get("content", "") or "")
        if not assistant_text.strip():
            assistant_text = str(resp.get("response", "") or "")
        return assistant_text

    def _merge_assistant_chunks(self, first: str, second: str) -> str:
        """Join two assistant chunks without forcing duplicated blank lines."""
        if not first:
            return second
        if not second:
            return first
        if first.endswith(("\n", " ", "\t")) or second.startswith(("\n", " ", "\t")):
            return first + second
        return first + "\n" + second

    def _continue_truncated_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, object],
        partial_assistant_text: str,
        request_id: str = "",
    ) -> dict[str, object]:
        """Ask the model for one continuation chunk when the first reply hits the length cap."""
        continuation_messages = list(messages)
        continuation_messages.append({"role": "assistant", "content": partial_assistant_text})
        continuation_messages.append(
            {
                "role": "user",
                "content": (
                    "Continue from the exact point where you stopped. "
                    "Return only the remaining code or remaining brief explanation. "
                    "Do not repeat earlier text. Do not add markdown fences."
                ),
            }
        )
        continuation_payload: dict[str, object] = {
            "model": model,
            "messages": continuation_messages,
            "stream": True,
            "think": False,
        }
        if options:
            continuation_payload["options"] = options
        return self._ollama_stream_chat("/api/chat", continuation_payload, timeout=240.0, request_id=request_id)

    def do_GET(self) -> None:  # noqa: N802
        """Serve HTML and read-only API endpoints for the participant browser app."""
        parsed = urlparse(self.path)
        AppHandler.close_requested_at = None
        if parsed.path == "/":
            AppHandler.client_seen = True
            self._html(html_page(self.csrf_token))
            return

        if parsed.path == "/api/state":
            # Mark active browser session once state is requested.
            AppHandler.client_seen = True
            AppHandler.touch_client_poll()
            self.store.mark_onboarding_presented()
            ids = self.store.get_snippet_ids()
            readme_text = self.store.readme_path.read_text(encoding="utf-8", errors="replace")
            completion = self.store.completion_status()
            readiness_issues = self.store.preflight_issues()
            self._json(
                {
                    "snippet_ids": ids,
                    "snippet_files": {
                        snippet_id: self.store._snippet_filename(snippet_id) for snippet_id in ids
                    },
                    "snippet_labels": {
                        snippet_id: self.store.snippet_label(snippet_id) for snippet_id in ids
                    },
                    "readme": readme_text,
                    "participant_profile": self.store.read_participant_profile(),
                    "completion": completion,
                    "readiness": {"ok": len(readiness_issues) == 0, "issues": readiness_issues},
                    "timer": self.store.timer_status(),
                }
            )
            return

        if parsed.path == "/api/ping":
            AppHandler.client_seen = True
            AppHandler.touch_client_poll()
            self._json({"ok": True, "timer": self.store.timer_status()})
            return


        if parsed.path == "/api/ollama_status":
            assigned = self._ollama_assigned_model()
            try:
                tags = self._ollama_request("/api/tags")
                models_obj = tags.get("models", [])
                models = models_obj if isinstance(models_obj, list) else []
                names: list[str] = []
                for item in models:
                    if isinstance(item, dict):
                        names.append(str(item.get("name", "")))
                found = (assigned in names) if assigned else False
                self._json(
                    {
                        "ok": True,
                        "model": assigned,
                        "model_found": found,
                        "installed_models": names,
                        "endpoint": self._ollama_base_url(),
                        "endpoint_label": self._ollama_endpoint_label(),
                    }
                )
            except Exception as exc:
                self._json(
                    {
                        "ok": False,
                        "model": assigned,
                        "model_found": False,
                        "endpoint": self._ollama_base_url(),
                        "endpoint_label": self._ollama_endpoint_label(),
                        "error": str(exc),
                    }
                )
            return

        if parsed.path == "/api/chat_history":
            AppHandler.touch_client_poll()
            qs = parse_qs(parsed.query)
            snippet_id = (qs.get("snippet_id", [""])[0] or "").strip()
            if not snippet_id:
                self._json({"error": "snippet_id is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                rows = self.store.read_chat_entries(snippet_id)
                self._json({"ok": True, "entries": rows})
            except Exception as exc:
                self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/export_preview":
            files = self.store.export_preview_files()
            self._json({"ok": True, "files": files})
            return

        if parsed.path == "/api/snippet":
            AppHandler.touch_client_poll()
            qs = parse_qs(parsed.query)
            snippet_id = (qs.get("snippet_id", [""])[0] or "").strip()
            if not snippet_id:
                self._json({"error": "snippet_id is required"}, status=HTTPStatus.BAD_REQUEST)
                return

            try:
                if self.store.study_started():
                    self.store.mark_snippet_started(snippet_id)
                edited_code = self.store.load_snippet(snippet_id)
                baseline_code = self.store.load_baseline_snippet(snippet_id)
                row = self.store.get_row(snippet_id)
                self._json({"snippet_id": snippet_id, "baseline_code": baseline_code, "edited_code": edited_code, "summary": row})
            except Exception as exc:
                self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        """Handle state-changing API calls from the participant browser app."""
        parsed = urlparse(self.path)

        # Allow browser-close beacons without CSRF headers. We still require local origin.
        if parsed.path == "/api/client-closing":
            origin = (self.headers.get("Origin") or "").strip()
            referer = (self.headers.get("Referer") or "").strip()
            origin_ok = (origin == self.allowed_origin) or (not origin and referer.startswith(self.allowed_origin))
            if not origin_ok:
                self._json({"error": "Request origin is not allowed."}, status=HTTPStatus.FORBIDDEN)
                return

            AppHandler.close_requested_at = time.monotonic()
            self._json({"ok": True})
            return

        AppHandler.close_requested_at = None

        # All other state-changing endpoints are protected by same-origin + CSRF checks.
        ok, reason = self._post_security_ok()
        if not ok:
            self._json({"error": reason}, status=HTTPStatus.FORBIDDEN)
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

        try:
            if parsed.path == "/api/begin_study":
                timer = self.store.begin_study()
                AppHandler.client_seen = True
                AppHandler.heartbeat_seen = True
                AppHandler.touch_client_poll()
                self._json({"ok": True, "timer": timer})
                return

            if parsed.path == "/api/save_snippet":
                AppHandler.touch_client_poll()
                if not self.store.study_started():
                    self._json({"error": "Review onboarding and click Begin Study before editing snippets."}, status=HTTPStatus.BAD_REQUEST)
                    return
                snippet_id = str(body.get("snippet_id", "")).strip()
                code = str(body.get("code", ""))
                summary = body.get("summary", {})
                strict_summary = bool(body.get("strict_summary", False))
                if not isinstance(summary, dict):
                    summary = {}
                self.store.save_snippet_and_summary(snippet_id, code, summary, validate_summary=strict_summary)
                self.store.mark_snippet_saved(snippet_id)
                self._json({"ok": True, "message": f"Saved {snippet_id}."})
                return

            if parsed.path == "/api/heartbeat":
                AppHandler.client_seen = True
                AppHandler.touch_client_poll()
                if self.store.study_started():
                    AppHandler.heartbeat_seen = True
                    self.store.heartbeat()
                self._json({"ok": True, "timer": self.store.timer_status()})
                return

            if parsed.path == "/api/save_profile":
                AppHandler.touch_client_poll()
                profile = self.store.write_participant_profile(body)
                self._json({"ok": True, "participant_profile": profile})
                return

            if parsed.path == "/api/cancel_chat":
                AppHandler.touch_client_poll()
                request_id = str(body.get("request_id", "") or "").strip()
                if not request_id:
                    self._json({"error": "request_id is required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                AppHandler.mark_chat_request_cancelled(request_id)
                self._json({"ok": True, "request_id": request_id})
                return

            if parsed.path == "/api/add_turn":
                AppHandler.touch_client_poll()
                if not self.store.study_started():
                    self._json({"error": "Review onboarding and click Begin Study before logging chat turns."}, status=HTTPStatus.BAD_REQUEST)
                    return
                snippet_id = str(body.get("snippet_id", "")).strip()
                role = str(body.get("role", "")).strip().lower()
                text = str(body.get("text", ""))
                provider = str(body.get("provider", ""))
                model = str(body.get("model", ""))
                session_id = str(body.get("session_id", ""))
                entry = self.store.append_turn(
                    snippet_id=snippet_id,
                    role=role,
                    text=text,
                    provider=provider,
                    model=model,
                    session_id=session_id,
                )
                self._json({"ok": True, "entry": entry})
                return


            if parsed.path == "/api/ollama_chat":
                AppHandler.touch_client_poll()
                if not self.store.study_started():
                    self._json({"error": "Review onboarding and click Begin Study before using the in-app LLM chat."}, status=HTTPStatus.BAD_REQUEST)
                    return
                snippet_id = str(body.get("snippet_id", "")).strip()
                prompt = str(body.get("prompt", ""))
                provider = str(body.get("provider", "") or "ollama").strip()
                model = str(body.get("model", "") or self._ollama_assigned_model()).strip()
                session_id = str(body.get("session_id", "") or "").strip()
                request_id = str(body.get("request_id", "") or "").strip()

                if not snippet_id:
                    self._json({"error": "snippet_id is required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if not prompt.strip():
                    self._json({"error": "prompt is required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if len(prompt) > self.store.max_turn_text_chars:
                    self._json(
                        {
                            "error": (
                                f"Prompt is too large ({len(prompt)} characters). "
                                f"Keep each LLM request under {self.store.max_turn_text_chars} characters "
                                "and send only the relevant function or block."
                            )
                        },
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                if not model:
                    self._json({"error": "No model configured in this participant kit."}, status=HTTPStatus.BAD_REQUEST)
                    return
                AppHandler.clear_chat_request_cancelled(request_id)

                context_budget = max(0, self.store.max_chat_request_chars - len(prompt))
                prior = self.store.chat_messages_for_ollama(
                    snippet_id,
                    max_chars=min(self.store.max_chat_context_chars, context_budget),
                )
                msgs = [{"role": "system", "content": self._participant_chat_system_prompt()}] + prior + [{"role": "user", "content": prompt}]
                payload: dict[str, object] = {
                    "model": model,
                    "messages": msgs,
                    "stream": True,
                    "think": False,
                }
                options = self._ollama_options_from_lock()
                if options:
                    payload["options"] = options

                try:
                    resp = self._ollama_stream_chat("/api/chat", payload, timeout=240.0, request_id=request_id)
                except OllamaChatCancelled:
                    self._json({"ok": True, "discarded": True, "request_id": request_id})
                    return
                except Exception as exc:
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return

                assistant_text = self._ollama_message_text(resp)
                if not assistant_text.strip():
                    self._json({"error": "The assigned LLM returned an empty response."}, status=HTTPStatus.BAD_REQUEST)
                    return
                done_reason = str(resp.get("done_reason", "") or "").strip().lower()
                truncated = done_reason == "length"
                if truncated:
                    try:
                        cont_resp = self._continue_truncated_chat(
                            model=model,
                            messages=msgs,
                            options=options,
                            partial_assistant_text=assistant_text,
                            request_id=request_id,
                        )
                        cont_text = self._ollama_message_text(cont_resp)
                        if cont_text.strip():
                            assistant_text = self._merge_assistant_chunks(assistant_text, cont_text)
                            truncated = str(cont_resp.get("done_reason", "") or "").strip().lower() == "length"
                    except OllamaChatCancelled:
                        self._json({"ok": True, "discarded": True, "request_id": request_id})
                        return
                    except Exception:
                        pass

                if AppHandler.consume_chat_request_cancelled(request_id):
                    self._json({"ok": True, "discarded": True, "request_id": request_id})
                    return

                user_entry = self.store.append_turn(
                    snippet_id=snippet_id,
                    role="user",
                    text=prompt,
                    provider=provider,
                    model=model,
                    session_id=session_id,
                )
                assistant_entry = self.store.append_turn(
                    snippet_id=snippet_id,
                    role="assistant",
                    text=assistant_text,
                    provider=provider,
                    model=model,
                    session_id=session_id,
                )
                AppHandler.clear_chat_request_cancelled(request_id)

                self._json(
                    {
                        "ok": True,
                        "assistant_text": assistant_text,
                        "truncated": truncated,
                        "discarded": False,
                        "request_id": request_id,
                        "user_entry": user_entry,
                        "assistant_entry": assistant_entry,
                    }
                )
                return

            if parsed.path == "/api/preflight":
                AppHandler.touch_client_poll()
                if not self.store.study_started():
                    self._json({"ok": False, "issues": ["Review onboarding and click Begin Study before finishing the study."]})
                    return
                issues = self.store.preflight_issues()
                if issues:
                    self._json({"ok": False, "issues": issues})
                else:
                    self._json({"ok": True, "issues": []})
                return

            if parsed.path == "/api/build_zip":
                AppHandler.touch_client_poll()
                if not self.store.study_started():
                    self._json({"ok": False, "message": "Review onboarding and click Begin Study before finishing the study."}, status=HTTPStatus.BAD_REQUEST)
                    return
                confirmed = bool(body.get("confirmed_assigned_profile", False))
                note = str(body.get("deviation_note", "") or "").strip()
                provider = str(body.get("provider", "") or "").strip()
                model = str(body.get("model", "") or "").strip()

                if (not confirmed) and (not note):
                    self._json(
                        {"ok": False, "message": "Attestation required: confirm assigned profile or provide deviation note."},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return

                self.store.write_finish_attestation(
                    confirmed_assigned_profile=confirmed,
                    deviation_note=note,
                    provider=provider,
                    model=model,
                )

                code, output = self.store.build_submission_zip()
                if code == 0:
                    self._json({"ok": True, "message": "Submission ZIP created successfully in exports/."})
                    self.close_connection = True
                    self._shutdown_server_soon()
                else:
                    self._json(
                        {
                            "ok": False,
                            "message": output or "Packaging failed. Check required fields and try again.",
                        },
                        status=HTTPStatus.BAD_REQUEST,
                    )
                return

            self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

def run_server() -> None:
    """Start local participant web app and open the default browser."""
    kit_root = Path(__file__).resolve().parent
    pid_path = kit_root / "participant_web_app.pid"
    store = StudyStore(kit_root)
    _move_runtime_cwd_off_kit(kit_root)
    store.resume_session_if_started()

    AppHandler.store = store
    # Per-process token + local origin protect browser POST requests.
    csrf_token = secrets.token_urlsafe(32)
    AppHandler.csrf_token = csrf_token
    AppHandler.shutdown_now = False
    AppHandler.client_seen = False
    AppHandler.heartbeat_seen = False
    AppHandler.close_requested_at = None
    AppHandler.last_client_poll_at = None

    # Pick first available localhost port so stale older servers do not block start.
    server: ThreadingHTTPServer | None = None
    chosen_port: int | None = None
    for port in range(8765, 8776):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), AppHandler)
            chosen_port = port
            break
        except OSError:
            continue

    if server is None or chosen_port is None:
        print("[server] Could not bind participant app to localhost ports 8765-8775.")
        print("[server] Close old study-app processes and retry.")
        return

    AppHandler.allowed_origin = f"http://127.0.0.1:{chosen_port}"
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    def open_browser() -> None:
        """Launch one participant browser window without opening an extra blank one."""
        # Add cache-busting query so participants always receive the latest app script.
        url = f"http://127.0.0.1:{chosen_port}/?v={int(time.time())}"
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        edge_candidates = [
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
        if local_app_data:
            edge_candidates.append(Path(local_app_data) / "Microsoft" / "Edge" / "Application" / "msedge.exe")

        for edge_path in edge_candidates:
            if edge_path.exists():
                try:
                    subprocess.Popen(
                        [
                            str(edge_path),
                            "--new-window",
                            "--start-maximized",
                            "--app=" + url,
                        ]
                    )
                    print(f"[launch] Opened Edge: {edge_path}")
                    return
                except Exception:
                    pass

        webbrowser.open_new(url)

    # Allow headless/debug runs without popping a browser window.
    if os.getenv("STUDY_WEBAPP_NO_BROWSER", "").strip().lower() not in {"1", "true", "yes", "y"}:
        threading.Timer(0.5, open_browser).start()

    def close_when_browser_disconnects() -> None:
        """Close this server when browser heartbeat is lost for an extended period after a real connection."""
        while not AppHandler.shutdown_now:
            time.sleep(1.0)
            close_requested_at = AppHandler.close_requested_at
            if close_requested_at is not None and (time.monotonic() - close_requested_at) > 5.0:
                print("[server] Browser window closed. Closing participant app server.")
                AppHandler.shutdown_now = True
                try:
                    store.mark_end()
                except Exception:
                    pass
                try:
                    server.shutdown()
                except Exception:
                    pass
                return
            last_poll = AppHandler.last_client_poll_at
            if AppHandler.client_seen and last_poll is not None and (time.monotonic() - last_poll) > 20.0:
                print("[server] Browser became idle or disconnected. Closing participant app server.")
                AppHandler.shutdown_now = True
                try:
                    store.mark_end()
                except Exception:
                    pass
                try:
                    server.shutdown()
                except Exception:
                    pass
                return
            # Wait for at least one explicit browser heartbeat before arming auto-close.
            if not AppHandler.heartbeat_seen:
                continue
            if store.seconds_since_last_heartbeat() > 120.0:
                print("[server] Browser disconnected. Closing participant app server.")
                AppHandler.shutdown_now = True
                try:
                    store.mark_end()
                except Exception:
                    pass
                try:
                    server.shutdown()
                except Exception:
                    pass
                return

    threading.Thread(target=close_when_browser_disconnects, daemon=True).start()
    print(f"Participant web app running at http://127.0.0.1:{chosen_port}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except Exception:
            pass

if __name__ == "__main__":
    run_server()















































































