"""Response equivalence: the oracle the regression test cannot provide."""

from __future__ import annotations

from aftermerge.patcher.equivalence import ResponseSnapshot, compare


def snap(ref: str, body: bytes, status: int = 200) -> ResponseSnapshot:
    return ResponseSnapshot(ref=ref, status_code=status, body=body)


BODY = b'{"count": 50, "orders": [{"id": 1, "items": []}]}'


def test_identical_responses_are_equivalent() -> None:
    result = compare(snap("good", BODY), snap("patched", BODY))
    assert result.equivalent
    assert "identical" in result.summary


def test_a_truncated_response_is_caught() -> None:
    """The failure mode the regression test cannot see.

    A fix that returns fewer rows makes database work constant and passes the
    scaling assertion perfectly, while being broken.
    """
    shorter = b'{"count": 5, "orders": []}'
    result = compare(snap("good", BODY), snap("patched", shorter))

    assert not result.equivalent
    assert "body length" in result.summary


def test_a_same_length_difference_is_caught() -> None:
    mutated = bytearray(BODY)
    mutated[10] = ord("9")
    result = compare(snap("good", BODY), snap("patched", bytes(mutated)))

    assert not result.equivalent
    assert "differ at byte" in result.summary


def test_a_status_change_is_caught() -> None:
    result = compare(snap("good", BODY), snap("patched", BODY, status=500))
    assert not result.equivalent
    assert "status 200 became 500" in result.summary


def test_declared_normalisations_are_applied() -> None:
    left = b'{"created_at": "2026-01-01T00:00:00", "id": 1}'
    right = b'{"created_at": "2026-06-30T12:34:56", "id": 1}'

    assert not compare(snap("good", left), snap("patched", right)).equivalent

    result = compare(
        snap("good", left), snap("patched", right), normalisations=(r'"created_at": "[^"]*"',)
    )
    assert result.equivalent
    assert "after 1 normalisation" in result.summary


def test_normalisations_cannot_hide_a_real_difference() -> None:
    left = b'{"created_at": "A", "count": 50}'
    right = b'{"created_at": "B", "count": 5}'
    result = compare(
        snap("good", left), snap("patched", right), normalisations=(r'"created_at": "[^"]*"',)
    )
    assert not result.equivalent
