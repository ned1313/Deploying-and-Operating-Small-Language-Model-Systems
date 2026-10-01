"""Read-only investigation tools for the complaint agent.

Tools are built per request so the trusted context (actor, store scope, customer, case) is bound in a
closure and never appears in the schema the model sees.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field, ValidationError

from ..case_api_client import CaseApiClient, CaseApiUnavailable
from ..context import AgentContext
from ..db import AppDatabase
from ..policy import Policy

ComplaintCategory = Literal["missing_items", "wrong_item", "late_delivery", "order_not_received", "wrong_address",
                            "food_quality", "food_safety", "cleanliness", "staff_conduct", "other"]


class GetOrderArgs(BaseModel):
    order_id: str = Field(description="Order id quoted by the customer, for example TA-10421.", pattern=r"^TA-\d{5}$")


class GetPriorResolutionsArgs(BaseModel):
    lookback_days: int = Field(default=90, ge=1, le=365, description="How many days of history to return.")


class GetResolutionPolicyArgs(BaseModel):
    complaint_category: ComplaintCategory = Field(description="The complaint category to look up rules for.")


class GetCaseStatusArgs(BaseModel):
    pass


def dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def error_result(kind: str, message: str) -> str:
    return dumps({"status": "error", "error_kind": kind, "message": message})


def _validation_message(error: ValidationError) -> str:
    details = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in error.errors())
    return error_result("invalid_arguments", details)


def build_investigation_tools(ctx: AgentContext, db: AppDatabase, cases: CaseApiClient,
                              policy: Policy) -> list[StructuredTool]:
    def get_order(order_id: str) -> str:
        order = db.get_order(order_id, ctx.customer_id, ctx.store_ids)
        if order is None:
            # Missing and out-of-scope orders look the same to the model.
            raise ToolException(error_result("not_found", f"No order {order_id} found for this customer."))
        return dumps({"status": "ok", "record_id": f"order:{order_id}", "order": order})

    def get_prior_resolutions(lookback_days: int = 90) -> str:
        rows = db.prior_resolutions(ctx.customer_id, ctx.complaint_date, lookback_days)
        resolutions = [{**row, "record_id": f"resolution:{row['resolution_id']}"} for row in rows]
        return dumps({"status": "ok", "record_id": f"resolutions:{ctx.customer_id}:{lookback_days}d",
                      "as_of": ctx.complaint_date, "lookback_days": lookback_days, "resolutions": resolutions})

    def get_resolution_policy(complaint_category: str) -> str:
        rules = [{"id": rule["id"], "record_id": f"policy:{policy.version}#{rule['id']}", "text": rule["text"]}
                 for rule in policy.rules_for_category(complaint_category)]
        return dumps({"status": "ok", "record_id": f"policy:{policy.version}", "policy_version": policy.version,
                      "complaint_category": complaint_category, "definitions": list(policy.definitions),
                      "rules": rules, "default": policy.closing})

    def get_case_status() -> str:
        try:
            case = cases.get_case(ctx.case_id, ctx.store_ids)
        except CaseApiUnavailable as error:
            raise ToolException(error_result("unavailable", str(error))) from error
        if case is None:
            raise ToolException(error_result("not_found", f"No case {ctx.case_id} found."))
        case["recorded_actions"] = [{**a, "record_id": f"action:{a['action_id']}"} for a in case["recorded_actions"]]
        return dumps({"status": "ok", "record_id": f"case:{ctx.case_id}", "case": case})

    def tool(func: Any, name: str, description: str, schema: type[BaseModel]) -> StructuredTool:
        return StructuredTool.from_function(
            func, name=name, description=description, args_schema=schema,
            handle_tool_error=lambda error: str(error),
            handle_validation_error=_validation_message,
        )

    return [
        tool(get_order, "get_order",
             "Look up an order by the order id the customer quoted. Returns items, prices, totals, and delivery times.",
             GetOrderArgs),
        tool(get_prior_resolutions, "get_prior_resolutions",
             "List this customer's earlier complaint resolutions (refunds and credits) up to the complaint date.",
             GetPriorResolutionsArgs),
        tool(get_resolution_policy, "get_resolution_policy",
             "Get the current resolution policy rules that apply to a complaint category.",
             GetResolutionPolicyArgs),
        tool(get_case_status, "get_case_status",
             "Get the status of this complaint's case, including any actions already recorded for the order.",
             GetCaseStatusArgs),
    ]
