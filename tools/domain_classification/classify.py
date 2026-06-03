"""Append software expertise areas to a CWE sample CSV with an Ollama model.

For each row in the input dataset, this script asks a local Ollama model for:
- one primary expertise area
- zero or more secondary expertise areas

The expertise taxonomy is loaded from ``expertise_areas.jsonl`` in the same
directory so the prompt text and output validation stay aligned. The output CSV
preserves every input column and appends the classifier results.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from json import JSONDecodeError, JSONDecoder
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

# Some code_sample fields exceed the default 131 072-byte CSV field limit.
# sys.maxsize overflows on 32-bit C longs (Windows), so fall back to 2 GiB.
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

MAX_ATTEMPTS = 3
MAX_SECONDARY_EXPERTISE_AREAS = 10
OLLAMA_URL = "http://localhost:11434/api/chat"
EXPERTISE_AREAS_PATH = Path(__file__).with_name("expertise_areas.jsonl")

logger = logging.getLogger(__name__)


def _load_expertise_catalog(path: Path) -> list[dict[str, str]]:
    """Load the expertise taxonomy from a JSONL file."""
    catalog: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path} at line {line_number}") from exc

            label = str(record.get("label", "") or "").strip()
            description = str(record.get("description", "") or "").strip()
            if not label or not description:
                raise ValueError(f"Missing label/description in {path} at line {line_number}")
            catalog.append({"label": label, "description": description})

    if not catalog:
        raise ValueError(f"No expertise taxonomy rows found in {path}")
    return catalog


EXPERTISE_CATALOG = _load_expertise_catalog(EXPERTISE_AREAS_PATH)
# The model does not always match label casing exactly, so normalize once and
# keep the canonical label text here.
EXPERTISE_LABEL_LOOKUP = {
    row["label"].casefold(): row["label"] for row in EXPERTISE_CATALOG
}


def _build_system_prompt() -> str:
    """Build the classifier prompt from the loaded expertise taxonomy."""
    expertise_lines = [
        f"{idx}. {row['label']} - {row['description']}"
        for idx, row in enumerate(EXPERTISE_CATALOG, start=1)
    ]
    return "\n".join(
        [
            "You are a software expertise classifier.",
            "Assign the software expertise areas most relevant to writing, maintaining, or securely repairing one code sample.",
            "These labels describe technical expertise areas, not business industry, end-user industry, or job seniority.",
            "",
            "Predefined Expertise Areas:",
            *expertise_lines,
            "",
            "Instructions:",
            "- Return only one valid JSON object and nothing else.",
            "- Use this exact schema:",
            '{"primary_expertise_area":"<one predefined label>","secondary_expertise_areas":["<secondary label>", "..."]}',
            "- primary_expertise_area must exactly match one predefined label from the list above.",
            "- secondary_expertise_areas must be a JSON array of unique predefined labels, with at most 10 items.",
            "- Every value inside secondary_expertise_areas must exactly match one predefined label from the list above.",
            "- Do not repeat the primary expertise area inside secondary_expertise_areas.",
            "- Pick the primary expertise area that best matches the code likely to be owned, reviewed, or fixed by a practitioner in that area.",
            "- Add secondary expertise areas only when the sample clearly spans those areas.",
            "- The code sample is the strongest direct signal. The project name, file path, language, and CWE are supporting context.",
            "- Do not assign Security / Application Security only because the code contains a vulnerability. Use it when the implementation itself is security-specific or AppSec expertise is clearly central to the code.",
            "- Use General Software Utility only when the project, path, language, and sample do not support a more specific expertise area.",
        ]
    )


SYSTEM_PROMPT = _build_system_prompt()


def _clean_text(value: Any) -> str:
    """Normalize nullable scalar values into trimmed text."""
    if value is None:
        return ""
    if pd.isna(value):
        return ""
    return str(value).strip()


def _row_identifier(row: dict[str, Any]) -> str:
    """Return the stable row identifier used for resume and logging."""
    for key in ("sample_id", "row_uid", "source_row_id"):
        value = _clean_text(row.get(key, ""))
        if value:
            return value
    return ""


def _configure_logging(log_path: Path) -> logging.Logger:
    """Configure console and file logging for one classification run."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, mode="a", encoding="utf-8"),
        ],
    )
    error_logger = logging.getLogger("row_errors")
    error_logger.setLevel(logging.ERROR)
    error_handler = logging.FileHandler(
        log_path.with_name("errors.log"), mode="a", encoding="utf-8"
    )
    error_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    error_logger.addHandler(error_handler)
    return error_logger


