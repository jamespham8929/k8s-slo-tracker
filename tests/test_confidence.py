"""Tests for the statistical confidence estimators."""

import math

import pytest

from slo_tracker.confidence import (
    BetaPrior,
    beta_binomial_interval,
    wilson_interval,
    z_for_confidence,
)


class TestZScores:
    def test_known_values(self):
        assert z_for_confidence(0.95) == pytest.approx(1.96, abs=0.01)
        assert z_for_confidence(0.99) == pytest.approx(2.576, abs=0.01)
        assert z_for_confidence(0.90) == pytest.approx(1.645, abs=0.01)

    def test_interpolated_value_is_monotonic(self):
        assert z_for_confidence(0.975) > z_for_confidence(0.95)
        assert z_for_confidence(0.85) < z_for_confidence(0.95)


class TestWilsonInterval:
    def test_empty_window_is_maximally_uncertain(self):
        iv = wilson_interval(0, 0)
        assert iv.lower == 0.0
        assert iv.upper == 1.0

    def test_single_error_in_small_window_has_low_lower_bound(self):
        # 1 error in 25 requests. Point estimate is 4%, but we should not be
        # confident the true rate is high. This is the false-page case.
        iv = wilson_interval(successes=24, failures=1)
        assert iv.point == pytest.approx(0.04, abs=0.001)
        assert iv.lower < 0.01, "lower bound must stay low on a single error"

    def test_large_sample_tightens_interval(self):
        small = wilson_interval(successes=24, failures=1)
        large = wilson_interval(successes=2400, failures=100)
        assert large.width < small.width
        # With lots of data, the lower bound approaches the true 4%.
        assert large.lower > 0.03

    def test_bounds_are_clamped_to_unit_interval(self):
        iv = wilson_interval(successes=0, failures=10)
        assert 0.0 <= iv.lower <= iv.upper <= 1.0

    def test_higher_confidence_widens_interval(self):
        narrow = wilson_interval(50, 5, confidence=0.80)
        wide = wilson_interval(50, 5, confidence=0.99)
        assert wide.width > narrow.width


class TestBetaPrior:
    def test_uniform_prior_mean(self):
        assert BetaPrior().mean == pytest.approx(0.5)

    def test_prior_from_slo_centers_on_error_budget(self):
        prior = BetaPrior.from_slo(target=0.999, strength=5.0)
        assert prior.mean == pytest.approx(0.001, abs=1e-6)
        assert prior.alpha + prior.beta == pytest.approx(5.0)


class TestBetaBinomialInterval:
    def test_zero_data_returns_prior_mean(self):
        prior = BetaPrior.from_slo(target=0.999, strength=5.0)
        iv = beta_binomial_interval(0, 0, prior=prior)
        assert iv.point == pytest.approx(prior.mean, abs=1e-6)

    def test_data_overrides_prior(self):
        prior = BetaPrior.from_slo(target=0.999, strength=5.0)
        iv = beta_binomial_interval(successes=900, failures=100, prior=prior)
        # Posterior should move strongly toward the observed 10% error rate.
        assert iv.point > 0.08

    def test_point_estimate_within_bounds(self):
        iv = beta_binomial_interval(successes=100, failures=10)
        assert iv.lower <= iv.point <= iv.upper
