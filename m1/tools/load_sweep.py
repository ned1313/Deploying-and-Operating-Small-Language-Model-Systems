#!/usr/bin/env python3
"""Demo 1.3: sweep request concurrency against the inference endpoint and report the throughput/latency tradeoff.

For each concurrency level the script keeps N requests in flight until --requests-per-level complete,
streaming responses to measure time-to-first-token (prefill) separately from total latency (decode).
While a level runs, the vLLM /metrics endpoint is sampled to capture running/waiting queues, KV-cache
usage, and preemptions. Use --pad-prompt-tokens to lengthen prompts and pressure the KV cache.
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

from openai import APIError, AsyncOpenAI

from common import (
    DEFAULT_POLICY,
    DEFAULT_RESULTS_DIR,
    DEFAULT_SCENARIOS,
    DEFAULT_SCHEMA,
    add_connection_arguments,
    build_messages,
    build_system_prompt,
    describe_error,
    fetch_metrics,
    load_json,
    metrics_url_from_base,
    parse_extra_body,
    print_table,
    safe_slug,
    summarize_latency,
    timestamp_slug,
)

# Filler paragraph appended to the user prompt to pad context; ~55 tokens per repetition.
FILLER = (
    "Additional context from the store log: the shift lead noted normal staffing, the point-of-sale system "
    "was online, the courier hand-off area was clear, and no other complaints were filed for this order window. "
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_arguments(parser)
    parser.add_argument("--concurrency", default="1,4,8,16,32", help="Comma-separated concurrency levels to sweep.")
    parser.add_argument("--requests-per-level", type=int, default=32, help="Requests to complete at each level.")
    parser.add_argument("--max-tokens", type=int, default=300, help="Output budget per request.")
    parser.add_argument("--pad-prompt-tokens", type=int, default=0,
                        help="Approximate extra prompt tokens appended to each request to pressure the KV cache.")
    parser.add_argument("--structured-output", choices=["none", "json_schema"], default="none",
                        help="Include json_schema guided decoding to measure its overhead under load.")
    parser.add_argument("--metrics-interval", type=float, default=1.0, help="Seconds between /metrics samples.")
    parser.add_argument("--label", default=None, help="Free-text label for the serving configuration under test.")
    parser.add_argument("--output", type=Path, default=None,
                        help="Results path (default: results/load-<model>[-label]-<timestamp>.json).")
    return parser.parse_args()


async def one_request(client: AsyncOpenAI, request: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    first_token = None
    completion_tokens = None
    prompt_tokens = None
    chunks = 0
    try:
        stream = await client.chat.completions.create(stream=True, stream_options={"include_usage": True}, **request)
        async for chunk in stream:
            if chunk.choices and (chunk.choices[0].delta.content or getattr(chunk.choices[0].delta, "reasoning_content", None)):
                if first_token is None:
                    first_token = time.perf_counter() - started
                chunks += 1
            if chunk.usage:
                completion_tokens = chunk.usage.completion_tokens
                prompt_tokens = chunk.usage.prompt_tokens
    except (APIError, asyncio.TimeoutError, OSError) as error:
        return {"ok": False, "latency": time.perf_counter() - started, "error": describe_error(error)}
    latency = time.perf_counter() - started
    return {"ok": True, "latency": latency, "ttft": first_token, "chunks": chunks,
            "completion_tokens": completion_tokens, "prompt_tokens": prompt_tokens}


async def sample_metrics(url: str, interval: float, samples: list[dict[str, float]], stop: asyncio.Event) -> None:
    while not stop.is_set():
        snapshot = fetch_metrics(url, timeout=2.0)
        if snapshot:
            snapshot["_t"] = time.perf_counter()
            samples.append(snapshot)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run_level(client: AsyncOpenAI, level: int, requests: list[dict[str, Any]], metrics_url: str,
                    interval: float) -> dict[str, Any]:
    samples: list[dict[str, float]] = []
    stop = asyncio.Event()
    sampler = asyncio.create_task(sample_metrics(metrics_url, interval, samples, stop))
    before = fetch_metrics(metrics_url, timeout=2.0) or {}
    queue = asyncio.Queue()
    for request in requests:
        queue.put_nowait(request)
    outcomes: list[dict[str, Any]] = []

    async def worker() -> None:
        while True:
            try:
                request = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            outcomes.append(await one_request(client, request))

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(level)))
    elapsed = time.perf_counter() - started
    stop.set()
    await sampler
    after = fetch_metrics(metrics_url, timeout=2.0) or {}

    good = [o for o in outcomes if o["ok"]]
    errors = [o for o in outcomes if not o["ok"]]
    completion_tokens = sum(o["completion_tokens"] or 0 for o in good)
    ttfts = [o["ttft"] for o in good if o["ttft"] is not None]

    def peak(name: str) -> float | None:
        values = [s[name] for s in samples if name in s]
        return max(values) if values else None

    kv_metric = "vllm:kv_cache_usage_perc" if any("vllm:kv_cache_usage_perc" in s for s in samples) else "vllm:gpu_cache_usage_perc"
    return {
        "concurrency": level,
        "requests": len(outcomes),
        "errors": len(errors),
        "error_samples": [e["error"] for e in errors[:3]],
        "wall_seconds": elapsed,
        "requests_per_second": len(good) / elapsed if elapsed else None,
        "output_tokens_per_second": completion_tokens / elapsed if elapsed else None,
        "mean_completion_tokens": statistics.mean(o["completion_tokens"] for o in good if o["completion_tokens"]) if good else None,
        "mean_prompt_tokens": statistics.mean(o["prompt_tokens"] for o in good if o["prompt_tokens"]) if good else None,
        "latency_seconds": summarize_latency([o["latency"] for o in good]),
        "ttft_seconds": summarize_latency(ttfts),
        "vllm": {
            "peak_running": peak("vllm:num_requests_running"),
            "peak_waiting": peak("vllm:num_requests_waiting"),
            "peak_kv_cache_usage_pct": (peak(kv_metric) or 0.0) * 100 if peak(kv_metric) is not None else None,
            "preemptions_delta": (after.get("vllm:num_preemptions_total", 0.0) - before.get("vllm:num_preemptions_total", 0.0))
            if before and after else None,
            "metrics_samples": len(samples),
        },
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    extra_body = parse_extra_body(args.extra_body)
    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    schema = load_json(DEFAULT_SCHEMA)
    system_prompt = build_system_prompt(DEFAULT_POLICY.read_text(encoding="utf-8"), schema)
    scenarios = load_json(DEFAULT_SCENARIOS)["scenarios"]
    metrics_url = metrics_url_from_base(args.base_url, args.metrics_url)
    padding = FILLER * max(0, round(args.pad_prompt_tokens / 55))

    async with AsyncOpenAI(base_url=args.base_url, api_key=args.api_key, max_retries=0, timeout=args.request_timeout) as client:
        if args.model is None:
            models = await client.models.list()
            args.model = models.data[0].id
            print(f"Discovered model: {args.model}")
        if fetch_metrics(metrics_url) is None:
            print(f"Warning: {metrics_url} not reachable; server-side queue and KV-cache figures will be blank.")

        levels_out = []
        for level in levels:
            requests = []
            for i in range(args.requests_per_level):
                scenario = scenarios[i % len(scenarios)]
                messages = build_messages(scenario, system_prompt)
                if padding:
                    messages[1]["content"] += "\n\n" + padding
                request: dict[str, Any] = {"model": args.model, "messages": messages, "temperature": 0.7,
                                           "max_tokens": args.max_tokens, **extra_body}
                if args.structured_output == "json_schema":
                    request["response_format"] = {"type": "json_schema",
                                                  "json_schema": {"name": "resolution_proposal", "schema": schema, "strict": True}}
                requests.append(request)
            print(f"\nConcurrency {level}: sending {len(requests)} requests (max_tokens={args.max_tokens}, pad~{args.pad_prompt_tokens} tok)...")
            result = await run_level(client, level, requests, metrics_url, args.metrics_interval)
            levels_out.append(result)
            lat, ttft, v = result["latency_seconds"], result["ttft_seconds"], result["vllm"]
            print(f"  done in {result['wall_seconds']:.1f}s  req/s={result['requests_per_second']:.2f}  "
                  f"out tok/s={result['output_tokens_per_second']:.0f}  "
                  f"latency p50/p95={lat['p50'] or 0:.2f}/{lat['p95'] or 0:.2f}s  "
                  f"ttft p50/p95={ttft['p50'] or 0:.2f}/{ttft['p95'] or 0:.2f}s  "
                  f"errors={result['errors']}  peak waiting={v['peak_waiting']}  "
                  f"kv%={v['peak_kv_cache_usage_pct'] if v['peak_kv_cache_usage_pct'] is None else round(v['peak_kv_cache_usage_pct'])}  "
                  f"preemptions={v['preemptions_delta']}")
            for sample in result["error_samples"]:
                print(f"  error: {sample['kind']} HTTP {sample['status_code']}: {sample['message'][:160]}")
            if result["errors"] == result["requests"]:
                print("  Every request failed at this level; stopping the sweep.")
                break

    return {
        "model": args.model, "label": args.label, "max_tokens": args.max_tokens,
        "pad_prompt_tokens": args.pad_prompt_tokens, "structured_output": args.structured_output,
        "requests_per_level": args.requests_per_level, "extra_body": extra_body, "levels": levels_out,
    }


def print_summary(summary: dict[str, Any]) -> None:
    headers = ["conc", "req/s", "out tok/s", "lat p50", "lat p95", "ttft p50", "ttft p95", "peak wait", "kv %", "preempt", "err"]
    rows = []
    for level in summary["levels"]:
        lat, ttft, v = level["latency_seconds"], level["ttft_seconds"], level["vllm"]
        rows.append([level["concurrency"], level["requests_per_second"], level["output_tokens_per_second"],
                     lat["p50"], lat["p95"], ttft["p50"], ttft["p95"], v["peak_waiting"], v["peak_kv_cache_usage_pct"],
                     v["preemptions_delta"], level["errors"]])
    print(f"\nLoad sweep: {summary['model']}  label={summary['label']}  max_tokens={summary['max_tokens']}  pad~{summary['pad_prompt_tokens']}")
    print_table(headers, rows)
    print("\nReading the table: out tok/s rising while p95 stays flat = spare capacity. "
          "Waiting > 0 or rising preemptions = the batch scheduler is saturated or KV cache is under pressure.")


def main() -> int:
    args = parse_arguments()
    try:
        summary = asyncio.run(run(args))
    except (OSError, ValueError, APIError) as error:
        print(f"Error: {describe_error(error)['message'] if isinstance(error, APIError) else error}", file=sys.stderr)
        return 1
    output = args.output or (DEFAULT_RESULTS_DIR / f"load-{safe_slug(summary['model'])}"
                             f"{'-' + safe_slug(args.label) if args.label else ''}-{timestamp_slug()}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print_summary(summary)
    print(f"\nDetailed results written to: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
