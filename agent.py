"""Transactional support agent for the MetaNovas policy-and-tool interview.

IMPORTANT DESIGN NOTES (read before editing):

* This agent is STATELESS. The evaluator calls `respond` over and over with the
  full transcript. Nothing may be stored in module globals between calls. Every
  turn the agent must reconstruct intent, completed work, pending work, and
  confirmation state purely from `messages`.

* The agent forwards the conversation to the platform model through
  `interview_sdk.chat` (which in the live sandbox talks ONLY to the platform
  proxy; locally it can call OpenAI/DeepSeek). The model output is then passed
  through deterministic guardrails so that the submission contract and the
  policy safety rules can never be violated by a careless model answer:
    - at most one tool call per turn
    - authenticate before any private read
    - a fresh, explicit confirmation before any write
    - never write to a reservation whose state/ownership is unknown or that is
      not in `awaiting_collection`
    - never return both text and a tool call in one turn

* The agent is GENERIC. It never hardcodes the practice scenario (Mara Voss,
  RS-BC348019D, etc.). Formal tasks use freshly generated records, so all
  reasoning must come from the tools + the supplied policy.
"""

from __future__ import annotations

import json
import re

from interview_sdk import chat


# Tools that mutate state and therefore require a fresh, explicit confirmation.
WRITE_TOOLS = {"change_reserved_asset"}
# Tools that read private member data and therefore require authentication first.
PRIVATE_READ_TOOLS = {
    "read_member_profile",
    "read_reservation",
    "inspect_asset_family",
}

# Words that mark an assistant message as a *change proposal* (used to decide
# whether a later customer confirmation is "fresh"). Deliberately action-oriented
# and avoiding generic verbs like "update"/"modify"/"switch" so that a neutral
# batch-execution status line ("Applying the confirmed plan to RS-..") is NOT
# mistaken for a new proposal (which would otherwise invalidate the confirmation).
# Matching uses word boundaries so that e.g. "exchange" does not count as a
# "change" proposal and "yesterday" does not count as a "yes" confirmation.
PROPOSAL_MARKERS = (
    "propose", "change", "replace", "swap", "cancel",
    "am changing", "will change",
)
# Words that mark a customer message as an explicit confirmation.
CONFIRM_MARKERS = (
    "confirm", "i confirm", "yes", "proceed", "go ahead", "approve",
    "approved", "do it", "please proceed", "looks good", "correct",
    "that's right", "go for it", "make the change",
)

_PROPOSAL_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(m) for m in PROPOSAL_MARKERS) + r")(?!\w)"
)
_CONFIRM_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(m) for m in CONFIRM_MARKERS) + r")(?!\w)"
)

# A customer revision AFTER a confirmation invalidates it (rule 7: latest
# instruction is authoritative; stale planned actions must be discarded).
# Deliberately strong markers so a neutral "continue" is not mistaken for a
# revision, and a message that is itself a confirmation ("actually, go ahead")
# is still treated as a confirmation.
REVISION_MARKERS = (
    "instead", "actually", "rather", "on second thought", "never mind",
    "nevermind", "change of plan", "switch to", "different option",
    "cancel that", "let's change", "lets change", "scratch that",
)
_REVISION_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(m) for m in REVISION_MARKERS) + r")(?!\w)"
)


