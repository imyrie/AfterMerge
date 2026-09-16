"""Proposing a fix: whole files in, a valid diff out."""

from __future__ import annotations

from pathlib import Path

import pytest

from aftermerge.patcher.patch import PatchRejected
from aftermerge.patcher.proposer import (
    AnthropicProposer,
    PatchContext,
    Proposal,
    RevertProposer,
    to_patch,
)

ROOT = Path(__file__).resolve().parents[2]
TARGET = "fixtures/shopdemo/orders/repository.py"

CTX = PatchContext(
    good_ref="cbb4790",
    bad_ref="8b4fd77",
    changed_files=(TARGET,),
    code_site="orders/repository.py",
    baseline_spans_per_request=2.0,
    candidate_spans_per_request=51.0,
    causing_diff="--- a/x\n+++ b/x\n",
)


def test_the_revert_proposer_restores_the_known_good_contents() -> None:
    proposal = RevertProposer(ROOT).propose(CTX)
    assert set(proposal.files) == {TARGET}
    assert "ANY($1::bigint[])" in proposal.files[TARGET]
    assert proposal.strategy == "revert"


def test_a_proposal_becomes_a_diff_that_git_produced() -> None:
    """The proposer never has to get unified-diff syntax right."""
    proposal = RevertProposer(ROOT).propose(CTX)
    patch = to_patch(proposal, base_ref="8b4fd77", repo_root=ROOT)

    assert patch.touched_files == {TARGET}
    assert patch.diff.startswith("diff --git")
    assert "ANY($1::bigint[])" in patch.diff


def test_a_proposal_identical_to_the_broken_commit_is_rejected() -> None:
    """Returning the input unchanged is not a fix."""
    current = RevertProposer(ROOT).propose(PatchContext(**{**CTX.__dict__, "good_ref": "8b4fd77"}))
    with pytest.raises(PatchRejected, match="identical"):
        to_patch(current, base_ref="8b4fd77", repo_root=ROOT)


def test_a_path_escaping_the_repository_is_rejected() -> None:
    proposal = Proposal(files={"../escaped.py": "x = 1\n"}, strategy="repair", origin="test")
    with pytest.raises(PatchRejected, match="escapes"):
        to_patch(proposal, base_ref="8b4fd77", repo_root=ROOT)


# --- llm path ----------------------------------------------------------------


class _FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self._text = text
        self.last_prompt: str | None = None

    def create(self, **kwargs):
        self.last_prompt = kwargs["messages"][0]["content"]
        return type("Response", (), {"content": [_FakeBlock(self._text)]})()


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


def test_llm_output_has_fences_stripped() -> None:
    client = _FakeClient("```python\nx = 1\n```")
    proposal = AnthropicProposer(client).propose(CTX)
    assert proposal.files[TARGET] == "x = 1\n"
    assert proposal.strategy == "repair"


def test_the_prompt_states_the_equivalence_requirement() -> None:
    """The model must know that returning fewer rows will be rejected."""
    client = _FakeClient("x = 1\n")
    AnthropicProposer(client).propose(CTX)
    prompt = client.messages.last_prompt or ""

    assert "byte-identical" in prompt
    assert "fewer rows" in prompt
    assert "Do not change any test" in prompt
    assert "51.0" in prompt and "2.0" in prompt
