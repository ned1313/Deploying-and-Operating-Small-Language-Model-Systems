"""Resolution proposal schemas (v1 for module 1 screening, v2 for the module 2 agent) and validation.

The JSON schemas are the source of truth: proposal.v1.json is maintained by hand and proposal.v2.json
is generated from it by `build_v2_schema` (run `python -m taco_shared.proposal` to rewrite it).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from .paths import DEFAULT_SCHEMA, PROPOSAL_SCHEMA_V2, load_json

TOOL_NAMES = ["get_order", "get_prior_resolutions", "get_resolution_policy", "get_case_status"]
MONETARY_TYPES = {"store_credit", "partial_refund", "full_refund"}
ESCALATION_CATEGORIES = {"food_safety", "other"}
APPROVAL_THRESHOLD = 25.00
RECENT_DAYS = 90
FILING_WINDOW_DAYS = 14


def load_schema_v1() -> dict[str, Any]:
    return load_json(DEFAULT_SCHEMA)


def build_v2_schema(v1: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(v1)
    schema["title"] = "TacoAlleyResolutionProposalV2"
    extra = {
        "case_id": {"type": "string", "description": "The case_id from the intake."},
        "policy_version": {"type": "string", "description": "policy_version returned by get_resolution_policy."},
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "description": "record_id values from the tool results that support this proposal.",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": TOOL_NAMES},
                    "record_id": {"type": "string"},
                },
                "required": ["tool", "record_id"],
                "additionalProperties": False,
            },
        },
        "clarification_or_escalation_reason": {
            "type": "string",
            "maxLength": 300,
            "description": "Why clarification or escalation is needed. Empty string if neither applies.",
        },
    }
    schema["properties"] = {**{"case_id": extra["case_id"]}, **schema["properties"],
                            **{key: value for key, value in extra.items() if key != "case_id"}}
    schema["required"] = ["case_id", *schema["required"], "policy_version", "evidence_refs",
                          "clarification_or_escalation_reason"]
    return schema


def load_schema_v2() -> dict[str, Any]:
    return load_json(PROPOSAL_SCHEMA_V2)


class EvidenceRef(BaseModel):
    tool: Literal["get_order", "get_prior_resolutions", "get_resolution_policy", "get_case_status"]
    record_id: str


class ProposalV1(BaseModel):
    order_id: str
    complaint_category: str
    resolution_type: str
    refund_amount: float
    affected_items: list[str]
    policy_rule_ids: list[str]
    requires_human_approval: bool
    customer_message: str
    justification: str


class ProposalV2(ProposalV1):
    case_id: str
    policy_version: str
    evidence_refs: list[EvidenceRef]
    clarification_or_escalation_reason: str


@dataclass
class Evidence:
    """Facts the agent actually retrieved, extracted from tool results."""

    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    prior_resolutions: list[dict[str, Any]] | None = None
    policy_version: str | None = None
    case: dict[str, Any] | None = None
    record_ids: set[str] = field(default_factory=set)

    def add(self, tool: str, result: Any) -> None:
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                return
        if not isinstance(result, dict) or result.get("status") != "ok":
            return
        _collect_record_ids(result, self.record_ids)
        if tool == "get_order":
            order = result["order"]
            self.orders[order["order_id"]] = order
        elif tool == "get_prior_resolutions":
            self.prior_resolutions = (self.prior_resolutions or []) + list(result["resolutions"])
        elif tool == "get_resolution_policy":
            self.policy_version = result["policy_version"]
        elif tool == "get_case_status":
            self.case = result["case"]


def _collect_record_ids(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("record_id"), str):
            found.add(value["record_id"])
        for item in value.values():
            _collect_record_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_record_ids(item, found)


@dataclass
class ValidationReport:
    schema_errors: list[str]
    business_rule_violations: list[str]

    @property
    def accepted(self) -> bool:
        return not self.schema_errors and not self.business_rule_violations

    def to_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "schema_errors": self.schema_errors,
                "business_rule_violations": self.business_rule_violations}


def schema_errors(proposal: Any, schema: dict[str, Any]) -> list[str]:
    return [f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
            for error in Draft202012Validator(schema).iter_errors(proposal)]


def validate_proposal(proposal: Any, evidence: Evidence, complaint_date: str,
                      schema: dict[str, Any] | None = None) -> ValidationReport:
    """Schema plus deterministic business rules, checked only against retrieved evidence."""
    errors = schema_errors(proposal, schema or load_schema_v2())
    if not isinstance(proposal, dict):
        return ValidationReport(errors or ["proposal is not a JSON object"], [])
    violations: list[str] = []
    resolution = proposal.get("resolution_type")
    amount = proposal.get("refund_amount")
    amount = float(amount) if isinstance(amount, (int, float)) and not isinstance(amount, bool) else None
    monetary = resolution in MONETARY_TYPES and (amount or 0) > 0
    category = proposal.get("complaint_category")

    order = evidence.orders.get(proposal.get("order_id"))
    if order is None:
        violations.append("order_id was not returned by get_order in this investigation")
    else:
        names = {item["name"] for item in order["items"]}
        unknown = [item for item in proposal.get("affected_items") or [] if item not in names]
        if unknown:
            violations.append(f"affected_items not on the order: {unknown}")
        if amount is not None and amount > float(order["total"]) + 0.005:
            violations.append(f"refund_amount {amount} exceeds the order total {order['total']} (R11)")
        days = (date.fromisoformat(complaint_date[:10]) - date.fromisoformat(order["order_date"][:10])).days
        if days > FILING_WINDOW_DAYS and monetary and category not in ESCALATION_CATEGORIES:
            violations.append(f"complaint filed {days} days after the order; no monetary resolution (R12)")

    if resolution not in MONETARY_TYPES and amount not in (None, 0, 0.0):
        violations.append(f"{resolution} must have refund_amount 0")

    if evidence.policy_version is None:
        violations.append("policy was not retrieved with get_resolution_policy")
    elif proposal.get("policy_version") != evidence.policy_version:
        violations.append(f"policy_version should be {evidence.policy_version}")

    refs = proposal.get("evidence_refs") or []
    missing_refs = [ref.get("record_id") for ref in refs if isinstance(ref, dict)
                    and ref.get("record_id") not in evidence.record_ids]
    if missing_refs:
        violations.append(f"evidence_refs not found in tool results: {missing_refs}")

    if category in ESCALATION_CATEGORIES and resolution != "escalate_to_manager":
        violations.append(f"{category} complaints must be escalate_to_manager (R7/R14)")

    if monetary and amount is not None and amount > APPROVAL_THRESHOLD and proposal.get("requires_human_approval") is not True:
        violations.append("monetary resolution over 25.00 requires human approval (R9)")
    if monetary and evidence.prior_resolutions is not None and proposal.get("requires_human_approval") is not True:
        complaint = date.fromisoformat(complaint_date[:10])
        recent = sum(1 for p in evidence.prior_resolutions
                     if 0 <= (complaint - date.fromisoformat(p["date"][:10])).days <= RECENT_DAYS)
        if recent >= 2:
            violations.append(f"customer has {recent} recent resolutions; requires human approval (R10)")

    order_id = proposal.get("order_id")
    compensated = any(p.get("order_id") == order_id and (p.get("amount") or 0) > 0
                      for p in evidence.prior_resolutions or [])
    if evidence.case and evidence.case.get("order_id") == order_id:
        compensated = compensated or any((a.get("amount") or 0) > 0 for a in evidence.case.get("recorded_actions", []))
    if compensated and monetary:
        violations.append("this order was already compensated; no further monetary resolution (R13)")

    return ValidationReport(errors, violations)


if __name__ == "__main__":
    PROPOSAL_SCHEMA_V2.write_text(json.dumps(build_v2_schema(load_schema_v1()), indent=2) + "\n",
                                  encoding="utf-8", newline="\n")
    print(f"Wrote {PROPOSAL_SCHEMA_V2}")
