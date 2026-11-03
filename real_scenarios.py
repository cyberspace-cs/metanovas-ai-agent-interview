"""Real-model rehearsal for the rotated evaluation patterns.

LOCAL ONLY — never submit this file. It drives the real `agent.respond` through
`interview_sdk.chat` against the in-memory tools from `mock_scenarios.py`, with a
small rule-based customer that confirms plans and (in scenario 3) revises the
batch after the first write.

Run with the same INTERVIEW_* environment variables as practice_runner.py.
"""

from __future__ import annotations

import json

import agent
from mock_scenarios import DB, POLICY, TOOLS, Env, _variant


def _assistant_message(answer, call_id=None):
    message = {"role": "assistant", "content": answer.get("content", "")}
    if call_id is not None:
        call = answer["tool_call"]
        message["tool_calls"] = [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call.get("arguments", {})),
            },
        }]
    if answer.get("_reasoning_content"):
        message["reasoning_content"] = answer["_reasoning_content"]
    return message


def run_real(title, task, *, db=None, revision=None, max_turns=42):
    print("\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78)
    print("CUSTOMER:", task)
    env = Env(db)
    messages = [{"role": "user", "content": task}]
    confirmed = False
    revision_sent = False
    writes = 0
    final_text = ""
    last_signature = None
    repeated = 0

    for step in range(max_turns):
        answer = agent.respond(messages=messages, tools=TOOLS, policy=POLICY)
        signature = json.dumps(answer, ensure_ascii=False, sort_keys=True, default=str)
        repeated = repeated + 1 if signature == last_signature else 0
        last_signature = signature
        if repeated >= 2:
            print("STOP: model repeated the same answer three times")
            break

        if answer.get("tool_call"):
            call = answer["tool_call"]
            args = call.get("arguments", {})
            print("AGENT TOOL:", call["name"], json.dumps(args, ensure_ascii=False))
            result = env.call(call["name"], args, write_authorized=confirmed)
            if isinstance(result, dict) and "error" in result:
                print("  -> ERROR:", result["error"])
            call_id = f"real_{step}"
            messages.append(_assistant_message(answer, call_id))
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": json.dumps(result, ensure_ascii=False)})
            if call["name"] == "change_reserved_asset" and not (
                    isinstance(result, dict) and "error" in result):
                writes += 1
                if revision and writes == 1 and not revision_sent:
                    print("CUSTOMER:", revision)
                    messages.append({"role": "user", "content": revision})
                    revision_sent = True
                    confirmed = False
            continue

        final_text = answer.get("content", "")
        print("AGENT:", final_text)
        messages.append(_assistant_message(answer))
        folded = final_text.casefold()
        if agent._GUARDRAIL_RE.search(folded):
            reply = "Please reassess my latest instruction and show me the updated plan."
            print("CUSTOMER:", reply)
            messages.append({"role": "user", "content": reply})
            confirmed = False
            continue
        if agent._CONFIRM_REQUEST_RE.search(folded):
            reply = "Yes, I confirm the exact plan you just described. Please proceed."
            print("CUSTOMER:", reply)
            messages.append({"role": "user", "content": reply})
            confirmed = True
            continue
        if agent._NEUTRAL_RE.search(folded):
            continue
        break

    return env, messages, final_text


def _show_result(name, checks):
    print("\n" + name)
    for label, cond in checks:
        print(("PASS" if cond else "FAIL"), "-", label)
    return all(cond for _, cond in checks)


def coupled_variant():
    def add_unavailable(d):
        from mock_scenarios import _asset
        d["asset_families"]["AF-1"]["options"]["EQ-U1"] = _asset(
            "EQ-U1", 25.0, 250, hours=14, available=False)
    task = (
        "You are Ana Reyes. Your six-digit member code is 111111. "
        "Reservation RS-D is awaiting collection. Replace its recorder with the lightest "
        "AVAILABLE option that has a balanced XLR input and at least 12 hours of battery. "
        "The fee increase must not exceed 6.00 and must be paid from project credit. State "
        "the exact fee and option, then ask for my confirmation before changing it."
    )
    env, _, final = run_real("REAL 1 - COUPLED VARIANT RESOLUTION", task,
                             db=_variant(add_unavailable))
    asset = env.db["reservations"]["RS-D"]["assets"][0]["asset_id"]
    return _show_result("REAL 1 CHECKS", [
        ("selected EQ-N1 (all constraints jointly)", asset == "EQ-N1"),
        ("did not select unavailable/over-budget options", asset not in ("EQ-U1", "EQ-N2")),
        ("final report names selected option and fee", "EQ-N1" in final and "5" in final),
    ])


def conditional_matrix():
    db = _variant(lambda d: d["reservations"]["RS-B"].update(status="collected"))
    task = (
        "You are Ana Reyes. Your six-digit member code is 111111. "
        "For each of reservations RS-A, RS-B and RS-C, inspect its current state. If and only "
        "if it is awaiting collection, replace its recorder with the lightest available option "
        "that has balanced XLR and at least 12 hours of battery, with a fee increase no more "
        "than 6.00 paid from project credit. Otherwise take no action. Give me a per-reservation "
        "plan first, then after my confirmation complete every eligible change and reconcile all "
        "three records in your final answer."
    )
    env, _, final = run_real("REAL 2 - CONDITIONAL ACTION MATRIX", task, db=db)
    return _show_result("REAL 2 CHECKS", [
        ("RS-A changed", env.db["reservations"]["RS-A"]["status"] == "awaiting_collection_changed"),
        ("RS-B left collected", env.db["reservations"]["RS-B"]["status"] == "collected"),
        ("RS-C changed", env.db["reservations"]["RS-C"]["status"] == "awaiting_collection_changed"),
        ("exactly two writes", sum(c["name"] == "change_reserved_asset" for c in env.calls) == 2),
        ("final reconciles all three", all(x in final for x in ("RS-A", "RS-B", "RS-C"))),
    ])


def batch_replan():
    task = (
        "You are Ana Reyes. Your six-digit member code is 111111. "
        "Reservations RS-A, RS-B and RS-C are awaiting collection. For each one, replace its "
        "recorder with EQ-N1 and pay any fee increase from project credit. Present the full "
        "plan first, then after my confirmation make every eligible change."
    )
    revision = (
        "Actually, change the plan for the remaining reservations: use EQ-N2 for RS-B instead, "
        "and leave RS-C unchanged. Do not repeat or undo RS-A."
    )
    env, _, final = run_real("REAL 3 - BATCH REPLAN UNDER REVISION", task, revision=revision)
    assets = {rid: env.db["reservations"][rid]["assets"][0]["asset_id"]
              for rid in ("RS-A", "RS-B", "RS-C")}
    return _show_result("REAL 3 CHECKS", [
        ("completed RS-A kept as EQ-N1", assets["RS-A"] == "EQ-N1"),
        ("remaining RS-B replanned to EQ-N2", assets["RS-B"] == "EQ-N2"),
        ("RS-C left unchanged", assets["RS-C"] == "EQ-C1"),
        ("exactly two writes (no stale/repeat write)",
         sum(c["name"] == "change_reserved_asset" for c in env.calls) == 2),
        ("final reconciles revised outcomes", all(x in final for x in ("RS-A", "RS-B", "RS-C"))),
    ])


def main():
    results = [coupled_variant(), conditional_matrix(), batch_replan()]
    print("\n" + "=" * 78)
    print("REAL MODEL RESULT:", "ALL PASS" if all(results) else "SOME FAILED")
    print("=" * 78)


if __name__ == "__main__":
    main()
