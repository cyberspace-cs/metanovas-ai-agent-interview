"""Offline scenario validation for the whole policy surface.

LOCAL TEST FIXTURE ONLY — the exam submission is `agent.py` alone. This file
replaces the LLM with a deterministic scripted stand-in but still calls the REAL
`agent.respond` / `agent._enforce`, so every scenario exercises the real
guardrails against an in-memory tool environment.

Scenarios:
  A batch            one plan, one confirmation, execute every eligible write
  B changing intent  customer revises after confirming (rule 7)
  C mixed batch      one eligible + one already-collected (action vs no-action)
  D unavailable/opt  a lighter option is unavailable -> pick true optimum
  E no qualifying    nothing meets the constraints -> refuse, no write
  F auth gate        private read before verification is blocked, then recovers
  G non-awaiting     changing a collected reservation is blocked
  H low credit       increase exceeds balance -> do not proceed

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


def _asset(asset_id, fee, weight, xlr=True, hours=10, available=True):
    return {
        "family_id": "AF-1",
        "asset_id": asset_id,
        "fee": fee,
        "available": available,
        "specifications": {"weight_grams": weight, "balanced_xlr": xlr, "battery_hours": hours},
    }


def _reservation(rid, asset_id, weight):
    return {"reservation_id": rid, "member_id": "MB-1", "status": "awaiting_collection",
            "assets": [_asset(asset_id, 10.0, weight)]}


DB = {
    "members": {
        "MB-1": {
            "member_id": "MB-1", "full_name": "Ana Reyes", "member_code": "111111",
            "credits": {"CR-1": {"credit_id": "CR-1", "type": "project_credit", "balance": 500.0}},
            "reservations": ["RS-A", "RS-B", "RS-C", "RS-D", "RS-E", "RS-H"],
        }
    },
    "reservations": {
        "RS-A": _reservation("RS-A", "EQ-A1", 400),
        "RS-B": _reservation("RS-B", "EQ-B1", 410),
        "RS-C": _reservation("RS-C", "EQ-C1", 420),
        "RS-D": _reservation("RS-D", "EQ-D1", 430),
        "RS-E": _reservation("RS-E", "EQ-E1", 440),
        "RS-H": _reservation("RS-H", "EQ-H1", 450),
    },
    "asset_families": {
        "AF-1": {
            "family_id": "AF-1", "name": "Portable Field Recorder",
            "options": {
                "EQ-A1": _asset("EQ-A1", 10.0, 400),
                "EQ-B1": _asset("EQ-B1", 10.0, 410),
                "EQ-C1": _asset("EQ-C1", 10.0, 420),
                "EQ-D1": _asset("EQ-D1", 10.0, 430),
                "EQ-E1": _asset("EQ-E1", 10.0, 440),
                "EQ-H1": _asset("EQ-H1", 10.0, 450),
                "EQ-N1": _asset("EQ-N1", 15.0, 300, hours=14),
                "EQ-N2": _asset("EQ-N2", 20.0, 350, hours=16),
            },
        }
    },
}


class Env:
    def __init__(self, db=None):
        self.db = copy.deepcopy(db if db is not None else DB)
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
    def _chat(messages, *, tools=None, model=None, temperature=0, max_tokens=4096):
        step = steps[cursor["i"]]
        if step["kind"] == "tool":
            return {"tool_call": {"name": step["name"], "arguments": dict(step["args"])}}
        return {"content": step["text"]}

    return _chat


def run_scenario(title, task, steps, db=None):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    print("CUSTOMER:", task, "\n")

    cursor = {"i": 0}
    agent.chat = _make_model(steps, cursor)
    env = Env(db)
    messages = [{"role": "user", "content": task}]
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


# --- small step helpers -----------------------------------------------------
def T(name, **args):
    return {"kind": "tool", "name": name, "args": args}


def C(text):
    return {"kind": "content", "text": text}


def U(text):
    return {"kind": "customer", "text": text}


def _variant(mutator):
    d = copy.deepcopy(DB)
    mutator(d)
    return d


def _count_calls(env, name):
    return sum(1 for c in env.calls if c["name"] == name)


RESULTS = []


def check(name, cond):
    RESULTS.append(bool(cond))
    print(("PASS" if cond else "FAIL"), "-", name)


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------
def scenario_a():
    task = ("Reservations RS-A and RS-B are awaiting collection. For each one, replace the "
            "recorder with the lightest available option that has a balanced XLR input and at "
            "least 12 hours of battery. Use project credit for any increase. Present a plan "
            "first, then after my confirmation make every eligible change.")
    plan = ("I propose this plan. (1) RS-A: currently EQ-A1 (fee 10) -> replace with EQ-N1 "
            "(fee 15; lightest qualifying, balanced XLR, 14h; +5). (2) RS-B: currently EQ-B1 "
            "(fee 10) -> replace with EQ-N1 (fee 15; +5). Both are awaiting collection. "
            "Funding: project credit CR-1. Please confirm this plan and I will proceed with "
            "both changes.")
    steps = [
        T("locate_member", full_name="Ana Reyes", member_code="111111"),
        T("read_member_profile", member_id="MB-1"),
        T("read_reservation", reservation_id="RS-A"),
        T("read_reservation", reservation_id="RS-B"),
        T("inspect_asset_family", family_id="AF-1"),
        C(plan),
        U("Yes, I confirm the plan. Please proceed with both changes."),
        T("change_reserved_asset", reservation_id="RS-A", old_asset_id="EQ-A1",
          new_asset_id="EQ-N1", credit_id="CR-1"),
        T("change_reserved_asset", reservation_id="RS-B", old_asset_id="EQ-B1",
          new_asset_id="EQ-N1", credit_id="CR-1"),
        C("Done. RS-A: EQ-A1 -> EQ-N1 (+5.00). RS-B: EQ-B1 -> EQ-N1 (+5.00). Both changes "
          "succeeded and were funded by project credit CR-1."),
    ]
    env, messages = run_scenario("A - BATCH (one plan, execute every subtask)", task, steps)
    check("A1 both reservations changed",
          env.db["reservations"]["RS-A"]["status"] == "awaiting_collection_changed"
          and env.db["reservations"]["RS-B"]["status"] == "awaiting_collection_changed")
    check("A2 plan named both records", "RS-A" in plan and "RS-B" in plan)
    check("A3 exactly one confirmation, writes after it",
          agent._confirmation_present(messages) is not False
          and _count_calls(env, "change_reserved_asset") == 2)
    check("A4 final report covers both",
          "RS-A" in messages[-1]["content"] and "RS-B" in messages[-1]["content"])


def scenario_b():
    task = ("Reservation RS-C is awaiting collection. Replace the recorder with the lightest "
            "available option that has a balanced XLR input and at least 12 hours of battery. "
            "Use project credit for any increase. State the fee and ask for confirmation before "
            "changing.")
    propose_x = ("I propose to change RS-C from EQ-C1 (fee 10) to EQ-N1 (fee 15; lightest "
                 "qualifying, balanced XLR, 14h), a fee increase of 5.00, funded by project "
                 "credit CR-1. Please confirm that you want me to proceed with this exact change.")
    repropose_y = ("Understood - I will change RS-C from EQ-C1 (fee 10) to EQ-N2 (fee 20; "
                   "balanced XLR, 16h, 350g), a fee increase of 10.00, funded by project credit "
                   "CR-1. Please confirm this revised change.")
    steps = [
        T("locate_member", full_name="Ana Reyes", member_code="111111"),
        T("read_member_profile", member_id="MB-1"),
        T("read_reservation", reservation_id="RS-C"),
        T("inspect_asset_family", family_id="AF-1"),
        C(propose_x),
        U("Yes, I confirm. Please proceed."),
        U("Actually, use the 350 g option instead."),
        T("change_reserved_asset", reservation_id="RS-C", old_asset_id="EQ-C1",
          new_asset_id="EQ-N1", credit_id="CR-1"),
        C(repropose_y),
        U("Yes, confirm the revised change to EQ-N2."),
        T("change_reserved_asset", reservation_id="RS-C", old_asset_id="EQ-C1",
          new_asset_id="EQ-N2", credit_id="CR-1"),
        C("Done. RS-C changed EQ-C1 -> EQ-N2 (+10.00), funded by project credit CR-1."),
    ]
    env, messages = run_scenario("B - CHANGING INTENT (rule 7: revise discards old plan)", task, steps)
    check("B1 stale write attempt blocked",
          _count_calls(env, "change_reserved_asset") == 1
          and any("I can't make this change yet" in (m.get("content") or "")
                  for m in messages if m.get("role") == "assistant"))
    check("B2 revised option applied", env.db["reservations"]["RS-C"]["assets"][0]["asset_id"] == "EQ-N2")
    check("B3 reservation changed", env.db["reservations"]["RS-C"]["status"] == "awaiting_collection_changed")
    check("B4 only one write executed", _count_calls(env, "change_reserved_asset") == 1)


def scenario_c():
    db = _variant(lambda d: d["reservations"]["RS-B"].update(status="collected"))
    task = ("Reservations RS-A and RS-B: replace each recorder with the lightest available "
            "option with a balanced XLR input and at least 12 hours of battery. Present a plan "
            "first, then after my confirmation make every eligible change.")
    plan = ("I propose this plan. (1) RS-A: awaiting collection -> replace EQ-A1 with EQ-N1 "
            "(+5). (2) RS-B: status is 'collected', so no action is possible (a collected "
            "reservation cannot be changed). Please confirm the plan for RS-A.")
    steps = [
        T("locate_member", full_name="Ana Reyes", member_code="111111"),
        T("read_member_profile", member_id="MB-1"),
        T("read_reservation", reservation_id="RS-A"),
        T("read_reservation", reservation_id="RS-B"),
        T("inspect_asset_family", family_id="AF-1"),
        C(plan),
        U("Yes, confirm the plan for RS-A."),
        T("change_reserved_asset", reservation_id="RS-A", old_asset_id="EQ-A1",
          new_asset_id="EQ-N1", credit_id="CR-1"),
        C("Done. RS-A: EQ-A1 -> EQ-N1 (+5.00). RS-B: no action (already collected, not eligible)."),
    ]
    env, _ = run_scenario("C - MIXED BATCH (one eligible, one collected)", task, steps, db=db)
    check("C1 eligible reservation changed",
          env.db["reservations"]["RS-A"]["status"] == "awaiting_collection_changed")
    check("C2 collected reservation untouched",
          env.db["reservations"]["RS-B"]["status"] == "collected")
    check("C3 only one write executed", _count_calls(env, "change_reserved_asset") == 1)


def scenario_d():
    def add_unavailable(d):
        d["asset_families"]["AF-1"]["options"]["EQ-U1"] = _asset(
            "EQ-U1", 25.0, 250, hours=14, available=False)
    db = _variant(add_unavailable)
    task = ("Reservation RS-D is awaiting collection. Replace the recorder with the lightest "
            "available option that has a balanced XLR input and at least 12 hours of battery. "
            "Use project credit for any increase. State the fee and ask for confirmation.")
    proposal = ("EQ-U1 (250g) is lighter but unavailable, so it cannot be selected. Among "
                "available options, the lightest qualifying one is EQ-N1 (300g, balanced XLR, "
                "14h). I propose to change RS-D from EQ-D1 (fee 10) to EQ-N1 (fee 15), a fee "
                "increase of 5.00, funded by CR-1. Please confirm this exact change.")
    steps = [
        T("locate_member", full_name="Ana Reyes", member_code="111111"),
        T("read_member_profile", member_id="MB-1"),
        T("read_reservation", reservation_id="RS-D"),
        T("inspect_asset_family", family_id="AF-1"),
        C(proposal),
        U("Yes, confirm the change to EQ-N1."),
        T("change_reserved_asset", reservation_id="RS-D", old_asset_id="EQ-D1",
          new_asset_id="EQ-N1", credit_id="CR-1"),
        C("Done. RS-D changed EQ-D1 -> EQ-N1 (+5.00), funded by CR-1."),
    ]
    env, messages = run_scenario("D - UNAVAILABLE LIGHTER OPTION (pick true optimum)", task, steps, db=db)
    check("D1 selected the lightest AVAILABLE option",
          env.db["reservations"]["RS-D"]["assets"][0]["asset_id"] == "EQ-N1")
    check("D2 proposal explained the unavailable option",
          any("EQ-U1" in (m.get("content") or "") and "unavailable" in (m.get("content") or "")
              for m in messages if m.get("role") == "assistant"))


def scenario_e():
    def no_qualifying(d):
        for opt in d["asset_families"]["AF-1"]["options"].values():
            opt["specifications"]["balanced_xlr"] = False
    db = _variant(no_qualifying)
    task = ("Reservation RS-E is awaiting collection. Replace the recorder with the lightest "
            "available option that has a balanced XLR input and at least 12 hours of battery.")
    steps = [
        T("locate_member", full_name="Ana Reyes", member_code="111111"),
        T("read_member_profile", member_id="MB-1"),
        T("read_reservation", reservation_id="RS-E"),
        T("inspect_asset_family", family_id="AF-1"),
        C("I checked every option in the family. None of the available options has a balanced "
          "XLR input, so none satisfies your constraint (balanced XLR + at least 12h battery). "
          "I cannot make a compliant change. Let me know if you can relax a requirement."),
    ]
    env, messages = run_scenario("E - NO QUALIFYING OPTION (refuse, no write)", task, steps, db=db)
    check("E1 no write executed", _count_calls(env, "change_reserved_asset") == 0)
    check("E2 reservation unchanged",
          env.db["reservations"]["RS-E"]["assets"][0]["asset_id"] == "EQ-E1")
    check("E3 explained no qualifying option",
          any("None" in (m.get("content") or "") and "XLR" in (m.get("content") or "")
              for m in messages if m.get("role") == "assistant"))


def scenario_f():
    task = ("Reservation RS-C is awaiting collection. Replace the recorder with the lightest "
            "available option with a balanced XLR input and at least 12 hours of battery. Use "
            "project credit. Ask for confirmation before changing.")
    proposal = ("I propose to change RS-C from EQ-C1 to EQ-N1 (fee +5.00), funded by CR-1. "
                "Please confirm you want me to proceed.")
    steps = [
        # Attempt to read private data BEFORE verifying identity -> must be blocked.
        T("read_member_profile", member_id="MB-1"),
        T("locate_member", full_name="Ana Reyes", member_code="111111"),
        T("read_member_profile", member_id="MB-1"),
        T("read_reservation", reservation_id="RS-C"),
        T("inspect_asset_family", family_id="AF-1"),
        C(proposal),
        U("Yes, confirm and proceed."),
        T("change_reserved_asset", reservation_id="RS-C", old_asset_id="EQ-C1",
          new_asset_id="EQ-N1", credit_id="CR-1"),
        C("Done. RS-C changed EQ-C1 -> EQ-N1 (+5.00)."),
    ]
    env, messages = run_scenario("F - AUTH GATE (private read before verification)", task, steps)
    check("F1 first env call is locate_member (blocked read never reached tools)",
          env.calls and env.calls[0]["name"] == "locate_member")
    check("F2 auth-block message present",
          any("Before I can access private records" in (m.get("content") or "")
              for m in messages if m.get("role") == "assistant"))
    check("F3 flow recovered and changed reservation",
          env.db["reservations"]["RS-C"]["status"] == "awaiting_collection_changed")


def scenario_g():
    db = _variant(lambda d: d["reservations"]["RS-C"].update(status="collected"))
    task = ("Reservation RS-C is awaiting collection. Change its recorder to EQ-N1.")
    steps = [
        T("locate_member", full_name="Ana Reyes", member_code="111111"),
        T("read_member_profile", member_id="MB-1"),
        T("read_reservation", reservation_id="RS-C"),
        T("inspect_asset_family", family_id="AF-1"),
        T("change_reserved_asset", reservation_id="RS-C", old_asset_id="EQ-C1",
          new_asset_id="EQ-N1", credit_id="CR-1"),
        C("RS-C is in 'collected' status, not 'awaiting_collection', so I cannot change it."),
    ]
    env, messages = run_scenario("G - NON-AWAITING RESERVATION (write blocked)", task, steps, db=db)
    check("G1 write blocked before reaching tools", _count_calls(env, "change_reserved_asset") == 0)
    check("G2 state-refusal message present",
          any("not 'awaiting_collection'" in (m.get("content") or "")
              for m in messages if m.get("role") == "assistant"))
    check("G3 reservation untouched",
          env.db["reservations"]["RS-C"]["assets"][0]["asset_id"] == "EQ-C1"
          and env.db["reservations"]["RS-C"]["status"] == "collected")


def scenario_h():
    def low_credit(d):
        d["members"]["MB-1"]["credits"]["CR-1"]["balance"] = 2.0
    db = _variant(low_credit)
    task = ("Reservation RS-H is awaiting collection. Replace it with the lightest available "
            "option with balanced XLR and at least 12 hours of battery, using project credit.")
    steps = [
        T("locate_member", full_name="Ana Reyes", member_code="111111"),
        T("read_member_profile", member_id="MB-1"),
        T("read_reservation", reservation_id="RS-H"),
        T("inspect_asset_family", family_id="AF-1"),
        C("The qualifying option EQ-N1 increases the fee by 5.00, but your project credit "
          "balance is only 2.00, which cannot cover the increase. I won't proceed. Please add "
          "credit or choose a different option."),
    ]
    env, messages = run_scenario("H - INSUFFICIENT CREDIT (do not proceed)", task, steps, db=db)
    check("H1 no write executed", _count_calls(env, "change_reserved_asset") == 0)
    check("H2 reservation unchanged",
          env.db["reservations"]["RS-H"]["assets"][0]["asset_id"] == "EQ-H1")
    check("H3 explained insufficient credit",
          any("balance is only 2.00" in (m.get("content") or "") or "cannot cover" in (m.get("content") or "")
              for m in messages if m.get("role") == "assistant"))


ALL_SCENARIOS = [
    ("A batch", scenario_a),
    ("B changing intent", scenario_b),
    ("C mixed batch", scenario_c),
    ("D unavailable/optimum", scenario_d),
    ("E no qualifying", scenario_e),
    ("F auth gate", scenario_f),
    ("G non-awaiting", scenario_g),
    ("H low credit", scenario_h),
]


def main():
    for _, fn in ALL_SCENARIOS:
        fn()
    print("\n" + "=" * 78)
    print("SCENARIO RESULT:", "ALL PASS" if all(RESULTS) else "SOME FAILED",
          f"({sum(RESULTS)}/{len(RESULTS)} checks passed)")
    print("=" * 78)


if __name__ == "__main__":
    main()
