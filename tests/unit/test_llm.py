"""Constructing a model client, and refusing clearly when it cannot."""

from __future__ import annotations

import pytest

from aftermerge import llm


def test_a_missing_key_refuses_rather_than_degrading(monkeypatch) -> None:
    """Silently falling back to the template would make provenance a lie.

    Every candidate records what produced it, so a run asked for a model must
    either use one or fail -- never quietly substitute something else.
    """
    monkeypatch.delenv(llm.API_KEY_ENV, raising=False)

    with pytest.raises(llm.LLMUnavailable) as excinfo:
        llm.get_client()

    message = str(excinfo.value)
    assert llm.API_KEY_ENV in message
    assert "deterministic" in message, "the refusal should name the working alternative"


def test_availability_reflects_the_environment(monkeypatch) -> None:
    monkeypatch.delenv(llm.API_KEY_ENV, raising=False)
    assert llm.available() is False
    monkeypatch.setenv(llm.API_KEY_ENV, "sk-ant-test")
    assert llm.available() is True


def test_a_client_is_built_when_a_key_is_present(monkeypatch) -> None:
    monkeypatch.setenv(llm.API_KEY_ENV, "sk-ant-test")
    assert llm.get_client() is not None


def test_the_model_is_overridable(monkeypatch) -> None:
    monkeypatch.delenv(llm.MODEL_ENV, raising=False)
    assert llm.model_name() == llm.DEFAULT_MODEL
    monkeypatch.setenv(llm.MODEL_ENV, "claude-opus-5")
    assert llm.model_name() == "claude-opus-5"
