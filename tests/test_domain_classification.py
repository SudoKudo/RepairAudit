from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Protocol, cast
from unittest.mock import patch

import pandas as pd


class _ClassifyModule(Protocol):
    SYSTEM_PROMPT: str

    def _build_user_message(
        self,
        *,
        project: str,
        file_path: str,
        language: str,
        vulnerability_type: str,
        cwe_primary: str,
        code_sample: str,
    ) -> str: ...

    def _extract_first_json_object(self, text: str) -> dict[str, Any] | None: ...

    def _validate_classification(
        self,
        data: Any,
    ) -> tuple[dict[str, Any] | None, str]: ...

    def _build_output_row(
        self,
        input_row: dict[str, Any],
        classification: dict[str, Any],
        model: str,
    ) -> dict[str, Any]: ...

    def _default_output_csv(self, input_csv: Path) -> Path: ...

    def _call_ollama(
        self,
        model: str,
        messages: list[dict[str, str]],
        timeout: int = 300,
        stream: bool = True,
    ) -> str: ...

    def main(
        self,
        model: str,
        resume: bool,
        stream: bool,
        input_csv: Path,
        output_csv: Path,
        chunk_size: int,
    ) -> None: ...


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "domain_classification"
    / "classify.py"
)
SPEC = importlib.util.spec_from_file_location("domain_classify", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load module from {MODULE_PATH}")
domain_classify = cast(_ClassifyModule, importlib.util.module_from_spec(SPEC))
SPEC.loader.exec_module(domain_classify)


class ExpertiseClassificationTests(unittest.TestCase):
    def test_build_user_message_escapes_quotes_and_backslashes(self) -> None:
        payload_text = domain_classify._build_user_message(
            project='repo "alpha"',
            file_path=r"src\api\handler.py",
            language="Python",
            vulnerability_type="Injection",
            cwe_primary="CWE-89",
            code_sample='print("hello")\npath = r"C:\\temp"',
        )

        payload = json.loads(payload_text)
        self.assertEqual(payload["project"], 'repo "alpha"')
        self.assertEqual(payload["file_path"], r"src\api\handler.py")
        self.assertEqual(payload["vulnerability_type"], "Injection")
        self.assertEqual(payload["cwe_primary"], "CWE-89")
        self.assertIn('print("hello")', payload["code_sample"])
        self.assertIn(r"C:\temp", payload["code_sample"])

    def test_extract_first_json_object_handles_fenced_json(self) -> None:
        text = (
            "```json\n"
            '{"primary_expertise_area":"Backend / API Development",'
            '"secondary_expertise_areas":["Database / Persistence"]}\n'
            "```"
        )
        obj = domain_classify._extract_first_json_object(text)

        self.assertIsInstance(obj, dict)
        self.assertEqual(obj["primary_expertise_area"], "Backend / API Development")

    def test_validate_classification_canonicalizes_and_deduplicates(self) -> None:
        normalized, reason = domain_classify._validate_classification(
            {
                "primary_expertise_area": "backend / api development",
                "secondary_expertise_areas": [
                    "Backend / API Development",
                    "database / persistence",
                    "Database / Persistence",
                ],
            }
        )

        self.assertEqual(reason, "")
        self.assertEqual(
            normalized,
            {
                "primary_expertise_area": "Backend / API Development",
                "secondary_expertise_areas": ["Database / Persistence"],
            },
        )

    def test_validate_classification_drops_unknown_secondary_label(self) -> None:
        normalized, reason = domain_classify._validate_classification(
            {
                "primary_expertise_area": "Backend / API Development",
                "secondary_expertise_areas": ["Observability"],
            }
        )

        self.assertEqual(reason, "")
        self.assertEqual(
            normalized,
            {
                "primary_expertise_area": "Backend / API Development",
                "secondary_expertise_areas": [],
            },
        )

    def test_build_output_row_preserves_original_columns(self) -> None:
        input_row = {
            "language": "Python",
            "sample_id": "S123",
            "source_project": "repo",
            "code_sample": "print('x')",
            "primary_job": "old",
            "jobs": "[]",
        }
        output_row = domain_classify._build_output_row(
            input_row,
            {
                "primary_expertise_area": "Backend / API Development",
                "secondary_expertise_areas": ["Database / Persistence"],
            },
            model="qwen",
        )

        self.assertEqual(output_row["sample_id"], "S123")
        self.assertEqual(output_row["primary_job"], "old")
        self.assertEqual(output_row["jobs"], "[]")
        self.assertEqual(output_row["primary_expertise_area"], "Backend / API Development")
        self.assertEqual(
            json.loads(output_row["secondary_expertise_areas"]),
            ["Database / Persistence"],
        )
        self.assertEqual(output_row["expertise_classifier_model"], "qwen")

    def test_main_appends_expertise_columns_to_output_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_csv = tmp_path / "input_dataset.csv"
            output_csv = tmp_path / "classified_dataset.csv"

            pd.DataFrame(
                [
                    {
                        "language": "Python",
                        "sample_id": "S001",
                        "hardness_strict": "high",
                        "length_bucket": "medium",
                        "vulnerability_type": "Injection",
                        "cwe_primary": "CWE-89",
                        "source_project": "repo",
                        "file_path": "src/db/query.py",
                        "code_sample": "cursor.execute(query)",
                        "primary_job": "",
                        "jobs": "",
                    }
                ]
            ).to_csv(input_csv, index=False)

            with patch.object(
                domain_classify,
                "_call_ollama",
                return_value=(
                    '{"primary_expertise_area":"Backend / API Development",'
                    '"secondary_expertise_areas":["Database / Persistence"]}'
                ),
            ):
                domain_classify.main(
                    model="mock-model",
                    resume=False,
                    stream=False,
                    input_csv=input_csv,
                    output_csv=output_csv,
                    chunk_size=10,
                )

            result = pd.read_csv(output_csv)
            self.assertEqual(result.loc[0, "sample_id"], "S001")
            self.assertEqual(result.loc[0, "primary_expertise_area"], "Backend / API Development")
            secondary = result.loc[0, "secondary_expertise_areas"]
            self.assertIsInstance(secondary, str)
            self.assertEqual(
                json.loads(cast(str, secondary)),
                ["Database / Persistence"],
            )
            self.assertEqual(result.loc[0, "expertise_classifier_model"], "mock-model")
            self.assertIn("primary_job", result.columns)
            self.assertIn("jobs", result.columns)

    def test_default_output_csv_uses_classified_sibling_for_raw_dataset_tree(self) -> None:
        raw_csv = Path("C:/repo/data/datasets/raw/example_source.csv")
        expected = Path("C:/repo/data/datasets/classified/example.csv")

        self.assertEqual(domain_classify._default_output_csv(raw_csv), expected)

    def test_system_prompt_declares_exact_output_schema(self) -> None:
        prompt = domain_classify.SYSTEM_PROMPT

        self.assertIn('"primary_expertise_area"', prompt)
        self.assertIn('"secondary_expertise_areas"', prompt)
        self.assertIn("technical expertise areas", prompt)
        self.assertIn("Do not assign Security / Application Security only because", prompt)


if __name__ == "__main__":
    unittest.main()
