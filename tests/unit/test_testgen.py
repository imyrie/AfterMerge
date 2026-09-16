"""Test generation: context derivation and the emitted source."""

from __future__ import annotations

from aftermerge.testgen.context import TestContext
from aftermerge.testgen.generator import AnthropicGenerator, TemplateGenerator


def context(**overrides) -> TestContext:
    base = dict(
        service="orders",
        route="GET /orders",
        baseline_version="cbb4790",
        candidate_version="8b4fd77",
        method="GET",
        path="/orders",
        query={"limit": "50"},
        baseline_spans_per_request=2.0,
        candidate_spans_per_request=51.0,
        code_site="orders/repository.py",
    )
    base.update(overrides)
    return TestContext(**base)  # type: ignore[arg-type]


# --- context -----------------------------------------------------------------


def test_size_parameter_is_detected() -> None:
    assert context().size_parameter == ("limit", 50)
    assert context(query={"page_size": "25"}).size_parameter == ("page_size", 25)
    assert context(query={"status": "paid"}).size_parameter is None
    assert context(query={"limit": "abc"}).size_parameter is None


def test_threshold_sits_above_baseline_and_below_candidate() -> None:
    """A threshold outside that band cannot tell the two builds apart."""
    ctx = context(query={})
    assert ctx.baseline_spans_per_request <= ctx.threshold < ctx.candidate_spans_per_request


def test_evidence_that_cannot_discriminate_is_rejected() -> None:
    """Both builds doing the same work supports no test at all."""
    assert not context(candidate_spans_per_request=2.0, query={}).discriminates
    assert not context(candidate_spans_per_request=2.0).discriminates


def test_amplification_handles_a_zero_baseline() -> None:
    assert context(baseline_spans_per_request=0.0).amplification == float("inf")
    assert (
        context(baseline_spans_per_request=0.0, candidate_spans_per_request=0.0).amplification
        == 1.0
    )


# --- generated source --------------------------------------------------------


def test_generated_source_is_valid_python() -> None:
    source = TemplateGenerator().generate(context()).source
    compile(source, "generated.py", "exec")


def test_a_size_parameter_produces_a_scaling_assertion() -> None:
    """Stronger than a threshold: it encodes the N+1 property itself."""
    candidate = TemplateGenerator().generate(context())

    assert "small.db_spans_per_request" in candidate.source
    assert "large.db_spans_per_request" in candidate.source
    assert "invariant under limit" in candidate.rationale


def test_without_a_size_parameter_it_falls_back_to_a_threshold() -> None:
    candidate = TemplateGenerator().generate(context(query={}))

    assert "db_spans_per_request <= 4" in candidate.source
    assert "threshold" in candidate.rationale


def test_the_assertion_is_on_work_never_on_time() -> None:
    """A latency assertion would be flaky, and missed this regression at 1.45x."""
    source = TemplateGenerator().generate(context()).source
    asserts = [line for line in source.splitlines() if line.strip().startswith("assert")]

    assert asserts
    assert all("db_spans_per_request" in a or "failures" in a for a in asserts)
    for banned in ("duration", "elapsed", "latency", "p95", "seconds"):
        assert banned not in " ".join(asserts).lower()


def test_generated_source_cites_the_measurements() -> None:
    source = TemplateGenerator().generate(context()).source
    assert "2.0 db ops/request" in source
    assert "51.0 db ops/request" in source
    assert "orders/repository.py" in source


def test_numeric_parameters_render_as_numbers() -> None:
    source = TemplateGenerator().generate(context()).source
    assert "limit=50" in source and "limit='50'" not in source


def test_a_failed_replay_is_not_treated_as_a_pass() -> None:
    source = TemplateGenerator().generate(context()).source
    assert "failures == 0" in source


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


def test_llm_output_has_markdown_fences_stripped() -> None:
    """Models add fences despite instructions; the file must still be importable."""
    client = _FakeClient("```python\ndef test_x():\n    assert True\n```")
    candidate = AnthropicGenerator(client).generate(context())

    assert not candidate.source.startswith("```")
    compile(candidate.source, "generated.py", "exec")


def test_the_prompt_carries_the_measured_evidence() -> None:
    """The model is given facts to encode, not asked to diagnose the cause."""
    client = _FakeClient("def test_x():\n    assert True\n")
    AnthropicGenerator(client).generate(context())
    prompt = client.messages.last_prompt or ""

    assert "2.0" in prompt and "51.0" in prompt
    assert "orders/repository.py" in prompt
    assert "never on elapsed time" in prompt


def test_the_generator_is_recorded_on_the_candidate() -> None:
    """Provenance matters: a reader must know what wrote the test."""
    assert TemplateGenerator().generate(context()).generated_by == "template"
    client = _FakeClient("def test_x():\n    assert True\n")
    assert AnthropicGenerator(client).generate(context()).generated_by.startswith("anthropic:")
