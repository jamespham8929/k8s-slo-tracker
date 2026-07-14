"""Tests for the SLO evaluator that wires Prometheus counts into the engine."""

from unittest.mock import MagicMock

from slo_tracker.adaptive import Severity
from slo_tracker.evaluator import REQUIRED_WINDOWS, EvaluatedSLO, SLOEvaluator
from slo_tracker.models import SLODefinition, SLOType


def make_slo(good_query="good WINDOW", total_query="total WINDOW", target=0.999):
    return SLODefinition(
        name="availability",
        service="checkout",
        namespace="payments",
        slo_type=SLOType.AVAILABILITY,
        target=target,
        window_days=30,
        good_query=good_query,
        total_query=total_query,
    )


class TestSLOEvaluator:
    def test_healthy_service_reports_none(self):
        prom = MagicMock()
        prom.query.return_value = 18000.0  # good == total in every window
        result = SLOEvaluator(prom).evaluate(make_slo())

        assert isinstance(result, EvaluatedSLO)
        assert result.definition.name == "availability"
        assert result.decision.severity == Severity.NONE

    def test_queries_every_required_window_for_good_and_total(self):
        prom = MagicMock()
        prom.query.return_value = 1000.0
        SLOEvaluator(prom).evaluate(make_slo())

        queried = [call.args[0] for call in prom.query.call_args_list]
        assert len(queried) == len(REQUIRED_WINDOWS) * 2
        for window in REQUIRED_WINDOWS:
            assert f"good {window}" in queried
            assert f"total {window}" in queried

    def test_sustained_burn_pages(self):
        prom = MagicMock()
        # 2% error rate (20x burn at a 99.9% SLO) with high traffic in every window.
        prom.query.side_effect = lambda promql: 17640.0 if promql.startswith("good") else 18000.0
        result = SLOEvaluator(prom).evaluate(make_slo())

        assert result.decision.should_page

    def test_no_queries_configured_reports_insufficient_data(self):
        prom = MagicMock()
        result = SLOEvaluator(prom).evaluate(make_slo(good_query=None, total_query=None))

        assert result.decision.severity == Severity.INSUFFICIENT_DATA
        prom.query.assert_not_called()
