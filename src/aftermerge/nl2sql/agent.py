"""Turning a metric request into SQL, and keeping it only if it survives.

The generation itself is the easy half. The half that matters is that a model
writing SQL fails in a way that looks like success: `StatusCode =
'STATUS_CODE_ERROR'` runs, returns zero, and reads as "no errors". This repo
shipped exactly that defect by hand once, so a generated query is treated the
way a generated patch is -- proposed, gated, and discarded if the gate refuses.

Attempts are bounded and each retry is told precisely what was wrong. A repair
prompt that says only "rejected" buys a second guess; one that says
`StatusCode='STATUS_CODE_ERROR' cannot match; StatusCode is one of ['Error',
'Ok', 'Unset']` buys a correction.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from aftermerge import llm
from aftermerge.llm import TokenUsage
from aftermerge.nl2sql import execute
from aftermerge.nl2sql.guards import GateResult, gate
from aftermerge.nl2sql.schema import SchemaCard

DEFAULT_MAX_ATTEMPTS = 3
#: Generous because a six-column aggregate written with aligned `AS` clauses is
#: mostly whitespace, and a reply cut off mid-statement used to surface as a
#: baffling syntax rejection rather than as "the reply did not finish".
MAX_TOKENS = 4000

SYSTEM_PROMPT = """You write ClickHouse SQL against an OpenTelemetry span table.

Rules, all of which are enforced by a gate that will reject your answer:
- Exactly one statement. SELECT or WITH only. No DDL, no DML, no SET.
- Read only the tables in the schema below. Never system tables or table \
functions like url() or remote().
- Always bound the scan: a Timestamp predicate, or a LIMIT.
- Use only the attribute map keys listed. A key that is not listed returns an \
empty string for every row rather than failing, which silently produces a wrong \
answer.
- Use only the listed values for enum columns. A filter on an unlisted value \
matches nothing and returns zero, which reads as a real measurement.

Conventions in this data:
- User-visible request latency is on the SERVER span, not the client spans, \
which measure only the pieces.
- Deployed version is ResourceAttributes['service.version']. Comparisons across \
a deploy group by it.
- Duration is nanoseconds. Divide by 1e6 for milliseconds.

Reply with the SQL and nothing else. No prose, no markdown fence, no trailing \
semicolon."""

_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)
#: An *unclosed* fence, which is what a truncated reply leaves behind. Without
#: this the leading "```sql" reached the gate, which reported that the statement
#: started with the word SQL -- true, useless, and not the actual problem.
_OPEN_FENCE = re.compile(r"^\s*```(?:sql)?\s*", re.I)
_CLOSE_FENCE = re.compile(r"```\s*$")
#: The prompt ends with "SQL:", and a model that echoes that label back has not
#: made a mistake about the data. Stripping it is the same courtesy as stripping
#: a fence; not stripping it scored a model zero for a punctuation habit.
_LABEL = re.compile(r"^\s*(?:sql|query)\s*:?\s*\n", re.I)


@dataclass(frozen=True)
class Attempt:
    sql: str
    gate_result: GateResult
    explanation: execute.Explanation | None = None
    #: The reply hit the token ceiling, so the statement is incomplete. Tracked
    #: separately because an incomplete statement fails the gate for reasons
    #: that say nothing about whether the model understood the question.
    truncated: bool = False

    @property
    def accepted(self) -> bool:
        if self.truncated:
            return False
        return self.gate_result.accepted and (self.explanation is not None and self.explanation.ok)

    @property
    def feedback(self) -> str:
        parts: list[str] = []
        if self.truncated:
            parts.append("- [truncated] the reply hit the token limit mid-statement")
        parts.append(self.gate_result.feedback)
        if self.explanation is not None:
            parts.append(self.explanation.feedback)
        return "\n".join(p for p in parts if p)


@dataclass(frozen=True)
class Proposal:
    """What the agent produced, accepted or not."""

    request: str
    model: str
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)
    usage: TokenUsage = field(default_factory=TokenUsage)

    @property
    def accepted(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].accepted

    @property
    def sql(self) -> str:
        return self.attempts[-1].sql if self.attempts else ""

    @property
    def rejection_summary(self) -> str:
        if not self.attempts:
            return "no attempt was made"
        return self.attempts[-1].feedback or "rejected without a reason, which is a bug"

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "model": self.model,
            "accepted": self.accepted,
            "attempts": len(self.attempts),
            "sql": self.sql,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "rejections": [
                [{"guard": r.guard, "reason": r.reason} for r in a.gate_result.rejections]
                for a in self.attempts
            ],
        }


def extract_sql(text: str) -> str:
    """Pull SQL out of a reply that may have ignored the no-markdown rule.

    Tolerated rather than rejected: a fenced block is a formatting slip, not a
    wrong answer, and failing an attempt over it would measure instruction
    compliance while reporting it as SQL correctness.
    """
    fenced = _FENCE.search(text)
    body = fenced.group(1) if fenced else text
    body = _OPEN_FENCE.sub("", body.strip())
    body = _CLOSE_FENCE.sub("", body.strip())
    body = _LABEL.sub("", body.strip())
    return body.strip().rstrip(";").strip()


def _user_prompt(request: str, card: SchemaCard) -> str:
    return f"{card.render()}\n\nMetric request: {request}\n\nSQL:"


def propose(
    request: str,
    *,
    card: SchemaCard,
    client: Any,
    ch: Any | None = None,
    model: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Proposal:
    """Generate, gate, and repair up to `max_attempts` times.

    `ch` is optional so the static gate can be exercised without a warehouse,
    but an accepted proposal requires one: without `EXPLAIN` there is nothing
    confirming the identifiers resolve, and "accepted" would mean less than it
    does everywhere else in this pipeline.
    """
    model = model or llm.model_name()
    messages: list[dict[str, Any]] = [{"role": "user", "content": _user_prompt(request, card)}]
    attempts: list[Attempt] = []
    usage = TokenUsage()

    for _ in range(max_attempts):
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        usage = usage + TokenUsage.from_response(response)
        raw = "".join(getattr(block, "text", "") for block in getattr(response, "content", []))
        sql = extract_sql(raw)
        truncated = getattr(response, "stop_reason", None) == "max_tokens"

        gate_result = gate(sql)
        explanation: execute.Explanation | None = None
        if gate_result.accepted and not truncated and ch is not None:
            explanation = execute.explain(ch, sql)

        attempt = Attempt(
            sql=sql,
            gate_result=gate_result,
            explanation=explanation,
            truncated=truncated,
        )
        attempts.append(attempt)
        if attempt.accepted:
            break

        messages.append({"role": "assistant", "content": sql})
        messages.append(
            {
                "role": "user",
                "content": (
                    "That query was rejected:\n"
                    f"{attempt.feedback}\n\n"
                    "Return a corrected single SELECT. SQL only."
                ),
            }
        )

    return Proposal(request=request, model=model, attempts=tuple(attempts), usage=usage)


def requests_from(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(v.strip() for v in values if v.strip())
