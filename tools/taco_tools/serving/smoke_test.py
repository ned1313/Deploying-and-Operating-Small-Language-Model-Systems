#!/usr/bin/env python3
"""Demo 1.1: validate a freshly deployed vLLM endpoint and record its deployment configuration.

Steps:
  1. GET /v1/models              - confirm the served model name and max context length
  2. POST /v1/chat/completions   - plain request; inspect request and response formatting
  3. POST /v1/chat/completions   - structured output with the resolution-proposal JSON schema
  4. Write a deployment record   - server-reported facts plus operator-supplied pins

Any 4xx from the server is reported as a COMPATIBILITY ISSUE (client/server/model mismatch),
not as a model-quality failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from openai import APIError, OpenAI

from taco_shared.cli import add_connection_arguments, parse_extra_body
from taco_shared.errors import describe_error
from taco_shared.jsonutil import parse_json_output
from taco_shared.metrics import fetch_metrics, metrics_url_from_base
from taco_shared.paths import DEFAULT_POLICY, DEFAULT_RESULTS_DIR, DEFAULT_SCENARIOS, DEFAULT_SCHEMA, load_json
from taco_shared.reporting import safe_slug
from taco_shared.screening import build_messages, build_system_prompt


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_arguments(parser)
    parser.add_argument("--scenario", default="S01", help="Scenario id used for the structured-output check.")
    parser.add_argument("--max-tokens", type=int, default=500)
    parser.add_argument("--skip-structured", action="store_true", help="Skip the json_schema response_format check.")
    record = parser.add_argument_group("deployment record (operator-supplied pins)")
    record.add_argument("--hf-repo", default=None, help="Hugging Face repository id of the artifact.")
    record.add_argument("--revision", default=None, help="Pinned model revision (commit sha or tag).")
    record.add_argument("--quantization", default=None, help="Quantization format, e.g. awq, gptq, fp8, gguf-q4_k_m.")
    record.add_argument("--chat-template", default=None, help="Chat template source: 'tokenizer default' or a file name.")
    record.add_argument("--tool-call-parser", default=None, help="vLLM --tool-call-parser value (prerequisite for module 2).")
    record.add_argument("--reasoning-parser", default=None, help="vLLM --reasoning-parser value, if any.")
    record.add_argument("--serving-image", default=None, help="Container image and tag used to serve the model.")
    record.add_argument("--serving-args", default=None, help="Full vLLM argument string as launched.")
    record.add_argument("--notes", default=None)
    parser.add_argument("--output", type=Path, default=None,
                        help="Deployment record path (default: results/deployment-record-<model>.json).")
    return parser.parse_args()


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def report_error(step: str, error: Exception) -> dict[str, Any]:
    details = describe_error(error)
    label = "COMPATIBILITY ISSUE" if details["kind"] == "compatibility" else details["kind"].upper()
    print(f"[{label}] {step}: HTTP {details['status_code']} {details['type']}")
    print(f"  {details['message']}")
    if details["kind"] == "compatibility":
        print("  The server rejected the request shape. This is a client/server/model-configuration mismatch, "
              "not evidence about model quality. Check quantization flags, parser names, and response_format support.")
    return details


def main() -> int:
    args = parse_arguments()
    extra_body = parse_extra_body(args.extra_body)
    metrics_url = metrics_url_from_base(args.base_url, args.metrics_url)
    client = OpenAI(base_url=args.base_url, api_key=args.api_key, max_retries=0, timeout=args.request_timeout)
    record: dict[str, Any] = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": args.base_url,
        "operator_pins": {
            "hf_repo": args.hf_repo, "revision": args.revision, "quantization": args.quantization,
            "chat_template": args.chat_template, "tool_call_parser": args.tool_call_parser,
            "reasoning_parser": args.reasoning_parser, "serving_image": args.serving_image,
            "serving_args": args.serving_args, "notes": args.notes,
        },
        "checks": {},
    }
    failures = 0

    banner("1. Model discovery: GET /v1/models")
    try:
        models = client.models.list()
    except APIError as error:
        record["checks"]["models"] = {"ok": False, "error": report_error("GET /v1/models", error)}
        print("Cannot continue without a model listing.")
        return 1
    served = []
    for model in models.data:
        raw = model.model_dump()
        served.append({"id": raw.get("id"), "root": raw.get("root"), "max_model_len": raw.get("max_model_len")})
        print(f"id={raw.get('id')}  root={raw.get('root')}  max_model_len={raw.get('max_model_len')}")
    if not served:
        print("Server returned an empty model list.")
        return 1
    if args.model is None:
        args.model = served[0]["id"]
        print(f"Using discovered model: {args.model}")
    record["model"] = args.model
    record["checks"]["models"] = {"ok": True, "served": served}

    banner("Server version and metrics endpoints")
    version_url = metrics_url.replace("/metrics", "/version")
    try:
        version = httpx.get(version_url, timeout=5.0)
        record["server_version"] = version.json() if version.status_code == 200 else None
        print(f"{version_url} -> {version.status_code} {version.text.strip()[:200]}")
    except (httpx.HTTPError, ValueError):
        record["server_version"] = None
        print(f"{version_url} -> unreachable (not fatal)")
    snapshot = fetch_metrics(metrics_url)
    print(f"{metrics_url} -> {'reachable, ' + str(len(snapshot)) + ' tracked series' if snapshot else 'unreachable (not fatal)'}")
    record["metrics_reachable"] = snapshot is not None

    banner("2. Plain chat completion")
    request_body: dict[str, Any] = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": "You are a concise assistant for Taco Alley restaurant staff."},
            {"role": "user", "content": "In one sentence, what should a staff member do first when a customer reports a missing item?"},
        ],
        "temperature": 0,
        "max_tokens": min(args.max_tokens, 200),
    }
    print("Request body (as sent on the wire):")
    print(json.dumps({**request_body, **extra_body}, indent=2))
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(**request_body, extra_body=extra_body or None)
    except APIError as error:
        failures += 1
        record["checks"]["plain_chat"] = {"ok": False, "error": report_error("plain chat completion", error)}
    else:
        latency = time.perf_counter() - started
        choice = response.choices[0]
        print("Response (trimmed):")
        print(json.dumps({
            "id": response.id, "model": response.model, "finish_reason": choice.finish_reason,
            "content": choice.message.content, "usage": response.usage.model_dump() if response.usage else None,
        }, indent=2))
        print(f"Latency: {latency:.2f}s")
        record["checks"]["plain_chat"] = {
            "ok": True, "latency_seconds": latency, "finish_reason": choice.finish_reason,
            "usage": response.usage.model_dump() if response.usage else None,
        }

    if not args.skip_structured:
        banner("3. Structured output: response_format=json_schema")
        schema = load_json(DEFAULT_SCHEMA)
        policy = DEFAULT_POLICY.read_text(encoding="utf-8")
        scenarios = load_json(DEFAULT_SCENARIOS)["scenarios"]
        scenario = next((item for item in scenarios if item["scenario_id"] == args.scenario), scenarios[0])
        messages = build_messages(scenario, build_system_prompt(policy, schema))
        print(f"Scenario {scenario['scenario_id']} ({scenario['label']}); prompt is {sum(len(m['content']) for m in messages)} characters.")
        started = time.perf_counter()
        try:
            response = client.chat.completions.create(
                model=args.model, messages=messages, temperature=0, max_tokens=args.max_tokens,
                response_format={"type": "json_schema", "json_schema": {"name": "resolution_proposal", "schema": schema, "strict": True}},
                extra_body=extra_body or None,
            )
        except APIError as error:
            failures += 1
            record["checks"]["structured_output"] = {"ok": False, "error": report_error("json_schema response_format", error)}
        else:
            latency = time.perf_counter() - started
            content = response.choices[0].message.content or ""
            check: dict[str, Any] = {"latency_seconds": latency, "finish_reason": response.choices[0].finish_reason,
                                     "usage": response.usage.model_dump() if response.usage else None}
            try:
                parsed, strict = parse_json_output(content)
                errors = [e.message for e in Draft202012Validator(schema).iter_errors(parsed)]
                check.update(ok=not errors, strict_json=strict, schema_errors=errors)
                print(json.dumps(parsed, indent=2))
                print(f"Strict JSON: {strict}   Schema valid: {not errors}   Latency: {latency:.2f}s")
                for message in errors:
                    print(f"  schema error: {message}")
                if errors:
                    failures += 1
                expected = scenario["expected"]
                print(f"Expected for reference: resolution={expected['resolution_type']} refund={expected['refund_amount']} "
                      f"rules={expected['required_policy_rule_ids']} (graded properly in Demo 1.2)")
            except ValueError as error:
                failures += 1
                check.update(ok=False, parse_error=str(error), raw_preview=content[:300])
                print(f"Could not parse JSON from response: {error}")
                print(content[:500])
            record["checks"]["structured_output"] = check

    banner("4. Deployment record")
    output = args.output or (DEFAULT_RESULTS_DIR / f"deployment-record-{safe_slug(args.model)}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2), encoding="utf-8")
    missing = [key for key, value in record["operator_pins"].items() if value is None and key != "notes"]
    print(f"Written to {output}")
    if missing:
        print(f"Operator pins not supplied (pass them as flags so the record is complete): {', '.join(missing)}")
    print(f"\nResult: {'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' check(s) failed'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
