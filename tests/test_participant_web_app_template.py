from __future__ import annotations

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
