from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "participant_web_app_template.py"
)
SPEC = importlib.util.spec_from_file_location("participant_web_app_template", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load module from {MODULE_PATH}")
participant_web_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(participant_web_app)


class ParticipantWebAppTemplateTests(unittest.TestCase):
    def _make_handler(self) -> object:
        handler = object.__new__(participant_web_app.AppHandler)
        handler.store = type(
            "StoreStub",
            (),
            {"lock_data": {"llm": {"base_url": "http://127.0.0.1:11434", "model": "qwen3.6:27b"}}},
        )()
        return handler

    def test_move_runtime_cwd_off_kit_changes_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            kit_root = tmp_path / "kit" / ".repairaudit"
            kit_root.mkdir(parents=True)

            original_cwd = Path.cwd()
            try:
                os.chdir(kit_root)
                with patch.object(participant_web_app.tempfile, "gettempdir", return_value=str(tmp_path)):
                    participant_web_app._move_runtime_cwd_off_kit(kit_root)
                self.assertEqual(Path.cwd().resolve(), tmp_path.resolve())
            finally:
                os.chdir(original_cwd)

    def test_participant_chat_system_prompt_is_request_following(self) -> None:
        prompt = participant_web_app.participant_chat_system_prompt()

        self.assertIn("Follow the participant's request exactly", prompt)
        self.assertIn("Do not assume a task, intent, or output format", prompt)
        self.assertIn("Do not use markdown fences unless the participant asks for them.", prompt)
        self.assertNotIn("You are assisting with a code repair task.", prompt)
        self.assertNotIn("If they ask whether code is vulnerable", prompt)
        self.assertNotIn("If they ask for a fix", prompt)

    def test_strict_save_requires_applied_turns_but_draft_save_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            public_root = tmp_path / "kit"
            kit_root = public_root / ".repairaudit"
            run_dir = kit_root / "run_pilot_P001"
            logs_dir = run_dir / "logs"
            edits_dir = run_dir / "edits"
            baseline_dir = run_dir / "baseline"
            timings_dir = run_dir / "timings"

            timings_dir.mkdir(parents=True)
            edits_dir.mkdir(parents=True)
            baseline_dir.mkdir(parents=True)
            logs_dir.mkdir(parents=True)

            (public_root / "README.md").write_text("kit", encoding="utf-8")
            (kit_root / "study_config.lock.json").write_text(
                json.dumps({"llm": {"provider": "ollama", "model": "qwen3.6:27b"}}),
                encoding="utf-8",
            )
            (kit_root / "package_submission.py").write_text("print('ok')\n", encoding="utf-8")
            (run_dir / "start_end_times.json").write_text(json.dumps({"study_started": True}), encoding="utf-8")
            (logs_dir / "chat_log.jsonl").write_text("", encoding="utf-8")
            (edits_dir / "snippet_01.c").write_text("", encoding="utf-8")
            (baseline_dir / "snippet_01.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            (logs_dir / "snippet_log.csv").write_text(
                "\n".join(
                        [
                        "snippet_id,tool,model,turns,applied_turns,strategy_primary,strategy_other_text,confidence_1to5,first_prompt,final_prompt,notes",
                        "S01,Ollama,qwen3.6:27b,2,0,zero_shot,,3,,,",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            store = participant_web_app.StudyStore(kit_root)
            summary = {
                "applied_turns": "",
                "strategy_primary": "zero_shot",
                "strategy_other_text": "",
                "confidence_1to5": "3",
                "notes": "",
            }

            store.save_snippet_and_summary("S01", "draft", summary, validate_summary=False)
            rows_after_draft = store.read_rows()
            self.assertEqual(rows_after_draft[0]["applied_turns"], "")

            with self.assertRaises(ValueError):
                store.save_snippet_and_summary("S01", "strict", summary, validate_summary=True)

    def test_strict_save_requires_other_strategy_text_and_non_empty_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            public_root = tmp_path / "kit"
            kit_root = public_root / ".repairaudit"
            run_dir = kit_root / "run_pilot_P001"
            logs_dir = run_dir / "logs"
            edits_dir = run_dir / "edits"
            baseline_dir = run_dir / "baseline"
            timings_dir = run_dir / "timings"

            timings_dir.mkdir(parents=True)
            edits_dir.mkdir(parents=True)
            baseline_dir.mkdir(parents=True)
            logs_dir.mkdir(parents=True)

            (public_root / "README.md").write_text("kit", encoding="utf-8")
            (kit_root / "study_config.lock.json").write_text(
                json.dumps({"llm": {"provider": "ollama", "model": "qwen3.6:27b"}}),
                encoding="utf-8",
            )
            (kit_root / "package_submission.py").write_text("print('ok')\n", encoding="utf-8")
            (run_dir / "start_end_times.json").write_text(json.dumps({"study_started": True}), encoding="utf-8")
            (logs_dir / "chat_log.jsonl").write_text("", encoding="utf-8")
            (edits_dir / "snippet_01.py").write_text("", encoding="utf-8")
            (baseline_dir / "snippet_01.py").write_text("print('baseline')\n", encoding="utf-8")
            (logs_dir / "snippet_log.csv").write_text(
                "\n".join(
                    [
                        "snippet_id,tool,model,turns,applied_turns,strategy_primary,strategy_other_text,confidence_1to5,first_prompt,final_prompt,notes",
                        "S01,Ollama,qwen3.6:27b,2,1,other,,3,,,",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            store = participant_web_app.StudyStore(kit_root)
            summary = {
                "applied_turns": "1",
                "strategy_primary": "other",
                "strategy_other_text": "",
                "confidence_1to5": "3",
                "notes": "",
            }

            with self.assertRaisesRegex(ValueError, "Describe the primary strategy"):
                store.save_snippet_and_summary("S01", "print('draft')\n", summary, validate_summary=True)

            summary["strategy_other_text"] = "manual audit"
            with self.assertRaisesRegex(ValueError, "Final Submitted Code cannot be blank"):
                store.save_snippet_and_summary("S01", "", summary, validate_summary=True)

    def test_stream_chat_assembles_full_reply(self) -> None:
        class FakeSocket:
            def settimeout(self, _value: float) -> None:
                return None

        class FakeRaw:
            _sock = FakeSocket()

        class FakeFP:
            raw = FakeRaw()

        class FakeResponse:
            def __init__(self) -> None:
                self.status = 200
                self.fp = FakeFP()
                self._lines = [
                    b'{"message":{"content":"Hello"},"done":false}\n',
                    b'{"message":{"content":" world"},"done":true,"done_reason":"stop"}\n',
                ]

            def readline(self) -> bytes:
                if not self._lines:
                    return b""
                return self._lines.pop(0)

            def read(self) -> bytes:
                return b""

        handler = self._make_handler()
        with patch.object(participant_web_app, "urlopen", return_value=FakeResponse()):
            resp = handler._ollama_stream_chat(  # type: ignore[attr-defined]
                "/api/chat",
                {"model": "qwen3.6:27b", "messages": [], "stream": True},
                request_id="req-1",
            )

        self.assertEqual(resp["message"]["content"], "Hello world")
        self.assertEqual(resp["done_reason"], "stop")

    def test_stream_chat_raises_when_cancelled(self) -> None:
        class FakeSocket:
            def settimeout(self, _value: float) -> None:
                return None

        class FakeRaw:
            _sock = FakeSocket()

        class FakeFP:
            raw = FakeRaw()

        class FakeResponse:
            def __init__(self) -> None:
                self.status = 200
                self.fp = FakeFP()
                self._lines = [
                    b'{"message":{"content":"Partial"},"done":false}\n',
                ]

            def readline(self) -> bytes:
                if not self._lines:
                    return b""
                return self._lines.pop(0)

            def read(self) -> bytes:
                return b""

        handler = self._make_handler()
        with patch.object(participant_web_app, "urlopen", return_value=FakeResponse()):
            with patch.object(
                participant_web_app.AppHandler,
                "consume_chat_request_cancelled",
                side_effect=[False, True],
            ):
                with self.assertRaises(participant_web_app.OllamaChatCancelled):
                    handler._ollama_stream_chat(  # type: ignore[attr-defined]
                        "/api/chat",
                        {"model": "qwen3.6:27b", "messages": [], "stream": True},
                        request_id="req-2",
                    )


if __name__ == "__main__":
    unittest.main()
