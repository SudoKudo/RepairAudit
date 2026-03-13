"""RepairAudit participant kit generation utilities.

This module creates participant-facing packages that contain only the files
needed to perform code edits and return structured workflow data.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
        "confidence_1to5",
        "first_prompt",
        "final_prompt",
        "notes",
    ]


def _load_snippets(metadata_csv: Path) -> list[dict[str, str]]:
    """Load valid snippet rows from metadata CSV."""
    if not metadata_csv.exists():
        raise FileNotFoundError(f"Missing metadata CSV: {metadata_csv}")

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
            sid = (row.get("snippet_id") or "").strip()
            base = (row.get("baseline_relpath") or "").strip()
            if sid and base:
                rows.append({"snippet_id": sid, "baseline_relpath": base})

    if not rows:
        raise ValueError(f"No usable rows found in metadata CSV: {metadata_csv}")
    return rows


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
                "python_experience": "",
                "llm_coding_experience": "",
                "security_experience": "",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

def _write_participant_readme(
    *,
    path: Path,
    participant_id: str,
    condition: str,
    phase: str,
    model_name: str,
    run_dir_name: str,
) -> None:
    """Write short launch/troubleshooting instructions for participants."""
    content = f"""# RepairAudit Participant Kit

Use the RepairAudit participant app for all study instructions and task steps. This README is only for launch and troubleshooting.

