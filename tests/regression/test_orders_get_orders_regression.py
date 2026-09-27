from aftermerge.reproducer.envelope import RequestEnvelope
from aftermerge.reproducer.replay import replay


def test_get_orders_does_not_scale_database_work(sandbox_under_test):
    baseline_db_ops = 2.0
    candidate_db_ops = 50.8

    result = replay(
        sandbox_under_test,
        RequestEnvelope.get("/orders", limit="50"),
        repeat=5,
    )

    assert not result.failures, f"replay failures: {result.failures}"

    max_allowed_db_ops = baseline_db_ops * 2

    assert result.db_spans_per_request <= max_allowed_db_ops, (
        f"GET /orders database work regressed: expected around "
        f"{baseline_db_ops} db ops/request (baseline cbb4790), but measured "
        f"{result.db_spans_per_request} db ops/request "
        f"(candidate observed {candidate_db_ops} db ops/request), "
        f"exceeding allowed threshold of {max_allowed_db_ops}"
    )
