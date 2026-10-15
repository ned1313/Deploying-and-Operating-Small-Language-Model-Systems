#!/usr/bin/env python3
"""Demo 5.2: compare a candidate release with the pinned baseline against the release gates.

    release --baseline r1-qwen3-14b --candidate r2-llama-3.1-8b

Each release's runs are found by label (run them with `--label <release id>`): the newest
results/e2e/e2e-*-<id>-*.json, results/failure/failure-tests-<id>-*.json, and
results/baseline/baseline-<id>-*.json. Gates are in quality-gates.yaml. Writes
results/release/release-<candidate>-vs-<baseline>-<ts>.{json,md}; exits 1 when the candidate is rejected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from taco_shared.reporting import safe_slug, timestamp_slug
from taco_tools.quality.quality import load_gates

RESULTS_ROOT = Path(os.environ.get("RESULTS_ROOT") or "/app/results")
RELEASES = Path(os.environ.get("RELEASES_DIR") or "/app/releases")


def newest(pattern: str) -> Path | None:
    matches = sorted(RESULTS_ROOT.glob(pattern), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def evidence(release_id: str) -> dict[str, Any]:
    label = safe_slug(release_id)
    files = {"e2e": newest(f"e2e/e2e-*-{label}-*.json"), "failure": newest(f"failure/failure-tests-{label}-*.json"),
             "baseline": newest(f"baseline/baseline-{label}-*.json")}
    data: dict[str, Any] = {"release_id": release_id, "files": {k: str(v.relative_to(RESULTS_ROOT)) if v else None
                                                                 for k, v in files.items()}}
    if files["e2e"]:
        e2e = json.loads(files["e2e"].read_text(encoding="utf-8"))
        data["e2e"] = {"model": e2e.get("model"), "rates": e2e["rates"], "outcomes": e2e["outcomes"],
                       "latency_seconds": e2e.get("latency_seconds"),
                       "failed": [r["scenario_id"] for r in e2e["rows"] if not (r.get("grades") or {}).get("passed")]}
    if files["failure"]:
        rows = json.loads(files["failure"].read_text(encoding="utf-8"))["rows"]
        data["failure"] = {"total": len(rows), "passed": sum(1 for r in rows if r["passed"]),
                           "failed": [r["id"] for r in rows if not r["passed"]]}
    if files["baseline"]:
        base = json.loads(files["baseline"].read_text(encoding="utf-8"))
        metrics = {name: value[0] for name, value in base["metrics"].items()}
        outcomes = base["grouped"].get("app.workflow_outcomes", {})
        data["load"] = {"workflow_p95_s": metrics.get("app.workflow_run_p95_s"),
                        "vllm_e2e_p95_s": metrics.get("vllm.e2e_p95_s"),
                        "peak_vram_gib": metrics.get("gpu.vram_used_max_gib"),
                        "failed_requests": (base["locust"].get("Aggregated") or {}).get("failures"),
                        "failed_retryable": outcomes.get("failed_retryable", 0),
                        "recorded": outcomes.get("recorded")}
    manifest = RELEASES / f"{release_id}.json"
    if manifest.exists():
        data["manifest"] = json.loads(manifest.read_text(encoding="utf-8"))
    return data


def gate(name: str, value: Any, threshold: str, ok: bool | None) -> dict[str, Any]:
    return {"gate": name, "value": value, "threshold": threshold, "result": {True: "pass", False: "FAIL", None: "missing"}[ok]}


def evaluate(baseline: dict[str, Any], candidate: dict[str, Any], gates: dict[str, Any]) -> list[dict[str, Any]]:
    g = gates["release"]
    results = []
    b_rates, c_rates = (baseline.get("e2e") or {}).get("rates"), (candidate.get("e2e") or {}).get("rates")
    if b_rates and c_rates:
        floor = max(b_rates["passed"] - g["e2e_pass_rate_drop_max"], g["e2e_pass_rate_min"])
        results.append(gate("e2e labeled pass rate", round(c_rates["passed"], 3), f">= {floor:.2f}",
                            c_rates["passed"] >= floor - 1e-9))
        floor = b_rates["policy_compliant"] - g["policy_compliant_drop_max"]
        results.append(gate("policy compliant", round(c_rates["policy_compliant"], 3), f">= {floor:.2f}",
                            c_rates["policy_compliant"] >= floor - 1e-9))
        for key in g["perfect"]:
            results.append(gate(key, c_rates.get(key), "= 1.00", c_rates.get(key) == 1.0))
    else:
        results.append(gate("e2e labeled pass rate", None, "e2e-eval results for both releases", None))
    failure = candidate.get("failure")
    results.append(gate("failure tests", f"{failure['passed']}/{failure['total']}" if failure else None, "all pass",
                        failure["passed"] == failure["total"] if failure else None))
    b_load, c_load = baseline.get("load"), candidate.get("load")
    if b_load and c_load and b_load.get("workflow_p95_s") and c_load.get("workflow_p95_s") is not None:
        ceiling = b_load["workflow_p95_s"] * g["workflow_p95_ratio_max"]
        results.append(gate("app steady load: workflow p95 (s)", c_load["workflow_p95_s"], f"<= {ceiling:.1f}",
                            c_load["workflow_p95_s"] <= ceiling))
    else:
        results.append(gate("app steady load: workflow p95 (s)", None, "baseline reports for both releases", None))
    if c_load:
        errors = (c_load.get("failed_requests") or 0, c_load.get("failed_retryable") or 0)
        results.append(gate("app steady load: errors", f"{errors[0]} failed requests, {errors[1]} failed_retryable",
                            f"<= {g['load_failed_requests_max']}, <= {g['load_failed_retryable_max']}",
                            errors[0] <= g["load_failed_requests_max"] and errors[1] <= g["load_failed_retryable_max"]))
        vram = c_load.get("peak_vram_gib")
        results.append(gate("peak VRAM (GiB)", vram, f"<= {g['peak_vram_gib_max']}",
                            None if vram is None else vram <= g["peak_vram_gib_max"]))
    return results


def markdown(report: dict[str, Any]) -> str:
    b, c = report["baseline"], report["candidate"]
    lines = [f"# Release gate: {c['release_id']} vs {b['release_id']}", "", f"**Decision: {report['decision']}**", "",
             "| Gate | Candidate | Threshold | Result |", "| --- | --- | --- | --- |"]
    lines += [f"| {g['gate']} | {g['value']} | {g['threshold']} | {g['result']} |" for g in report["gates"]]
    lines += ["", "| Measure | Baseline | Candidate |", "| --- | --- | --- |"]
    for label, path in (("model", ("e2e", "model")), ("e2e passed", ("e2e", "rates", "passed")),
                        ("policy compliant", ("e2e", "rates", "policy_compliant")),
                        ("e2e processing p95 (s)", ("e2e", "latency_seconds", "p95")),
                        ("load workflow p95 (s)", ("load", "workflow_p95_s")), ("vLLM e2e p95 (s)", ("load", "vllm_e2e_p95_s")),
                        ("peak VRAM (GiB)", ("load", "peak_vram_gib")), ("failure tests failed", ("failure", "failed"))):
        def pick(data: dict[str, Any]) -> Any:
            for key in path:
                data = (data or {}).get(key) if isinstance(data, dict) else None
            return data
        lines.append(f"| {label} | {pick(b)} | {pick(c)} |")
    lines += ["", f"Candidate e2e failures: {(c.get('e2e') or {}).get('failed')}",
              f"Baseline e2e failures: {(b.get('e2e') or {}).get('failed')}", "",
              "24 labeled scenarios and a short load run give wide confidence intervals: a gate that passes or",
              "fails by one scenario is a reason to rerun, not proof.", "",
              f"Files: baseline {b['files']}, candidate {c['files']}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", required=True, help="release id of the pinned baseline, e.g. r1-qwen3-14b")
    parser.add_argument("--candidate", required=True, help="release id of the candidate")
    args = parser.parse_args()
    gates = load_gates()
    baseline, candidate = evidence(args.baseline), evidence(args.candidate)
    results = evaluate(baseline, candidate, gates)
    failed = [g["gate"] for g in results if g["result"] == "FAIL"]
    missing = [g["gate"] for g in results if g["result"] == "missing"]
    decision = "REJECT" if failed else ("INCOMPLETE" if missing else "PROMOTE")
    report = {"kind": "release", "decision": decision, "failed": failed, "missing": missing, "gates": results,
              "baseline": baseline, "candidate": candidate}
    out = RESULTS_ROOT / "release" / f"release-{safe_slug(args.candidate)}-vs-{safe_slug(args.baseline)}-{timestamp_slug()}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    print(f"wrote {out.name}.json and .md")
    return 0 if decision == "PROMOTE" else 1


if __name__ == "__main__":
    sys.exit(main())
