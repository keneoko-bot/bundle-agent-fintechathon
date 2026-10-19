"""One small adapter over OpenAI and Anthropic tool calling.

If no key is configured (or a call fails), callers fall back to the deterministic
workflows, and the run is labelled DETERMINISTIC_FALLBACK in the audit trail.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

from app.config import Settings

Executor = Callable[[str, dict], dict]


class LLM(Protocol):
    provider: str
    model: str

    def run_tools(self, system: str, user: str, tools: list[dict], execute: Executor,
                  max_steps: int = 8) -> str: ...

    def extract_json(self, system: str, user: str) -> dict: ...


def _parse_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def reasoning_effort_for(model: str, setting: str | None) -> str | None:
    """Chat Completions only allows tool calling on some newer models with reasoning off.

    "auto" sends reasoning_effort="none" for gpt-6 models (per OpenAI's model page) and nothing
    for older models such as gpt-4.1-mini. Any other value is sent as given; "off" sends nothing.
    """
    if not setting or setting == "off":
        return None
    if setting == "auto":
        return "none" if model.startswith("gpt-6") else None
    return setting


class OpenAILLM:
    """OpenAI, or any OpenAI-compatible API (DeepSeek, Qwen, Ollama) via base_url."""

    provider = "openai"

    def __init__(self, api_key: str, model: str, base_url: str | None = None,
                 reasoning_effort: str | None = "auto"):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        self.model = model
        self.extra: dict[str, Any] = {}
        effort = reasoning_effort_for(model, reasoning_effort)
        if effort:
            self.extra["reasoning_effort"] = effort

    def run_tools(self, system, user, tools, execute, max_steps=8):
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        spec = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools]
        for _ in range(max_steps):
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, tools=spec, **self.extra)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return msg.content or ""
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments or "{}"}}
                               for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = execute(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, default=str)})
        return ""

    def extract_json(self, system, user):
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            **self.extra,
        )
        return _parse_json(resp.choices[0].message.content or "{}")


class AnthropicLLM:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str):
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key)
        self.model = model

    def run_tools(self, system, user, tools, execute, max_steps=8):
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        spec = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                for t in tools]
        for _ in range(max_steps):
            resp = self.client.messages.create(
                model=self.model, max_tokens=1024, system=system, tools=spec, messages=messages)
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                return "".join(b.text for b in resp.content if b.type == "text")
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in tool_uses:
                result = execute(tu.name, dict(tu.input or {}))
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": json.dumps(result, default=str)})
            messages.append({"role": "user", "content": results})
        return ""

    def extract_json(self, system, user):
        resp = self.client.messages.create(
            model=self.model, max_tokens=600, system=system,
            messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in resp.content if b.type == "text")
        return _parse_json(text)


_override: LLM | None = None
_use_override = False


def set_llm_override(llm: LLM | None) -> None:
    """Tests use this to plug in a scripted fake LLM (or None to force fallback)."""
    global _override, _use_override
    _override, _use_override = llm, True


def clear_llm_override() -> None:
    global _override, _use_override
    _override, _use_override = None, False


def get_llm(settings: Settings) -> LLM | None:
    if _use_override:
        return _override
    if settings.llm_provider == "openai" and settings.openai_api_key:
        return OpenAILLM(settings.openai_api_key, settings.llm_model or "",
                         base_url=settings.llm_base_url, reasoning_effort=settings.llm_reasoning_effort)
    if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicLLM(settings.anthropic_api_key, settings.llm_model or "")
    return None
