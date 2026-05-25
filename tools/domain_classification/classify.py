"""
classify.py

Domain classification for repair_audit samples.

For each row in the input CSV, prompts an Ollama model to assign a primary
domain and secondary domains based on project name, file path, language,
and function body.

The output CSV and log file are written to the same directory as the input file.

Usage:
    python classify.py --input /path/to/draft(in).csv --model <ollama-model-name> [options]
"""

import argparse
import json
import logging
import re  # used by _extract_classification
from pathlib import Path

import pandas as pd
import requests

MAX_ATTEMPTS = 3
OLLAMA_URL = "http://localhost:11434/api/chat"

SYSTEM_PROMPT = """\
You are a code domain classifier. Your task is to assign a primary domain label and a list of secondary domains to a given code function based on the project it belongs to, its file path, programming language, and function body.

Predefined Domain Labels:
1. Web — Functions implementing server-side or client-side web logic, including HTTP request/response handling, REST or GraphQL APIs, web frameworks, session management, template rendering, and browser-based interactions.
2. Mobile — Functions written for Android or iOS applications, mobile SDKs, or cross-platform mobile frameworks, including UI rendering, device sensor access, push notifications, and mobile-specific lifecycle management.
3. Machine Learning / AI — Functions implementing data preprocessing, model training, evaluation, inference, or ML pipeline orchestration, including work with ML frameworks, datasets, and statistical computation.
4. Security / Cryptography — Functions implementing cryptographic algorithms, security protocols, authentication, authorization, vulnerability detection, or secure communication, including work with cryptographic libraries and security standards.
5. Embedded / IoT — Functions written for microcontrollers, embedded systems, or IoT devices, including hardware interfacing, interrupt handling, real-time scheduling, sensor/actuator control, and low-level communication protocols.
6. Infrastructure / DevOps / Cloud — Functions managing deployment pipelines, containerization, cloud resource provisioning, monitoring, logging, load balancing, or service orchestration, including work with cloud SDKs and DevOps tooling.
7. Desktop / Enterprise — Functions implementing desktop application logic, enterprise software workflows, GUI components, inter-process communication, or enterprise integration patterns, typically targeting Windows, macOS, or Linux desktop environments.
8. Game / Graphics — Functions implementing game logic, rendering pipelines, physics simulation, shader programming, asset management, or graphical computation, including work with game engines and graphics APIs.
9. Blockchain — Functions implementing smart contract logic, consensus mechanisms, transaction validation, wallet management, token operations, or peer-to-peer blockchain networking, including work with blockchain frameworks and Web3 libraries.
10. General Utility — Functions performing domain-agnostic operations such as string manipulation, memory management, data structure operations, arithmetic, error handling, logging, or configuration parsing, with no domain-specific logic that would indicate a more specific domain.

Instructions:
- Assign exactly one primary domain from the predefined list above.
- Assign one or more secondary domains reflecting any other domains you associate with the function. Secondary domains should prefer labels from the predefined list when they fit. If no predefined label fits, use a concise free-form string. Secondary domains must not exceed 10.
- Base your primary domain decision primarily on the project name and file path. Use the function body as a secondary signal to confirm or override.
- If the function body contains no domain-specific logic despite a domain-specific project, assign General Utility as the primary domain.
- Return only a valid JSON object. No explanation, no extra text.\
"""

USER_TEMPLATE = """\
{
  "project": "{project}",
  "file_path": "{file_path}",
  "language": "{language}",
  "function": "{function}"
}\
"""

logger = logging.getLogger(__name__)


def _configure_logging(log_path: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, mode="a"),
        ],
    )


def _build_user_message(project: str, file_path: str, language: str, function: str) -> str:
    return (
        USER_TEMPLATE
        .replace("{project}", project)
        .replace("{file_path}", file_path)
        .replace("{language}", language)
        .replace("{function}", function)
    )


