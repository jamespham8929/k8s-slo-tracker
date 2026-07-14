"""Tests for the count-based measurement types."""

from slo_tracker.measurement import MultiWindowMeasurement, WindowMeasurement


class TestWindowMeasurement:
    def test_bad_count_is_total_minus_good(self):
        m = WindowMeasurement(window="1h", good_count=95, total_count=100)
        assert m.bad_count == 5

    def test_bad_count_never_negative(self):
        # good > total should not produce a negative bad count.
        m = WindowMeasurement(window="1h", good_count=110, total_count=100)
        assert m.bad_count == 0.0

    def test_error_ratio(self):
        m = WindowMeasurement(window="1h", good_count=90, total_count=100)
        assert m.error_ratio == 0.1

    def test_error_ratio_is_zero_for_empty_window(self):
        m = WindowMeasurement(window="5m", good_count=0, total_count=0)
        assert m.error_ratio == 0.0

    def test_is_empty(self):
        assert WindowMeasurement("5m", 0, 0).is_empty
        assert not WindowMeasurement("5m", 1, 1).is_empty


class TestMultiWindowMeasurement:
    def test_get_returns_window(self):
        w = WindowMeasurement("1h", 99, 100)
        m = MultiWindowMeasurement(service="checkout", slo="availability", windows={"1h": w})
        assert m.get("1h") is w

    def test_get_missing_window_returns_none(self):
        m = MultiWindowMeasurement(service="checkout", slo="availability", windows={})
        assert m.get("6h") is None
