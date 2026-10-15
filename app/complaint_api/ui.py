"""Minimal server-rendered review page. Every value is HTML-escaped: complaint text is untrusted input."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import quote

from taco_shared.context import Actor

STYLE = """
body{font-family:system-ui,sans-serif;margin:2rem;max-width:80rem;color:#222}
table{border-collapse:collapse;margin:.5rem 0}td,th{border:1px solid #ccc;padding:.3rem .6rem;text-align:left;vertical-align:top}
.banner{background:#fff4ce;padding:.5rem;border:1px solid #e0c060;margin-bottom:1rem}
.ok{color:#176b2c}.bad{color:#a4262c}.muted{color:#666}pre{white-space:pre-wrap;background:#f6f6f6;padding:.5rem;margin:.3rem 0}
button{margin-right:.5rem;padding:.3rem .8rem}textarea{width:30rem;height:3rem}
.tabs a{margin-right:.8rem}.tabs b{margin-right:.8rem}
.kind{font-family:monospace;padding:0 .3rem;border-radius:3px;background:#eef}
.kind-tool{background:#e8f5e9}.kind-finalize,.kind-validate{background:#fff3e0}.kind-record{background:#e3f2fd}
.kind-escalate,.kind-rejected,.kind-limit{background:#fdecea}.kind-nudge,.kind-clarify{background:#f3e5f5}
details summary{cursor:pointer;color:#555}
"""

STATUSES = ("pending_review", "recorded", "escalated", "needs_clarification", "failed_retryable", "rejected",
            "investigating", "all")


def page(title: str, actor: Actor | None, body: str, message: str | None = None) -> str:
    who = f"{escape(actor.actor_id)} ({escape(actor.role)}, stores {escape(', '.join(actor.store_ids))})" if actor else "-"
    note = f"<p class='bad'>{escape(message)}</p>" if message else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title><style>{STYLE}</style>"
            f"</head><body><div class='banner'>Demo identity: <b>{who}</b>. The actor is an unauthenticated claim "
            f"(host-only, synthetic data). Inference authentication is separate.</div>"
            f"<h1>{escape(title)}</h1>{note}{body}</body></html>")


def actor_picker(actors: list[dict[str, Any]], status: str) -> str:
    links = " | ".join(f"<a href='/ui?actor={quote(a['actor_id'])}&status={quote(status)}'>{escape(a['actor_id'])}</a>"
                       f" <span class='muted'>({escape(a['role'])})</span>" for a in actors)
    return f"<p>Act as: {links}</p>"


def status_tabs(actor: Actor, current: str, counts: dict[str, int]) -> str:
    tabs = []
    for status in STATUSES:
        label = f"{status.replace('_', ' ')} ({counts.get(status, 0)})"
        tabs.append(f"<b>{escape(label)}</b>" if status == current else
                    f"<a href='/ui?actor={quote(actor.actor_id)}&status={quote(status)}'>{escape(label)}</a>")
    return f"<p class='tabs'>{''.join(tabs)}</p>"


def case_list(actor: Actor, cases: list[dict[str, Any]], status: str) -> str:
    if not cases:
        return f"<p class='muted'>No cases with status {escape(status.replace('_', ' '))} in your stores.</p>"
    rows = "".join(
        f"<tr><td><a href='/ui/cases/{quote(c['case_id'])}?actor={quote(actor.actor_id)}&back={quote(status)}'>"
        f"{escape(c['case_id'])}</a></td><td>{escape(c['status'])}</td><td>{escape(c.get('outcome_reason') or '')}</td>"
        f"<td>{escape(c['store_id'])}</td><td>v{c['proposal_version']}</td><td>{escape(c['submitted_by'])}</td>"
        f"<td>{escape(c.get('scenario_id') or '')}</td><td>{escape(c['updated_at'])}</td></tr>" for c in cases)
    return ("<table><tr><th>Case</th><th>Status</th><th>Reason</th><th>Store</th><th>Version</th><th>Submitted by</th>"
            f"<th>Scenario</th><th>Updated</th></tr>{rows}</table>")


def _cell(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return f"<pre>{escape(json.dumps(value, indent=2))}</pre>"
    return escape(str(value))


def _args(args: Any) -> str:
    return json.dumps(args, separators=(", ", ": "), default=str)


def step_summary(step: dict[str, Any]) -> str:
    """One readable line per workflow step; the full step is in the row's details."""
    kind = step.get("kind")
    if kind == "model":
        calls = [f"tool_call {c['name']} {_args(c['args'])}" for c in step.get("tool_calls", [])]
        calls += [f"invalid tool call {c.get('name')}: {c.get('error')}" for c in step.get("invalid_tool_calls", [])]
        if calls:
            return "; ".join(calls)
        content = (step.get("content") or "").strip()
        return "answer: " + (content[:200] + ("..." if len(content) > 200 else ""))
    if kind == "tool":
        if not step.get("executed", True):
            return f"{step.get('name')} not executed ({step.get('error_kind')})"
        outcome = "ok " + (step.get("record_id") or "") if step.get("status") == "ok" else f"error {step.get('error_kind')}"
        retries = f", {step['retries']} case API retries" if step.get("retries") else ""
        return f"{step.get('name')} <- {outcome}{retries}"
    if kind == "nudge":
        return "not yet called " + ", ".join(step.get("missing", []))
    if kind == "finalize":
        error = f", error: {step['error']}" if step.get("error") else ""
        return f"v{step.get('proposal_version')} ({step.get('mode')}) finish={step.get('finish_reason')}{error}"
    if kind == "validate":
        problems = step.get("schema_errors", []) + step.get("business_rule_violations", [])
        return f"v{step.get('proposal_version')}: " + ("accepted" if step.get("accepted") else "rejected: " + "; ".join(problems))
    if kind == "record":
        if step.get("conflict"):
            return f"refused by the case API: {step['conflict']}"
        action = step.get("action") or {}
        replay = " (replayed)" if step.get("replayed") else ""
        return (f"{action.get('action_id')} {action.get('type')} {action.get('amount')} "
                f"key {step.get('idempotency_key')}{replay}")
    if kind in {"clarify", "escalate"}:
        return str(step.get("reason"))
    return ""


def steps_table(steps: list[dict[str, Any]]) -> str:
    if not steps:
        return "<p class='muted'>No workflow steps recorded.</p>"
    rows = []
    for index, step in enumerate(steps, start=1):
        kind = str(step.get("kind"))
        seconds = step.get("elapsed_seconds")
        tokens = "/".join(str(step.get(k)) for k in ("input_tokens", "output_tokens") if step.get(k) is not None)
        fault = f"<br><span class='bad'>injected: {escape(step['injected_fault'])}</span>" if step.get("injected_fault") else ""
        rows.append(
            f"<tr><td>{index}</td><td><span class='kind kind-{escape(kind)}'>{escape(kind)}</span></td>"
            f"<td>{escape(step_summary(step))}{fault}<details><summary>details</summary>{_cell(step)}</details></td>"
            f"<td>{'' if seconds is None else f'{seconds:.2f}s'}</td><td>{escape(tokens)}</td></tr>")
    return ("<table><tr><th>#</th><th>Step</th><th>What happened</th><th>Time</th><th>Tokens in/out</th></tr>"
            + "".join(rows) + "</table>")


def case_detail(actor: Actor, view: dict[str, Any], back: str = "pending_review") -> str:
    proposal = view.get("proposal") or {}
    validation = view.get("validation") or {}
    accepted = validation.get("accepted")
    fields = ("order_id", "complaint_category", "resolution_type", "refund_amount", "affected_items",
              "policy_rule_ids", "requires_human_approval", "customer_message", "justification", "evidence_refs")
    proposal_rows = "".join(f"<tr><th>{escape(k)}</th><td>{_cell(proposal.get(k))}</td></tr>" for k in fields if k in proposal)
    reviews = "".join(f"<li>v{r['proposal_version']} {escape(r['decision'])} by {escape(r['reviewer'])}"
                      f"{': ' + escape(r['note']) if r.get('note') else ''} <span class='muted'>{escape(r['at'])}</span></li>"
                      for r in view.get("reviews", []))
    counters = ", ".join(f"{escape(k)}={v}" for k, v in view.get("counters", {}).items())
    parts = [
        f"<p>Status: <b>{escape(view['status'])}</b>"
        f"{' (' + escape(view['outcome_reason']) + ')' if view.get('outcome_reason') else ''} | store {escape(view['store_id'])}"
        f" | submitted by {escape(view['submitted_by'])} | proposal v{view['proposal_version']}</p>",
        f"<h2>Complaint</h2><pre>{escape(view.get('complaint') or '')}</pre>",
    ]
    if view.get("clarification_question"):
        parts.append(f"<p>Question for the customer: {escape(view['clarification_question'])}</p>")
    parts += [
        f"<h2>Proposal</h2><table>{proposal_rows or '<tr><td>none</td></tr>'}</table>",
        f"<p>Validation: <span class='{'ok' if accepted else 'bad'}'>{'accepted' if accepted else 'rejected'}</span> "
        f"{escape('; '.join(validation.get('schema_errors', []) + validation.get('business_rule_violations', [])))}</p>",
        f"<p class='muted'>{counters}</p>",
        f"<h2>Reviews</h2><ul>{reviews or '<li class=muted>none</li>'}</ul>",
    ]
    if view.get("action"):
        parts.append(f"<h2>Recorded action</h2>{_cell(view['action'])}")
    if view["status"] == "pending_review" and actor.can_review(view["store_id"]):
        parts.append(
            f"<h2>Decision</h2><form method='post' action='/ui/cases/{quote(view['case_id'])}/decision'>"
            f"<input type='hidden' name='actor' value='{escape(actor.actor_id)}'>"
            f"<input type='hidden' name='proposal_version' value='{view['proposal_version']}'>"
            f"<p><textarea name='note' placeholder='Note (required to request a revision)'></textarea></p>"
            f"<button name='decision' value='approve'>Approve</button>"
            f"<button name='decision' value='revise'>Request revision</button>"
            f"<button name='decision' value='reject'>Reject</button></form>")
    parts.append(f"<h2>Workflow steps</h2>{steps_table(view.get('steps', []))}")
    parts.append(f"<p><a href='/ui?actor={quote(actor.actor_id)}&status={quote(back)}'>Back to the case list</a></p>")
    return "".join(parts)


# Quality review (module 5) ----------------------------------------------------------------------

QUALITY_ROLES = {"quality_reviewer", "area_manager"}
RUBRIC = (("tone", "Tone: courteous, plain, no blame (1-5)"),
          ("helpfulness", "Helpfulness: says what happens next and when (1-5)"),
          ("escalation", "Escalation: hands off to a person exactly when policy or judgment requires (1-5)"))


def _verdict(value: Any) -> str:
    if value is None:
        return "<span class='muted'>n/a</span>"
    return "<span class='ok'>pass</span>" if value else "<span class='bad'>fail</span>"


def quality_list(actor: Actor, queue: list[dict[str, Any]], counts: dict[str, Any]) -> str:
    who = quote(actor.actor_id)
    summary = (f"<p class='muted'>{sum(counts['samples'].values())} samples, {counts['unscored']} not yet scored "
               f"(run the <code>quality score</code> tool), {counts['reviews']} reviews.</p>")
    if not queue:
        return summary + "<p class='muted'>No flagged samples are waiting for review.</p>"
    rows = "".join(
        f"<tr><td><a href='/ui/quality/{quote(item['sample_id'])}?actor={who}'>{escape(item['case_id'])}</a></td>"
        f"<td>{escape(item.get('scenario_id') or '')}</td><td>{escape(item['status'] or '')}</td>"
        f"<td>{escape(item['model'] or '')}</td><td>{_verdict(item['checks_passed'])}</td>"
        f"<td>{_verdict(item['label_passed'])}</td><td>{escape('; '.join(item['flag_reasons']))}</td>"
        f"<td>{escape(item['updated_at'])}</td></tr>" for item in queue)
    return (summary + "<h2>Review queue (flagged, not yet reviewed)</h2><table><tr><th>Case</th><th>Scenario</th>"
            "<th>Status</th><th>Model</th><th>Checks</th><th>Label</th><th>Why flagged</th><th>Updated</th></tr>"
            f"{rows}</table>")


def quality_detail(actor: Actor, sample: dict[str, Any], score: dict[str, Any] | None,
                   reviews: list[dict[str, Any]], dispositions: tuple[str, ...]) -> str:
    proposal = sample.get("proposal") or {}
    fields = ("order_id", "complaint_category", "resolution_type", "refund_amount", "affected_items",
              "policy_rule_ids", "requires_human_approval", "customer_message", "justification")
    proposal_rows = "".join(f"<tr><th>{escape(k)}</th><td>{_cell(proposal.get(k))}</td></tr>" for k in fields if k in proposal)
    versions = sample.get("versions") or {}
    trace = (f"<a href='{escape(sample['trace_url'])}'>open the trace in LangSmith</a>" if sample.get("trace_url")
             else f"LangSmith run ids: {escape(', '.join(sample.get('run_ids', [])))}")
    parts = [
        f"<p>Case <a href='/ui/cases/{quote(sample['case_id'])}?actor={quote(actor.actor_id)}&back=all'>"
        f"{escape(sample['case_id'])}</a> | status <b>{escape(sample['status'])}</b>"
        f"{' (' + escape(sample['outcome_reason']) + ')' if sample.get('outcome_reason') else ''}"
        f" | sampled: {escape(sample['sampled_reason'])}</p>",
        f"<p>Request <code>{escape(sample.get('request_id') or '')}</code> | {trace}</p>",
        f"<p class='muted'>Model {escape(versions.get('model', ''))}, policy {escape(versions.get('policy_version', ''))}, "
        f"prompt {escape(versions.get('prompt_version', ''))}, app {escape(versions.get('app_version', ''))}</p>",
        f"<h2>Complaint</h2><pre>{escape(sample.get('complaint') or '')}</pre>",
        f"<h2>Proposal</h2><table>{proposal_rows or '<tr><td>none</td></tr>'}</table>",
    ]
    if score is None:
        parts.append("<p class='muted'>Not scored yet: run the quality tool.</p>")
    else:
        rows = "".join(f"<tr><td>{escape(name)}</td><td>{_verdict(result['passed'])}</td>"
                       f"<td>{escape(result.get('detail') or '')}</td></tr>" for name, result in score["checks"].items())
        label = score.get("label")
        label_line = ("<p class='muted'>No label (production traffic has none).</p>" if label is None else
                      f"<p>Label: {_verdict(label.get('passed'))} {escape('; '.join(label.get('issues', [])))}</p>")
        parts += [f"<h2>Deterministic checks</h2><table><tr><th>Check</th><th>Result</th><th>Detail</th></tr>{rows}</table>",
                  label_line,
                  f"<p>Successful resolution: {_verdict(score.get('successful'))} | flagged: "
                  f"{escape('; '.join(score['flag_reasons']) or 'no')}</p>"]
    done = "".join(f"<li>{escape(r['reviewer'])}: tone {r['tone']}, helpfulness {r['helpfulness']}, escalation "
                   f"{r['escalation']}, safe to send {_verdict(r['safe_to_send'])}, {escape(r['disposition'])}"
                   f"{': ' + escape(r['note']) if r.get('note') else ''} <span class='muted'>{escape(r['at'])}</span></li>"
                   for r in reviews)
    parts.append(f"<h2>Reviews</h2><ul>{done or '<li class=muted>none</li>'}</ul>")
    if actor.role in QUALITY_ROLES:
        scores = "".join(
            f"<p>{escape(text)}: " + "".join(f"<label><input type='radio' name='{name}' value='{n}' required> {n}</label> "
                                             for n in range(1, 6)) + "</p>" for name, text in RUBRIC)
        options = "".join(f"<option value='{escape(d)}'>{escape(d)}</option>" for d in dispositions)
        parts.append(
            f"<h2>Rubric review</h2><form method='post' action='/ui/quality/{quote(sample['sample_id'])}/review'>"
            f"<input type='hidden' name='actor' value='{escape(actor.actor_id)}'>{scores}"
            f"<p>Safe to send as written: <label><input type='radio' name='safe_to_send' value='yes' required> yes</label> "
            f"<label><input type='radio' name='safe_to_send' value='no'> no</label></p>"
            f"<p>Disposition: <select name='disposition'>{options}</select></p>"
            f"<p><textarea name='note' placeholder='Note'></textarea></p><button>Save review</button></form>")
    parts.append(f"<p><a href='/ui/quality?actor={quote(actor.actor_id)}'>Back to the review queue</a></p>")
    return "".join(parts)
