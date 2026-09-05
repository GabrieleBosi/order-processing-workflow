"""Thin wrapper around the Anthropic SDK for the two LLM steps.

The model is used only where text is irregular (extraction from free text,
line-level risk assessment). Everything else in the pipeline is plain code.
When no credentials are configured the pipeline degrades to deterministic
heuristics, so the whole system runs (tests, evals, demo) without a key.
"""

from __future__ import annotations

import time
from typing import TypeVar

from pydantic import BaseModel

from .config import Config
from .models import LLMUsage

T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens (input, output) - used for the per-step cost profile.
MODEL_PRICES = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class LLMRefusalError(RuntimeError):
    """The model (and any fallback) declined the request."""


class LLMClient:
    """Structured-output calls with usage/cost accounting."""

    def __init__(self, config: Config, model: str | None = None):
        self.config = config
        # `model` overrides the configured one for this client only: the eval
        # judge runs on a different model from the pipeline under test.
        self.model = model or config.model
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def structured(self, system: str, user: str, output_model: type[T]) -> tuple[T, LLMUsage]:
        """One structured-output call; returns the parsed model and its usage."""
        client = self._get_client()
        started = time.monotonic()
        response = None
        if self.config.use_server_fallbacks:
            # Server-side refusal fallbacks: on a policy decline the API
            # re-runs the request on a fallback model within the same call.
            try:
                response = client.beta.messages.parse(
                    model=self.model,
                    max_tokens=self.config.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_format=output_model,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except Exception:
                response = None  # fall through to the plain call
        if response is None:
            response = client.messages.parse(
                model=self.model,
                max_tokens=self.config.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output_model,
            )
        duration_ms = (time.monotonic() - started) * 1000
        if response.stop_reason == "refusal":
            raise LLMRefusalError("model declined the request")
        usage = self._usage(response, duration_ms)
        return response.parsed_output, usage

    def _usage(self, response, duration_ms: float) -> LLMUsage:
        model = getattr(response, "model", self.model)
        input_tokens = getattr(response.usage, "input_tokens", 0) or 0
        output_tokens = getattr(response.usage, "output_tokens", 0) or 0
        price_in, price_out = MODEL_PRICES.get(model, MODEL_PRICES.get(self.model, (5.0, 25.0)))
        cost = input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out
        return LLMUsage(
            model=model,
            calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
        )


def get_llm(config: Config, model: str | None = None) -> LLMClient | None:
    """Return a client when LLM mode is enabled, else None (heuristic mode)."""
    if not config.llm_enabled():
        return None
    return LLMClient(config, model=model)
