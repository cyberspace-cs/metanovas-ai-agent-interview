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


# --- hardening: rule 7 — a revision after confirmation drops it ------------
t("revision after confirm drops confirmation",
  agent._confirmation_present([
      {"role": "assistant", "content": "I propose to change RS-1 to B."},
      {"role": "user", "content": "yes proceed"},
      {"role": "user", "content": "actually use C instead"},
  ]) is False)
t("confirm-including-revision keeps confirmation",
  agent._confirmation_present([
      {"role": "assistant", "content": "I propose to change RS-1 to B."},
      {"role": "user", "content": "actually, go ahead and proceed"},
  ]) is True)


# --- hardening: domain-agnostic 'ids must be observed' ---------------------
WRITE_SPEC = [{
    "type": "function",
    "function": {
        "name": "change_reserved_asset",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": ["reservation_id", "old_asset_id", "new_asset_id", "credit_id"],
        },
    },
}]
msgs_unobs = [{"role": "tool", "content": json.dumps(await_res)}, prop, conf]
r = agent._enforce({"tool_call": {"name": "change_reserved_asset",
                                  "arguments": {"reservation_id": "RS-1", "old_asset_id": "EQ-OLD",
                                                "new_asset_id": "EQ-UNSEEN", "credit_id": "CR-1"}}},
                   msgs_unobs, WRITE_SPEC)
t("unobserved new id blocked", "content" in r and "haven't read" in r.get("content", ""))

family = {"family_id": "AF-1", "options": {"EQ-OLD": {"available": True}, "EQ-NEW": {"available": True}}}
profile_ok = {"member_id": "MB-X", "credits": {"CR-1": {"credit_id": "CR-1", "balance": 100}}}
msgs_full = [{"role": "tool", "content": json.dumps(await_res)},
             {"role": "tool", "content": json.dumps(family)},
             {"role": "tool", "content": json.dumps(profile_ok)},
             prop, conf]
r = agent._enforce({"tool_call": {"name": "change_reserved_asset",
                                  "arguments": {"reservation_id": "RS-1", "old_asset_id": "EQ-OLD",
                                                "new_asset_id": "EQ-NEW", "credit_id": "CR-1"}}},
                   msgs_full, WRITE_SPEC)
t("all ids observed -> write allowed",
  r.get("tool_call", {}).get("name") == "change_reserved_asset")


# --- rule 7: revision BEFORE confirmation requires re-proposal ------------
t("revision before confirm requires re-proposal",
  agent._confirmation_present([
      {"role": "assistant", "content": "I propose to change RS-1 to B."},
      {"role": "user", "content": "actually use C instead"},
      {"role": "user", "content": "yes, go ahead"},
  ]) is False)
t("re-proposal after revision allows",
  agent._confirmation_present([
      {"role": "assistant", "content": "I propose to change RS-1 to B."},
      {"role": "user", "content": "actually use C instead"},
      {"role": "assistant", "content": "I propose to change RS-1 to C."},
      {"role": "user", "content": "yes, confirm"},
  ]) is True)


# --- domain-agnostic: a DIFFERENT schema write is not false-blocked -------
DIFF_TOOLS = [{"type": "function", "function": {
    "name": "change_booking",
    "parameters": {"type": "object", "properties": {},
                   "required": ["booking_id", "old_item_id", "new_item_id", "fund_id"]}}}]
msgs_diff = [
    {"role": "tool", "content": json.dumps({"booking_id": "BK-1", "status": "confirmed",
                                            "items": [{"item_id": "IT-1"}]})},
    {"role": "tool", "content": json.dumps({"item_id": "IT-2", "available": True})},
    {"role": "tool", "content": json.dumps({"fund_id": "FD-1", "balance": 100})},
    {"role": "assistant", "content": "I propose to change booking BK-1 from IT-1 to IT-2."},
    {"role": "user", "content": "yes, proceed"},
]
r = agent._enforce({"tool_call": {"name": "change_booking",
                                  "arguments": {"booking_id": "BK-1", "old_item_id": "IT-1",
                                                "new_item_id": "IT-2", "fund_id": "FD-1"}}},
                   msgs_diff, DIFF_TOOLS)
t("different schema write allowed (no false block)",
  r.get("tool_call", {}).get("name") == "change_booking")

r = agent._enforce({"tool_call": {"name": "change_booking",
                                  "arguments": {"booking_id": "BK-9", "old_item_id": "IT-1",
                                                "new_item_id": "IT-2", "fund_id": "FD-1"}}},
                   msgs_diff, DIFF_TOOLS)
t("unobserved target record blocked",
  "content" in r and "haven't read" in r.get("content", ""))


# --- batch: second write allowed after the first write result ------------
await_res2 = {"reservation_id": "RS-2", "status": "awaiting_collection",
              "assets": [{"asset_id": "EQ-OLD2"}]}
changed1 = {"reservation_id": "RS-1", "status": "awaiting_collection_changed",
            "assets": [{"asset_id": "EQ-NEW"}]}
family2 = {"family_id": "AF-1", "options": {"EQ-NEW2": {"available": True}}}
msgs_batch2 = [{"role": "tool", "content": json.dumps(await_res)},
               {"role": "tool", "content": json.dumps(await_res2)},
               {"role": "tool", "content": json.dumps(family2)},
               {"role": "tool", "content": json.dumps(profile_ok)},
               {"role": "assistant", "content": "I propose a plan for RS-1 and RS-2: change RS-1 and RS-2 to the qualifying option."},
               {"role": "user", "content": "yes, confirm the plan"},
               {"role": "tool", "content": json.dumps(changed1)}]
r = agent._enforce({"tool_call": {"name": "change_reserved_asset",
                                  "arguments": {"reservation_id": "RS-2", "old_asset_id": "EQ-OLD2",
                                                "new_asset_id": "EQ-NEW2", "credit_id": "CR-1"}}},
                   msgs_batch2, WRITE_SPEC)
t("batch second write allowed after first",
  r.get("tool_call", {}).get("name") == "change_reserved_asset")


# --- output normalisation + realistic confirmation/auth variants ----------
r = agent._enforce({"content": '{"content": "Hello Mara"}'}, [], [])
t("double-encoded content is unwrapped", r == {"content": "Hello Mara"})
r = agent._enforce({"content": '```json\n{"content": "Hello again"}\n```'}, [], [])
t("code-fenced contract content is unwrapped", r == {"content": "Hello again"})
t("natural affirmative counts as confirmation",
  agent._confirmation_present([
      {"role": "assistant", "content": "Here is my plan: replace A with B. Shall I proceed?"},
      {"role": "user", "content": "That works for me, please do."},
  ]) is True)
t("neutral batch execution does not invalidate confirmation",
  agent._confirmation_present([
      {"role": "assistant", "content": "I propose to replace A with B. Please confirm."},
      {"role": "user", "content": "Sure, go ahead."},
      {"role": "assistant", "content": "Applying the confirmed plan to RS-1."},
  ]) is True)
nested_auth = [
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "locate_member", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"member": {"id": "MB-1"}})},
]
t("nested member verification authenticates", agent._is_authenticated(nested_auth) is True)
failed_auth = [
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c2", "type": "function", "function": {"name": "locate_member", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"error": "Member not found"})},
]
t("failed lookup does not authenticate", agent._is_authenticated(failed_auth) is False)


print("\nRESULT:", "ALL PASS" if not FAILED else "SOME FAILED")
