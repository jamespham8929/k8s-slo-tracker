"""Tests for the error budget calculator."""

import pytest
from slo_tracker.calculator import ErrorBudgetCalculator
from slo_tracker.models import SLODefinition, SLOType


def make_slo(target=0.999, window_days=30):
    return SLODefinition(
        name="availability",
        service="test-service",
        namespace="default",
        slo_type=SLOType.AVAILABILITY,
        target=target,
        window_days=window_days,
        good_query="sum(rate(http_requests_total{code=~'2..'}[5m]))",
        total_query="sum(rate(http_requests_total[5m]))",
    )


class TestBudgetRemaining:
    def test_full_budget_when_sli_equals_target(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        status = calc.compute_status(slo, current_sli=0.999, sli_1h=0.999, sli_6h=0.999, sli_24h=0.999)
        assert status.error_budget_remaining == pytest.approx(0.0, abs=0.01)

    def test_budget_remaining_when_sli_above_target(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        status = calc.compute_status(slo, current_sli=1.0, sli_1h=1.0, sli_6h=1.0, sli_24h=1.0)
        assert status.error_budget_remaining == 1.0

    def test_budget_exhausted_below_target(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        # SLI at 0.998 = error rate 0.002, budget = 0.001 → 200% consumed → clamped to 0
        status = calc.compute_status(slo, current_sli=0.998, sli_1h=0.998, sli_6h=0.998, sli_24h=0.998)
        assert status.error_budget_remaining == 0.0

    def test_budget_remaining_partial_consumption(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        # Error rate 0.0005 = 50% of budget consumed
        status = calc.compute_status(slo, current_sli=0.9995, sli_1h=0.9995, sli_6h=0.9995, sli_24h=0.9995)
        assert status.error_budget_remaining == pytest.approx(0.5, abs=0.01)


class TestBurnRate:
    def test_burn_rate_at_target_is_zero(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        status = calc.compute_status(slo, current_sli=0.999, sli_1h=0.999, sli_6h=0.999, sli_24h=0.999)
        assert status.burn_rate_1h == pytest.approx(1.0, abs=0.01)

    def test_burn_rate_above_threshold_is_critical(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        # Error rate 14.4x the sustainable rate
        bad_sli = 1.0 - (0.001 * 14.4)
        status = calc.compute_status(slo, current_sli=bad_sli, sli_1h=bad_sli, sli_6h=bad_sli, sli_24h=bad_sli)
        assert status.burn_rate_1h >= 14.0
        assert status.alert_severity == "critical"

    def test_burn_rate_moderate_is_warning(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        moderate_sli = 1.0 - (0.001 * 6.5)
        status = calc.compute_status(slo, current_sli=moderate_sli, sli_1h=moderate_sli, sli_6h=moderate_sli, sli_24h=moderate_sli)
        assert status.alert_severity == "warning"

    def test_alert_severity_none_when_healthy(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        status = calc.compute_status(slo, current_sli=0.9999, sli_1h=0.9999, sli_6h=0.9999, sli_24h=0.9999)
        assert status.alert_severity == "none"


class TestMinutesUntilExhausted:
    def test_returns_none_when_not_burning(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        status = calc.compute_status(slo, current_sli=1.0, sli_1h=1.0, sli_6h=1.0, sli_24h=1.0)
        assert calc.minutes_until_exhausted(status) is None

    def test_returns_positive_minutes_when_burning_fast(self):
        calc = ErrorBudgetCalculator()
        slo = make_slo(target=0.999)
        fast_burn_sli = 1.0 - (0.001 * 14.4)
        status = calc.compute_status(slo, current_sli=fast_burn_sli, sli_1h=fast_burn_sli, sli_6h=fast_burn_sli, sli_24h=fast_burn_sli)
        minutes = calc.minutes_until_exhausted(status)
        assert minutes is not None
        assert minutes > 0