def _build_system_prompt(policy: str) -> dict:
    rules = (
        "You are a precise transactional customer-support agent. Follow the "
        "supplied policy EXACTLY. The conversation is stateless: you receive "
        "the complete transcript on every turn, so reconstruct the current "
        "intent, what is already done, what is pending, and whether you have a "
        "fresh confirmation purely from the messages.\n\n"
        "RUNTIME SCHEMAS: the `tools` list and `policy` are authoritative for "
        "THIS task. Derive the real tool names, required arguments, the target "
        "record id field, and the mutation tool from them. Do not assume the "
        "practice task's field names apply here; adapt to the schema you are "
        "given.\n\n"
        "HARD CONSTRAINTS (violating any fails the task):\n"
        "1. Authenticate the customer with locate_member (full name + six-digit "
        "code) BEFORE any private read (read_member_profile, read_reservation, "
        "inspect_asset_family). Never guess internal identifiers, and never read "
        "or change records that do not belong to the verified member.\n"
        "2. Call AT MOST ONE tool per turn. Never call a tool and also return "
        "customer text in the same turn.\n"
        "3. For any write/change: first state the exact target, the current "
        "option, the proposed option, the exact fee difference (use "
        "compute_amount to calculate it), and the funding source. Then ask for "
        "an explicit confirmation using the words 'confirm' or 'proceed'. Only "
        "AFTER the customer explicitly confirms that exact change may you call "
        "the write tool. One confirmation authorises the change(s) it covers.\n"
        "4. Never invent records, options, fees, balances, or policy facts. "
        "Use tool results as the only source of truth.\n"
        "5. Apply EVERY constraint in the customer request to the current tool "
        "evidence before selecting an option. For min/max constraints compare "
        "ALL qualifying options and pick the true optimum. Never substitute an "
        "option from a different equipment family.\n"
        "6. Refuse requests outside policy and explain with verified facts "
        "(e.g. a reservation not in 'awaiting_collection' cannot be changed; a "
        "desired option that is unavailable or in another family cannot be "
        "selected). Escalate only when policy requires.\n"
        "7. The customer's latest instruction is authoritative. If they revise "
        "a request, DISCARD stale planned actions immediately and recalculate "
        "from current records. Do NOT complete an obsolete plan, even if it was "
        "previously confirmed.\n"
        "8. For batch requests: inspect ALL relevant records first, then present "
        "an order-by-order plan naming each record, its verified status, and "
        "the intended action or explicit no-action decision. Get one "
        "confirmation on that plan. Then complete every eligible subtask (one "
        "write per turn) and report what was actually done. Do not stop after "
        "only one subtask.\n\n"
        "WORKFLOW for a change request:\n"
        "a) locate_member -> read_member_profile -> read_reservation -> "
        "inspect_asset_family (gather evidence, one tool per turn).\n"
        "b) Evaluate every constraint against the evidence. If nothing qualifies, "
        "say so with verified facts.\n"
        "c) If a change is warranted: state target/current/proposed/fee "
        "difference/funding source, then ask 'Please confirm that you want me to "
        "proceed with this exact change.'\n"
        "d) After explicit confirmation, call the write tool exactly once.\n"
        "e) When executing a confirmed BATCH plan, call the write tool for each "
        "order one turn at a time. Use only neutral status lines such as "
        "'Applying the confirmed plan to <id>.' -- do NOT introduce a new change "
        "proposal during execution.\n"
        "f) Report the verified outcome: selected option, exact fee impact, "
        "funding source, and whether each change succeeded.\n\n"
        "RESPONSE CONTRACT (return exactly one shape, nothing else):\n"
        "  To speak:        {\"content\": \"your message\"}\n"
        "  To call a tool:  {\"tool_call\": {\"name\": \"tool_name\", \"arguments\": {…}}}\n"
    )
    return {
        "role": "system",
        "content": rules + "\n\n# Policy (authoritative)\n" + policy,
    }


def _tool_required(tools) -> dict:
    req: dict[str, set] = {}
    for t in (tools or []):
        fn = t.get("function", t)
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters", {}) or {}
        req[name] = set(params.get("required", []) or [])
    return req


_WRITE_PREFIXES = (
    "change", "update", "cancel", "create", "delete", "modify", "remove", "add",
    "set", "replace", "swap", "move", "transfer", "assign", "edit", "alter",
    "revise", "adjust",
)


def _is_write(name: str) -> bool:
    if name in WRITE_TOOLS:
        return True
    return name.startswith(_WRITE_PREFIXES)


def _is_private_read(name: str) -> bool:
    if name in PRIVATE_READ_TOOLS:
        return True
    lowered = name.lower()
    if "read" in lowered or "inspect" in lowered or "get" in lowered or "list" in lowered:
        if "public" in lowered:
            return False
        for token in ("profile", "member", "reservation", "credit", "asset", "order", "account"):
            if token in lowered:
                return True
    return False


def _is_authenticated(messages) -> bool:
    """True if a successful locate_member result appears in the transcript."""
    for m in messages:
        if m.get("role") != "tool":
            continue
        raw = m.get("content", "")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, str) and data.startswith("MB-"):
            return True
        if isinstance(data, dict) and data.get("member_id"):
            return True
    return False


