"""Local runner for the original field-equipment contract practice."""

from __future__ import annotations

import ast
import json
import operator
from copy import deepcopy
from pathlib import Path

from agent import respond


ROOT = Path(__file__).resolve().parent
POLICY = (ROOT / "practice_policy.md").read_text(encoding="utf-8")
TASK = json.loads((ROOT / "practice_task.json").read_text(encoding="utf-8"))
DB = json.loads((ROOT / "practice_fixture.json").read_text(encoding="utf-8"))


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


S = {"type": "string"}
TOOLS = [
    tool("locate_member", "Verify and locate a member by full name and six-digit code.", {"full_name": S, "member_code": S}, ["full_name", "member_code"]),
    tool("read_member_profile", "Read the verified member's credits and reservation references.", {"member_id": S}, ["member_id"]),
    tool("read_reservation", "Read one reservation's state and reserved assets.", {"reservation_id": S}, ["reservation_id"]),
    tool("inspect_asset_family", "List specifications, fees, and availability for one equipment family.", {"family_id": S}, ["family_id"]),
    tool("compute_amount", "Evaluate basic arithmetic for fee calculations.", {"expression": S}, ["expression"]),
    tool(
        "change_reserved_asset",
        "After fresh confirmation, replace an asset on a reservation awaiting collection.",
        {"reservation_id": S, "old_asset_id": S, "new_asset_id": S, "credit_id": S},
        ["reservation_id", "old_asset_id", "new_asset_id", "credit_id"],
    ),
]


class PracticeEnvironment:
    def __init__(self):
        self.db = deepcopy(DB)
        self.calls = []
        self.member_id = None

    def _member(self, member_id):
        if self.member_id != member_id:
            raise ValueError("Verify this member before reading private records")
        return self.db["members"][member_id]

    def call(self, name, arguments, *, write_authorized=False):
        self.calls.append({"name": name, "arguments": deepcopy(arguments)})
        try:
            if name == "locate_member":
                for member_id, member in self.db["members"].items():
                    if member["full_name"].casefold() == arguments["full_name"].casefold() and member["member_code"] == arguments["member_code"]:
                        self.member_id = member_id
                        return member_id
                raise ValueError("Member not found")
            if name == "read_member_profile":
                return deepcopy(self._member(arguments["member_id"]))
            if name == "read_reservation":
                reservation = self.db["reservations"][arguments["reservation_id"]]
                self._member(reservation["member_id"])
                return deepcopy(reservation)
            if name == "inspect_asset_family":
                if not self.member_id:
                    raise ValueError("Verify a member before inspecting reservation options")
                return deepcopy(self.db["asset_families"][arguments["family_id"]])
            if name == "compute_amount":
                return self._calculate(arguments["expression"])
            if name == "change_reserved_asset":
                if not write_authorized:
                    raise ValueError("A fresh explicit confirmation is required")
                reservation = self.db["reservations"][arguments["reservation_id"]]
                member = self._member(reservation["member_id"])
                if reservation["status"] != "awaiting_collection":
                    raise ValueError("Reservation is not awaiting collection")
                current = next((item for item in reservation["assets"] if item["asset_id"] == arguments["old_asset_id"]), None)
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
                return deepcopy(reservation)
            raise ValueError("Unknown tool")
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": str(exc)}

    @staticmethod
    def _calculate(expression):
        operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

        def evaluate(node):
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return float(node.value)
            if isinstance(node, ast.BinOp) and type(node.op) in operations:
                return operations[type(node.op)](evaluate(node.left), evaluate(node.right))
            raise ValueError("Invalid expression")

        return str(round(evaluate(ast.parse(expression, mode="eval")), 2))


def main():
    env = PracticeEnvironment()
    info = TASK["user_scenario"]["instructions"]
    prompt = info["known_info"] + "\n\n" + info["reason_for_call"]
    messages = [{"role": "user", "content": prompt}]
    confirmed = False
    write_completed = False
    print("\nPRACTICE REQUEST\n" + prompt + "\n")
    for step in range(16):
        answer = respond(messages=messages, tools=TOOLS, policy=POLICY)
        if answer.get("tool_call"):
            call = answer["tool_call"]
            call_id = f"call_{step}"
            print("AGENT TOOL:", call["name"], json.dumps(call.get("arguments", {})))
            result = env.call(call["name"], call.get("arguments", {}), write_authorized=confirmed)
            if call["name"] == "change_reserved_asset":
                confirmed = False
                write_completed = not (isinstance(result, dict) and "error" in result)
            assistant_message = {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}))}}]}
            if answer.get("_reasoning_content"):
                assistant_message["reasoning_content"] = answer["_reasoning_content"]
            messages.append(assistant_message)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})
            continue
        text = answer.get("content", "")
        print("AGENT:", text)
        messages.append({"role": "assistant", "content": text})
        if not confirmed and not write_completed and any(word in text.casefold() for word in ("confirm", "proceed", "go ahead")):
            confirmed = True
            reply = "Yes, I confirm that exact reservation change and fee. Please proceed."
            print("MEMBER:", reply)
            messages.append({"role": "user", "content": reply})
            continue
        break
    changed = any(reservation["status"] == "awaiting_collection_changed" for reservation in env.db["reservations"].values())
    print(f"\nPRACTICE RESULT: {'change completed' if changed else 'no completed change'}")
    print("Review the trace above for evidence use, confirmation timing, and final communication.")


if __name__ == "__main__":
    main()
