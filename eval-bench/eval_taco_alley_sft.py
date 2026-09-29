#!/usr/bin/env python3
"""Evaluate a Taco Alley SFT model served behind an OpenAI-compatible API."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import statistics
import sys
import time
from contextlib import aclosing
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit, urlunsplit

from openai import APIError, AsyncOpenAI
from tqdm import tqdm


SEED = 42
EVAL_SAMPLE_SIZE = 25
INPUT_FIELDS = ["customer_id", "date", "location", "message"]
OUTPUT_FIELDS = ["category", "sub_category", "tone_urgency"]
REQUIRED_FIELDS = [*INPUT_FIELDS, *OUTPUT_FIELDS]
TONE_ALLOWED = {"Low", "Moderate", "High", "Urgent"}
SYSTEM_PROMPT = (
    "You are a complaint structuring assistant for Taco Alley. "
    "Input is a JSON object with fields customer_id, date, location, message. "
    "Return only valid JSON with exactly these fields in the output: "
    f"{REQUIRED_FIELDS}. "
    "Keep the original input fields unchanged and add correct values for category, "
    "sub_category, and tone_urgency. Do not add any extra keys or commentary. "
    f"Allowed tone_urgency values: {json.dumps(sorted(TONE_ALLOWED))}."
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate a Taco Alley SFT model served behind an OpenAI-compatible API.")
    parser.add_argument(
        "--base-url",
        required=True,
        help="OpenAI-compatible API base URL, such as http://192.168.1.251:8080/v1.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY") or "not-needed",
        help="API key (default: OPENAI_API_KEY, or not-needed for servers without authentication).",
    )
    parser.add_argument(
        "--model",
        help="Model name to request (default: discover the first model from the API's /models endpoint).",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "datasets" / "taco_alley_customer_complaints.csv",
        help="Complaint CSV path (default: repository dataset).",
    )
    parser.add_argument("--max-new-tokens", "-n", type=int, default=220)
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high"],
        default="none",
        help="Requested reasoning effort (default: none). Support depends on the server and model.",
    )
    parser.add_argument("--request-timeout", type=int, default=300, help="Request timeout in seconds.")
    parser.add_argument("--concurrency", type=int, default=4, help="Maximum concurrent evaluation requests (default: 4).")
    parser.add_argument("--no-progress", action="store_true", help="Disable live progress and per-request status logs.")
    parser.add_argument(
        "--eval-sample-size", "--limit", dest="limit", type=int, default=EVAL_SAMPLE_SIZE,
        help="Maximum samples to evaluate (default: 25).",
    )
    parser.add_argument("--sample-seed", type=int, default=SEED, help="Seed for reproducible sampling without replacement.")
    parser.add_argument(
        "--include-full-responses",
        action="store_true",
        help="Save full raw responses and parsed predictions, which may contain personal information.",
    )
    parser.add_argument("--output", type=Path, default=Path("taco_alley_sft_eval.json"))
    return parser.parse_args()


def load_examples(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as source:
        raw_rows = list(csv.DictReader(source))
    if not raw_rows:
        raise ValueError("Dataset contains no rows.")
    missing_columns = [field for field in REQUIRED_FIELDS if field not in raw_rows[0]]
    if missing_columns:
        raise ValueError(f"Dataset is missing required columns: {missing_columns}")

    examples = []
    for record_number, row in enumerate(raw_rows, start=1):
        record = {field: str(row.get(field, "")).strip() for field in REQUIRED_FIELDS}
        if any(not record[field] for field in REQUIRED_FIELDS):
            continue
        if not re.match(r"^CUST-\d{5,}", record["customer_id"]):
            continue
        if record["tone_urgency"].lower() not in {tone.lower() for tone in TONE_ALLOWED}:
            continue
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", record["date"]):
            continue
        if record["date"] > time.strftime("%Y-%m-%d"):
            continue
        examples.append(
            {
                "sample_id": record_number,
                "input_obj": {field: record[field] for field in INPUT_FIELDS},
                "target_obj": record,
            }
        )
    if not examples:
        raise ValueError("No valid examples remain after notebook-equivalent validation.")
    return examples


def build_system_prompt(examples: list[dict[str, Any]]) -> str:
    categories = sorted({example["target_obj"]["category"] for example in examples})
    subcategories = sorted({example["target_obj"]["sub_category"] for example in examples})
    return (
        f"{SYSTEM_PROMPT} "
        f"Allowed category values: {json.dumps(categories, ensure_ascii=False)}. "
        f"Allowed sub_category values: {json.dumps(subcategories, ensure_ascii=False)}. "
        "Use exactly one of the listed values for each output field, preserving its spelling and case."
    )


def reject_json_constant(value: str) -> Any:
    raise ValueError(f"Invalid JSON constant: {value}")


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def load_strict_json(text: str) -> Any:
    return json.loads(text, parse_constant=reject_json_constant, object_pairs_hook=unique_json_object)


def parse_json_from_output(text: str) -> tuple[Any, bool]:
    cleaned = text.strip()
    try:
        return load_strict_json(cleaned), True
    except ValueError:
        pass
    if cleaned.startswith("```"):
        cleaned = "\n".join(line for line in cleaned.splitlines() if not line.strip().startswith("```"))
    try:
        return load_strict_json(cleaned), False
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("No JSON object found in model output.") from None
        return load_strict_json(cleaned[start : end + 1]), False


def validate_prediction(prediction: Any, input_obj: dict[str, str]) -> list[str]:
    if not isinstance(prediction, dict):
        return ["Output must be a JSON object."]
    errors = []
    if set(prediction) - set(REQUIRED_FIELDS):
        errors.append("Output contains extra keys.")
    for field in REQUIRED_FIELDS:
        if field not in prediction:
            errors.append(f"Missing field: {field}.")
        elif not isinstance(prediction[field], str) or not prediction[field].strip():
            errors.append(f"Field must be a nonempty string: {field}.")
    for field in INPUT_FIELDS:
        if prediction.get(field) != input_obj[field]:
            errors.append(f"Input field changed: {field}.")
    tone = prediction.get("tone_urgency")
    if not isinstance(tone, str) or tone not in TONE_ALLOWED:
        errors.append("Invalid tone_urgency.")
    return errors


def describe_api_error(
    error: Exception, client: AsyncOpenAI, arguments: argparse.Namespace,
    input_obj: dict[str, str] | None = None, route: str = "chat/completions",
) -> dict[str, Any]:
    request = getattr(error, "request", None)
    raw_url = str(request.url) if request is not None else f"{str(client.base_url).rstrip('/')}/{route}"
    url = urlsplit(raw_url)
    endpoint = urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
    sensitive = [client.api_key, url.username, url.password, url.query]
    sensitive.extend((input_obj or {}).values())

    def clean(value: Any) -> str:
        text = str(value)
        for secret in sorted({value for value in sensitive if value}, key=len, reverse=True):
            for variant in (secret, json.dumps(secret, ensure_ascii=True)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
                text = text.replace(variant, "[REDACTED]")
        text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", text)
        text = " ".join(text.split())
        text = "".join(character for character in text if character.isprintable())
        return text[:2000]

    body = getattr(error, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    details: dict[str, Any] = {
        "type": type(error).__name__,
        "status_code": getattr(error, "status_code", None),
        "endpoint": clean(endpoint),
        "method": request.method if request is not None else "POST" if route == "chat/completions" else "GET",
    }
    if isinstance(body, dict):
        message = body.get("message") or body.get("detail")
        if isinstance(message, list):
            message = "; ".join(
                f"{item.get('loc', [])}: {item.get('msg', '')}"
                for item in message if isinstance(item, dict)
            )
        details["message"] = clean(message) if isinstance(message, str) else "Server returned an error without a message."
        for field in ("type", "code", "param"):
            if isinstance(body.get(field), (str, int)):
                details[f"server_{field}"] = clean(body[field])
    else:
        details["message"] = clean(body if isinstance(body, str) else str(error))
    request_id = getattr(error, "request_id", None)
    if request_id:
        details["request_id"] = clean(request_id)
    if route == "chat/completions":
        details["request_parameters"] = {
            "model": clean(arguments.model), "reasoning_effort": arguments.reasoning_effort,
            "max_tokens": arguments.max_new_tokens, "temperature": 0, "seed": SEED,
            "timeout_seconds": arguments.request_timeout,
        }
    hints = []
    if "/v1/" not in url.path:
        hints.append("vLLM normally uses a base URL ending in /v1; check --base-url (custom proxy routes may differ).")
    status_code = details["status_code"]
    if status_code == 404:
        hints.append("Check the endpoint path and --model against the server's /v1/models listing.")
    elif status_code in {400, 422}:
        hints.append("Check the server message for unsupported parameters, reasoning_effort values, or context/token limits.")
    elif status_code in {401, 403}:
        hints.append("Check the API key and server access policy.")
    elif status_code == 429:
        hints.append("Check rate limits or reduce --concurrency.")
    elif status_code is None:
        hints.append("Check server connectivity and --request-timeout; inspect server logs for malformed responses.")
    details["hints"] = hints
    return details


class EvaluationProgress:
    def __init__(self, total: int, concurrency: int, disabled: bool = False):
        self.total = total
        self.disabled = disabled
        self.interactive = not disabled and sys.stderr.isatty()
        self.active: dict[int, dict[str, Any]] = {}
        self.sent = self.processed = self.api_errors = self.invalid = 0
        self.stopping = False
        self.overall = tqdm(
            total=total, desc="Evaluation", unit="sample", position=0,
            dynamic_ncols=True, disable=not self.interactive, file=sys.stderr,
        )
        self.slots = [
            tqdm(
                total=1, desc=f"Slot {slot + 1}: idle", position=slot + 1,
                bar_format="{desc}", dynamic_ncols=True, leave=False,
                disable=not self.interactive, file=sys.stderr,
            )
            for slot in range(min(total, concurrency))
        ]

    def log(self, message: str):
        if not self.disabled:
            tqdm.write(message, file=sys.stderr)

    def start(self, index: int, sample_id: int):
        occupied = {request["slot"] for request in self.active.values()}
        slot = next(slot for slot in range(len(self.slots)) if slot not in occupied)
        self.active[index] = {
            "slot": slot, "sample_id": sample_id, "started": time.perf_counter(),
            "status": "Sending / waiting for response",
        }
        self.sent += 1
        if not self.interactive:
            self.log(f"Request {index}/{self.total} | sample {sample_id} | sending / waiting for response")
        self.refresh()

    def status(self, index: int, status: str):
        self.active[index]["status"] = status
        if not self.interactive:
            self.log(f"Request {index}/{self.total} | sample {self.active[index]['sample_id']} | {status}")
        self.refresh()

    def finish(self, index: int, row: dict[str, Any]):
        request = self.active.pop(index)
        self.processed += 1
        if row["api_error"] is not None:
            self.api_errors += 1
            error = row["api_error"]
            outcome = f"API error: {error['type']} (HTTP {error['status_code']})"
            self.log(f"Request {index} diagnostics: {json.dumps(error, ensure_ascii=True)}")
        elif row["validation_errors"]:
            self.invalid += 1
            outcome = f"validation issues: {len(row['validation_errors'])}"
        else:
            outcome = "complete"
        self.slots[request["slot"]].set_description_str(f"Slot {request['slot'] + 1}: idle", refresh=False)
        self.overall.update(1)
        self.log(
            f"Request {index}/{self.total} | sample {request['sample_id']} | {outcome} | "
            f"{row['latency_seconds']:.1f}s | finish={row['finish_reason']} | processed {self.processed}/{self.total}"
        )
        self.refresh()

    def abort(self, reason: str):
        self.stopping = True
        self.log(f"Stopping new requests; draining {len(self.active)} in flight. {reason}")
        self.refresh()

    def refresh(self):
        if not self.interactive:
            return
        now = time.perf_counter()
        for index, request in self.active.items():
            self.slots[request["slot"]].set_description_str(
                f"Slot {request['slot'] + 1} | request {index}/{self.total} | sample {request['sample_id']} | "
                f"{request['status']} | {now - request['started']:.1f}s",
                refresh=False,
            )
        self.overall.set_postfix_str(
            f"{'draining' if self.stopping else 'running'}, sent={self.sent}, active={len(self.active)}, "
            f"queued={self.total - self.sent}, API errors={self.api_errors}, validation issues={self.invalid}",
            refresh=False,
        )
        self.overall.refresh()
        for bar in self.slots:
            bar.refresh()

    def close(self):
        incomplete = self.processed < self.total
        state = "Stopped" if self.stopping else "Interrupted" if incomplete else "Complete"
        self.overall.set_description_str(state, refresh=False)
        for bar in reversed(self.slots):
            bar.close()
        self.overall.close()
        self.log(
            f"{state}: processed {self.processed}/{self.total}, sent={self.sent}, "
            f"not sent={self.total - self.sent}, API errors={self.api_errors}, validation issues={self.invalid}"
        )


async def generate_structured_prediction(
    client: AsyncOpenAI, arguments: argparse.Namespace, input_obj: dict[str, str], system_prompt: str,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    prompt = json.dumps(input_obj, ensure_ascii=False)
    started = time.perf_counter()
    result: dict[str, Any] = {
        "prediction": None,
        "raw_text": "",
        "strict_json_valid": False,
        "json_parse_success": False,
        "finish_reason": None,
        "api_error": None,
        "fatal_error": False,
    }
    try:
        response = await client.chat.completions.create(
            model=arguments.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            reasoning_effort=arguments.reasoning_effort,
            max_tokens=arguments.max_new_tokens,
            seed=SEED,
            timeout=arguments.request_timeout,
        )
        choice = response.choices[0]
        result["finish_reason"] = choice.finish_reason
        result["raw_text"] = choice.message.content or ""
        if not isinstance(result["raw_text"], str):
            raise ValueError("Response content must be text.")
    except (APIError, AttributeError, IndexError, TypeError, ValueError) as error:
        status_code = getattr(error, "status_code", None)
        result["api_error"] = describe_api_error(error, client, arguments, input_obj)
        result["fatal_error"] = status_code in {400, 401, 403, 404, 405, 422}
    finally:
        result["latency_seconds"] = time.perf_counter() - started
    if result["api_error"] is not None:
        return result
    if on_status is not None:
        on_status("Response received; parsing JSON")
    try:
        result["prediction"], result["strict_json_valid"] = parse_json_from_output(result["raw_text"])
        result["json_parse_success"] = True
    except ValueError:
        pass
    return result


async def generate_predictions(
    client: AsyncOpenAI, arguments: argparse.Namespace, subset: list[dict[str, Any]],
    system_prompt: str, stop: asyncio.Event, progress: EvaluationProgress,
):
    remaining = iter(enumerate(subset, start=1))
    pending = {}

    def schedule_next():
        item = next(remaining, None)
        if item is not None:
            index, example = item
            progress.start(index, example["sample_id"])
            task = asyncio.create_task(generate_structured_prediction(
                client, arguments, example["input_obj"], system_prompt,
                on_status=lambda status: progress.status(index, status),
            ))
            pending[task] = (index, example)

    try:
        for _ in range(min(arguments.concurrency, len(subset))):
            schedule_next()
        while pending:
            completed, _ = await asyncio.wait(pending, timeout=0.25, return_when=asyncio.FIRST_COMPLETED)
            progress.refresh()
            for task in sorted(completed, key=lambda task: pending[task][0]):
                index, example = pending.pop(task)
                yield index, example, task.result()
            if not stop.is_set():
                for _ in completed:
                    schedule_next()
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def evaluate_model(client: AsyncOpenAI, arguments: argparse.Namespace, examples: list[dict[str, Any]]) -> dict[str, Any]:
    if arguments.concurrency < 1:
        raise ValueError("--concurrency must be positive.")
    system_prompt = build_system_prompt(examples)
    subset = random.Random(arguments.sample_seed).sample(examples, min(arguments.limit, len(examples)))
    progress = EvaluationProgress(len(subset), arguments.concurrency, getattr(arguments, "no_progress", False))
    stop = asyncio.Event()
    try:
        async with aclosing(generate_predictions(client, arguments, subset, system_prompt, stop, progress)) as predictions:
            return await evaluate_samples(arguments, subset, progress, stop, predictions)
    finally:
        progress.close()


async def evaluate_samples(
    arguments: argparse.Namespace, subset: list[dict[str, Any]], progress: EvaluationProgress,
    stop: asyncio.Event, predictions: AsyncIterator[tuple[int, dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    rows_by_index = {}
    consecutive_errors = 0
    abort_reason = None
    started = time.perf_counter()
    progress.log(f"Evaluating {arguments.model} on {len(subset)} samples (concurrency={arguments.concurrency})...")
    async for index, example, result in predictions:
        progress.status(index, "Processing API error" if result["api_error"] else "Validating and scoring")
        prediction = result["prediction"]
        gold = example["target_obj"]
        row = {
            "sample_id": example["sample_id"],
            "expected_labels": {field: gold[field] for field in OUTPUT_FIELDS},
            "prediction": {field: prediction[field] for field in OUTPUT_FIELDS if field in prediction}
            if isinstance(prediction, dict) else None,
            "latency_seconds": result["latency_seconds"],
            "finish_reason": result["finish_reason"],
            "api_error": result["api_error"],
            "validation_errors": [],
        }
        if arguments.include_full_responses:
            row["raw_response"] = result["raw_text"]
            row["prediction"] = prediction
        if result["api_error"] is not None:
            consecutive_errors += 1
            for metric in (
                "strict_json_valid", "json_parse_success", "json_recovered", "schema_valid",
                "category_match", "sub_category_match", "tone_match", "weighted_score", "all_fields_correct",
            ):
                row[metric] = None
            if result["fatal_error"]:
                error = result["api_error"]
                abort_reason = f"API configuration error (HTTP {error['status_code']}): {error['message']}"
            elif consecutive_errors >= 3:
                abort_reason = "Stopped after three consecutive API failures."
        else:
            consecutive_errors = 0
            errors = validate_prediction(prediction, example["input_obj"])
            schema_valid = float(not errors)
            matches = {
                field: float(isinstance(prediction, dict) and prediction.get(field) == gold[field])
                for field in OUTPUT_FIELDS
            }
            row.update(
                strict_json_valid=float(result["strict_json_valid"]),
                json_parse_success=float(result["json_parse_success"]),
                json_recovered=float(result["json_parse_success"] and not result["strict_json_valid"]),
                schema_valid=schema_valid,
                category_match=matches["category"],
                sub_category_match=matches["sub_category"],
                tone_match=matches["tone_urgency"],
                weighted_score=0.6 * schema_valid + 0.3 * matches["category"] + 0.1 * matches["tone_urgency"],
                all_fields_correct=float(result["strict_json_valid"] and not errors and all(matches.values())),
                validation_errors=errors,
            )
            if not result["json_parse_success"]:
                row["validation_errors"].insert(0, "No valid JSON could be parsed.")
            elif not result["strict_json_valid"]:
                row["validation_errors"].insert(0, "Response is not strictly JSON; JSON was recovered.")
        rows_by_index[index] = row
        progress.finish(index, row)
        if abort_reason and not stop.is_set():
            stop.set()
            progress.abort(abort_reason)
    rows = [rows_by_index[index] for index in sorted(rows_by_index)]
    elapsed = time.perf_counter() - started
    scored_rows = [row for row in rows if row["api_error"] is None]
    metric_fields = {
        "strict_json_valid_rate": "strict_json_valid",
        "json_parse_success_rate": "json_parse_success",
        "json_recovery_rate": "json_recovered",
        "schema_valid_rate": "schema_valid",
        "category_accuracy": "category_match",
        "sub_category_accuracy": "sub_category_match",
        "tone_accuracy": "tone_match",
        "weighted_score": "weighted_score",
        "all_fields_correct_rate": "all_fields_correct",
    }
    return {
        "model": arguments.model,
        "concurrency": arguments.concurrency,
        "reasoning_effort": arguments.reasoning_effort,
        "sample_seed": arguments.sample_seed,
        "planned_samples": len(subset),
        "samples": len(rows),
        "scored_samples": len(scored_rows),
        "api_failures": len(rows) - len(scored_rows),
        "api_failure_rate": (len(rows) - len(scored_rows)) / len(rows) if rows else 0.0,
        "abort_reason": abort_reason,
        "quality_metric_denominator": "scored_samples (API failures excluded)",
        "weighted_score_formula": "0.6 * schema_valid + 0.3 * category_match + 0.1 * tone_match",
        "seconds": elapsed,
        "samples_per_second": len(rows) / elapsed if elapsed else 0.0,
        **{
            name: statistics.mean(row[field] for row in scored_rows) if scored_rows else None
            for name, field in metric_fields.items()
        },
        "rows": rows,
    }


async def run_evaluation(arguments: argparse.Namespace, examples: list[dict[str, Any]]) -> dict[str, Any]:
    async with AsyncOpenAI(base_url=arguments.base_url, api_key=arguments.api_key, max_retries=0) as client:
        if arguments.model is None:
            try:
                models = await client.models.list(timeout=arguments.request_timeout)
            except (APIError, ValueError) as error:
                details = describe_api_error(error, client, arguments, route="models")
                raise ValueError(f"Model discovery failed: {json.dumps(details, ensure_ascii=True)}") from error
            if not models.data or not models.data[0].id:
                raise ValueError("The server returned no model name; specify --model explicitly.")
            arguments.model = models.data[0].id
            print(f"Discovered model: {arguments.model}")
        return await evaluate_model(client, arguments, examples)


def main() -> int:
    arguments = parse_arguments()
    if not arguments.dataset.is_file() or arguments.limit < 1 or arguments.concurrency < 1:
        print("Error: dataset must exist, and --limit and --concurrency must be positive.", file=sys.stderr)
        return 2
    try:
        examples = load_examples(arguments.dataset)
        metrics = asyncio.run(run_evaluation(arguments, examples))
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    arguments.output.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print("\nEvaluation summary:")
    for key, value in metrics.items():
        if key != "rows":
            print(f"{key}: {value:.4f}" if isinstance(value, float) else f"{key}: {value}")
    print(f"Detailed results written to: {arguments.output}")
    return 1 if metrics["api_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())