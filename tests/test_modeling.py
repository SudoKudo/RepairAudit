from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import pandas as pd


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "analysis"
    / "modeling.py"
)
SPEC = importlib.util.spec_from_file_location("modeling_module", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load module from {MODULE_PATH}")
modeling = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(modeling)


class ModelingTests(unittest.TestCase):
    def test_language_experience_balance_reports_contingency_check(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "participant_id": "P001",
                    "language": "python",
                    "participant_language_experience": "advanced",
                },
                {
                    "participant_id": "P001",
                    "language": "java",
                    "participant_language_experience": "advanced",
                },
                {
                    "participant_id": "P002",
                    "language": "python",
                    "participant_language_experience": "basic",
                },
                {
                    "participant_id": "P002",
                    "language": "java",
                    "participant_language_experience": "basic",
                },
            ]
        )

        balance = modeling.language_experience_balance(df)

        self.assertEqual(balance["status"], "ok")
        self.assertEqual(balance["n_assignments"], 4)
        self.assertIn("python", balance["table"])
        self.assertIn("advanced", balance["table"]["python"])


if __name__ == "__main__":
    unittest.main()