## 1) Ollama Setup (Required)
1. Install Ollama from [https://ollama.com/download](https://ollama.com/download).
2. Open a terminal and run: `ollama pull {model_name}`
3. Start Ollama before opening the study app:
   - `ollama serve`
4. Keep Ollama running in the background while you complete the study.

## 2) Start The Study App
1. On Windows: double-click `Launch_Study_Web_App.bat`.
2. On macOS/Linux: run `bash Launch_Study_Web_App.sh` from a terminal in this folder.
3. Keep the command window or terminal open while you work.
4. The browser app opens automatically. Use that app for everything.
5. The study timer does **not** start until you review the in-app onboarding and click **Begin Study**.

## 3) Your Assignment
- Participant ID: `{participant_id}`
- Condition: `{condition}`
- Phase: `{phase}`
- Assigned model: `{model_name}`

## 4) In-App Workflow
- Complete the participant profile in the app.
- Review the onboarding/help panel.
- Click **Begin Study** to start the timer and unlock the task workflow.
- Follow the app instructions for all 8 snippets.
- Use **Finish (Build ZIP)** in the app when done.

## 5) What The App Auto-Logs
- LLM provider/model
- Total turns
- First and final prompts
- Full turn history in `chat_log.jsonl`
- Session timing with pause/resume recovery

## 6) What You Must Enter Manually
- `applied_turns`
- `strategy_primary`
- `confidence_1to5`
- `notes` (optional)

## 7) Kit Folder Structure
```text
{participant_id}/
|-- README.md
|-- study_config.lock.json
|-- participant_web_app.py
|-- Launch_Study_Web_App.bat
|-- Launch_Study_Web_App.sh
|-- package_submission.py
`-- {run_dir_name}/
    |-- baseline/*.py
    |-- edits/*.py
    |-- logs/participant_profile.json
    |-- logs/snippet_log.csv
    |-- logs/chat_log.jsonl
    |-- condition.txt
    `-- start_end_times.json
```

## 8) Privacy Rules
Do not include personal identifiers or sensitive account data in prompts, code comments, or notes.

## 9) Help
If the app cannot connect to Ollama, start/restart `ollama serve` and try again.
If the app does not open automatically, copy the localhost URL shown in the launcher window into your browser.
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

KIT_ROOT = Path(__file__).resolve().parent
RUN_DIR = KIT_ROOT / "{run_dir_name}"
EXPORTS = KIT_ROOT / "exports"
LOG_CSV = RUN_DIR / "logs" / "snippet_log.csv"
CHAT_LOG = RUN_DIR / "logs" / "chat_log.jsonl"
PROFILE_JSON = RUN_DIR / "logs" / "participant_profile.json"


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
        "python_experience": {{"none", "basic", "intermediate", "advanced"}},
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


def main() -> None:
    if not RUN_DIR.exists():
        raise FileNotFoundError(f"Missing run folder: {{RUN_DIR}}")

    log_errors = _validate_snippet_log()
    chat_errors = _validate_chat_log()
    profile_errors = _validate_participant_profile()
    all_errors = log_errors + chat_errors + profile_errors
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



def _write_participant_launchers(*, kit_dir: Path) -> None:
    """Write platform launchers that start the participant app."""
    launcher_bat = r"""@echo off
setlocal
cd /d %~dp0

REM Clear stale participant web-app Python processes so this kit always starts clean.
REM This avoids "port already in use" conflicts from previous kits/sessions.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'participant_web_app\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }" >nul 2>&1

if exist venv\Scripts\python.exe (
  venv\Scripts\python.exe participant_web_app.py
) else if exist C:\Windows\py.exe (
  py -3 participant_web_app.py
) else if exist C:\Windows\System32\py.exe (
  py -3 participant_web_app.py
) else (
  python participant_web_app.py
)
"""
    launcher_sh = """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -x "venv/bin/python" ]; then
  "venv/bin/python" participant_web_app.py
elif command -v python3 >/dev/null 2>&1; then
  python3 participant_web_app.py
else
  python participant_web_app.py
fi
"""
    (kit_dir / "Launch_Study_Web_App.bat").write_text(launcher_bat, encoding="utf-8")
    (kit_dir / "Launch_Study_Web_App.sh").write_text(launcher_sh, encoding="utf-8")
def build_participant_kit(args: argparse.Namespace) -> None:
    """Build a locked participant kit with baseline snippets and submission helpers."""
    snippets = _load_snippets(Path(args.metadata_csv))
    snippet_ids = [row["snippet_id"] for row in snippets]

    out_root = Path(args.out_root)
    kit_dir = out_root / args.participant_id
    run_dir_name = f"run_{args.phase}_{args.participant_id}"
    run_dir = kit_dir / run_dir_name

    if kit_dir.exists():
        if not args.overwrite:
            raise FileExistsError(
                f"Kit already exists: {kit_dir}. Use --overwrite to replace it."
            )
        shutil.rmtree(kit_dir)

    (run_dir / "edits").mkdir(parents=True, exist_ok=True)
    # Keep an immutable baseline snapshot so participants can copy from it while
    # saving their final answer to the editable output file.
    (run_dir / "baseline").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)

    for row in snippets:
        sid = row["snippet_id"]
        src = Path(row["baseline_relpath"])
        if not src.exists():
            raise FileNotFoundError(f"Missing baseline snippet: {src}")
        # Keep the editable answer file intentionally blank at start.
        (run_dir / "edits" / f"{sid}.py").write_text("", encoding="utf-8")
        shutil.copy2(src, run_dir / "baseline" / f"{sid}.py")

    (run_dir / "condition.txt").write_text(args.condition.strip() + "\n", encoding="utf-8")
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
        "snippet_ids": snippet_ids,
        "llm": {
            "provider": args.llm_provider,
            "model": args.llm_model,
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
    (kit_dir / "study_config.lock.json").write_text(json.dumps(locked_config, indent=2), encoding="utf-8")

    _write_participant_readme(
        path=kit_dir / "README.md",
        participant_id=args.participant_id,
        condition=args.condition,
        phase=args.phase,
        model_name=args.llm_model,
        run_dir_name=run_dir_name,
    )
    _write_submission_packager(
        path=kit_dir / "package_submission.py",
        run_dir_name=run_dir_name,
        participant_id=args.participant_id,
        condition=args.condition,
        phase=args.phase,
    )
    template_app = Path(__file__).resolve().with_name("participant_web_app_template.py")
    if not template_app.exists():
        raise FileNotFoundError(f"Missing participant app template: {template_app}")
    shutil.copy2(template_app, kit_dir / "participant_web_app.py")
    _write_participant_launchers(kit_dir=kit_dir)

    print("Participant kit created.")
    print(f"Kit: {kit_dir}")
    print(f"Run folder: {run_dir}")
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

    target_dirs: list[Path] = []
    participant_id = (args.participant_id or "").strip()

    if participant_id and args.all:
        raise ValueError("Use either --participant_id or --all, not both.")

    if participant_id:
        one = kits_root / participant_id
        if one.exists() and one.is_dir():
            target_dirs = [one]
        else:
            print(f"No kit found for participant_id={participant_id} at {one}")
            return
    elif args.all:
        target_dirs = sorted([d for d in kits_root.iterdir() if d.is_dir()], key=lambda p: p.name)
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
            shutil.rmtree(d)
            print(f"Removed: {d}")
            removed += 1

    if args.dry_run:
        print(f"Preview complete. {len(target_dirs)} kit(s) would be removed.")
    else:
        print(f"Done. Removed {removed} kit(s).")
































