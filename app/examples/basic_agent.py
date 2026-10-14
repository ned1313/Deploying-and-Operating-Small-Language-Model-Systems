#!/usr/bin/env python3
"""Module 2 teaching script: a Taco Alley complaint agent built with LangChain.

    basic_agent.py --check                         confirm the endpoint, model, database, and case API
    basic_agent.py --mode chat  --scenario A01     plain chat call, no tools (clip 2)
    basic_agent.py --show-tools                    what the model sees: tool specs and the proposal schema (clip 3)
    basic_agent.py --mode tools --scenario A01     create_agent investigation, structured proposal, validation (clip 4)
    basic_agent.py --mode tools --scenario S01 --actor rep-wg-01     scope demo: order outside the actor's stores
    basic_agent.py --mode tools --complaint "... Order TA-10431." --customer-id CUST-14159 --case-id CASE-10431

Settings come from the environment (.env in compose) and the secret file; see m2/.env.example.
Exit codes: 0 ok/accepted, 1 connectivity or API error, 2 proposal rejected by validation, 3 tool-call limit hit.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

from langchain_core.utils.function_calling import convert_to_openai_tool
from openai import APIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.loop import run_chat, run_investigation  # noqa: E402
from agent.render import StepPrinter  # noqa: E402
from agent.scenarios import build_tools, context_for, find_scenario  # noqa: E402
from taco_shared.agent.model import build_chat_model, check_endpoint  # noqa: E402
from taco_shared.case_api_client import CaseApiClient  # noqa: E402
from taco_shared.config import InferenceSettings  # noqa: E402
from taco_shared.context import build_context  # noqa: E402
from taco_shared.db import AppDatabase  # noqa: E402
from taco_shared.errors import describe_error  # noqa: E402
from taco_shared.policy import load_policy  # noqa: E402
from taco_shared.agent.tools import build_investigation_tools  # noqa: E402
from taco_shared.proposal import load_schema_v2  # noqa: E402
from taco_shared.reporting import timestamp_slug  # noqa: E402


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Check endpoint, model, database, and case API.")
    action.add_argument("--show-tools", action="store_true", help="Print tool specs and the proposal schema.")
    parser.add_argument("--mode", choices=["chat", "tools"], default="tools")
    parser.add_argument("--scenario", help="Agent scenario id, e.g. A01 or S04.")
    parser.add_argument("--complaint", help="Free-text complaint instead of a scenario (include the order id).")
    parser.add_argument("--customer-id", help="Customer id for --complaint.")
    parser.add_argument("--case-id", default="CASE-ADHOC", help="Case id for --complaint.")
    parser.add_argument("--complaint-date", default=date.today().isoformat(), help="Complaint date for --complaint.")
    parser.add_argument("--actor", help="Actor id (default: the scenario's actor, or TACO_ACTOR).")
    parser.add_argument("--no-structured-output", action="store_true", help="Finalize prompt-only, without json_schema.")
    parser.add_argument("--max-tool-calls", type=int, help="Override AGENT_MAX_TOOL_CALLS.")
    parser.add_argument("--verbose", action="store_true", help="Also print full tool results and message objects.")
    parser.add_argument("--save-transcript", action="store_true", help="Write the run to results/ (the only write).")
    return parser.parse_args()


def run_check(settings: InferenceSettings) -> int:
    print(f"Inference endpoint: {settings.base_url}   model: {settings.model}   key: {settings.api_key_source}")
    result = check_endpoint(settings)
    for model in result.get("served", []):
        print(f"  served: {model['id']}  max_model_len={model['max_model_len']}  root={model['root']}")
    if not result["ok"]:
        error = result["error"]
        print(f"  FAILED at {error['step']}: [{error['kind']}] {error.get('message')}")
        if error["kind"] in {"transport", "server"}:
            print("  This is a network, gateway, or server problem, not a model-quality problem.")
        return 1
    print(f"  chat ok in {result['latency_seconds']:.2f}s: {result['reply']!r}")
    status = 0
    try:
        meta = AppDatabase(Path(settings.app_db)).meta()
        print(f"Application database: {settings.app_db} (policy {meta['policy_version']}, dataset {meta['agent_version']})")
    except (FileNotFoundError, OSError) as error:
        print(f"Application database: NOT READY ({error})")
        status = 1
    healthy = CaseApiClient(settings.case_api_url).health()
    print(f"Case API: {settings.case_api_url} {'healthy' if healthy else 'UNREACHABLE (start it with: podman-compose up -d case-api)'}")
    return status if healthy else 1


def show_tools(settings: InferenceSettings) -> int:
    ctx = build_context(settings.actor_id, "CUST-00000", "CASE-00000", date.today().isoformat())
    tools = build_investigation_tools(ctx, None, None, load_policy(settings.policy_version))  # type: ignore[arg-type]
    print("Tools as sent to the model (OpenAI tool format):")
    for tool in tools:
        print(json.dumps(convert_to_openai_tool(tool), indent=2))
    print("\nProposal schema used for the finalize call (response_format json_schema):")
    print(json.dumps(load_schema_v2(), indent=2))
    return 0


def main() -> int:
    args = parse_arguments()
    settings = InferenceSettings.from_env()
    if args.no_structured_output:
        settings = replace(settings, structured_output="none")
    if args.max_tool_calls:
        settings = replace(settings, max_tool_calls=args.max_tool_calls)
    if args.check:
        return run_check(settings)
    if args.show_tools:
        return show_tools(settings)

    if args.scenario:
        scenario = find_scenario(args.scenario)
        intake, actor = scenario["intake"], args.actor or scenario["actor_id"]
        print(f"Scenario {scenario['scenario_id']} ({scenario['label']})  actor={actor}")
    elif args.complaint and args.customer_id:
        intake = {"case_id": args.case_id, "customer_id": args.customer_id, "complaint_date": args.complaint_date,
                  "complaint_channel": "Phone", "complaint": args.complaint}
        actor = args.actor or settings.actor_id
    else:
        print("Give --scenario, or --complaint with --customer-id.", file=sys.stderr)
        return 2
    print(f"Complaint: {intake['complaint']}\n")

    model = build_chat_model(settings)
    printer = StepPrinter(verbose=args.verbose)
    try:
        if args.mode == "chat":
            result = run_chat(model, intake)
            print(result.answer)
            print("\nNo order, history, or policy was looked up: every fact above is the model's guess.")
        else:
            ctx = context_for(intake, actor)
            print(f"Trusted context: actor={ctx.actor_id} stores={','.join(ctx.store_ids)} customer={ctx.customer_id} "
                  f"case={ctx.case_id}  (never sent as tool arguments)\n")
            result = run_investigation(model, build_tools(settings, ctx), intake, settings, on_step=printer)
            if args.verbose:
                for message in result.transcript.messages:
                    print(repr(message))
    except APIError as error:
        details = describe_error(error)
        print(f"\n[{details['kind'].upper()}] HTTP {details['status_code']}: {details['message']}")
        if details["kind"] in {"transport", "server"}:
            print("The inference endpoint is unreachable or failing; this is not a model-quality problem.")
        return 1

    if args.save_transcript:
        output = Path(settings.results_dir) / f"transcript-{args.scenario or 'adhoc'}-{args.mode}-{timestamp_slug()}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"settings": settings.describe(), "intake": intake, "actor": actor,
                                      **result.to_dict()}, indent=2, default=str), encoding="utf-8")
        print(f"\nTranscript written to {output}")

    if result.mode == "chat":
        return 0
    if result.transcript.limit_hit:
        print(f"\nTool-call limit of {settings.max_tool_calls} reached; the run was cut short.")
        return 3
    return 0 if result.report and result.report.accepted else 2


if __name__ == "__main__":
    sys.exit(main())
