"""Participant-facing local web app template used in generated study kits.

The generated file serves baseline snippets, collects edited code plus summary
metadata, proxies local Ollama chat calls, and builds the participant return ZIP.
"""

from __future__ import annotations

import csv
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
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
    "python_experience": ["none", "basic", "intermediate", "advanced"],
    "llm_coding_experience": ["never", "rarely", "monthly", "weekly", "daily"],
    "security_experience": ["none", "self-taught", "coursework", "professional"],
}
PARTICIPANT_PROFILE_FIELDS = list(PARTICIPANT_PROFILE_OPTIONS.keys())


class StudyStore:
    """Data layer for the participant web app.

    This class keeps file contracts and validation rules centralized so the UI
    can stay simple while the study data remains consistent.
    """

    def __init__(self, kit_root: Path) -> None:
        self.kit_root = kit_root
        self.lock_path = kit_root / "study_config.lock.json"
        self.readme_path = kit_root / "README.md"
        self.packager_path = kit_root / "package_submission.py"

        self.lock_data = self._read_json(self.lock_path)
        self.run_dir = self._find_run_dir()
        self.edits_dir = self.run_dir / "edits"
        self.baseline_dir = self.run_dir / "baseline"
        self.log_csv = self.run_dir / "logs" / "snippet_log.csv"
        self.chat_log = self.run_dir / "logs" / "chat_log.jsonl"
        self.timer_path = self.run_dir / "start_end_times.json"
        self.attestation_path = self.run_dir / "logs" / "llm_attestation.json"
        self.client_meta_path = self.run_dir / "logs" / "client_meta.json"
        self.participant_profile_path = self.run_dir / "logs" / "participant_profile.json"

        self.fields = [
            "snippet_id",
            "tool",
            "model",
            "turns",
            "applied_turns",
            "strategy_primary",
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
        path = self.edits_dir / f"{snippet_id}.py"
        if not path.exists():
            raise FileNotFoundError(f"Snippet file not found: {path}")
        return path.read_text(encoding="utf-8", errors="replace")

    def load_baseline_snippet(self, snippet_id: str) -> str:
        """Load read-only baseline text shown above the editable answer box."""
        path = self.baseline_dir / f"{snippet_id}.py"
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
        path = self.edits_dir / f"{snippet_id}.py"
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
            self._validate_summary(summary)
        self.write_rows(rows)

        # Track latest autosave/save so participants can see draft persistence feedback.
        timer_payload = self._read_json(self.timer_path)
        timer_payload["last_autosave_utc"] = utc_now()
        self._write_json(self.timer_path, timer_payload)

    def _validate_summary(self, summary: dict[str, str]) -> None:
        """Validate participant-entered summary fields before export or strict save."""
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

    def write_client_meta(self, payload: dict[str, object]) -> None:
        """Persist lightweight client fingerprint metadata (non-PII)."""
        safe = {
            "captured_utc": utc_now(),
            "platform": str(payload.get("platform", "") or ""),
            "user_agent": str(payload.get("user_agent", "") or ""),
            "language": str(payload.get("language", "") or ""),
            "app_version": str(payload.get("app_version", "") or ""),
        }
        self.client_meta_path.parent.mkdir(parents=True, exist_ok=True)
        self.client_meta_path.write_text(json.dumps(safe, indent=2), encoding="utf-8")

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

    def chat_messages_for_ollama(self, snippet_id: str) -> list[dict[str, str]]:
        """Map snippet chat history to Ollama chat message format."""
        entries = self.read_chat_entries(snippet_id)
        msgs: list[dict[str, str]] = []
        for entry in entries:
            role = str(entry.get("role", "")).strip().lower()
            txt = str(entry.get("text", ""))
            if role in {"user", "assistant"} and txt.strip():
                msgs.append({"role": role, "content": txt})
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
    return """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>RepairAudit Participant App</title>
<style>
:root{--bg:#f4f8ff;--panel:#ffffff;--text:#0f2039;--muted:#5c6f8b;--line:#d5e3ff;--accent:#2d79ff;--accent-dark:#1f5fd1;--ok:#0a8f4e;--bad:#c53b32}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#f9fbff 0%,#eff5ff 100%);font-family:Segoe UI,Arial,sans-serif;color:var(--text)}
.wrap{max-width:1600px;margin:0 auto;padding:16px}
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
.readme{max-height:220px;overflow:auto;white-space:pre-wrap;font-size:13px;color:#334f76;border:1px solid #e1ebff;border-radius:10px;padding:9px;background:#fbfdff}
textarea,input{width:100%;border:1px solid #ccddff;border-radius:10px;padding:8px 10px;background:#fff;color:var(--text)}
textarea{font-family:Consolas,monospace;font-size:13px;min-height:220px}
#baseline_code{background:#f8fbff;min-height:170px}
#chat_prompt{min-height:105px}
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
.chatlog{margin-top:8px;min-height:220px;max-height:420px;overflow:auto;border:1px solid #d8e6ff;border-radius:10px;padding:9px;background:#fbfdff}
.chatlog.expanded{max-height:680px}
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
    <div class="sub" id="meta">Loading study information...</div>
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
        <button class="btn alt tiny" id="copyBaselineBtn" title="Copy baseline code to clipboard.">Copy Baseline</button>
      </div>
      <textarea id="baseline_code" readonly title="Baseline snippet is read-only. Use it as your reference."></textarea>
      <div class="lbl" style="margin:10px 0 4px 0" title="Paste and refine your final edited snippet here.">Your Edited Code</div>
      <textarea id="edited_code" title="This is the final code that will be exported for this snippet."></textarea>

      <hr style="border:none;border-top:1px solid #e5edff;margin:12px 0" />
      <div class="sp">
        <strong title="Use this panel to chat with local Ollama. Prompts and replies are auto-logged to this snippet.">In-App LLM Chat (Ollama)</strong>
        <div class="row">
          <span class="tag" id="chatTurnCount" title="Auto-logged turns for the current snippet.">0 turns</span>
          <button class="btn alt tiny" id="toggleChatSizeBtn" type="button" title="Expand or collapse the visible chat history area.">Expand Chat</button>
        </div>
      </div>
      <div class="lbl" id="ollamaStatus" style="margin-top:6px" title="Connection/model status for local Ollama.">Checking local Ollama connection...</div>
      <div class="chatlog" id="chatHistory" title="Chat history for the currently selected snippet."></div>
      <div class="full" style="margin-top:8px">
        <label class="lbl" title="Enter one prompt for Ollama about the current snippet.">Chat Prompt</label>
        <textarea id="chat_prompt" placeholder="Ask Ollama for help with this snippet..." title="Press Ctrl+Enter to send quickly."></textarea>
      </div>
      <div class="row" style="margin-top:8px">
        <button class="btn" id="sendChatBtn" title="Send prompt to Ollama and auto-log both user and assistant turns.">Send To Ollama</button></div>
      <div class="lbl" style="margin-top:6px">Prompts and replies here are auto-logged for this snippet.</div>
    </section>

    <section class="card">
      <div class="sp"><strong title="Web app usage instructions for this study task.">Web App Guide</strong><div class="row"><button class="btn alt tiny guideaction" id="showOnboardingBtn" title="Open the short onboarding guide again.">Show Onboarding</button><button class="btn alt tiny guideaction" id="toggleReadme" title="Show or hide the guide panel.">Hide</button></div></div>
      <div class="readme" id="readme" style="margin-top:8px" title="Step-by-step instructions for completing this study inside the app."></div>

      <hr style="border:none;border-top:1px solid #e5edff;margin:12px 0" />
      <strong title="Required and optional metadata for this snippet.">Snippet Summary</strong>
      <div class="form" style="margin-top:8px">
        <div><label class="lbl" title="Auto-filled from the configured assistant provider.">Tool (Auto)</label><input id="tool" readonly title="Auto-filled provider (read-only)." /></div>
        <div><label class="lbl" title="Auto-filled from the configured model in the kit.">Model (Auto)</label><input id="model" readonly title="Auto-filled model name (read-only)." /></div>
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
        <div><label class="lbl" title="Your confidence that the final snippet is secure.">Confidence (1-5)</label><select id="confidence_1to5" title="1 = low confidence, 5 = high confidence."><option value="">Select...</option><option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="4">4</option><option value="5">5</option></select></div>
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
    <div class="lbl" style="margin-top:6px">Complete this once before clicking Begin Study.</div>
    <div class="form" style="margin-top:8px">
      <div><label class="lbl" title="Your overall programming experience.">Programming Experience</label><select id="programming_experience"><option value="">Select...</option><option value="<1 year">&lt;1 year</option><option value="1-2 years">1-2 years</option><option value="3-5 years">3-5 years</option><option value="6+ years">6+ years</option></select></div>
      <div><label class="lbl" title="Your Python experience.">Python Experience</label><select id="python_experience"><option value="">Select...</option><option value="none">None</option><option value="basic">Basic</option><option value="intermediate">Intermediate</option><option value="advanced">Advanced</option></select></div>
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
    if(xhr.readyState !== 4){ return; }
    var data = {};
    try { data = JSON.parse(xhr.responseText || "{}"); } catch(_e) { data = {}; }
    if(xhr.status >= 200 && xhr.status < 300){
      onOk && onOk(data);
    } else {
      onErr && onErr(data.error || data.message || ("Request failed: " + xhr.status));
    }
  };
  xhr.onerror = function(){
    onErr && onErr("Network request failed.");
  };
  xhr.send(body ? JSON.stringify(body) : null);
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
    left.textContent = sid;
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
  var fields = ["programming_experience","python_experience","llm_coding_experience","security_experience"];
  for(var i=0;i<fields.length;i++){
    var key = fields[i];
    var el = byId(key);
    if(el){ el.value = profile[key] || ""; }
  }
}

function collectProfile(){
  var out = {};
  var fields = ["programming_experience","python_experience","llm_coding_experience","security_experience"];
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
  var profileInputs = ["programming_experience","python_experience","llm_coding_experience","security_experience"];
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
  var fields = ["tool","model","applied_turns","strategy_primary","confidence_1to5","notes"];
  for(var i=0;i<fields.length;i++){
    var key = fields[i];
    var el = byId(key);
    if(el){ el.value = row[key] || ""; }
  }
}

function collectSummary(){
  var out = {};
  out.tool = (state && state.provider) ? String(state.provider) : "";
  out.model = (state && state.model) ? String(state.model) : "";
  var fields = ["applied_turns","strategy_primary","confidence_1to5","notes"];
  for(var i=0;i<fields.length;i++){
    var key = fields[i];
    var el = byId(key);
    out[key] = el ? (el.value || "").trim() : "";
  }
  return out;
}

function buildInAppGuide(data){
  var modelName = (data && data.model) ? String(data.model) : "assigned model";
  var provider = (data && data.provider) ? String(data.provider) : "configured provider";
  return [
    "Quick Steps",
    "1) Select a snippet from the left sidebar.",
    "2) Review Baseline (read-only).",
    "3) Paste/finalize your answer in Your Edited Code.",
    "4) Use In-App LLM Chat to help you (auto-logged).",
    "5) Complete the Participant Profile in the onboarding popup and click Begin Study.",
    "6) The timer starts only after Begin Study.",
    "7) Fill snippet summary fields, click Save, repeat for all snippets, then click Finish (Build ZIP).",
    "",
    "LLM Usage",
    "- Assigned provider: " + provider,
    "- Assigned model: " + modelName,
    "- At least one in-app LLM turn is required for each snippet.",
    "- A turn means one logged user or assistant message in the in-app chat for the current snippet.",
    "",
    "Prompt Strategies",
    "- Zero-Shot: ask directly for a fix with no examples. Example: Fix the SQL injection vulnerability in this function. Return only the corrected code.",
    "- Few-Shot: give examples before asking for the fix. Example: Example unsafe ... Example safe ... Now fix this function.",
    "- Chain-of-Thought: ask the model to reason step by step before giving the final fix. Example: Think step by step about why this code is vulnerable, then provide the final corrected code.",
    "- Adaptive Chain-of-Thought: ask the model to choose concise or step-by-step reasoning based on task difficulty. Example: If the fix is simple, answer briefly. If complex, reason step by step and then provide the final corrected code.",
    "",
    "Snippet Summary Fields You Must Enter",
    "- Tip: hover over metric names, field labels, and button names in the app to see tooltips.",
    "- Applied Turns: non-negative integer, must be <= total logged turns. Count only assistant turns that changed your final code.",
    "- Primary Strategy: choose the main prompt strategy you used for this snippet.",
    "- Confidence: choose a value from 1 to 5.",
    "- Notes: optional short factual notes.",
    "",
    "Auto-Logged By App",
    "- Tool/provider and model",
    "- Total turns",
    "- First and final prompts",
    "- Full chat transcript per snippet",
    "- Session timer (resume-aware)",
    "",
    "If Ollama Refuses Or Times Out",
    "- Retry once with a narrower repair request.",
    "- If Ollama still fails, keep your current code changes and continue.",
    "",
    "Privacy Reminder",
    "- Do not include personal identifiers or sensitive account data."
  ].join("\\n");
}

function setChatTurnBadge(turns){
  var el = byId("chatTurnCount");
  if(el){
    var count = Math.max(0, Number(turns || 0));
    el.textContent = count + (count === 1 ? " turn" : " turns");
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

function copyBaseline(){
  var baseline = byId("baseline_code");
  var text = baseline ? String(baseline.value || "") : "";
  if(!text){
    setMsg("No baseline code available to copy.", false);
    return;
  }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(function(){
      setMsg("Baseline copied to clipboard.", true);
    }, function(){
      setMsg("Could not copy baseline to clipboard.", false);
    });
    return;
  }
  try{
    baseline.focus();
    baseline.select();
    var ok = document.execCommand && document.execCommand("copy");
    setMsg(ok ? "Baseline copied to clipboard." : "Could not copy baseline to clipboard.", !!ok);
  } catch(_err){
    setMsg("Could not copy baseline to clipboard.", false);
  }
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
  var modelName = escHtml((data && data.model) ? String(data.model) : "assigned model");
  var provider = escHtml((data && data.provider) ? String(data.provider) : "configured provider");
  return [
    "<p>This app is the full task workflow. You do not need to switch back to the README while completing the kit.</p>",
    "<h3>How To Complete Each Snippet</h3>",
    "<ul>",
    "<li>Select a snippet from the left list.</li>",
    "<li>Review the baseline code, then write your final answer in <strong>Your Edited Code</strong>.</li>",
    "<li>Complete the participant profile below, review this onboarding guide, and click <strong>Begin Study</strong>. The timer starts only then.</li>",
    "<li>Use the in-app Ollama chat. At least one in-app turn is required for each snippet.</li>",
    "<li>Fill in the snippet summary and save before moving on.</li>",
    "<li>After all snippets are complete, click <strong>Finish (Build ZIP)</strong>.</li>",
    "</ul>",
    "<h3>Assigned Model</h3>",
    "<p>Provider: <strong>" + provider + "</strong><br/>Model: <strong>" + modelName + "</strong></p>",
    "<h3>What Counts As A Turn</h3>",
    "<p>A turn is one logged message in the in-app chat. The app auto-logs total turns for each snippet.</p>",
    "<p><strong>Applied Turns</strong> means how many assistant turns directly changed your final code.</p>",
    "<h3>Prompt Strategies</h3>",
    "<ul>",
    "<li><strong>Zero-Shot</strong>: ask directly for a fix with no examples.<div class='example'>Fix the SQL injection vulnerability in this function. Return only the corrected code.</div></li>",
    "<li><strong>Few-Shot</strong>: provide short examples before asking for the fix.<div class='example'>Example unsafe: ... Example safe: ... Now fix this function.</div></li>",
    "<li><strong>Chain-of-Thought</strong>: ask the model to reason step by step before giving the final fix.<div class='example'>Think step by step about why this code is vulnerable, then provide the final corrected code.</div></li>",
    "<li><strong>Adaptive Chain-of-Thought</strong>: ask the model to decide whether concise or step-by-step reasoning is needed.<div class='example'>If the fix is simple, answer briefly. If complex, reason step by step and then provide the final corrected code.</div></li>",
    "</ul>",
    "<h3>If Ollama Refuses Or Fails</h3>",
    "<ul>",
    "<li>Retry once with a narrower repair request.</li>",
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
  back.classList.add("show");
  back.setAttribute("aria-hidden", "false");
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
  var ids = ["prevBtn","saveBtn","nextBtn","zipBtn","copyBaselineBtn","sendChatBtn","applied_turns","strategy_primary","confidence_1to5","notes","appliedZeroBtn","appliedOneBtn","appliedAllBtn"];
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
  var beginBtn = byId("beginStudyBtn");
  if(beginBtn){ beginBtn.disabled = studyStarted(); }
  var closeBtn = byId("closeOnboardingBtn");
  if(closeBtn){ closeBtn.disabled = !studyStarted(); closeBtn.style.opacity = studyStarted() ? "1" : "0.5"; }
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
    return "Ollama returned an empty response. Retry once with a narrower repair request.";
  }
  if(lower.indexOf("timed out") !== -1 || lower.indexOf("timeout") !== -1){
    return "Ollama timed out before returning a response. Retry once or shorten the request.";
  }
  if(lower.indexOf("connection") !== -1 || lower.indexOf("refused") !== -1 || lower.indexOf("unavailable") !== -1){
    return "Could not reach Ollama. Confirm it is running, then retry.";
  }
  return "Ollama request failed: " + raw;
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
      meta.textContent = "Participant: " + d.participant_id + " | Condition: " + d.condition + " | Phase: " + d.phase + " | Model: " + d.model;
    }
    var readme = byId("readme");
    if(readme){ readme.textContent = buildInAppGuide(d); }
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
    if(sidLbl){ sidLbl.textContent = currentSid; }
    var baseline = byId("baseline_code");
    if(baseline){ baseline.value = d.baseline_code || ""; }
    var edited = byId("edited_code");
    if(edited){
      var editedText = d.edited_code || "";
      edited.value = editedText;
    }
    fillSummary(d.summary || {});
    var tool = byId("tool");
    if(tool){ tool.value = (state && state.provider) ? String(state.provider) : ""; }
    var model = byId("model");
    if(model){ model.value = (state && state.model) ? String(state.model) : ""; }
    var autoTurns = getAutoTurnsForCurrent();
    var autoTurnsNote = byId("autoTurnsNote");
    if(autoTurnsNote){ autoTurnsNote.textContent = "Auto-logged turns for this snippet: " + autoTurns; }
    setChatTurnBadge(autoTurns);
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
  var payload = {snippet_id: currentSid, code: code, summary: collectSummary()};
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
  var payload = {snippet_id: currentSid, code: code, summary: collectSummary()};
  api("/api/save_snippet", "POST", payload, function(){
    setMsg("Saved " + currentSid + ".", true);
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

function loadChatHistory(){
  var box = byId("chatHistory");
  if(!box || !currentSid){ return; }
  api("/api/chat_history?snippet_id=" + encodeURIComponent(currentSid), "GET", null, function(d){
    var entries = (d && d.entries) ? d.entries : [];
    setChatTurnBadge(getAutoTurnsForCurrent());
    if(!entries.length){
      box.innerHTML = "<div class='lbl'>No turns logged yet for this snippet.</div>";
      return;
    }
    var html = "";
    for(var i=0;i<entries.length;i++){
      var row = entries[i] || {};
      var role = String(row.role || "assistant");
      var turn = String(row.turn_index || "");
      html += "<div class='chatrow " + (role === "user" ? "user" : "assistant") + "'>";
      html += "<div class='chatmeta'>Turn " + escHtml(turn) + " - " + escHtml(role) + "</div>";
      html += "<div>" + escHtml(row.text || "") + "</div>";
      html += "</div>";
    }
    box.innerHTML = html;
    box.scrollTop = box.scrollHeight;
  }, function(msg){
    box.innerHTML = "<div class='msg err'>Could not load chat history: " + escHtml(msg) + "</div>";
  });
}

function refreshOllamaStatus(){
  var el = byId("ollamaStatus");
  if(!el){ return; }
  api("/api/ollama_status", "GET", null, function(d){
    if(d && d.ok){
      var model = d.model || "";
      if(d.model_found){
        el.textContent = "Ollama connected. Model ready: " + model;
      } else {
        el.textContent = "Ollama connected, but assigned model not found locally: " + model;
      }
    } else {
      el.textContent = "Ollama status unavailable.";
    }
  }, function(msg){
    el.textContent = "Ollama unavailable: " + msg;
  });
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
  var btn = byId("sendChatBtn");
  if(btn){ btn.disabled = true; btn.textContent = "Saving Draft..."; }

  saveDraftCurrent(function(){
    if(btn){ btn.textContent = "Sending..."; }
    api("/api/ollama_chat", "POST", {
      snippet_id: currentSid,
      prompt: prompt,
      provider: "ollama",
      model: (state && state.model) ? state.model : "",
      session_id: ((state && state.participant_id) ? String(state.participant_id) : "session") + "_session_1"
    }, function(resp){
      if(promptEl){ promptEl.value = ""; }
      var assistantText = (resp && resp.assistant_text) ? String(resp.assistant_text) : "";
      if(isRefusalLike(assistantText)){
        setMsg("Ollama response was logged, but it looks like a refusal or non-answer. Retry once with a narrower repair request if needed.", false);
      } else {
        setMsg("Ollama response logged for " + currentSid + ".", true);
      }
      refreshState(function(){
        loadSnippet();
      });
      if(btn){ btn.disabled = false; btn.textContent = "Send To Ollama"; }
    }, function(msg){
      if(btn){ btn.disabled = false; btn.textContent = "Send To Ollama"; }
      setMsg(formatChatFailure(msg), false);
    });
  }, function(msg){
    if(btn){ btn.disabled = false; btn.textContent = "Send To Ollama"; }
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
        provider: (state && state.provider) ? String(state.provider) : "",
        model: (state && state.model) ? String(state.model) : ""
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
  var copy = byId("copyBaselineBtn");
  if(copy){ copy.onclick = copyBaseline; }
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

  var summaryInputs = ["applied_turns","strategy_primary","confidence_1to5"];
  for(var si=0; si<summaryInputs.length; si++){
    var se = byId(summaryInputs[si]);
    if(se){
      addEvt(se, "change", function(){ validateSummaryInputs(false); });
      addEvt(se, "blur", function(){ validateSummaryInputs(false); });
    }
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
        self.wfile.write(raw)

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
        self.wfile.write(raw)


    def _ollama_request(self, path: str, payload: dict[str, object] | None = None, *, timeout: float = 90.0) -> dict[str, object]:
        """Call local Ollama HTTP API and return parsed JSON object."""
        url = f"http://127.0.0.1:11434{path}"
        data = None
        method = "GET"
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            method = "POST"

        req = Request(url=url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except URLError as exc:
            raise RuntimeError(f"Could not reach local Ollama API at 127.0.0.1:11434 ({exc}).") from exc
        except Exception as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc

        try:
            obj = json.loads(raw)
        except Exception as exc:
            raise RuntimeError("Ollama returned non-JSON response.") from exc

        if not isinstance(obj, dict):
            raise RuntimeError("Ollama returned unexpected response format.")
        return obj

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
            ids = self.store.get_snippet_ids()
            readme_text = self.store.readme_path.read_text(encoding="utf-8", errors="replace")
            llm = self.store.lock_data.get("llm", {})
            provider = llm.get("provider", "ollama") if isinstance(llm, dict) else "ollama"
            model = llm.get("model", "") if isinstance(llm, dict) else ""
            completion = self.store.completion_status()
            readiness_issues = self.store.preflight_issues()
            self._json(
                {
                    "participant_id": str(self.store.lock_data.get("participant_id", "")).strip(),
                    "condition": str(self.store.lock_data.get("condition", "")).strip(),
                    "phase": str(self.store.lock_data.get("phase", "")).strip(),
                    "provider": str(provider),
                    "model": str(model),
                    "snippet_ids": ids,
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
                self._json({"ok": True, "model": assigned, "model_found": found, "installed_models": names})
            except Exception as exc:
                self._json({"ok": False, "model": assigned, "model_found": False, "error": str(exc)})
            return

        if parsed.path == "/api/chat_history":
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
            qs = parse_qs(parsed.query)
            snippet_id = (qs.get("snippet_id", [""])[0] or "").strip()
            if not snippet_id:
                self._json({"error": "snippet_id is required"}, status=HTTPStatus.BAD_REQUEST)
                return

            try:
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
                self._json({"ok": True, "timer": timer})
                return

            if parsed.path == "/api/save_snippet":
                if not self.store.study_started():
                    self._json({"error": "Review onboarding and click Begin Study before editing snippets."}, status=HTTPStatus.BAD_REQUEST)
                    return
                snippet_id = str(body.get("snippet_id", "")).strip()
                code = str(body.get("code", ""))
                summary = body.get("summary", {})
                if not isinstance(summary, dict):
                    summary = {}
                self.store.save_snippet_and_summary(snippet_id, code, summary, validate_summary=False)
                self._json({"ok": True, "message": f"Saved {snippet_id}."})
                return

            if parsed.path == "/api/heartbeat":
                AppHandler.client_seen = True
                if self.store.study_started():
                    AppHandler.heartbeat_seen = True
                    self.store.heartbeat()
                self._json({"ok": True, "timer": self.store.timer_status()})
                return

            if parsed.path == "/api/client_meta":
                self.store.write_client_meta(body)
                self._json({"ok": True})
                return

            if parsed.path == "/api/save_profile":
                profile = self.store.write_participant_profile(body)
                self._json({"ok": True, "participant_profile": profile})
                return

            if parsed.path == "/api/add_turn":
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
                if not self.store.study_started():
                    self._json({"error": "Review onboarding and click Begin Study before using Ollama chat."}, status=HTTPStatus.BAD_REQUEST)
                    return
                snippet_id = str(body.get("snippet_id", "")).strip()
                prompt = str(body.get("prompt", ""))
                provider = str(body.get("provider", "") or "ollama").strip()
                model = str(body.get("model", "") or self._ollama_assigned_model()).strip()
                session_id = str(body.get("session_id", "") or "").strip()

                if not snippet_id:
                    self._json({"error": "snippet_id is required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if not prompt.strip():
                    self._json({"error": "prompt is required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if not model:
                    self._json({"error": "No model configured in this participant kit."}, status=HTTPStatus.BAD_REQUEST)
                    return

                prior = self.store.chat_messages_for_ollama(snippet_id)
                msgs = prior + [{"role": "user", "content": prompt}]
                payload: dict[str, object] = {"model": model, "messages": msgs, "stream": False}
                options = self._ollama_options_from_lock()
                if options:
                    payload["options"] = options

                try:
                    resp = self._ollama_request("/api/chat", payload)
                except Exception as exc:
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return

                msg_obj = resp.get("message", {})
                assistant_text = ""
                if isinstance(msg_obj, dict):
                    assistant_text = str(msg_obj.get("content", "") or "")
                if not assistant_text.strip():
                    assistant_text = str(resp.get("response", "") or "")
                if not assistant_text.strip():
                    self._json({"error": "Ollama returned an empty response."}, status=HTTPStatus.BAD_REQUEST)
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

                self._json(
                    {
                        "ok": True,
                        "assistant_text": assistant_text,
                        "user_entry": user_entry,
                        "assistant_entry": assistant_entry,
                    }
                )
                return

            if parsed.path == "/api/preflight":
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
    store = StudyStore(kit_root)
    store.resume_session_if_started()

    AppHandler.store = store
    # Per-process token + local origin protect browser POST requests.
    csrf_token = secrets.token_urlsafe(32)
    AppHandler.csrf_token = csrf_token
    AppHandler.shutdown_now = False
    AppHandler.client_seen = False
    AppHandler.heartbeat_seen = False
    AppHandler.close_requested_at = None

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

    def open_browser() -> None:
        """Launch Edge first for consistency; fall back to default browser."""
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
                    subprocess.Popen([str(edge_path), url])
                    print(f"[launch] Opened Edge: {edge_path}")
                    return
                except Exception:
                    pass

        webbrowser.open(url)

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

if __name__ == "__main__":
    run_server()















































































