#!/usr/bin/env python3
"""Demo 1.2: screen a model candidate on 22 Taco Alley complaints with facts inlined.

Each prompt contains the order record, prior resolutions, and the policy. The model must return a
JSON resolution proposal. Grading covers schema validity, factual grounding against the inlined
facts, policy compliance, latency, and token use. Tool selection is NOT graded here (module 2).

This is a screening test, not a statistically conclusive benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from openai import APIError, AsyncOpenAI

from taco_shared.cli import add_connection_arguments, parse_extra_body
from taco_shared.errors import describe_error
from taco_shared.grading import grade
from taco_shared.jsonutil import parse_json_output
from taco_shared.metrics import fetch_metrics, metrics_url_from_base
from taco_shared.paths import DEFAULT_POLICY, DEFAULT_RESULTS_DIR, DEFAULT_SCENARIOS, DEFAULT_SCHEMA, load_json
from taco_shared.reporting import peak_vram_from_log, safe_slug, summarize_latency, timestamp_slug
from taco_shared.screening import build_messages, build_system_prompt

WEIGHTS = {"schema_valid": 0.35, "grounded": 0.25, "policy_compliant": 0.30, "category_correct": 0.10}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_arguments(parser)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N scenarios.")
    parser.add_argument("--only", default=None, help="Comma-separated scenario ids to run, e.g. S04,S13.")
    parser.add_argument("--structured-output", choices=["none", "json_object", "json_schema"], default="none",
                        help="none = prompt-only (measures the model's own schema compliance); "
                             "json_schema = server-side guided decoding (measures the backend's enforcement).")
    parser.add_argument("--max-tokens", type=int, default=600)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Parallel requests. Keep 1 for clean per-request latency; raise it in Demo 1.3.")
    parser.add_argument("--label", default=None, help="Free-text label for this configuration (e.g. baseline, kv-fp8).")
    parser.add_argument("--vram-log", type=Path, default=None, help="CSV written by tools/sample_vram.sh for peak VRAM.")
    parser.add_argument("--include-full-responses", action="store_true", help="Store raw model text in the results file.")
    parser.add_argument("--output", type=Path, default=None,
                        help="Results path (default: results/screening-<model>[-label]-<timestamp>.json).")
    return parser.parse_args()


def response_format_for(mode: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "json_schema":
        return {"type": "json_schema", "json_schema": {"name": "resolution_proposal", "schema": schema, "strict": True}}
    return None


async def run_scenario(
    client: AsyncOpenAI, args: argparse.Namespace, scenario: dict[str, Any], system_prompt: str,
    schema: dict[str, Any], validator: Draft202012Validator, extra_body: dict[str, Any], semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    row: dict[str, Any] = {"scenario_id": scenario["scenario_id"], "label": scenario["label"]}
    request: dict[str, Any] = {
        "model": args.model, "messages": build_messages(scenario, system_prompt),
        "temperature": args.temperature, "max_tokens": args.max_tokens, "seed": 42,
        "extra_body": extra_body or None,
    }
    response_format = response_format_for(args.structured_output, schema)
    if response_format:
        request["response_format"] = response_format
    async with semaphore:
        started = time.perf_counter()
        try:
            response = await client.chat.completions.create(**request)
        except APIError as error:
            row.update(latency_seconds=time.perf_counter() - started, api_error=describe_error(error))
            return row
        row["latency_seconds"] = time.perf_counter() - started
    choice = response.choices[0]
    content = choice.message.content or ""
    usage = response.usage.model_dump() if response.usage else {}
    reasoning = getattr(choice.message, "reasoning_content", None) or getattr(choice.message, "reasoning", None)
    row.update(
        api_error=None, finish_reason=choice.finish_reason,
        prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        had_reasoning_content=bool(reasoning),
    )
    if args.include_full_responses:
        row["raw_response"] = content
        if reasoning:
            row["raw_reasoning"] = reasoning
    try:
        prediction, strict = parse_json_output(content)
    except ValueError as error:
        row.update(strict_json=False, json_parsed=False, prediction=None,
                   grades=grade(None, scenario, [f"Unparseable output: {error}"]))
        return row
    schema_errors = [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in validator.iter_errors(prediction)]
    if not strict:
        schema_errors.insert(0, "Response was not strictly JSON; JSON was recovered from surrounding text.")
    row.update(strict_json=strict, json_parsed=True, prediction=prediction,
               grades=grade(prediction, scenario, schema_errors))
    return row


async def run(args: argparse.Namespace) -> dict[str, Any]:
    extra_body = parse_extra_body(args.extra_body)
    schema = load_json(args.schema)
    validator = Draft202012Validator(schema)
    policy = args.policy.read_text(encoding="utf-8")
    dataset = load_json(args.scenarios)
    scenarios = dataset["scenarios"]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",")}
        scenarios = [s for s in scenarios if s["scenario_id"] in wanted]
    if args.limit:
        scenarios = scenarios[: args.limit]
    system_prompt = build_system_prompt(policy, schema)
    metrics_url = metrics_url_from_base(args.base_url, args.metrics_url)

    async with AsyncOpenAI(base_url=args.base_url, api_key=args.api_key, max_retries=0, timeout=args.request_timeout) as client:
        if args.model is None:
            models = await client.models.list()
            args.model = models.data[0].id
            print(f"Discovered model: {args.model}")
        metrics_before = fetch_metrics(metrics_url)
        semaphore = asyncio.Semaphore(args.concurrency)
        print(f"Screening {args.model} on {len(scenarios)} scenarios "
              f"(structured_output={args.structured_output}, concurrency={args.concurrency}, max_tokens={args.max_tokens})")
        started = time.perf_counter()
        tasks = [run_scenario(client, args, s, system_prompt, schema, validator, extra_body, semaphore) for s in scenarios]
        rows: list[dict[str, Any]] = []
        for coroutine in asyncio.as_completed(tasks):
            row = await coroutine
            rows.append(row)
            if row.get("api_error"):
                err = row["api_error"]
                print(f"  {row['scenario_id']:>4} {row['label']:<38} ERROR {err['kind']} HTTP {err['status_code']}: {err['message'][:120]}")
            else:
                g = row["grades"]
                flags = f"schema={'Y' if g['schema_valid'] else 'n'} grounded={'Y' if g['grounded'] else 'n'} " \
                        f"policy={'Y' if g['policy_compliant'] else 'n'} category={'Y' if g['category_correct'] else 'n'}"
                verdict = "PASS" if g["passed"] else "FAIL"
                print(f"  {row['scenario_id']:>4} {row['label']:<38} {verdict} {flags} "
                      f"{row['latency_seconds']:5.1f}s out={row.get('completion_tokens')} finish={row.get('finish_reason')}")
        elapsed = time.perf_counter() - started
        metrics_after = fetch_metrics(metrics_url)

    rows.sort(key=lambda r: r["scenario_id"])
    scored = [r for r in rows if not r.get("api_error")]
    latencies = [r["latency_seconds"] for r in scored]

    def rate(key: str) -> float | None:
        return statistics.mean(1.0 if r["grades"][key] else 0.0 for r in scored) if scored else None

    rates = {key: rate(key) for key in ("schema_valid", "grounded", "policy_compliant", "category_correct", "passed")}
    rates["strict_json"] = statistics.mean(1.0 if r["strict_json"] else 0.0 for r in scored) if scored else None
    weighted = sum(WEIGHTS[k] * (rates[k] or 0.0) for k in WEIGHTS) if scored else None
    completion_tokens = [r["completion_tokens"] for r in scored if isinstance(r.get("completion_tokens"), int)]
    prompt_tokens = [r["prompt_tokens"] for r in scored if isinstance(r.get("prompt_tokens"), int)]
    truncated = sum(1 for r in scored if r.get("finish_reason") == "length")

    summary = {
        "model": args.model,
        "label": args.label,
        "dataset_version": dataset.get("dataset_version"),
        "structured_output": args.structured_output,
        "concurrency": args.concurrency,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "extra_body": extra_body,
        "scenarios": len(rows),
        "scored": len(scored),
        "api_errors": len(rows) - len(scored),
        "truncated_responses": truncated,
        "rates": rates,
        "weighted_score": weighted,
        "weights": WEIGHTS,
        "latency_seconds": summarize_latency(latencies),
        "wall_seconds": elapsed,
        "tokens": {
            "prompt_mean": statistics.mean(prompt_tokens) if prompt_tokens else None,
            "completion_mean": statistics.mean(completion_tokens) if completion_tokens else None,
            "completion_total": sum(completion_tokens) if completion_tokens else None,
            "completion_tokens_per_second": (sum(completion_tokens) / elapsed) if completion_tokens and elapsed else None,
            "reasoning_tokens_seen": any(r.get("reasoning_tokens") for r in scored),
        },
        "vram": peak_vram_from_log(args.vram_log),
        "vllm_metrics_before": metrics_before,
        "vllm_metrics_after": metrics_after,
        "rows": rows,
    }
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    print("\nScreening summary")
    print(f"  model:               {summary['model']}  label={summary['label']}")
    print(f"  structured output:   {summary['structured_output']}")
    print(f"  scenarios / errors:  {summary['scenarios']} / {summary['api_errors']}   truncated={summary['truncated_responses']}")
    for key, value in summary["rates"].items():
        print(f"  {key + ':':<21}{value:.2f}" if value is not None else f"  {key + ':':<21}-")
    if summary["weighted_score"] is not None:
        print(f"  weighted score:      {summary['weighted_score']:.3f}   ({summary['weights']})")
    lat = summary["latency_seconds"]
    if lat["mean"] is not None:
        print(f"  latency s:           mean={lat['mean']:.2f} p50={lat['p50']:.2f} p95={lat['p95']:.2f} max={lat['max']:.2f}")
    tok = summary["tokens"]
    if tok["completion_mean"] is not None:
        print(f"  tokens:              prompt_mean={tok['prompt_mean']:.0f} completion_mean={tok['completion_mean']:.0f} "
              f"gen_tok/s={tok['completion_tokens_per_second']:.1f}")
    if summary["vram"]:
        print(f"  peak VRAM:           {summary['vram']['peak_memory_used_mib']:.0f} / {summary['vram']['memory_total_mib']:.0f} MiB")
    failures = [r for r in summary["rows"] if r.get("api_error") or not r["grades"]["passed"]]
    if failures:
        print("\nScenarios needing review:")
        for row in failures:
            if row.get("api_error"):
                print(f"  {row['scenario_id']} {row['label']}: API error {row['api_error']['message'][:100]}")
                continue
            for issue in row["grades"]["issues"][:3]:
                print(f"  {row['scenario_id']} {row['label']}: {issue}")


def main() -> int:
    args = parse_arguments()
    try:
        summary = asyncio.run(run(args))
    except (OSError, ValueError, APIError) as error:
        print(f"Error: {describe_error(error)['message'] if isinstance(error, APIError) else error}", file=sys.stderr)
        return 1
    output = args.output or (DEFAULT_RESULTS_DIR / f"screening-{safe_slug(summary['model'])}"
                             f"{'-' + safe_slug(args.label) if args.label else ''}-{timestamp_slug()}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print_summary(summary)
    print(f"\nDetailed results written to: {output}")
    return 1 if summary["api_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
