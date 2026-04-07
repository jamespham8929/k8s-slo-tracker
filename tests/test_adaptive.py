"""Tests for the adaptive burn-rate engine.

These tests encode the behavior that motivates the whole project: a single
error on a low-traffic service must not page, but a sustained burn must.
"""

import pytest

from slo_tracker.adaptive import (
    AdaptiveBurnRateEngine,
    Estimator,
    Severity,
    min_samples_for_burn,
)
from slo_tracker.measurement import MultiWindowMeasurement, WindowMeasurement


def measurement(service="checkout", slo="availability", **windows):
    """Build a MultiWindowMeasurement from window=(good, total) kwargs."""
    win = {
        name: WindowMeasurement(window=name, good_count=good, total_count=total)
        for name, (good, total) in windows.items()
    }
    return MultiWindowMeasurement(service=service, slo=slo, windows=win)


class TestMinSamples:
    def test_high_traffic_threshold(self):
        # 99.9% SLO, 14.4x burn -> 1.44% expected error rate -> ~347 reqs for 5 errors
        n = min_samples_for_burn(target=0.999, burn_rate=14.4, min_expected_errors=5.0)
        assert n == pytest.approx(347.2, abs=1.0)

    def test_lower_burn_needs_more_traffic(self):
        fast = min_samples_for_burn(0.999, 14.4)
        slow = min_samples_for_burn(0.999, 1.0)
        assert slow > fast


class TestLowTrafficFalsePage:
    def test_single_error_in_sparse_window_does_not_page(self):
        engine = AdaptiveBurnRateEngine(target=0.999)
        # 1 error in 25 requests over 5m, and the slower windows are also sparse.
        m = measurement(
            **{
                "5m": (24, 25),
                "1h": (299, 300),
                "6h": (1799, 1800),
                "3d": (8999, 9000),
                "1d": (3599, 3600),
            }
        )
        decision = engine.evaluate(m)
        assert not decision.should_page, decision.reason

    def test_sustained_burn_on_busy_service_pages(self):
        engine = AdaptiveBurnRateEngine(target=0.999)
        # 2% error rate sustained across a high-traffic 1h window (20x burn).
        m = measurement(
            **{
                "5m": (1470, 1500),
                "1h": (17640, 18000),
                "6h": (105840, 108000),
                "3d": (1500000, 1512000),
            }
        )
        decision = engine.evaluate(m)
        assert decision.should_page
        assert decision.fired_tier is not None
        assert decision.severity == Severity.CRITICAL


class TestAdaptation:
    def test_falls_back_to_slower_window_when_fast_is_sparse(self):
        engine = AdaptiveBurnRateEngine(target=0.999)
        # 1h window too sparse to decide, but 6h has enough traffic and shows burn.
        m = measurement(
            **{
                "5m": (10, 10),
                "1h": (95, 100),          # only 100 reqs, below min for 14.4x
                "30m": (48, 50),
                "6h": (17000, 18000),     # ~5.5% errors, plenty of data
                "1d": (68000, 72000),
            }
        )
        decision = engine.evaluate(m)
        # Either it adapts and fires, or escalates to a slower tier. It must not
        # silently report healthy on a real multi-percent error rate.
        assert decision.severity != Severity.NONE

    def test_all_empty_windows_report_insufficient_data(self):
        engine = AdaptiveBurnRateEngine(target=0.999)
        m = measurement(
            **{"5m": (0, 0), "1h": (0, 0), "6h": (0, 0), "3d": (0, 0), "1d": (0, 0)}
        )
        decision = engine.evaluate(m)
        assert decision.severity == Severity.INSUFFICIENT_DATA


class TestHealthyService:
    def test_no_alert_when_within_budget(self):
        engine = AdaptiveBurnRateEngine(target=0.999)
        m = measurement(
            **{
                "5m": (1500, 1500),
                "1h": (17998, 18000),     # 0.01% errors, well under budget
                "6h": (107995, 108000),
                "3d": (1511990, 1512000),
            }
        )
        decision = engine.evaluate(m)
        assert decision.severity == Severity.NONE


class TestEstimatorParity:
    def test_bayesian_estimator_also_suppresses_sparse_false_page(self):
        engine = AdaptiveBurnRateEngine(target=0.999, estimator=Estimator.BAYESIAN)
        m = measurement(
            **{
                "5m": (24, 25),
                "1h": (299, 300),
                "6h": (1799, 1800),
                "3d": (8999, 9000),
                "1d": (3599, 3600),
            }
        )
        decision = engine.evaluate(m)
        assert not decision.should_page