def _call_ollama(model: str, messages: list[dict], timeout: int = 300, stream: bool = True) -> str:
    if stream:
        response = requests.post(
            OLLAMA_URL,
            json={"model": model, "messages": messages, "stream": True},
            timeout=timeout,
            stream=True,
        )
        response.raise_for_status()
        chunks = []
        print()
        for line in response.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            token = chunk.get("message", {}).get("content", "")
            print(token, end="", flush=True)
            chunks.append(token)
            if chunk.get("done"):
                break
        print()
        return "".join(chunks)
    else:
        response = requests.post(
            OLLAMA_URL,
            json={"model": model, "messages": messages, "stream": False},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()["message"]["content"]


def _extract_classification(text: str) -> dict | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else None

    if raw is None:
        bare = re.search(r"\{.*\}", text, re.DOTALL)
        if bare is None:
            return None
        raw = bare.group(0)

    try:
        data = json.loads(raw)
        if "primary_domain" in data and "secondary_domains" in data:
            return data
    except json.JSONDecodeError:
        pass
    return None


def main(model: str, resume: bool, stream: bool, input_csv: Path, output_csv: Path) -> None:
    df = pd.read_csv(input_csv)
    logger.info(f"Loaded {len(df)} rows from {input_csv} — model: {model}")

    completed: set[str] = set()
    if resume and output_csv.exists():
        completed = set(pd.read_csv(output_csv)["sample_id"].astype(str))
        logger.info(f"Resuming — {len(completed)} already completed")

    if not resume and output_csv.exists():
        output_csv.unlink()

    for i, row in enumerate(df.itertuples(index=False), 1):
        sample_id = str(row.sample_id)

        if resume and sample_id in completed:
            continue

        logger.info(f"[{i}/{len(df)}] {sample_id} ({row.language}, {row.cwe_primary})")

        user_content = _build_user_message(
            project=str(row.source_project),
            file_path=str(row.file_path),
            language=str(row.language),
            function=str(row.code_sample),
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        classification = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response_text = _call_ollama(model, messages, stream=stream)
            except Exception as e:
                logger.warning(f"[{sample_id}] attempt {attempt}: Ollama request failed: {e}")
                continue

            classification = _extract_classification(response_text)
            if classification:
                break

            logger.warning(f"[{sample_id}] attempt {attempt}: could not parse JSON from response")

        if classification is None:
            logger.warning(f"[{sample_id}] discarded after {MAX_ATTEMPTS} attempts")
            classification = {"primary_domain": None, "secondary_domains": []}

        result = pd.DataFrame([{
            "sample_id": sample_id,
            "language": row.language,
            "cwe_primary": row.cwe_primary,
            "source_project": row.source_project,
            "file_path": row.file_path,
            "primary_domain": classification["primary_domain"],
            "secondary_domains": json.dumps(classification["secondary_domains"]),
        }])

        result.to_csv(output_csv, mode="a", header=not output_csv.exists(), index=False)

    logger.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Classify repair_audit samples by domain using an Ollama model"
    )
    parser.add_argument(
        "--input", required=True,
        help="Path to the input CSV file (e.g. /data/draft(in).csv)",
    )
    parser.add_argument(
        "--model", required=True,
        help="Ollama model name (e.g. qwen2.5-coder:14b)",
    )
    parser.add_argument(
        "--no-stream", action="store_true",
        help="Disable token streaming (silent until response is complete)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--resume", action="store_true", default=True,
        help="Skip already-classified rows (default)",
    )
    mode.add_argument(
        "--fresh", action="store_true",
        help="Overwrite output and start from scratch",
    )

    args = parser.parse_args()

    input_csv = Path(args.input).resolve()
    output_csv = input_csv.parent / "draft(out).csv"

    _configure_logging(input_csv.parent / "classify.log")
    logger.info(f"Input:  {input_csv}")
    logger.info(f"Output: {output_csv}")

    main(
        model=args.model,
        resume=not args.fresh,
        stream=not args.no_stream,
        input_csv=input_csv,
        output_csv=output_csv,
    )
