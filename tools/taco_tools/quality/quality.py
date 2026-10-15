#!/usr/bin/env python3
"""Demo 5.1: score sampled outcomes, flag degraded ones, and work the human review queue.

    quality score [--rescore] [--label L]     score new or changed samples, then write a report
    quality report [--since ISO] [--label L]  report on scored samples without scoring
    quality queue                             flagged samples waiting for a review
    quality show <case id | sample id>        one sample: checks, label, versions, request id, trace link
    quality review <sample id> --tone 4 --helpfulness 4 --escalation 5 --safe yes --disposition ok [--note ...]

Scores combine the deterministic checks (every sample), grade() against a label (synthetic cases only),
and the rubric reviews already recorded. Reports go to results/quality/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from taco_shared.policy import load_policy
from taco_shared.quality.checks import CHECKS, checks_passed, grade_label, run_checks, successful
from taco_shared.quality.store import DISPOSITIONS, QualityStore
from taco_shared.reporting import print_table, safe_slug, timestamp_slug

QUALITY_DB = Path(os.environ.get("QUALITY_DB") or "/data/quality/quality.db")
RESULTS = Path(os.environ.get("RESULTS_DIR") or "/app/results/quality")
GATES = Path(os.environ.get("QUALITY_GATES") or Path(__file__).resolve().parents[2] / "quality-gates.yaml")


def load_gates() -> dict[str, Any]:
    return yaml.safe_load(GATES.read_text(encoding="utf-8"))


def score_sample(store: QualityStore, sample: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    policy = load_policy((sample.get("versions") or {}).get("policy_version") or "2026.09")
    checks = run_checks(sample, policy)
    ok = checks_passed(checks)
    label = grade_label(sample)
    success = successful(sample, ok, label)
    reasons = [f"{name}: {result['detail']}" for name, result in checks.items() if result["passed"] is False]
    if label is not None and not label["passed"]:
        reasons.append("label: " + "; ".join(label["issues"][:3]))
    low = gates["flags"]["rubric_score_max"]
    for review in store.reviews(sample["sample_id"]):
        scores = [review[k] for k in ("tone", "helpfulness", "escalation") if review[k] is not None]
        if (scores and min(scores) <= low) or review["safe_to_send"] == 0:
            reasons.append(f"rubric: {review['reviewer']} scored {min(scores) if scores else '-'} "
                           f"(safe to send: {bool(review['safe_to_send'])})")
    store.save_score(sample, checks, ok, label, success, reasons)
    return {"sample_id": sample["sample_id"], "case_id": sample["case_id"], "status": sample["status"],
            "checks_passed": ok, "label_passed": None if label is None else label["passed"], "successful": success,
            "flag_reasons": reasons}


def score_pending(store: QualityStore, rescore: bool = False) -> list[dict[str, Any]]:
    gates = load_gates()
    return [score_sample(store, sample, gates) for sample in list(store.samples_to_score(rescore))]


def rate(values: list[Any]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(1 for v in known if v) / len(known), 3) if known else None


def summarize(rows: list[dict[str, Any]], gates: dict[str, Any]) -> dict[str, Any]:
    """Per model: pass rates over all rows, plus the degradation verdict over the last N final rows."""
    degradation = gates["degradation"]
    models: dict[str, Any] = {}
    for model in sorted({row["model"] or "unknown" for row in rows}):
        mine = [row for row in rows if (row["model"] or "unknown") == model]
        final = [row for row in mine if row["successful"] is not None]
        window = final[-degradation["window_samples"]:]
        checks = {name: rate([row["checks"][name]["passed"] for row in mine if name in row["checks"]]) for name in CHECKS}
        recent = {"samples": len(window), "checks_pass_rate": rate([r["checks_passed"] for r in window]),
                  "labeled_pass_rate": rate([r["label_passed"] for r in window]),
                  "escalation_rate": rate([r["escalated"] for r in window])}
        baseline = degradation["baseline"]
        reasons = []
        if len(window) >= degradation["min_samples"]:
            if recent["checks_pass_rate"] is not None and recent["checks_pass_rate"] < degradation["checks_pass_rate_min"]:
                reasons.append(f"checks pass rate {recent['checks_pass_rate']} < {degradation['checks_pass_rate_min']}")
            floor = round(baseline["labeled_pass_rate"] - degradation["labeled_pass_drop_max"], 3)
            if recent["labeled_pass_rate"] is not None and recent["labeled_pass_rate"] < floor:
                reasons.append(f"labeled pass rate {recent['labeled_pass_rate']} < baseline - "
                               f"{degradation['labeled_pass_drop_max']} = {floor}")
            ceiling = round(baseline["escalation_rate"] * degradation["escalation_rate_multiplier_max"], 3)
            if recent["escalation_rate"] is not None and recent["escalation_rate"] > ceiling:
                reasons.append(f"escalation rate {recent['escalation_rate']} > {ceiling}")
        models[model] = {
            "samples": len(mine), "final": len(final),
            "checks_pass_rate": rate([row["checks_passed"] for row in mine]),
            "labeled": sum(1 for row in mine if row["label_passed"] is not None),
            "labeled_pass_rate": rate([row["label_passed"] for row in mine]),
            "successful": sum(1 for row in final if row["successful"]),
            "successful_rate": rate([row["successful"] for row in final]),
            "escalation_rate": rate([row["escalated"] for row in final]),
            "flagged": sum(1 for row in mine if row["flagged"]), "checks": checks,
            "recent": recent, "degraded": bool(reasons), "degraded_reasons": reasons,
        }
    return models


def markdown(label: str, summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [f"# Quality report: {label}", "", "| Model | Samples | Checks pass | Labeled | Label pass | Successful "
             "| Escalated | Flagged | Recent window | Degraded |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for model, s in summary.items():
        lines.append(f"| {model} | {s['samples']} | {s['checks_pass_rate']} | {s['labeled']} | {s['labeled_pass_rate']} | "
                     f"{s['successful']}/{s['final']} | {s['escalation_rate']} | {s['flagged']} | {s['recent']['samples']} | "
                     f"{'yes: ' + '; '.join(s['degraded_reasons']) if s['degraded'] else 'no'} |")
    lines += ["", "## Checks pass rate by check", "", "| Model | " + " | ".join(CHECKS) + " |",
              "| --- |" + " --- |" * len(CHECKS)]
    for model, s in summary.items():
        lines.append(f"| {model} | " + " | ".join(str(s["checks"][name]) for name in CHECKS) + " |")
    flagged = [row for row in rows if row["flagged"]]
    lines += ["", "## Flagged samples", ""]
    lines += [f"- `{row['case_id']}` ({row.get('scenario_id') or '-'}, {row['status']}, {row['model']}): "
              + "; ".join(row["flag_reasons"]) for row in flagged] or ["None."]
    lines += ["", "Checks use only the sample's evidence; labels exist only for synthetic cases. A pass here does",
              "not prove the classification was right: that is what labels and the rubric review add.", ""]
    return "\n".join(lines)


def report(store: QualityStore, label: str, since: str | None, model: str | None) -> dict[str, Any]:
    rows = store.scored(since=since, model=model)
    summary = summarize(rows, load_gates())
    out = RESULTS / f"quality-{safe_slug(label)}-{timestamp_slug()}"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"kind": "quality", "label": label, "since": since, "model": model, "summary": summary,
               "flagged": [{k: row[k] for k in ("sample_id", "case_id", "scenario_id", "status", "model", "flag_reasons")}
                           for row in rows if row["flagged"]]}
    out.with_suffix(".json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(label, summary, rows), encoding="utf-8")
    print_table(["model", "samples", "checks pass", "label pass", "successful", "escalated", "flagged", "degraded"],
                [[m, s["samples"], s["checks_pass_rate"], s["labeled_pass_rate"], f"{s['successful']}/{s['final']}",
                  s["escalation_rate"], s["flagged"], "YES" if s["degraded"] else "no"] for m, s in summary.items()])
    for m, s in summary.items():
        for reason in s["degraded_reasons"]:
            print(f"  {m} degraded: {reason}")
    print(f"wrote {out.name}.json and .md")
    return payload


def show(store: QualityStore, sample_id: str) -> int:
    sample = store.sample(sample_id)
    if sample is None:
        print(f"no sample for {sample_id}", file=sys.stderr)
        return 1
    versions = sample.get("versions") or {}
    proposal = sample.get("proposal") or {}
    print(f"Sample {sample['sample_id']}  case {sample['case_id']}  status {sample['status']}"
          f"{' (' + sample['outcome_reason'] + ')' if sample.get('outcome_reason') else ''}  sampled: {sample['sampled_reason']}")
    print(f"  request {sample.get('request_id')}  model {versions.get('model')}  policy {versions.get('policy_version')}"
          f"  prompt {versions.get('prompt_version')}  app {versions.get('app_version')}")
    print(f"  trace {sample.get('trace_url') or 'run ids ' + ', '.join(sample.get('run_ids', []))}")
    print(f"  complaint: {sample.get('complaint')}")
    if proposal:
        print(f"  proposal: {proposal.get('complaint_category')} {proposal.get('resolution_type')} "
              f"{proposal.get('refund_amount')} items {proposal.get('affected_items')} rules {proposal.get('policy_rule_ids')} "
              f"approval {proposal.get('requires_human_approval')}")
        print(f"  customer message: {proposal.get('customer_message')}")
    score = store.score(sample["sample_id"])
    if score is None:
        print("  not scored yet")
    else:
        for name, result in score["checks"].items():
            verdict = {True: "pass", False: "FAIL", None: "n/a"}[result["passed"]]
            print(f"  check {name:<24} {verdict:<5} {result.get('detail') or ''}")
        label = score.get("label")
        print("  label: " + ("none" if label is None else ("pass" if label["passed"] else "FAIL " + "; ".join(label["issues"]))))
        print(f"  successful: {score['successful']}  flagged: {'; '.join(score['flag_reasons']) or 'no'}")
    for review in store.reviews(sample["sample_id"]):
        print(f"  review by {review['reviewer']}: tone {review['tone']} helpfulness {review['helpfulness']} escalation "
              f"{review['escalation']} safe {bool(review['safe_to_send'])} {review['disposition']} {review['note'] or ''}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    score_p = sub.add_parser("score")
    score_p.add_argument("--rescore", action="store_true", help="score every sample again")
    score_p.add_argument("--label", default="latest")
    score_p.add_argument("--since", help="report only samples created at or after this ISO time")
    report_p = sub.add_parser("report")
    report_p.add_argument("--label", default="report")
    report_p.add_argument("--since")
    report_p.add_argument("--model")
    sub.add_parser("queue")
    show_p = sub.add_parser("show")
    show_p.add_argument("sample")
    review_p = sub.add_parser("review")
    review_p.add_argument("sample")
    review_p.add_argument("--reviewer", default="qa-01")
    review_p.add_argument("--tone", type=int, required=True)
    review_p.add_argument("--helpfulness", type=int, required=True)
    review_p.add_argument("--escalation", type=int, required=True)
    review_p.add_argument("--safe", choices=["yes", "no"], required=True)
    review_p.add_argument("--disposition", choices=DISPOSITIONS, required=True)
    review_p.add_argument("--note")
    args = parser.parse_args()

    if not QUALITY_DB.exists():
        print(f"{QUALITY_DB} does not exist: set QUALITY_SAMPLE_RATE in app/.env and recreate complaint-api",
              file=sys.stderr)
        return 2
    store = QualityStore(QUALITY_DB)
    if args.command == "score":
        scored = score_pending(store, args.rescore)
        print(f"scored {len(scored)} sample(s); {sum(1 for s in scored if s['flag_reasons'])} flagged")
        report(store, args.label, args.since, None)
    elif args.command == "report":
        report(store, args.label, args.since, args.model)
    elif args.command == "queue":
        items = store.queue()
        print_table(["sample", "case", "scenario", "status", "model", "why flagged"],
                    [[i["sample_id"], i["case_id"], i.get("scenario_id") or "-", i["status"], i["model"],
                      "; ".join(i["flag_reasons"])[:110]] for i in items])
        print(f"{len(items)} sample(s) waiting for review")
    elif args.command == "show":
        return show(store, args.sample)
    elif args.command == "review":
        sample = store.sample(args.sample)
        if sample is None:
            print(f"no sample for {args.sample}", file=sys.stderr)
            return 1
        store.add_review(sample["sample_id"], args.reviewer, args.tone, args.helpfulness, args.escalation,
                         args.safe == "yes", args.disposition, args.note)
        score_sample(store, sample, load_gates())
        print(f"review saved for {sample['sample_id']} ({sample['case_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
