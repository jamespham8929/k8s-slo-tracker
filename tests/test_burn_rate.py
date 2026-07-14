"""Tests for the multi-window burn-rate helpers."""

from slo_tracker.adaptive import DEFAULT_LADDER
from slo_tracker.burn_rate import (
    STANDARD_WINDOWS,
    burn_rate_to_exhaustion_hours,
    compute_multi_window_alert_severity,
    is_paging_alert,
)


class TestStandardWindows:
    def test_derived_from_the_engine_ladder(self):
        # The windows must stay in lockstep with the engine's ladder so the
        # generated Prometheus rules and the live engine cannot disagree.
        assert len(STANDARD_WINDOWS) == len(DEFAULT_LADDER)
        for window, tier in zip(STANDARD_WINDOWS, DEFAULT_LADDER, strict=True):
            assert window.long_window == tier.long_window
            assert window.short_window == tier.short_window
            assert window.burn_rate_threshold == tier.burn_rate
            assert window.severity == tier.severity.value


class TestExhaustionHours:
    def test_one_x_burn_exhausts_the_window(self):
        assert burn_rate_to_exhaustion_hours(1.0, window_days=30) == 720.0

    def test_faster_burn_exhausts_sooner(self):
        assert burn_rate_to_exhaustion_hours(14.4, window_days=30) == 720.0 / 14.4

    def test_non_positive_burn_never_exhausts(self):
        assert burn_rate_to_exhaustion_hours(0.0) == float("inf")
        assert burn_rate_to_exhaustion_hours(-1.0) == float("inf")


class TestIsPagingAlert:
    def test_pages_when_one_hour_burn_at_page_threshold(self):
        assert is_paging_alert(burn_rate_1h=14.4, burn_rate_6h=0.0)

    def test_pages_when_six_hour_burn_at_its_threshold(self):
        assert is_paging_alert(burn_rate_1h=0.0, burn_rate_6h=6.0)

    def test_no_page_when_both_below_threshold(self):
        assert not is_paging_alert(burn_rate_1h=5.0, burn_rate_6h=5.0)


class TestMultiWindowSeverity:
    def test_fast_burn_is_critical(self):
        severity = compute_multi_window_alert_severity(
            burn_rate_1h=15, burn_rate_5m=15,
            burn_rate_6h=0, burn_rate_30m=0,
            burn_rate_3d=0, burn_rate_6h_slow=0,
        )
        assert severity == "critical"

    def test_medium_burn_is_critical(self):
        severity = compute_multi_window_alert_severity(
            burn_rate_1h=0, burn_rate_5m=0,
            burn_rate_6h=7, burn_rate_30m=7,
            burn_rate_3d=0, burn_rate_6h_slow=0,
        )
        assert severity == "critical"

    def test_slow_burn_is_warning(self):
        severity = compute_multi_window_alert_severity(
            burn_rate_1h=0, burn_rate_5m=0,
            burn_rate_6h=0, burn_rate_30m=0,
            burn_rate_3d=2, burn_rate_6h_slow=2,
        )
        assert severity == "warning"

    def test_single_window_breach_does_not_fire(self):
        # A fast window spiking without its confirming window must not fire.
        severity = compute_multi_window_alert_severity(
            burn_rate_1h=15, burn_rate_5m=0,
            burn_rate_6h=0, burn_rate_30m=0,
            burn_rate_3d=0, burn_rate_6h_slow=0,
        )
        assert severity == "none"
