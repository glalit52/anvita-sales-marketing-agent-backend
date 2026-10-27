"""LLM gateway: one place for model routing, prompt caching, refusal fallbacks and cost metering."""

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import anthropic

from app.core.config import get_settings

log = logging.getLogger(__name__)

# USD per million tokens: (input, output). Cache writes bill at 1.25x input, cache reads at 0.1x.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
}
# Models that accept the server-side refusal fallback ("default" routing).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    def cost(self, model: str) -> Decimal:
        inp, out = PRICING.get(model, (Decimal("2"), Decimal("10")))
        m = Decimal(1_000_000)
        return (
            inp * self.input_tokens / m
            + out * self.output_tokens / m
            + inp * Decimal("1.25") * self.cache_write_tokens / m
            + inp * Decimal("0.1") * self.cache_read_tokens / m
        ).quantize(Decimal("0.000001"))


@dataclass(slots=True)
class LLMResponse:
    # Normalised blocks: {"type": "text", "text"} | {"type": "tool_use", "id", "name", "input"}
    blocks: list[dict[str, Any]]
    stop_reason: str
    usage: Usage
    model: str
    # Exactly what the provider returned; sent back verbatim on the next turn (thinking blocks must be
    # passed back unchanged).
    raw_content: Any = None
    refusal_category: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.blocks if b["type"] == "text").strip()

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.blocks if b["type"] == "tool_use"]

    def assistant_message(self) -> dict[str, Any]:
        content = self.raw_content if self.raw_content is not None else self.blocks
        return {"role": "assistant", "content": content}


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        effort: str = "medium",
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)

    async def complete(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        effort: str = "medium",
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        output_config: dict[str, Any] = {"effort": effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": output_config,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            if get_settings().llm_refusal_fallbacks and model in FALLBACK_MODELS:
                resp = await self._client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                resp = await self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMError("LLM rate limited") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"LLM API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("LLM connection error") from e

        blocks: list[dict[str, Any]] = []
        for b in resp.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                inp = b.input if isinstance(b.input, dict) else json.loads(b.input or "{}")
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": inp})
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        details = getattr(resp, "stop_details", None)
        return LLMResponse(
            blocks=blocks,
            stop_reason=resp.stop_reason or "end_turn",
            usage=usage,
            model=resp.model,
            raw_content=resp.content,
            refusal_category=getattr(details, "category", None) if details else None,
        )


class OfflineLLM:
    """Deterministic stand-in used when no API key is configured (local dev, demos, CI).

    It exercises the same tool loop as the real model: look up the knowledge base, then answer from
    the best hit or hand over to a human. Never used in production (settings validation enforces a key).
    """

    model = "offline"

    async def complete(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        effort: str = "medium",
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        last_user = _last_user_text(messages)
        if json_schema is not None:
            return LLMResponse(
                [{"type": "text", "text": json.dumps(heuristic_classify(last_user))}], "end_turn", Usage(), self.model
            )
        tool_names = {t["name"] for t in tools or []}
        last = messages[-1] if messages else {}
        results = _tool_results(last)
        if results is None and "search_knowledge" in tool_names:
            return LLMResponse(
                [{"type": "tool_use", "id": "offline_1", "name": "search_knowledge", "input": {"query": last_user}}],
                "tool_use",
                Usage(),
                self.model,
            )
        if results is not None:
            data = json.loads(results[0]) if results else {}
            hits = data.get("results") or []
            if hits:
                answer = hits[0]["content"]
                answer = answer.split("\nA: ", 1)[1] if "\nA: " in answer else answer  # FAQ chunks are "Q: …\nA: …"
                return LLMResponse([{"type": "text", "text": answer[:600]}], "end_turn", Usage(), self.model)
            if "handoff_to_human" in tool_names and not data.get("handed_off"):
                return LLMResponse(
                    [
                        {
                            "type": "tool_use",
                            "id": "offline_2",
                            "name": "handoff_to_human",
                            "input": {"reason": "No answer in knowledge base", "summary": last_user[:200]},
                        }
                    ],
                    "tool_use",
                    Usage(),
                    self.model,
                )
        return LLMResponse(
            [{"type": "text", "text": "Thanks for your message! A teammate will get back to you shortly."}],
            "end_turn",
            Usage(),
            self.model,
        )


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m["role"] != "user":
            continue
        content = m["content"]
        if isinstance(content, str):
            return content
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return ""


def _tool_results(message: dict[str, Any]) -> list[str] | None:
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list):
        return None
    results = [c.get("content", "") for c in content if isinstance(c, dict) and c.get("type") == "tool_result"]
    return results or None


_NEGATIVE = re.compile(r"\b(angry|worst|terrible|again|refund|complain|complaint|disappointed|pathetic|bekaar)\b", re.I)
_POSITIVE = re.compile(r"\b(thanks|thank you|great|perfect|awesome|love|dhanyavad|shukriya)\b", re.I)
_INTENTS = [
    (re.compile(r"\b(book|appointment|slot|schedule|available)\b", re.I), "Book appointment"),
    (re.compile(r"\b(price|cost|how much|fees|charges|kitna)\b", re.I), "Pricing question"),
    (re.compile(r"\b(emi|finance|installment|loan)\b", re.I), "Financing"),
    (re.compile(r"\b(refund|complain|complaint|again)\b", re.I), "Complaint"),
    (re.compile(r"\b(cancel|reschedule)\b", re.I), "Reschedule or cancel"),
]


def heuristic_classify(text: str) -> dict[str, str]:
    sentiment = "negative" if _NEGATIVE.search(text) else "positive" if _POSITIVE.search(text) else "neutral"
    intent = next((label for rx, label in _INTENTS if rx.search(text)), "General enquiry")
    return {"intent": intent, "sentiment": sentiment, "summary": text[:200]}


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        s = get_settings()
        if s.anthropic_api_key:
            _client = AnthropicLLM(s.anthropic_api_key.get_secret_value(), timeout=s.llm_timeout_seconds)
        else:
            log.warning("No ANVITA_ANTHROPIC_API_KEY set; using the offline LLM")
            _client = OfflineLLM()
    return _client


def set_llm(client: LLMClient | None) -> None:
    """Override the gateway (tests)."""
    global _client
    _client = client
