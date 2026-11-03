"""Offline end-to-end runner.

Same loop as `practice_runner.py`, but the LLM is replaced by a deterministic
rule-based stand-in (`mock_chat`) so you can verify the FULL pipeline — agent
prompt + guardrails + tool execution + confirmation flow — for FREE, without a
real API key or balance.

This is a LOCAL TEST FIXTURE ONLY. It hardcodes the practice scenario values
(Mara Voss / RS-BC348019D / AF-DFD6C6D1 ...) and is NOT part of the submission.

Usage:
    python mock_runner.py
"""

from __future__ import annotations

import agent
import practice_runner


def _assistant_tool_names(messages) -> list:
    names = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", tc)
            names.append(fn.get("name"))
    return names


def mock_chat(messages, *, tools=None, model=None, temperature=0, max_tokens=4096):
    """A competent, deterministic stand-in that drives the practice scenario.

    It mimics a correct agent: authenticate -> read profile -> read reservation
    -> inspect family -> compute fee -> propose + ask confirmation -> (after
    confirmation) perform the write. The real guardrails in agent._enforce still
    apply on top of this, so this also proves the wrapper lets a valid flow through.
    """
    names = _assistant_tool_names(messages)

    if "locate_member" not in names:
        return {"tool_call": {"name": "locate_member",
                              "arguments": {"full_name": "Mara Voss", "member_code": "588701"}}}
    if "read_member_profile" not in names:
        return {"tool_call": {"name": "read_member_profile",
                              "arguments": {"member_id": "MB-DF323084F4"}}}
    if "read_reservation" not in names:
        return {"tool_call": {"name": "read_reservation",
                              "arguments": {"reservation_id": "RS-BC348019D"}}}
    if "inspect_asset_family" not in names:
        return {"tool_call": {"name": "inspect_asset_family",
                              "arguments": {"family_id": "AF-DFD6C6D1"}}}
    if "compute_amount" not in names:
        return {"tool_call": {"name": "compute_amount",
                              "arguments": {"expression": "53.5 - 39.5"}}}

    # Propose the exact change, then ask for explicit confirmation.
    if not agent._confirmation_present(messages):
        return {"content": (
            "I verified reservation RS-BC348019D (awaiting collection) currently holds the "
            "Portable Field Recorder (EQ-C2B2059D, fee 39.5). Among available options in the "
            "same family with a balanced XLR input and at least 12 hours of battery, the lightest "
            "is EQ-5C143A54 (365 g, 12 h battery, balanced XLR, fee 53.5). The exact additional "
            "fee is 53.5 - 39.5 = 14.0, covered by your project credit CR-AFCE80FF (balance 75.0). "
            "Please confirm that you want me to proceed with this exact change."
        )}

    # Confirmation is present -> perform exactly one write.
    return {"tool_call": {"name": "change_reserved_asset", "arguments": {
        "reservation_id": "RS-BC348019D",
        "old_asset_id": "EQ-C2B2059D",
        "new_asset_id": "EQ-5C143A54",
        "credit_id": "CR-AFCE80FF",
    }}}


def main():
    # Swap the real model client for the offline stand-in.
    agent.chat = mock_chat
    practice_runner.main()


if __name__ == "__main__":
    main()
