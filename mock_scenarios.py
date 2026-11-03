"""Offline scenario validation for BATCH and CHANGING-INTENT flows.

This is a LOCAL TEST FIXTURE ONLY (not part of the submission). It replaces the
LLM with a deterministic scripted stand-in, but still calls the REAL
`agent.respond` and therefore exercises the real guardrails in `agent._enforce`.

Scenario A — BATCH:
    Two reservations are awaiting collection. The agent must inspect both,
    present one order-by-order plan, get ONE confirmation, then execute every
    eligible write (one per turn) and report what was done.

Scenario B — CHANGING INTENT (rule 7):
    The agent proposes option X, the customer confirms, then the customer
    revises to option Y "instead". The old confirmation must be invalidated
    (a stale write attempt is blocked), the plan recalculated, and the change
    applied only after a fresh confirmation.

Usage:
    python mock_scenarios.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import agent


POLICY = (Path(__file__).resolve().parent / "practice_policy.md").read_text(encoding="utf-8")

S = {"type": "string"}


def tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOLS = [
    tool("locate_member", "Verify and locate a member by full name and six-digit code.",
         {"full_name": S, "member_code": S}, ["full_name", "member_code"]),
    tool("read_member_profile", "Read the verified member's credits and reservations.",
         {"member_id": S}, ["member_id"]),
    tool("read_reservation", "Read one reservation's state and reserved assets.",
         {"reservation_id": S}, ["reservation_id"]),
    tool("inspect_asset_family", "List specifications, fees, and availability for one family.",
         {"family_id": S}, ["family_id"]),
    tool("change_reserved_asset", "After fresh confirmation, replace an asset on an awaiting reservation.",
         {"reservation_id": S, "old_asset_id": S, "new_asset_id": S, "credit_id": S},
         ["reservation_id", "old_asset_id", "new_asset_id", "credit_id"]),
]


def _asset(asset_id, fee, weight, xlr=True, hours=10):
    return {
        "family_id": "AF-1",
        "asset_id": asset_id,
        "fee": fee,
        "available": True,
        "specifications": {"weight_grams": weight, "balanced_xlr": xlr, "battery_hours": hours},
    }


DB = {
    "members": {
        "MB-1": {
            "member_id": "MB-1",
            "full_name": "Ana Reyes",
            "member_code": "111111",
            "credits": {"CR-1": {"credit_id": "CR-1", "type": "project_credit", "balance": 500.0}},
            "reservations": ["RS-A", "RS-B", "RS-C"],
        }
    },
    "reservations": {
        "RS-A": {"reservation_id": "RS-A", "member_id": "MB-1", "status": "awaiting_collection",
                 "assets": [_asset("EQ-A1", 10.0, 400)]},
        "RS-B": {"reservation_id": "RS-B", "member_id": "MB-1", "status": "awaiting_collection",
                 "assets": [_asset("EQ-B1", 10.0, 410)]},
        "RS-C": {"reservation_id": "RS-C", "member_id": "MB-1", "status": "awaiting_collection",
                 "assets": [_asset("EQ-C1", 10.0, 420)]},
    },
    "asset_families": {
        "AF-1": {
            "family_id": "AF-1",
            "name": "Portable Field Recorder",
            "options": {
                "EQ-A1": _asset("EQ-A1", 10.0, 400),
                "EQ-B1": _asset("EQ-B1", 10.0, 410),
                "EQ-C1": _asset("EQ-C1", 10.0, 420),
                "EQ-N1": _asset("EQ-N1", 15.0, 300, hours=14),   # lightest qualifying
                "EQ-N2": _asset("EQ-N2", 20.0, 350, hours=16),   # second qualifying
            },
        }
    },
}


class Env:
    def __init__(self):
        self.db = copy.deepcopy(DB)
        self.member_id = None
        self.calls = []

    def _member(self, member_id):
        if self.member_id != member_id:
            raise ValueError("Verify this member before reading private records")
        return self.db["members"][member_id]

    def call(self, name, arguments, *, write_authorized=False):
        self.calls.append({"name": name, "args": copy.deepcopy(arguments)})
        try:
            if name == "locate_member":
                for mid, member in self.db["members"].items():
                    if (member["full_name"].casefold() == arguments["full_name"].casefold()
                            and member["member_code"] == arguments["member_code"]):
                        self.member_id = mid
                        return mid
                raise ValueError("Member not found")
            if name == "read_member_profile":
                return copy.deepcopy(self._member(arguments["member_id"]))
            if name == "read_reservation":
                reservation = self.db["reservations"][arguments["reservation_id"]]
                self._member(reservation["member_id"])
                return copy.deepcopy(reservation)
            if name == "inspect_asset_family":
                if not self.member_id:
                    raise ValueError("Verify a member before inspecting options")
                return copy.deepcopy(self.db["asset_families"][arguments["family_id"]])
            if name == "change_reserved_asset":
                if not write_authorized:
                    raise ValueError("A fresh explicit confirmation is required")
                reservation = self.db["reservations"][arguments["reservation_id"]]
                member = self._member(reservation["member_id"])
                if reservation["status"] != "awaiting_collection":
                    raise ValueError("Reservation is not awaiting collection")
                current = next((a for a in reservation["assets"]
                                if a["asset_id"] == arguments["old_asset_id"]), None)
                if not current:
                    raise ValueError("Current asset not found")
                family = self.db["asset_families"][current["family_id"]]
                replacement = family["options"].get(arguments["new_asset_id"])
                if not replacement or not replacement["available"]:
                    raise ValueError("Replacement is unavailable or in another family")
                credit = member["credits"].get(arguments["credit_id"])
                increase = round(replacement["fee"] - current["fee"], 2)
                if not credit or credit["balance"] < max(increase, 0):
                    raise ValueError("Project credit cannot cover the increase")
                credit["balance"] = round(credit["balance"] - max(increase, 0), 2)
                reservation["assets"] = [{
                    "family_id": current["family_id"],
                    "asset_id": replacement["asset_id"],
                    "name": family["name"],
                    "fee": replacement["fee"],
                    "specifications": replacement["specifications"],
                }]
                reservation["status"] = "awaiting_collection_changed"
                return copy.deepcopy(reservation)
            raise ValueError("Unknown tool")
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": str(exc)}


def _make_model(steps, cursor):
    """Return a chat() that emits `steps[cursor['i']]`. The driver advances the
    cursor after each turn, so exactly one action is produced per respond() call.
    """

    def _chat(messages, *, tools=None, model=None, temperature=0, max_tokens=4096):
        step = steps[cursor["i"]]
        if step["kind"] == "tool":
            return {"tool_call": {"name": step["name"], "arguments": dict(step["args"])}}
        return {"content": step["text"]}

    return _chat


def _log(messages, kind, text):
    print(f"AGENT {kind}: {text}")


def run_scenario(title, task_prompt, steps):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    print("CUSTOMER:", task_prompt, "\n")

    cursor = {"i": 0}
    agent.chat = _make_model(steps, cursor)
    env = Env()
    messages = [{"role": "user", "content": task_prompt}]
    confirmed = False

    while cursor["i"] < len(steps):
        step = steps[cursor["i"]]
        if step["kind"] == "customer":
            text = step["text"]
            messages.append({"role": "user", "content": text})
            print("CUSTOMER:", text)
            low = text.casefold()
            if agent._REVISION_RE.search(low) and not agent._CONFIRM_RE.search(low):
                confirmed = False
            if agent._CONFIRM_RE.search(low):
                confirmed = True
            cursor["i"] += 1
            continue

        answer = agent.respond(messages=messages, tools=TOOLS, policy=POLICY)
        if answer.get("tool_call"):
            call = answer["tool_call"]
            result = env.call(call["name"], call.get("arguments", {}), write_authorized=confirmed)
            cid = f"call_{cursor['i']}"
            print("AGENT TOOL:", call["name"], json.dumps(call.get("arguments", {})))
            if isinstance(result, dict) and "error" in result:
                print("  -> ERROR:", result["error"])
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [{"id": cid, "type": "function",
                                "function": {"name": call["name"],
                                             "arguments": json.dumps(call.get("arguments", {}))}}],
            })
            messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(result)})
        else:
            text = answer.get("content", "")
            print("AGENT:", text)
            messages.append({"role": "assistant", "content": text})
        cursor["i"] += 1

    return env, messages


def _count_calls(env, name):
    return sum(1 for c in env.calls if c["name"] == name)


def _find_change_index(messages, reservation_id):
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", tc)
            if fn.get("name") == "change_reserved_asset":
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                if args.get("reservation_id") == reservation_id:
                    return i
    return -1


def _first_confirm_index(messages):
    for i, m in enumerate(messages):
        if m.get("role") == "user" and agent._CONFIRM_RE.search((m.get("content") or "").casefold()):
            return i
    return -1


RESULTS = []


def check(name, cond):
    RESULTS.append(cond)
    print(("PASS" if cond else "FAIL"), "-", name)


def batch_scenario():
    task = (
        "Reservations RS-A and RS-B are awaiting collection. For each one, replace the "
        "recorder with the lightest available option that has a balanced XLR input and at "
        "least 12 hours of battery. Use project credit for any increase. Present a plan "
        "first, then after my confirmation make every eligible change."
    )
    plan = (
        "I propose this plan. (1) RS-A: currently EQ-A1 (fee 10) -> replace with EQ-N1 "
        "(fee 15; lightest qualifying option with balanced XLR and 14h battery; fee +5). "
        "(2) RS-B: currently EQ-B1 (fee 10) -> replace with EQ-N1 (fee 15; +5). Both are "
        "awaiting collection. Funding: project credit CR-1. Please confirm this plan and I "
        "will proceed with both changes."
    )
    steps = [
        {"kind": "tool", "name": "locate_member",
         "args": {"full_name": "Ana Reyes", "member_code": "111111"}},
        {"kind": "tool", "name": "read_member_profile", "args": {"member_id": "MB-1"}},
        {"kind": "tool", "name": "read_reservation", "args": {"reservation_id": "RS-A"}},
        {"kind": "tool", "name": "read_reservation", "args": {"reservation_id": "RS-B"}},
        {"kind": "tool", "name": "inspect_asset_family", "args": {"family_id": "AF-1"}},
        {"kind": "content", "text": plan},
        {"kind": "customer", "text": "Yes, I confirm the plan. Please proceed with both changes."},
        {"kind": "tool", "name": "change_reserved_asset",
         "args": {"reservation_id": "RS-A", "old_asset_id": "EQ-A1",
                  "new_asset_id": "EQ-N1", "credit_id": "CR-1"}},
        {"kind": "tool", "name": "change_reserved_asset",
         "args": {"reservation_id": "RS-B", "old_asset_id": "EQ-B1",
                  "new_asset_id": "EQ-N1", "credit_id": "CR-1"}},
        {"kind": "content",
         "text": "Done. RS-A: EQ-A1 -> EQ-N1 (+5.00). RS-B: EQ-B1 -> EQ-N1 (+5.00). "
                 "Both changes succeeded and were funded by project credit CR-1."},
    ]
    env, messages = run_scenario("SCENARIO A - BATCH (one plan, execute every subtask)", task, steps)

    check("A1 both reservations changed",
          env.db["reservations"]["RS-A"]["status"] == "awaiting_collection_changed"
          and env.db["reservations"]["RS-B"]["status"] == "awaiting_collection_changed")
    check("A2 plan named both records", "RS-A" in plan and "RS-B" in plan)
    check("A3 exactly one confirmation before writes",
          _first_confirm_index(messages) >= 0
          and _find_change_index(messages, "RS-A") > _first_confirm_index(messages)
          and _find_change_index(messages, "RS-B") > _first_confirm_index(messages))
    check("A4 two writes actually applied", _count_calls(env, "change_reserved_asset") == 2)
    check("A5 final report covers both", "RS-A" in messages[-1]["content"]
          and "RS-B" in messages[-1]["content"])


def revision_scenario():
    task = (
        "Reservation RS-C is awaiting collection. Replace the recorder with the lightest "
        "available option that has a balanced XLR input and at least 12 hours of battery. "
        "Use project credit for any increase. State the fee and ask for confirmation before "
        "changing."
    )
    propose_x = (
        "I propose to change RS-C from EQ-C1 (fee 10) to EQ-N1 (fee 15; lightest qualifying, "
        "balanced XLR, 14h battery), a fee increase of 5.00, funded by project credit CR-1. "
        "Please confirm that you want me to proceed with this exact change."
    )
    repropose_y = (
        "Understood - I will change RS-C from EQ-C1 (fee 10) to EQ-N2 (fee 20; balanced XLR, "
        "16h battery, 350g), a fee increase of 10.00, funded by project credit CR-1. Please "
        "confirm this revised change."
    )
    steps = [
        {"kind": "tool", "name": "locate_member",
         "args": {"full_name": "Ana Reyes", "member_code": "111111"}},
        {"kind": "tool", "name": "read_member_profile", "args": {"member_id": "MB-1"}},
        {"kind": "tool", "name": "read_reservation", "args": {"reservation_id": "RS-C"}},
        {"kind": "tool", "name": "inspect_asset_family", "args": {"family_id": "AF-1"}},
        {"kind": "content", "text": propose_x},
        {"kind": "customer", "text": "Yes, I confirm. Please proceed."},
        {"kind": "customer", "text": "Actually, use the 350 g option instead."},
        # Stale write attempt: must be blocked (old confirmation invalidated).
        {"kind": "tool", "name": "change_reserved_asset",
         "args": {"reservation_id": "RS-C", "old_asset_id": "EQ-C1",
                  "new_asset_id": "EQ-N1", "credit_id": "CR-1"}},
        {"kind": "content", "text": repropose_y},
        {"kind": "customer", "text": "Yes, confirm the revised change to EQ-N2."},
        {"kind": "tool", "name": "change_reserved_asset",
         "args": {"reservation_id": "RS-C", "old_asset_id": "EQ-C1",
                  "new_asset_id": "EQ-N2", "credit_id": "CR-1"}},
        {"kind": "content",
         "text": "Done. RS-C changed EQ-C1 -> EQ-N2 (+10.00), funded by project credit CR-1."},
    ]
    env, messages = run_scenario("SCENARIO B - CHANGING INTENT (rule 7: revise discards old plan)", task, steps)

    # The stale attempt was blocked: no change for RS-C before the re-proposal.
    final_asset = env.db["reservations"]["RS-C"]["assets"][0]["asset_id"]

    check("B1 stale write attempt was blocked",
          _count_calls(env, "change_reserved_asset") == 1
          and any("I can't make this change yet" in (m.get("content") or "")
                  for m in messages if m.get("role") == "assistant"))
    check("B2 revised option applied, not the stale one", final_asset == "EQ-N2")
    check("B3 reservation is now changed",
          env.db["reservations"]["RS-C"]["status"] == "awaiting_collection_changed")
    check("B4 only one write actually executed", _count_calls(env, "change_reserved_asset") == 1)


def main():
    batch_scenario()
    revision_scenario()
    print("\n" + "=" * 78)
    print("SCENARIO RESULT:", "ALL PASS" if all(RESULTS) else "SOME FAILED")
    print("=" * 78)


if __name__ == "__main__":
    main()
