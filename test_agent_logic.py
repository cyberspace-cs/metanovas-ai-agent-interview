"""Offline unit tests for the deterministic guardrails in agent.py.

These do NOT call any model or network. They validate the safety logic that the
live evaluator scores under 'Harness discipline and policy safety':
authentication gating, fresh-confirmation gating, reservation-state checks,
and the response contract.
"""

import json

import agent


FAILED = False


def t(name, cond):
    global FAILED
    status = "PASS" if cond else "FAIL"
    print(f"{status} - {name}")
    if not cond:
        FAILED = True


# --- authentication -------------------------------------------------------
t("no auth initially", agent._is_authenticated([{"role": "user", "content": "hi"}]) is False)
auth_msgs = [{"role": "tool", "content": json.dumps("MB-DF323084F4")}]
t("auth after locate_member result", agent._is_authenticated(auth_msgs) is True)


# --- confirmation freshness ----------------------------------------------
prop = {"role": "assistant", "content": "I propose to change RS-1 from A to B, fee +5. Please confirm you want me to proceed."}
conf = {"role": "user", "content": "Yes, please proceed."}
t("confirmation present after proposal", agent._confirmation_present([prop, conf]) is True)
t("no confirmation when only proposal", agent._confirmation_present([prop]) is False)
prop2 = {"role": "assistant", "content": "I will change RS-1 to C instead."}
t("revision drops stale confirmation", agent._confirmation_present([prop, conf, prop2]) is False)


# --- reservation lookup ---------------------------------------------------
res = {"reservation_id": "RS-1", "status": "awaiting_collection", "assets": [{"asset_id": "EQ-OLD"}]}
toolmsg = {"role": "tool", "content": json.dumps(res)}
t("find reservation by id", agent._find_reservation([toolmsg], "RS-1") is not None)
t("reservation not found for other id", agent._find_reservation([toolmsg], "RS-2") is None)


TOOLS = [{
    "type": "function",
    "function": {
        "name": "compute_amount",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
    },
}]


# --- enforce: private read gating ----------------------------------------
r = agent._enforce({"tool_call": {"name": "read_member_profile", "arguments": {"member_id": "MB-X"}}},
                   [{"role": "user", "content": "hi"}], [])
t("private read blocked without auth", "content" in r and "tool_call" not in r)
r = agent._enforce({"tool_call": {"name": "read_member_profile", "arguments": {"member_id": "MB-X"}}},
                   auth_msgs, [])
t("private read allowed with auth", "tool_call" in r)


# --- enforce: write gating ------------------------------------------------
not_await = {"reservation_id": "RS-1", "status": "collected", "assets": [{"asset_id": "EQ-OLD"}]}
msgs_notawait = [{"role": "tool", "content": json.dumps(not_await)}, conf]
r = agent._enforce({"tool_call": {"name": "change_reserved_asset",
                                  "arguments": {"reservation_id": "RS-1", "old_asset_id": "EQ-OLD",
                                                "new_asset_id": "EQ-NEW", "credit_id": "CR-1"}}},
                   msgs_notawait, [])
t("write blocked when not awaiting_collection",
  "content" in r and "awaiting_collection" in r.get("content", ""))

await_res = {"reservation_id": "RS-1", "status": "awaiting_collection", "assets": [{"asset_id": "EQ-OLD"}]}
msgs_await = [{"role": "tool", "content": json.dumps(await_res)}, prop, conf]
r = agent._enforce({"tool_call": {"name": "change_reserved_asset",
                                  "arguments": {"reservation_id": "RS-1", "old_asset_id": "EQ-WRONG",
                                                "new_asset_id": "EQ-NEW", "credit_id": "CR-1"}}},
                   msgs_await, [])
t("write blocked when old_asset mismatch", "content" in r)

msgs_no_conf = [{"role": "tool", "content": json.dumps(await_res)}]
r = agent._enforce({"tool_call": {"name": "change_reserved_asset",
                                  "arguments": {"reservation_id": "RS-1", "old_asset_id": "EQ-OLD",
                                                "new_asset_id": "EQ-NEW", "credit_id": "CR-1"}}},
                   msgs_no_conf, [])
t("write blocked without confirmation", "content" in r)

r = agent._enforce({"tool_call": {"name": "change_reserved_asset",
                                  "arguments": {"reservation_id": "RS-1", "old_asset_id": "EQ-OLD",
                                                "new_asset_id": "EQ-NEW", "credit_id": "CR-1"}}},
                   msgs_await, [])
t("write allowed when all conditions met",
  r.get("tool_call", {}).get("name") == "change_reserved_asset")


# --- enforce: contract + required args ------------------------------------
t("content shape preserved", agent._enforce({"content": "Hello"}, [], []) == {"content": "Hello"})
r = agent._enforce({"tool_call": {"name": "compute_amount", "arguments": {}}}, [], TOOLS)
t("missing required arg blocked", "content" in r)


# --- hardening: word-boundary marker matching (no false yes / change) ------
t("yesterday is NOT a confirmation",
  agent._confirmation_present([
      {"role": "assistant", "content": "I propose to change RS-1 to B."},
      {"role": "user", "content": "I will call you yesterday evening."},
  ]) is False)
t("exchange is NOT a proposal",
  agent._confirmation_present([
      {"role": "assistant", "content": "We can exchange the cable if you like."},
      {"role": "user", "content": "yes proceed"},
  ]) is False)


# --- hardening: no stale / duplicate write --------------------------------
changed = {"reservation_id": "RS-1", "status": "awaiting_collection_changed", "assets": [{"asset_id": "EQ-NEW"}]}
read_again = {"reservation_id": "RS-1", "status": "awaiting_collection", "assets": [{"asset_id": "EQ-OLD"}]}
msgs_stale = [{"role": "tool", "content": json.dumps(changed)},
              {"role": "tool", "content": json.dumps(read_again)},
              {"role": "user", "content": "yes proceed"}]
r = agent._enforce({"tool_call": {"name": "change_reserved_asset",
                                  "arguments": {"reservation_id": "RS-1", "old_asset_id": "EQ-OLD",
                                                "new_asset_id": "EQ-NEW", "credit_id": "CR-1"}}},
                   msgs_stale, [])
t("stale/duplicate write blocked",
  "content" in r and "already been changed" in r.get("content", ""))


# --- hardening: credit must belong to verified member ----------------------
profile = {"member_id": "MB-X", "credits": {"CR-OK": {"credit_id": "CR-OK", "balance": 50}}}
msgs_cred = [{"role": "tool", "content": json.dumps(profile)},
             {"role": "tool", "content": json.dumps(await_res)},
             {"role": "user", "content": "yes proceed"}]
r = agent._enforce({"tool_call": {"name": "change_reserved_asset",
                                  "arguments": {"reservation_id": "RS-1", "old_asset_id": "EQ-OLD",
                                                "new_asset_id": "EQ-NEW", "credit_id": "CR-WRONG"}}},
                   msgs_cred, [])
t("wrong credit blocked", "content" in r and "does not belong" in r.get("content", ""))


print("\nRESULT:", "ALL PASS" if not FAILED else "SOME FAILED")
