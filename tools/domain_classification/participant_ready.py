"""Build and validate the participant-ready dataset used for kit sampling."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CLASSIFIED_INPUT_PATH = Path("data") / "datasets" / "classified" / "dataset_classified.csv"
DEFAULT_PARTICIPANT_READY_PATH = (
    Path("data") / "datasets" / "classified" / "dataset_participant_ready.csv"
)
DEFAULT_REJECTED_PATH = (
    Path("data") / "datasets" / "classified" / "dataset_participant_rejected.csv"
)
UNKNOWN_CWE_VALUES = {"", "NVD-CWE-NOINFO"}
UNKNOWN_VULNERABILITY_TYPES = {"", "Unknown/Unspecified"}


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


def _clean_text(value: Any) -> str:
    """Normalize nullable values into stripped text."""
    return str(value or "").strip()


def _is_vulnerable_flag(value: Any) -> bool:
    """Treat common true-like dataset values as vulnerable flags."""
    return _clean_text(value).casefold() in {"1", "true", "yes", "y"}


def _decode_escaped_newlines(text: str) -> str:
    """Expand literal newline and tab escapes found in flattened code cells."""
    if "\n" in text or "\\n" not in text:
        return text
    return (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\t", "    ")
    )


def _normalized_code_sample(text: str) -> str:
    """Return code text in a form suitable for lightweight readiness checks."""
    return _decode_escaped_newlines(_clean_text(text)).replace("\r\n", "\n").replace("\r", "\n")


def code_sample_quality_issues(code_sample: Any, language: Any) -> list[str]:
    """Return obvious code-shape problems that make a row poor participant material."""
    normalized = _normalized_code_sample(_clean_text(code_sample))
    if not normalized:
        return ["missing_code_sample"]

    issues: list[str] = []
    if "\n" not in normalized and "//" in normalized:
        issues.append("flattened_line_comment")

    if len(normalized) > 12000:
        issues.append("oversized_code_sample")

    if not _clean_text(language):
        issues.append("missing_language")
    return issues


def participant_ready_issues(row: Mapping[str, Any]) -> list[str]:
    """Return the reasons one dataset row should not be handed to participants."""
    issues: list[str] = []

    if not _is_vulnerable_flag(row.get("is_vulnerable", "")):
        issues.append("not_marked_vulnerable")

    cwe_primary = _clean_text(row.get("cwe_primary", "")).upper()
    if cwe_primary in UNKNOWN_CWE_VALUES:
        issues.append("missing_specific_cwe")

    vulnerability_type = _clean_text(row.get("vulnerability_type", ""))
    if vulnerability_type in UNKNOWN_VULNERABILITY_TYPES:
        issues.append("missing_specific_vulnerability_type")

    issues.extend(
        issue
        for issue in code_sample_quality_issues(
            row.get("code_sample", ""),
            row.get("language", ""),
        )
        if issue not in issues
    )
    return issues


def is_participant_ready_row(row: Mapping[str, Any]) -> bool:
    """Return True when a dataset row passes the participant-facing filter."""
    return not participant_ready_issues(row)


def _annotated_row(row: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Preserve the input row and append readiness fields."""
    annotated = {str(key): _clean_text(value) for key, value in row.items()}
    issues = participant_ready_issues(annotated)
    annotated["participant_ready"] = "1" if not issues else "0"
    annotated["participant_ready_reasons"] = json.dumps(issues, ensure_ascii=False)
    return annotated, issues


def build_participant_ready_dataset(
    *,
    input_csv: Path,
    output_csv: Path,
    rejected_output_csv: Path | None = None,
) -> dict[str, Any]:
    """Write a participant-ready CSV and, optionally, a rejected-rows audit CSV."""
    if not input_csv.exists():
        raise FileNotFoundError(f"Input dataset not found: {input_csv}")

    total_rows = 0
    with input_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise ValueError(f"Dataset CSV has no header row: {input_csv}")

        for extra in ("participant_ready", "participant_ready_reasons"):
            if extra not in fieldnames:
                fieldnames.append(extra)

        output_csv.parent.mkdir(parents=True, exist_ok=True)
        if rejected_output_csv is not None:
            rejected_output_csv.parent.mkdir(parents=True, exist_ok=True)

        ready_rows = 0
        rejected_rows = 0
        issue_counts: Counter[str] = Counter()

        with output_csv.open("w", newline="", encoding="utf-8") as ready_handle:
            ready_writer = csv.DictWriter(ready_handle, fieldnames=fieldnames)
            ready_writer.writeheader()

            rejected_handle = None
            rejected_writer = None
            try:
                if rejected_output_csv is not None:
                    rejected_handle = rejected_output_csv.open("w", newline="", encoding="utf-8")
                    rejected_writer = csv.DictWriter(rejected_handle, fieldnames=fieldnames)
                    rejected_writer.writeheader()

                for row in reader:
                    total_rows += 1
                    annotated, issues = _annotated_row(row)
                    if issues:
                        rejected_rows += 1
                        issue_counts.update(issues)
                        if rejected_writer is not None:
                            rejected_writer.writerow(annotated)
                        continue

                    ready_rows += 1
                    ready_writer.writerow(annotated)
            finally:
                if rejected_handle is not None:
                    rejected_handle.close()

    return {
        "input_csv": str(input_csv),
        "output_csv": str(output_csv),
        "rejected_output_csv": str(rejected_output_csv) if rejected_output_csv else "",
        "total_rows": total_rows,
        "ready_rows": ready_rows,
        "rejected_rows": rejected_rows,
        "issue_counts": dict(issue_counts),
    }


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI used to prepare the participant-ready dataset."""
    parser = argparse.ArgumentParser(
        description="Filter the classified dataset down to participant-ready rows."
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_CLASSIFIED_INPUT_PATH),
        help="Classified source CSV to filter.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_PARTICIPANT_READY_PATH),
        help="Participant-ready CSV to write.",
    )
    parser.add_argument(
        "--rejected_output",
        default=str(DEFAULT_REJECTED_PATH),
        help="Rejected-row audit CSV to write.",
    )
    return parser


def main() -> None:
    """Run the participant-ready dataset builder from the command line."""
    parser = _build_parser()
    args = parser.parse_args()
    summary = build_participant_ready_dataset(
        input_csv=Path(args.input),
        output_csv=Path(args.output),
        rejected_output_csv=Path(args.rejected_output) if args.rejected_output else None,
    )

    print(f"Input rows:            {summary['total_rows']}")
    print(f"Participant-ready rows:{summary['ready_rows']}")
    print(f"Rejected rows:         {summary['rejected_rows']}")
    print(f"Output CSV:            {summary['output_csv']}")
    if summary["rejected_output_csv"]:
        print(f"Rejected CSV:          {summary['rejected_output_csv']}")
    if summary["issue_counts"]:
        print("Rejection reasons:")
        for key, value in sorted(summary["issue_counts"].items()):
            print(f"  - {key}: {value}")


if __name__ == "__main__":
    main()