def _confirmation_present(messages) -> bool:
    """True if a customer confirmation appears AFTER the most recent assistant
    change proposal. This enforces freshness and specificity: if the agent makes
    a NEW proposal (e.g. after the customer revises intent) the old confirmation
    no longer counts and a new one is required.
    """
    last_proposal_idx = -1
    last_confirm_idx = -1
    for i, m in enumerate(messages):
        role = m.get("role")
        text = (m.get("content") or "").casefold()
        if role == "assistant":
            if _PROPOSAL_RE.search(text):
                last_proposal_idx = i
        elif role == "user":
            if _CONFIRM_RE.search(text):
                last_confirm_idx = i
    if last_proposal_idx < 0:
        return False
    # Rule 7: a later customer message that revises intent invalidates the
    # confirmation, unless that message is itself a confirmation.
    if last_confirm_idx >= 0:
        for later in messages[last_confirm_idx + 1:]:
            if later.get("role") != "user":
                continue
            text = (later.get("content") or "").casefold()
            if _REVISION_RE.search(text) and not _CONFIRM_RE.search(text):
                return False
    return last_confirm_idx > last_proposal_idx


def _find_reservation(messages, reservation_id):
    """Return the most recent read_reservation result for `reservation_id`, or
    None if the agent has not yet verified that reservation's state.
    """
    if not reservation_id:
        return None
    found = None
    for m in messages:
        if m.get("role") != "tool":
            continue
        raw = m.get("content", "")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("reservation_id") == reservation_id:
            found = data
    return found


def _parse_tool(m):
    raw = m.get("content", "")
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None


def _reservation_already_changed(messages, reservation_id):
    """True if a successful change result for this reservation already exists in
    the transcript. Prevents stale / duplicate writes (the harness imposes a
    severe penalty for unexpected or stale write actions).
    """
    if not reservation_id:
        return False
    for m in messages:
        if m.get("role") != "tool":
            continue
        data = _parse_tool(m)
        if not isinstance(data, dict):
            continue
        if data.get("reservation_id") != reservation_id or "error" in data:
            continue
        status = data.get("status")
        if status is not None and status != "awaiting_collection":
            return True
    return False


def _member_credit_ids(messages):
    """Return the set of credit ids belonging to the verified member, or None if
    the profile has not been read / cannot be parsed. Callers must NOT block when
    this returns None (unknown structure -> fail open, not closed).
    """
    for m in messages:
        if m.get("role") != "tool":
            continue
        data = _parse_tool(m)
        if not isinstance(data, dict) or "credits" not in data:
            continue
        credits = data["credits"]
        if isinstance(credits, dict):
            return set(credits.keys())
        if isinstance(credits, list):
            return {c.get("credit_id") for c in credits if isinstance(c, dict)}
        return None
    return None


def _id_observed(messages, value) -> bool:
    """True if `value` appears in any prior tool result or customer message.

    Domain-agnostic "read before write / never invent identifiers" check: every
    id we are about to pass to a write tool must have been observed in the
    transcript. Works for any tool schema (reservation_id, booking_id, ...).
    """
    if not isinstance(value, str) or not value:
        return False
    for m in messages:
        if m.get("role") not in ("tool", "user"):
            continue
        raw = m.get("content", "")
        if isinstance(raw, str) and value in raw:
            return True
    return False


