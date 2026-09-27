"""Constructing a real Anthropic client, and refusing clearly when it cannot.

Generators and proposers take an injected client rather than building their own,
so their logic is testable without a key or a network. This module is the one
place that reaches for credentials, and it is deliberately the only place.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_MODEL = "claude-sonnet-5"
API_KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "AFTERMERGE_MODEL"


class LLMUnavailable(Exception):
    """Raised when a model-backed path was asked for but cannot run."""


def model_name() -> str:
    return os.environ.get(MODEL_ENV) or DEFAULT_MODEL


def get_client() -> Any:
    """An Anthropic client built from the environment.

    Fails loudly rather than degrading to the deterministic path. Silently
    substituting a template when a model was requested would make a run's
    provenance a lie, and provenance is recorded on every candidate.
    """
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        raise LLMUnavailable(
            f"{API_KEY_ENV} is not set. Export it, or use the deterministic "
            "generator, which needs no credentials."
        )
    try:
        import anthropic
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency is declared
        raise LLMUnavailable("the anthropic package is not installed; run `uv sync`") from exc

    return anthropic.Anthropic(api_key=api_key)


def available() -> bool:
    return bool(os.environ.get(API_KEY_ENV))