def _build_user_message(
    *,
    project: str,
    file_path: str,
    language: str,
    vulnerability_type: str,
    cwe_primary: str,
    code_sample: str,
) -> str:
    """Serialize one classification request payload as valid JSON text."""
    payload = {
        "project": _clean_text(project),
        "file_path": _clean_text(file_path),
        "language": _clean_text(language),
        "vulnerability_type": _clean_text(vulnerability_type),
        "cwe_primary": _clean_text(cwe_primary),
        "code_sample": _clean_text(code_sample),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _call_ollama(
    model: str,
    messages: list[dict[str, str]],
    timeout: int = 300,
    stream: bool = True,
) -> str:
    """Send one chat request to Ollama and return the combined response text."""
    payload = json.dumps(
        {"model": model, "messages": messages, "stream": stream},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    if stream:
        chunks: list[str] = []
        print()
        try:
            with urlopen(request, timeout=timeout) as response:
                # Ollama streams one JSON object per line. Read the stream as it
                # arrives so long runs still show progress in the terminal.
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    token = str(chunk.get("message", {}).get("content", "") or "")
                    print(token, end="", flush=True)
                    chunks.append(token)
                    if chunk.get("done"):
                        break
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc
        print()
        return "".join(chunks)

    try:
        with urlopen(request, timeout=timeout) as response:
            payload_text = response.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc

    response_payload = json.loads(payload_text)
    return str(response_payload.get("message", {}).get("content", "") or "")


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first decodable JSON object from model output."""
    candidates: list[str] = []
    # Most bad responses still contain one usable JSON object. Check the common
    # fenced-code path first, then fall back to scanning the raw text.
    fenced_start = text.find("```")
    if fenced_start >= 0:
        fenced_end = text.find("```", fenced_start + 3)
        if fenced_end > fenced_start:
            body = text[fenced_start + 3 : fenced_end].strip()
            if body.lower().startswith("json"):
                body = body[4:].strip()
            candidates.append(body)
    candidates.append(text.strip())

    decoder = JSONDecoder()
    for candidate in candidates:
        for idx, ch in enumerate(candidate):
            if ch != "{":
                continue
            try:
                obj, _end = decoder.raw_decode(candidate[idx:])
            except JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _normalize_known_expertise(value: Any) -> str | None:
    """Return a canonical predefined label when the text matches one."""
    text = " ".join(_clean_text(value).split())
    if not text:
        return None
    return EXPERTISE_LABEL_LOOKUP.get(text.casefold())


def _validate_classification(data: Any) -> tuple[dict[str, Any] | None, str]:
    """Validate and normalize one parsed model response."""
    if not isinstance(data, dict):
        return None, "top-level JSON value is not an object"

    primary_expertise_area = _normalize_known_expertise(data.get("primary_expertise_area"))
    if primary_expertise_area is None:
        return None, "primary_expertise_area is missing or not in the predefined taxonomy"

    secondary_raw = data.get("secondary_expertise_areas")
    if secondary_raw is None:
        secondary_items: list[Any] = []
    elif isinstance(secondary_raw, list):
        secondary_items = secondary_raw
    else:
        return None, "secondary_expertise_areas is not a JSON array"

    cleaned_secondary: list[str] = []
    seen: set[str] = set()
    for item in secondary_items:
        normalized = _normalize_known_expertise(item)
        if normalized is None:
            # Keep the taxonomy closed, but do not throw away a valid primary
            # label just because the model padded the secondary list with a bad
            # extra.
            continue
        if normalized == primary_expertise_area:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned_secondary.append(normalized)

    if len(cleaned_secondary) > MAX_SECONDARY_EXPERTISE_AREAS:
        return None, f"secondary_expertise_areas exceeds {MAX_SECONDARY_EXPERTISE_AREAS} items"

    return {
        "primary_expertise_area": primary_expertise_area,
        "secondary_expertise_areas": cleaned_secondary,
    }, ""


def _read_completed_ids(output_csv: Path) -> set[str]:
    """Return already-classified sample IDs from an existing output file."""
    if not output_csv.exists():
        return set()
    try:
        with output_csv.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return set()
            return {v for row in reader if (v := _row_identifier(row))}
    except Exception:
        return set()


def _build_output_row(
    input_row: dict[str, Any],
    classification: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    """Preserve the input row and append classifier outputs."""
    output_row = dict(input_row)
    output_row["primary_expertise_area"] = classification["primary_expertise_area"]
    output_row["secondary_expertise_areas"] = json.dumps(
        classification["secondary_expertise_areas"],
        ensure_ascii=False,
    )
    output_row["expertise_classifier_model"] = model
    return output_row


def _string_keyed_row(row: dict[Any, Any]) -> dict[str, Any]:
    """Normalize record keys into strings for stable CSV output typing."""
    return {str(key): value for key, value in row.items()}


def _append_skipped_row(skipped_csv: Path, row: dict[str, Any], reason: str) -> None:
    """Append one raw input row to the skipped-rows CSV with an error reason."""
    out_row = dict(row)
    out_row["error"] = reason
    # Ensure keys are stable strings
    out_row = _string_keyed_row(out_row)

    write_header = not skipped_csv.exists()
    skipped_csv.parent.mkdir(parents=True, exist_ok=True)
    with skipped_csv.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out_row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow({k: ("" if v is None else v) for k, v in out_row.items()})


def _iter_csv_chunks(path: Path, chunk_size: int):
    """Yield lists of row dicts from a CSV without loading the file into memory."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        chunk: list[dict[str, Any]] = []
        for row in reader:
            chunk.append(dict(row))
            if len(chunk) == chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk


def _default_output_csv(input_csv: Path) -> Path:
    """Choose a default classified output path based on the input location."""
    resolved_input = input_csv.resolve()
    if (
        resolved_input.parent.name == "raw"
        and resolved_input.parent.parent.name == "datasets"
    ):
        output_dir = resolved_input.parent.parent / "classified"
        output_name = resolved_input.name
        if output_name.endswith("_source.csv"):
            output_name = output_name.replace("_source.csv", ".csv")
        return output_dir / output_name

    return resolved_input.with_name(f"{resolved_input.stem}_classified.csv")


def main(model: str, resume: bool, stream: bool, input_csv: Path, output_csv: Path, chunk_size: int, dry_run: bool = False, error_logger: logging.Logger | None = None) -> None:
    """Run expertise classification for one input CSV."""
    skipped_csv = output_csv.with_name(f"{output_csv.stem}_skipped.csv")
    with input_csv.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        next(reader)  # skip header
        total_rows = sum(1 for _ in reader)
    logger.info("Processing %s rows from %s - model: %s", total_rows, input_csv, model)

    completed: set[str] = set()
    if resume and output_csv.exists():
        completed = _read_completed_ids(output_csv)
        logger.info("Resuming - %s already completed", len(completed))

    if not resume and output_csv.exists():
        output_csv.unlink()

    classified_rows = 0
    discarded_rows = 0
    skipped_rows = 0
    i = 0

    for chunk in _iter_csv_chunks(input_csv, chunk_size):
        for raw_row in chunk:
            i += 1
            row = _string_keyed_row(raw_row)
            row_id = _row_identifier(row)
            if resume and row_id in completed:
                continue

            try:
                language = _clean_text(row.get("language", ""))
                cwe_primary = _clean_text(row.get("cwe_primary", ""))
                vulnerability_type = _clean_text(row.get("vulnerability_type", ""))
                source_project = _clean_text(row.get("source_project", ""))
                file_path = _clean_text(row.get("file_path", ""))
                code_sample = _clean_text(row.get("code_sample", ""))

                logger.info("[%s/%s] %s (%s, %s)", i, total_rows, row_id or "<no-id>", language, cwe_primary)

                user_content = _build_user_message(
                    project=source_project,
                    file_path=file_path,
                    language=language,
                    vulnerability_type=vulnerability_type,
                    cwe_primary=cwe_primary,
                    code_sample=code_sample,
                )
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ]

                if dry_run:
                    classification = {"primary_expertise_area": EXPERTISE_CATALOG[0]["label"], "secondary_expertise_areas": []}
                    classified_rows += 1
                else:
                    classification = None
                    skip_this_sample = False
                    for attempt in range(1, MAX_ATTEMPTS + 1):
                        try:
                            response_text = _call_ollama(model, messages, stream=stream)
                        except KeyboardInterrupt:
                            # Treat an interrupt during a sample as a skip for that sample.
                            reason = "interrupted (KeyboardInterrupt)"
                            logger.warning("[%s] attempt %s: %s - skipping sample", row_id or "<no-id>", attempt, reason)
                            _append_skipped_row(skipped_csv, row, reason)
                            skipped_rows += 1
                            skip_this_sample = True
                            break
                        except Exception as exc:
                            logger.warning("[%s] attempt %s: Ollama request failed: %s", row_id or "<no-id>", attempt, exc)
                            continue

                        extracted = _extract_first_json_object(response_text)
                        if extracted is None:
                            logger.warning("[%s] attempt %s: could not extract a JSON object", row_id or "<no-id>", attempt)
                            continue

                        classification, reason = _validate_classification(extracted)
                        if classification is not None:
                            break

                        logger.warning("[%s] attempt %s: invalid classification payload: %s", row_id or "<no-id>", attempt, reason)

                    if skip_this_sample:
                        # move on to next row without writing an output classification
                        continue

                    if classification is None:
                        reason = f"discarded after {MAX_ATTEMPTS} attempts"
                        logger.warning("[%s] %s", row_id or "<no-id>", reason)
                        _append_skipped_row(skipped_csv, row, reason)
                        skipped_rows += 1
                        continue
                    else:
                        classified_rows += 1

                result = pd.DataFrame([_build_output_row(row, classification, model)])
                result.to_csv(output_csv, mode="a", header=not output_csv.exists(), index=False)

            except Exception as exc:
                skipped_rows += 1
                reason = f"error: {exc}"
                logger.warning("[%s/%s] skipped row %s due to error: %s", i, total_rows, row_id or "<no-id>", exc)
                _append_skipped_row(skipped_csv, row, reason)
                if error_logger:
                    error_logger.error("row=%s row_id=%s error=%s", i, row_id or "<no-id>", exc)

    logger.info(
        "Done. classified=%s discarded=%s skipped=%s total=%s",
        classified_rows,
        discarded_rows,
        skipped_rows,
        total_rows,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Append software expertise areas to a sample CSV with an Ollama model"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to the input CSV file.",
    )
    parser.add_argument(
        "--output",
        help="Optional output CSV path. Defaults to a classified sibling path derived from the input.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Ollama model name (for example, qwen2.5-coder:14b)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Ollama calls and write placeholder classifications — use to verify chunking without a live model",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable token streaming (silent until response is complete)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="Number of rows to read from the input CSV at a time (default: 500)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fresh",
        action="store_true",
        help="Overwrite the output CSV and start from scratch",
    )

    args = parser.parse_args()

    input_csv = Path(args.input).resolve()
    if args.output:
        output_csv = Path(args.output).resolve()
    else:
        output_csv = _default_output_csv(input_csv)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    error_logger = _configure_logging(output_csv.parent / "classify.log")
    logger.info("Input:  %s", input_csv)
    logger.info("Output: %s", output_csv)

    main(
        model=args.model,
        resume=not args.fresh,
        stream=not args.no_stream,
        input_csv=input_csv,
        output_csv=output_csv,
        chunk_size=args.chunk_size,
        dry_run=args.dry_run,
        error_logger=error_logger,
    )
