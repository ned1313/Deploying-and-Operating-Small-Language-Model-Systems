"""Build the shared dataset from the complaint CSV.

    python -m taco_shared.dataset.build            # rewrite taco_shared/data/generated/
    python -m taco_shared.dataset.build --check    # rebuild in memory and fail if the committed files differ

Every random choice uses a per-row RNG seeded from (dataset_seed, source_row), so editing one row or
one curated scenario never shifts another row's synthesized data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .. import paths
from ..paths import load_json
from ..policy import load_policy
from ..policy_engine import evaluate

SCREENING_VERSION = "screening-v2"
AGENT_VERSION = "agent-v1"
CUSTOMER_ID = re.compile(r"^CUST-\d{5}$")
REQUIRED_FIELDS = ("customer_id", "date", "location", "category", "sub_category", "channel", "message")

RATES = {
    "filing_window": 0.03,      # share of order-defect rows filed 15-30 days after the order
    "prior_history": 0.20,      # share of rows whose customer has earlier resolutions
    "already_compensated": 0.02,  # share of order-defect rows with a resolution on the same order
    "recovered_wrong_address": 0.40,
}
DELIVERY_CHANNELS = ["DoorDash", "Uber Eats", "Website", "Mobile App"]
PICKUP_CHANNELS = ["Website", "Mobile App", "Phone", "In store"]
DELIVERY_SUBCATEGORIES = {"Late delivery", "Wrong address/dropoff", "Damaged/spilled", "Order not received"}
DINE_IN_SUBCATEGORIES = {"Dining area", "Restroom", "Rude staff", "Ignored customer", "Slow service"}
ITEM_CATEGORIES = {"missing_items", "wrong_item", "food_quality"}
DELIVERY_FEE = Decimal("3.99")


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def as_float(value: Decimal) -> float:
    return float(value)


class Reference:
    def __init__(self) -> None:
        menu = load_json(paths.DATA_DIR / "menu.json")
        self.menu = {item["name"]: item for item in menu["items"]}
        self.customization_prefixes = tuple(menu["customization_prefixes"])
        self.aliases = sorted(((alias, item["name"]) for item in menu["items"] for alias in item["aliases"]),
                              key=lambda pair: -len(pair[0]))
        self.random_items = [name for name in self.menu if name != "Taco Family Pack"]
        self.stores = {store["location"]: store for store in load_json(paths.DATA_DIR / "stores.json")["stores"]}
        self.store_by_id = {store["store_id"]: store for store in self.stores.values()}
        self.category_map = {(m["category"], m["sub_category"]): m
                             for m in load_json(paths.DATA_DIR / "category_map.json")["mappings"]}

    def mentioned_item(self, message: str) -> str | None:
        text = message.lower()
        for alias, name in self.aliases:
            for match in re.finditer(re.escape(alias), text):
                prefix = text[max(0, match.start() - 6):match.start()]
                if not prefix.endswith(self.customization_prefixes):
                    return name
        return None


def read_csv(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(raw).hexdigest()
    rows = list(csv.DictReader(raw.decode("utf-8").splitlines()))
    kept, dropped = [], []
    today_limit = date(2027, 1, 1)
    for index, row in enumerate(rows):
        reason = None
        missing = [name for name in REQUIRED_FIELDS if not (row.get(name) or "").strip()]
        if missing:
            reason = f"empty {', '.join(missing)}"
        elif not CUSTOMER_ID.match(row["customer_id"]):
            reason = f"malformed customer_id {row['customer_id']!r}"
        else:
            try:
                parsed = date.fromisoformat(row["date"])
            except ValueError:
                reason = f"impossible date {row['date']!r}"
            else:
                if parsed >= today_limit:
                    reason = f"future date {row['date']!r}"
        if reason:
            dropped.append({"source_row": index, "customer_id": row.get("customer_id"), "reason": reason})
        else:
            kept.append({"source_row": index, **{key: (value or "").strip() for key, value in row.items()}})
    return kept, dropped, digest


def order_id_for(source_row: int) -> str:
    return f"TA-{10001 + source_row}"


def case_id_for(order_id: str) -> str:
    return f"CASE-{order_id.split('-')[1]}"


def choose_fulfillment(rng: random.Random, row: dict[str, Any]) -> tuple[str, str]:
    sub, channel = row["sub_category"], row["channel"]
    if sub in DELIVERY_SUBCATEGORIES or (sub == "Missing item" and channel in {"DoorDash", "Uber Eats"}):
        return "delivery", channel if channel in DELIVERY_CHANNELS else rng.choice(DELIVERY_CHANNELS)
    if sub in DINE_IN_SUBCATEGORIES or channel == "In-store":
        return "dine-in", "In store"
    if channel in {"DoorDash", "Uber Eats"}:
        return "delivery", channel
    if channel in {"Website", "Mobile App"}:
        return rng.choice(["delivery", "pickup"]), channel
    fulfillment = rng.choice(["delivery", "pickup"])
    return fulfillment, rng.choice(DELIVERY_CHANNELS if fulfillment == "delivery" else PICKUP_CHANNELS)


def price_order(ref: Reference, store: dict[str, Any], lines: list[tuple[str, int]], fulfillment: str) -> dict[str, Any]:
    items = [{"name": name, "quantity": qty, "unit_price": as_float(Decimal(str(ref.menu[name]["unit_price"])))}
             for name, qty in lines]
    subtotal = money(sum((Decimal(str(i["unit_price"])) * i["quantity"] for i in items), Decimal("0")))
    tax = money(subtotal * Decimal(str(store["tax_rate"])))
    fee = DELIVERY_FEE if fulfillment == "delivery" else Decimal("0")
    return {"items": items, "subtotal": as_float(subtotal), "tax": as_float(tax), "delivery_fee": as_float(fee),
            "total": as_float(subtotal + tax + fee)}


def synthesize(ref: Reference, row: dict[str, Any], seed: str, curated: dict[str, Any] | None) -> dict[str, Any]:
    rng = random.Random(f"{seed}:{row['source_row']}")
    curated = curated or {}
    spec = curated.get("order", {})
    store = ref.stores[row["location"]]
    complaint_date = date.fromisoformat(row["date"])
    mapping = ref.category_map[(row["category"], row["sub_category"])]
    message = curated.get("message") or row["message"]
    mentioned = ref.mentioned_item(message)
    category = mapping["policy_category"]
    if mapping.get("safety_keywords") and any(word in message.lower() for word in mapping["safety_keywords"]):
        if row["sub_category"] != "Undercooked/overcooked" or (mentioned and ref.menu[mentioned].get("meat")):
            category = "food_safety"

    fulfillment, channel = choose_fulfillment(rng, row)
    fulfillment, channel = spec.get("fulfillment", fulfillment), spec.get("channel", channel)

    if "items" in spec:
        lines = [(name, qty) for name, qty in spec["items"]]
    else:
        count = rng.randint(1, 4)
        names = rng.sample(ref.random_items, count)
        lines = [(name, rng.randint(1, 3)) for name in names]
        if mentioned:
            lines = [(name, qty) for name, qty in lines if name != mentioned]
            mentioned_qty = 2 if "one of the" in message.lower() else 1
            lines.insert(rng.randint(0, len(lines)), (mentioned, mentioned_qty))
        if category == "missing_items" and len(lines) == 1:
            extra = rng.choice([name for name in ref.random_items if name != lines[0][0]])
            lines.append((extra, 1))

    window = not curated and category in {"missing_items", "wrong_item", "late_delivery", "order_not_received",
                                          "wrong_address", "food_quality"} and rng.random() < RATES["filing_window"]
    default_offset = 0 if curated else rng.choice([0, 0, 0, 1])
    offset = spec.get("order_date_offset_days", rng.randint(15, 30) if window else default_offset)
    order_date = complaint_date - timedelta(days=offset)
    order_id = order_id_for(row["source_row"])

    order = {"order_id": order_id, "customer_id": row["customer_id"], "store_id": store["store_id"],
             "location": row["location"], "order_date": order_date.isoformat(), "channel": channel,
             "fulfillment": fulfillment, **price_order(ref, store, lines, fulfillment),
             "promised_delivery_time": None, "actual_delivery_time": None}

    recovered = None
    if fulfillment == "delivery":
        promised = datetime.combine(order_date, datetime.min.time()) + timedelta(hours=rng.randint(11, 20),
                                                                                 minutes=rng.choice([0, 15, 30, 45]))
        if category == "late_delivery":
            stated = re.search(r"(\d+) minutes later", message)
            late = int(stated.group(1)) if stated else rng.choice([rng.randint(5, 14), rng.randint(15, 30), rng.randint(31, 70)])
        elif category == "wrong_address":
            recovered = rng.random() < RATES["recovered_wrong_address"]
            late = rng.randint(31, 60) if recovered else -2
        else:
            late = rng.randint(-5, 5)
        if "minutes_late" in spec:
            late = spec["minutes_late"]
        order["promised_delivery_time"] = promised.isoformat()
        if not (category == "order_not_received" or late is None):
            order["actual_delivery_time"] = (promised + timedelta(minutes=late)).isoformat()

    facts: dict[str, Any] = {"category": category, "affected": []}
    if recovered is not None:
        facts["recovered"] = recovered
    if category in ITEM_CATEGORIES or (category == "food_safety" and mentioned):
        if mentioned:
            facts["affected"] = [{"name": mentioned, "quantity": 1}]
        elif category in ITEM_CATEGORIES:
            facts["ambiguous"] = "complaint does not name a specific menu item"
    if "facts" in curated:
        facts = {"category": category, **curated["facts"]}

    priors: list[dict[str, Any]] = []
    for index, prior in enumerate(curated.get("prior_resolutions", [])):
        priors.append(_prior(row, order, index, prior["days_before"], prior.get("order_id"),
                             prior["resolution_type"], prior["amount"], prior["reason"], complaint_date))
    if not curated:
        if rng.random() < RATES["prior_history"]:
            for index in range(rng.randint(1, 3)):
                kind = rng.choice([("partial_refund", rng.choice([3.95, 4.25, 4.50, 6.50]), "missing item"),
                                   ("store_credit", 5.00, "late delivery"),
                                   ("partial_refund", rng.choice([8.75, 9.75, 10.25]), "food quality")])
                priors.append(_prior(row, order, index, rng.randint(1, 180), f"TA-9{rng.randint(1000, 9999)}",
                                     kind[0], kind[1], kind[2], complaint_date))
        if category in ITEM_CATEGORIES and facts["affected"] and rng.random() < RATES["already_compensated"]:
            item = facts["affected"][0]["name"]
            priors.append(_prior(row, order, len(priors), offset, "same", "partial_refund",
                                 ref.menu[item]["unit_price"], "missing item", complaint_date))

    complaint = f"{message} Order {order_id}."
    return {"row": row, "order": order, "facts": facts, "priors": priors, "complaint": complaint,
            "message": message, "store": store}


def _prior(row: dict[str, Any], order: dict[str, Any], index: int, days_before: int, order_ref: str | None,
           resolution_type: str, amount: float, reason: str, complaint_date: date) -> dict[str, Any]:
    order_id = order["order_id"] if order_ref == "same" else (order_ref or f"TA-9{(row['source_row'] * 7 + index * 131) % 9000 + 1000}")
    resolution_id = f"RES-{row['source_row']:04d}-{index + 1}"
    return {"resolution_id": resolution_id, "record_id": f"resolution:{resolution_id}", "customer_id": row["customer_id"],
            "date": (complaint_date - timedelta(days=days_before)).isoformat(), "order_id": order_id,
            "resolution_type": resolution_type, "amount": amount, "reason": reason}


def visible_priors(all_priors: list[dict[str, Any]], customer_id: str, complaint_date: str) -> list[dict[str, Any]]:
    return sorted((p for p in all_priors if p["customer_id"] == customer_id and p["date"] <= complaint_date),
                  key=lambda p: (p["date"], p["resolution_id"]))


def prompt_prior(prior: dict[str, Any]) -> dict[str, Any]:
    return {key: prior[key] for key in ("date", "order_id", "resolution_type", "amount", "reason")}


def prompt_order(order: dict[str, Any]) -> dict[str, Any]:
    keys = ("order_id", "order_date", "channel", "fulfillment", "store_id", "location", "items", "subtotal", "tax",
            "delivery_fee", "total", "promised_delivery_time", "actual_delivery_time")
    return {key: order[key] for key in keys}


def build(source: Path = paths.SOURCE_CSV) -> dict[str, Any]:
    ref = Reference()
    curated_doc = load_json(paths.CURATED_SCENARIOS)
    seed = curated_doc["dataset_seed"]
    curated_by_row = {entry["source_row"]: entry for entry in curated_doc["scenarios"]}
    kept, dropped, digest = read_csv(source)
    kept_rows = {row["source_row"] for row in kept}
    missing_sources = [entry["id"] for entry in curated_doc["scenarios"] if entry["source_row"] not in kept_rows]
    if missing_sources:
        raise ValueError(f"curated scenarios point at dropped rows: {missing_sources}")

    built = [synthesize(ref, row, seed, curated_by_row.get(row["source_row"])) for row in kept]
    all_priors = [prior for item in built for prior in item["priors"]]
    policy = load_policy()

    complaints, orders, cases, customers = [], [], [], {}
    unmatched: dict[str, int] = {}
    for item in built:
        row, order = item["row"], item["order"]
        priors = visible_priors(all_priors, row["customer_id"], row["date"])
        label = evaluate(item["facts"], order, priors, row["date"])
        curated = curated_by_row.get(row["source_row"])
        same_order = [p for p in priors if p["order_id"] == order["order_id"]]
        case = {"case_id": case_id_for(order["order_id"]), "order_id": order["order_id"],
                "customer_id": row["customer_id"], "store_id": order["store_id"],
                "status": "resolved" if same_order else "open", "opened_at": row["date"],
                "recorded_actions": [{"action_id": f"ACT-{p['resolution_id'][4:]}", "type": p["resolution_type"],
                                      "amount": p["amount"], "recorded_at": p["date"]} for p in same_order],
                "notes": f"Customer contacted support via {row['channel']}."}
        if label["status"] == "ambiguous" and "menu item" in label["note"]:
            unmatched[row["sub_category"]] = unmatched.get(row["sub_category"], 0) + 1
        complaints.append({
            "source_row": row["source_row"], "scenario_id": curated["id"] if curated else None,
            "customer_id": row["customer_id"], "complaint_date": row["date"], "store_id": order["store_id"],
            "location": row["location"], "csv_category": row["category"], "csv_sub_category": row["sub_category"],
            "complaint_channel": row["channel"], "tone_urgency": row["tone_urgency"], "message": item["message"],
            "complaint": item["complaint"], "order_id": order["order_id"], "case_id": case["case_id"],
            "policy_category": item["facts"]["category"], "facts": item["facts"],
            "label_status": label["status"], "label_note": label["note"], "expected": label["expected"],
        })
        orders.append(order)
        cases.append(case)
        customers.setdefault(row["customer_id"], {"customer_id": row["customer_id"], "home_store_id": order["store_id"]})

    by_row = {c["source_row"]: c for c in complaints}
    orders_by_id = {o["order_id"]: o for o in orders}
    screening, agent = [], []
    for entry in curated_doc["scenarios"]:
        complaint = by_row[entry["source_row"]]
        if complaint["label_status"] != "labeled":
            raise ValueError(f"curated scenario {entry['id']} is not labeled: {complaint['label_note']}")
        order = orders_by_id[complaint["order_id"]]
        priors = visible_priors(all_priors, complaint["customer_id"], complaint["complaint_date"])
        common = {"scenario_id": entry["id"], "label": entry["label"], "source_row": entry["source_row"],
                  "complaint_date": complaint["complaint_date"], "customer_id": complaint["customer_id"],
                  "store_id": complaint["store_id"], "case_id": complaint["case_id"]}
        if not entry.get("agent_only"):
            screening.append({**common, "complaint": complaint["complaint"], "order": prompt_order(order),
                              "prior_resolutions": [prompt_prior(p) for p in priors], "expected": complaint["expected"]})
        agent.append({
            "scenario_id": entry["id"], "label": entry["label"], "source_row": entry["source_row"],
            "actor_id": entry["actor_id"],
            "intake": {"case_id": complaint["case_id"], "customer_id": complaint["customer_id"],
                       "complaint_date": complaint["complaint_date"], "complaint_channel": complaint["complaint_channel"],
                       "complaint": complaint["complaint"]},
            "expected": complaint["expected"],
            "expected_tools": {"required": ["get_order", "get_prior_resolutions", "get_resolution_policy"],
                               "order_id": complaint["order_id"]},
        })

    statuses: dict[str, int] = {}
    for c in complaints:
        statuses[c["label_status"]] = statuses.get(c["label_status"], 0) + 1
    report = {
        "source": "taco_shared/data/source/taco_alley_customer_complaints.csv",
        "source_sha256_lf": digest, "dataset_seed": seed, "policy_version": policy.version,
        "screening_version": SCREENING_VERSION, "agent_version": AGENT_VERSION, "rates": RATES,
        "rows_read": len(kept) + len(dropped), "rows_kept": len(kept), "rows_dropped": len(dropped),
        "dropped": dropped, "label_status_counts": dict(sorted(statuses.items())),
        "ambiguous_by_sub_category": dict(sorted(unmatched.items())),
        "curated_scenarios": len(curated_doc["scenarios"]), "screening_scenarios": len(screening),
        "agent_scenarios": len(agent), "orders": len(orders), "prior_resolutions": len(all_priors),
    }
    return {
        "complaints.jsonl": complaints,
        "customers.json": sorted(customers.values(), key=lambda c: c["customer_id"]),
        "orders.json": orders,
        "prior_resolutions.json": sorted(all_priors, key=lambda p: p["resolution_id"]),
        "cases.json": cases,
        "screening_scenarios.json": {"dataset_version": SCREENING_VERSION, "policy_version": policy.version,
                                     "description": "Curated Taco Alley complaints with the order record and prior resolutions inlined. Generated by taco_shared.dataset.build; do not edit.",
                                     "scenarios": screening},
        "agent_scenarios.json": {"dataset_version": AGENT_VERSION, "policy_version": policy.version,
                                 "description": "The same curated scenarios in agent form: facts are reachable only through tools. Generated by taco_shared.dataset.build; do not edit.",
                                 "scenarios": agent},
        "build_report.json": report,
    }


def serialize(name: str, content: Any) -> str:
    if name.endswith(".jsonl"):
        return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in content)
    return json.dumps(content, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Fail if committed generated files differ from a rebuild.")
    parser.add_argument("--output", type=Path, default=paths.GENERATED_DIR)
    args = parser.parse_args()
    outputs = {name: serialize(name, content) for name, content in build().items()}
    if args.check:
        stale = [name for name, text in outputs.items()
                 if not (args.output / name).is_file() or (args.output / name).read_text(encoding="utf-8") != text]
        print("generated data is up to date" if not stale else f"stale generated files: {stale}")
        return 1 if stale else 0
    args.output.mkdir(parents=True, exist_ok=True)
    for name, text in outputs.items():
        (args.output / name).write_text(text, encoding="utf-8", newline="\n")
    report = json.loads(outputs["build_report.json"])
    print(json.dumps({key: report[key] for key in ("rows_kept", "rows_dropped", "label_status_counts",
                                                   "ambiguous_by_sub_category", "screening_scenarios",
                                                   "agent_scenarios", "prior_resolutions")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
