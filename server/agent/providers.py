"""LLM providers behind one interface so the agent loop is provider-agnostic.

Both providers implement::

    async def run_turn(system, history, tools, on_event) -> new_history_items

`history` is a provider-neutral list of items::

    {"role": "user", "content": str}
    {"role": "assistant", "content": str}                      # final prose
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}]}
    {"role": "tool", "id": str, "name": str, "output": str}    # compact observation only

`on_event(kind, data)` is awaited with kind "text" ({"delta"}), "usage"
({"input_tokens", "output_tokens"}) and "tool_call" ({"id", "name", "args"}),
which must return the observation string to feed back to the model.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional

from ..ratelimit import LIMITER
from ..schemas import AgentInfo
from .prompts import EXAMPLE_PROMPTS

OnEvent = Callable[[str, dict[str, Any]], Awaitable[Any]]
MAX_MODEL_ROUNDS = 12          # model round-trips per turn; the loop caps tool executions at 10
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
REQUEST_TIMEOUT_S = 120.0


class ProviderError(Exception):
    """A model call failed in a way the planner should hear about."""


class ToolLike:
    """Duck type of `agent.tools.ToolSpec`: name, description, parameters."""

    name: str
    description: str
    parameters: dict[str, Any]


class Provider(ABC):
    name: str
    model: str

    @abstractmethod
    async def run_turn(self, system: str, history: list[dict[str, Any]], tools: list[ToolLike],
                       on_event: OnEvent) -> list[dict[str, Any]]:
        """Run one user turn (with tool round-trips) and return the new history items."""


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ------------------------------------------------------------------------ OpenAI
class OpenAIProvider(Provider):
    """OpenAI Responses API with streaming and function tools."""

    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        from openai import AsyncOpenAI

        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_S, max_retries=2)

    @staticmethod
    def tool_schemas(tools: list[ToolLike]) -> list[dict[str, Any]]:
        return [{"type": "function", "name": t.name, "description": t.description,
                 "parameters": t.parameters, "strict": False} for t in tools]

    @staticmethod
    def to_input(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in history:
            role = item.get("role")
            if role == "user":
                items.append({"role": "user", "content": item["content"]})
            elif role == "assistant":
                if item.get("content"):
                    items.append({"role": "assistant", "content": item["content"]})
                for call in item.get("tool_calls", []):
                    items.append({"type": "function_call", "name": call["name"],
                                  "arguments": json.dumps(call["args"]), "call_id": call["id"]})
            elif role == "tool":
                items.append({"type": "function_call_output", "call_id": item["id"], "output": item["output"]})
        return items

    async def run_turn(self, system: str, history: list[dict[str, Any]], tools: list[ToolLike],
                       on_event: OnEvent) -> list[dict[str, Any]]:
        from openai import APIError

        items = self.to_input(history)
        schemas = self.tool_schemas(tools)
        new_items: list[dict[str, Any]] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        try:
            for _round in range(MAX_MODEL_ROUNDS):
                text, calls = await self._stream_once(system, items, schemas, on_event, usage)
                if not calls:
                    if text:
                        new_items.append({"role": "assistant", "content": text})
                    break
                tool_calls = [{"id": c.call_id, "name": c.name, "args": _parse_args(c.arguments)} for c in calls]
                new_items.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
                if text:
                    items.append({"role": "assistant", "content": text})
                for call, tc in zip(calls, tool_calls):
                    items.append({"type": "function_call", "name": call.name,
                                  "arguments": call.arguments or "{}", "call_id": call.call_id})
                    output = await on_event("tool_call", tc)
                    items.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
                    new_items.append({"role": "tool", "id": call.call_id, "name": call.name, "output": output})
        except APIError as exc:
            raise ProviderError(f"OpenAI error: {getattr(exc, 'message', None) or exc}") from exc
        await on_event("usage", usage)
        return new_items

    async def _stream_once(self, system: str, items: list[dict[str, Any]], schemas: list[dict[str, Any]],
                           on_event: OnEvent, usage: dict[str, int]) -> tuple[str, list[Any]]:
        """One streamed model response: returns (text, function_call items)."""
        stream = await self.client.responses.create(
            model=self.model, instructions=system, input=items, tools=schemas,
            stream=True, reasoning={"effort": "low"},
        )
        parts: list[str] = []
        calls: list[Any] = []
        async for event in stream:
            kind = event.type
            if kind == "response.output_text.delta":
                parts.append(event.delta)
                await on_event("text", {"delta": event.delta})
            elif kind == "response.output_item.done" and getattr(event.item, "type", None) == "function_call":
                calls.append(event.item)
            elif kind == "response.completed":
                response_usage = getattr(event.response, "usage", None)
                if response_usage is not None:
                    usage["input_tokens"] += int(response_usage.input_tokens or 0)
                    usage["output_tokens"] += int(response_usage.output_tokens or 0)
            elif kind == "response.failed":
                error = getattr(event.response, "error", None)
                raise ProviderError(f"OpenAI response failed: {getattr(error, 'message', None) or 'unknown error'}")
            elif kind == "response.incomplete":
                details = getattr(event.response, "incomplete_details", None)
                raise ProviderError(f"OpenAI response incomplete: {getattr(details, 'reason', None) or 'unknown reason'}")
            elif kind == "error":
                raise ProviderError(f"OpenAI stream error: {getattr(event, 'message', None) or 'unknown error'}")
        return "".join(parts), calls


# --------------------------------------------------------------------- Anthropic
class AnthropicProvider(Provider):
    """Anthropic Messages API with streaming and tool_use / tool_result blocks."""

    name = "anthropic"
    max_tokens = 2000

    def __init__(self, api_key: str, model: str) -> None:
        from anthropic import AsyncAnthropic

        self.model = model
        self.client = AsyncAnthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_S, max_retries=2)

    @staticmethod
    def tool_schemas(tools: list[ToolLike]) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]

    @staticmethod
    def to_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for item in history:
            role = item.get("role")
            if role == "user":
                messages.append({"role": "user", "content": item["content"]})
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if item.get("content"):
                    blocks.append({"type": "text", "text": item["content"]})
                for call in item.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]})
                if blocks:
                    messages.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                result = {"type": "tool_result", "tool_use_id": item["id"], "content": item["output"]}
                last = messages[-1] if messages else None
                if last and last["role"] == "user" and isinstance(last["content"], list):
                    last["content"].append(result)
                else:
                    messages.append({"role": "user", "content": [result]})
        return messages

    async def run_turn(self, system: str, history: list[dict[str, Any]], tools: list[ToolLike],
                       on_event: OnEvent) -> list[dict[str, Any]]:
        from anthropic import APIError

        messages = self.to_messages(history)
        schemas = self.tool_schemas(tools)
        new_items: list[dict[str, Any]] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        try:
            for _round in range(MAX_MODEL_ROUNDS):
                final = await self._stream_once(system, messages, schemas, on_event)
                if final.usage is not None:
                    usage["input_tokens"] += int(final.usage.input_tokens or 0)
                    usage["output_tokens"] += int(final.usage.output_tokens or 0)
                text = "".join(block.text for block in final.content if block.type == "text")
                tool_uses = [block for block in final.content if block.type == "tool_use"]
                if final.stop_reason != "tool_use" or not tool_uses:
                    if text:
                        new_items.append({"role": "assistant", "content": text})
                    break
                tool_calls = [{"id": b.id, "name": b.name, "args": dict(b.input or {})} for b in tool_uses]
                new_items.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
                messages.append({"role": "assistant", "content": final.content})
                results: list[dict[str, Any]] = []
                for block, tc in zip(tool_uses, tool_calls):
                    output = await on_event("tool_call", tc)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                    new_items.append({"role": "tool", "id": block.id, "name": block.name, "output": output})
                messages.append({"role": "user", "content": results})
        except APIError as exc:
            raise ProviderError(f"Anthropic error: {getattr(exc, 'message', None) or exc}") from exc
        await on_event("usage", usage)
        return new_items

    async def _stream_once(self, system: str, messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
                           on_event: OnEvent) -> Any:
        async with self.client.messages.stream(
            model=self.model, max_tokens=self.max_tokens, system=system, messages=messages, tools=schemas,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    await on_event("text", {"delta": event.delta.text})
            return await stream.get_final_message()


# ---------------------------------------------------------------------- selection
_cache: tuple[tuple[str, ...], Optional[Provider]] | None = None


def _env_key() -> tuple[str, ...]:
    return (
        os.environ.get("OPENAI_API_KEY", "").strip(),
        os.environ.get("OPENAI_MODEL", "").strip(),
        os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        os.environ.get("ANTHROPIC_MODEL", "").strip(),
    )


def get_provider() -> Optional[Provider]:
    """The configured provider (OpenAI first, then Anthropic) or None when no key is set.

    Re-evaluated from the environment on every call so key changes take effect
    immediately; the client object is reused while the environment is unchanged."""
    global _cache
    key = _env_key()
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    openai_key, openai_model, anthropic_key, anthropic_model = key
    provider: Optional[Provider] = None
    if openai_key:
        provider = OpenAIProvider(openai_key, openai_model or DEFAULT_OPENAI_MODEL)
    elif anthropic_key:
        provider = AnthropicProvider(anthropic_key, anthropic_model or DEFAULT_ANTHROPIC_MODEL)
    _cache = (key, provider)
    return provider


def agent_info() -> AgentInfo:
    """What `/api/config` and `/api/health` report about the agent."""
    provider = get_provider()
    return AgentInfo(
        enabled=provider is not None,
        provider=provider.name if provider else None,
        model=provider.model if provider else None,
        turns_per_hour=LIMITER.max_for("agent_hour"),
        example_prompts=list(EXAMPLE_PROMPTS),
    )