def _enforce(result: dict, messages: list, tools: list) -> dict:
    """Apply the submission contract and policy guardrails to a raw model answer."""
    if not isinstance(result, dict):
        return {"content": "I didn't catch that. Could you restate your request?"}

    tool_call = result.get("tool_call")
    content = result.get("content")

    if tool_call and isinstance(tool_call, dict):
        name = (tool_call.get("name") or "").strip()
        arguments = tool_call.get("arguments") or {}
        if not name:
            return {"content": "I need a valid action. Could you clarify your request?"}

        # --- Guardrail: authentication before private reads ---
        if _is_private_read(name) and not _is_authenticated(messages):
            return {
                "content": (
                    "Before I can access private records I need to verify your "
                    "identity. Please confirm your full name and six-digit member "
                    "code so I can locate your account."
                )
            }

        # --- Guardrail: evidence + state checks before a write ---
        if _is_write(name):
            required = _tool_required(tools).get(name, set())
            reservation_id = arguments.get("reservation_id")
            reservation = _find_reservation(messages, reservation_id)

            # Domain-specific checks — applied ONLY when the transcript actually
            # contains a reservation-like record (the practice schema). On a
            # different formal schema these are skipped; the tool + policy then
            # enforce record state, so we never false-block another domain.
            if isinstance(reservation, dict):
                status = reservation.get("status")
                if status is not None and status != "awaiting_collection":
                    return {
                        "content": (
                            f"Reservation {reservation_id or reservation.get('reservation_id')} "
                            f"is in '{status}' status, not 'awaiting_collection'. Under "
                            "policy I cannot change it. Let me know if there is another "
                            "record you'd like to adjust."
                        )
                    }
                current_ids = [
                    a.get("asset_id")
                    for a in reservation.get("assets", [])
                    if isinstance(a, dict)
                ]
                if (current_ids and arguments.get("old_asset_id")
                        and arguments.get("old_asset_id") not in current_ids):
                    return {
                        "content": (
                            f"The asset {arguments.get('old_asset_id')} is not the one "
                            f"currently on reservation {reservation_id}. I can only "
                            "replace the asset that is actually reserved, so I won't "
                            "proceed with this change."
                        )
                    }
                if _reservation_already_changed(messages, reservation.get("reservation_id")):
                    return {
                        "content": (
                            f"Reservation {reservation.get('reservation_id')} has already "
                            "been changed and is no longer in 'awaiting_collection'. Under "
                            "policy I can't change it again. Let me know if you need a "
                            "different action."
                        )
                    }
            elif reservation_id and not _id_observed(messages, reservation_id):
                return {
                    "content": (
                        f"I need to verify record {reservation_id} before changing it. "
                        "Let me read its current state first."
                    )
                }

            # Domain-agnostic: every id we are about to pass must have been
            # observed in the transcript (tool results or the customer's words).
            # This blocks inventing records / options / funding on ANY schema.
            missing_obs = [
                key for key in required
                if key.endswith("_id") and isinstance(arguments.get(key), str)
                and arguments.get(key) and not _id_observed(messages, arguments.get(key))
            ]
            if missing_obs:
                return {
                    "content": (
                        "Before I change anything I need to verify the current record "
                        "and the exact identifiers involved. I haven't read these yet: "
                        f"{', '.join(sorted(missing_obs))}. Let me read the record "
                        "first, then I can proceed."
                    )
                }

            # --- Guardrail: funding source must belong to the verified member ---
            credit_ids = _member_credit_ids(messages)
            if credit_ids is not None and arguments.get("credit_id") not in credit_ids:
                return {
                    "content": (
                        f"Credit {arguments.get('credit_id')} does not belong to "
                        "your verified account, so I can't use it. Please confirm "
                        "which of your own credits should fund this change."
                    )
                }

            # --- Guardrail: fresh explicit confirmation before any write ---
            if not _confirmation_present(messages):
                return {
                    "content": (
                        "I can't make this change yet. To stay within policy I must "
                        "have your explicit confirmation first. Please confirm the "
                        "exact change (target record, current option, proposed option, "
                        "fee difference, and funding source) and I will proceed."
                    )
                }

        # --- Guardrail: required arguments present ---
        required = _tool_required(tools).get(name, set())
        missing = [r for r in required if not arguments.get(r)]
        if missing:
            return {
                "content": (
                    f"To call {name} I still need: {', '.join(missing)}. "
                    "Could you provide that?"
                )
            }

        # Return exactly one tool call. Preserve the model's reasoning so the
        # local DeepSeek runner can echo it back (thinking mode requires it);
        # the live platform proxy ignores this field.
        out = {"tool_call": {"name": name, "arguments": arguments}}
        if isinstance(result, dict) and result.get("_reasoning_content"):
            out["_reasoning_content"] = result["_reasoning_content"]
        return out

    # No tool call -> speak to the customer.
    text = content if isinstance(content, str) else ""
    if not text.strip():
        text = "Could you clarify what you'd like me to do?"
    out = {"content": text}
    if isinstance(result, dict) and result.get("_reasoning_content"):
        out["_reasoning_content"] = result["_reasoning_content"]
    return out


def respond(messages: list[dict], tools: list[dict], policy: str) -> dict:
    """Return either assistant text or exactly one tool call.

    The evaluator calls this function repeatedly with the complete transcript.
    Keep all durable state in the transcript, never in module globals.
    """
    system = _build_system_prompt(policy)
    result = chat([system, *messages], tools=tools, temperature=0)
    return _enforce(result, messages, tools)
