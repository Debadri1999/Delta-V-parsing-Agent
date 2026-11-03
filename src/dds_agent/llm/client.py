"""OpenAI-compatible chat client for Purdue GenAI Studio with JSON output.

Responsibilities: on-prem enforcement, prompt-size budgeting, JSON-mode with a
graceful fallback, JSON repair retries, rate-limit backoff and a usage ledger.
Prompt and completion *content* is never logged.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import openai
from openai import OpenAI

from dds_agent.config import Settings, assert_on_prem

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


class PromptTooLargeError(RuntimeError):
    """The prompt exceeds the configured token budget."""


class LLMResponseError(RuntimeError):
    """The model did not return usable JSON after retries."""


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for FHX-heavy text (about 3 characters per token)."""
    return len(text) // 3 + 1


def extract_json(text: str) -> Any:
    """Parse the first JSON object or array in a model reply.

    Handles ``<think>`` blocks, markdown fences and leading/trailing chatter.

    Raises:
        ValueError: If no JSON value can be decoded.
    """
    text = _THINK_RE.sub("", text).strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return decoder.raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object found in model reply")


@dataclass
class CallRecord:
    """One LLM call, for the QA report."""

    agent: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    ok: bool


@dataclass
class UsageLedger:
    """Accumulates :class:`CallRecord` entries across a run."""

    calls: list[CallRecord] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        by_model: dict[str, dict[str, float]] = {}
        for c in self.calls:
            m = by_model.setdefault(c.model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0})
            m["calls"] += 1
            m["prompt_tokens"] += c.prompt_tokens
            m["completion_tokens"] += c.completion_tokens
            m["seconds"] += c.seconds
        return {"total_calls": len(self.calls), "failed_calls": sum(not c.ok for c in self.calls), "by_model": by_model}


class LLMClient:
    """Chat client returning parsed JSON.

    Args:
        settings: Runtime settings (endpoint, budgets, backoff).
    """

    def __init__(self, settings: Settings) -> None:
        assert_on_prem(settings.base_url)
        self.settings = settings
        self._client = OpenAI(api_key=settings.api_key, base_url=settings.base_url,
                              timeout=settings.request_timeout_s, max_retries=0)
        self._json_mode: dict[str, bool] = {}
        self.ledger = UsageLedger()

    def chat_json(self, *, agent: str, model: str, system: str, user: str,
                  max_tokens: int | None = None, json_retries: int = 2) -> Any:
        """Send one chat request and return the decoded JSON reply.

        Args:
            agent: Calling agent name (for the ledger).
            model: Model id.
            system: System prompt.
            user: User prompt.
            max_tokens: Completion cap (defaults to settings).
            json_retries: Extra attempts when the reply is not valid JSON.

        Raises:
            PromptTooLargeError: If the prompt is over budget.
            LLMResponseError: If no valid JSON is produced.
        """
        tokens = estimate_tokens(system) + estimate_tokens(user)
        if tokens > self.settings.max_prompt_tokens:
            raise PromptTooLargeError(f"{agent}: prompt ~{tokens} tokens exceeds budget {self.settings.max_prompt_tokens}")
        messages: list[dict[str, str]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error = ""
        for attempt in range(json_retries + 1):
            reply = self._complete(agent, model, messages, max_tokens or self.settings.max_output_tokens)
            try:
                return extract_json(reply)
            except ValueError as exc:
                last_error = str(exc)
                logger.warning("%s: invalid JSON from %s (attempt %d)", agent, model, attempt + 1)
                messages = messages[:2] + [
                    {"role": "assistant", "content": reply[:4000]},
                    {"role": "user", "content": "Your reply was not valid JSON. Reply again with only the JSON object."},
                ]
        raise LLMResponseError(f"{agent}: {last_error}")

    def _complete(self, agent: str, model: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        use_json = self._json_mode.get(model, True)
        backoff = list(self.settings.rate_limit_backoff_s)
        while True:
            start = time.perf_counter()
            kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": self.settings.temperature,
                                      "max_tokens": max_tokens}
            if use_json:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = self._client.chat.completions.create(**kwargs)
            except (openai.RateLimitError, openai.BadRequestError) as exc:
                # GenAI Studio reports rate limits as HTTP 400 "Rate limit exceeded".
                if isinstance(exc, openai.RateLimitError) or "rate limit" in str(exc).lower():
                    if not backoff:
                        raise
                    wait = backoff.pop(0)
                    logger.warning("%s: rate limited by %s, retrying in %.0fs", agent, model, wait)
                    time.sleep(wait)
                    continue
                if use_json and "response_format" in str(exc).lower():
                    logger.info("%s does not support JSON mode; using prompt-only JSON", model)
                    self._json_mode[model] = use_json = False
                    continue
                if "context" in str(exc).lower():
                    raise PromptTooLargeError(f"{agent}: {exc}") from exc
                raise
            except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as exc:
                if not backoff:
                    raise
                wait = backoff.pop(0)
                logger.warning("%s: %s from %s, retrying in %.0fs", agent, type(exc).__name__, model, wait)
                time.sleep(wait)
                continue
            elapsed = time.perf_counter() - start
            content = resp.choices[0].message.content or ""
            usage = resp.usage
            self.ledger.calls.append(CallRecord(agent, model, getattr(usage, "prompt_tokens", 0) or 0,
                                                getattr(usage, "completion_tokens", 0) or 0, elapsed, bool(content)))
            logger.info("%s -> %s: %d prompt / %d completion tokens in %.1fs", agent, model,
                        getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0, elapsed)
            if not content and resp.choices[0].finish_reason == "length":
                logger.warning("%s: %s hit the completion limit before answering", agent, model)
            return content
