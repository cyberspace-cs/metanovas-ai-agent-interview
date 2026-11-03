"""Tiny model client for the MetaNovas interview.

Inside the interview sandbox this talks only to a Unix-socket model proxy.  For
local practice it can call a supported provider directly using environment
variables documented in CANDIDATE_INSTRUCTIONS.md.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any


PROVIDERS = {
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-5-mini"),
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-v4-flash"),
}


class ModelError(RuntimeError):
    pass


def _normalise_message(message: dict) -> dict:
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        call = tool_calls[0]
        function = call.get("function", call)
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        result = {
            "tool_call": {"name": function.get("name", ""), "arguments": arguments},
        }
        # Local DeepSeek practice must send this field back after a tool call.
        # The live platform keeps it inside the host proxy and never exposes it
        # to candidate code or traces.
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            result["_reasoning_content"] = reasoning
        return result
    result = {"content": content}
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        result["_reasoning_content"] = reasoning
    return result


def _proxy_chat(payload: dict) -> dict:
    path = os.environ["MODEL_PROXY_SOCKET"]
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(60)
        client.connect(path)
        client.sendall(encoded)
        chunks = bytearray()
        while b"\n" not in chunks:
            block = client.recv(65536)
            if not block:
                break
            chunks.extend(block)
            if len(chunks) > 2_000_000:
                raise ModelError("Model proxy response was too large")
    if not chunks:
        raise ModelError("Model proxy returned no response")
    response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    if response.get("error"):
        raise ModelError(response["error"])
    return response["message"]


def _local_openai_chat(payload: dict, provider: str) -> dict:
    if provider not in PROVIDERS:
        raise ModelError("Local practice supports openai or deepseek")
    endpoint, default_model = PROVIDERS[provider]
    key = os.getenv("INTERVIEW_API_KEY", "")
    if not key:
        raise ModelError("Set INTERVIEW_API_KEY for local practice")
    body = {
        "model": payload.get("model") or os.getenv("INTERVIEW_MODEL") or default_model,
        "messages": payload["messages"],
        "temperature": payload.get("temperature", 0),
        "max_tokens": min(int(payload.get("max_tokens", 4096)), 4096),
    }
    if provider == "deepseek":
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = "max"
        body.pop("temperature", None)
    if payload.get("tools"):
        body["tools"] = payload["tools"]
        body["tool_choice"] = "auto"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise ModelError(f"Provider returned HTTP {exc.code}: {detail}") from exc
    return _normalise_message(data["choices"][0]["message"])


def chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    model: str | None = None,
    temperature: float = 0,
    max_tokens: int = 4096,
) -> dict:
    """Return ``{"content": ...}`` or ``{"tool_call": {name, arguments}}``.

    Use the complete message history supplied to ``agent.respond``.  Tool
    schemas already follow the OpenAI function-tool format.
    """
    payload = {
        "messages": messages,
        "tools": tools or [],
        "model": model,
        "temperature": max(0, min(float(temperature), 1)),
        "max_tokens": max(1, min(int(max_tokens), 4096)),
    }
    if os.getenv("MODEL_PROXY_SOCKET"):
        return _proxy_chat(payload)
    provider = os.getenv("INTERVIEW_PROVIDER", "openai").strip().lower()
    return _local_openai_chat(payload, provider)
