"""Confidence-gated, traffic-adaptive burn-rate alerting.

This is the heart of the project. It replaces the naive rule

    page if observed_burn_rate >= threshold

with

    page if the LOWER confidence bound of the burn rate >= threshold
            AND the window holds enough samples to detect that burn

and, when a fast window is too sparse to decide, it adapts by borrowing the
next slower window instead of going blind.

Why this matters
----------------
Multi-window burn-rate alerting from the Google SRE workbook is the industry
default. Its blind spot is sample size. On a service doing 5 requests a minute,
the 5-minute window holds ~25 requests. One error is a 4% error ratio, which at
a 99.9% SLO is a burn rate of 40x, far past any paging threshold. You get paged
for a single blip. Teams "solve" this by muting low-traffic services, which
means they get no SLO coverage at all.

The approach here keeps coverage while killing the false pages: a single error
in 25 requests produces a wide confidence interval whose lower bound is still
low, so it does not page. Sustained errors tighten the interval and the lower
bound crosses the threshold, so a real burn still pages, just a beat later and
with statistical justification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .confidence import BetaPrior, Interval, beta_binomial_interval, wilson_interval
from .measurement import MultiWindowMeasurement, WindowMeasurement


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    NONE = "none"
    INSUFFICIENT_DATA = "insufficient_data"


class Estimator(str, Enum):
    WILSON = "wilson"
    BAYESIAN = "bayesian"


@dataclass(frozen=True)
class BurnWindow:
    """One rung of the multi-window ladder."""

    long_window: str
    short_window: str
    burn_rate: float
    severity: Severity


# Google SRE workbook 4-window, 3-tier ladder.
DEFAULT_LADDER: tuple[BurnWindow, ...] = (
    BurnWindow("1h", "5m", 14.4, Severity.CRITICAL),
    BurnWindow("6h", "30m", 6.0, Severity.CRITICAL),
    BurnWindow("3d", "6h", 1.0, Severity.WARNING),
)

# When a long window cannot decide, fall back to this slower window.
ADAPTIVE_FALLBACK: dict[str, str] = {
    "1h": "6h",
    "6h": "1d",
    "3d": "3d",
}


@dataclass
class WindowVerdict:
    window: str
    observed_burn: float
    lower_bound_burn: float
    interval: Interval
    sample_count: float
    min_samples_required: float
    sufficient_data: bool
    confident_breach: bool


@dataclass
class AlertDecision:
    service: str
    slo: str
    severity: Severity
    fired_tier: BurnWindow | None
    reason: str
    used_fallback: bool = False
    verdicts: list[WindowVerdict] = field(default_factory=list)

    @property
    def should_page(self) -> bool:
        return self.severity == Severity.CRITICAL

    @property
    def should_ticket(self) -> bool:
        return self.severity == Severity.WARNING


def min_samples_for_burn(
    target: float,
    burn_rate: float,
    min_expected_errors: float = 5.0,
) -> float:
    """Minimum window traffic needed before a burn at `burn_rate` is detectable.

    At the alert threshold the expected error rate is burn_rate * (1 - target).
    To distinguish that from noise we want at least `min_expected_errors`
    expected failures in the window. Below that the window simply cannot carry a
    confident decision, no matter the observed ratio.

    Example: target 99.9%, burn 14.4x -> expected error rate 1.44%. To expect 5
    errors you need about 347 requests in the window. A service doing 5 rps clears
    that in the 1h window (18000 reqs) but never in the 5m window (1500 reqs at
    full rate, far fewer when idle).
    """
    error_rate_at_threshold = burn_rate * max(1e-9, 1.0 - target)
    return min_expected_errors / error_rate_at_threshold


class AdaptiveBurnRateEngine:
    """Evaluates a multi-window measurement into a single alert decision."""

    def __init__(
        self,
        target: float,
        confidence: float = 0.95,
        estimator: Estimator = Estimator.WILSON,
        ladder: tuple[BurnWindow, ...] = DEFAULT_LADDER,
        min_expected_errors: float = 5.0,
        prior: BetaPrior | None = None,
    ):
        self.target = target
        self.confidence = confidence
        self.estimator = estimator
        self.ladder = ladder
        self.min_expected_errors = min_expected_errors
        self.prior = prior or BetaPrior.from_slo(target)
        self._error_budget = max(1e-9, 1.0 - target)

    def evaluate(self, measurement: MultiWindowMeasurement) -> AlertDecision:
        verdicts: list[WindowVerdict] = []

        for tier in self.ladder:
            long_v, used_fallback = self._verdict_with_adaptation(
                measurement, tier.long_window, tier.burn_rate
            )
            short_window = measurement.get(tier.short_window)
            short_v = self._window_verdict(short_window, tier.short_window, tier.burn_rate) \
                if short_window else None

            verdicts.append(long_v)

            if not long_v.sufficient_data:
                # Cannot trust this tier even after adaptation. Skip to the next,
                # slower tier rather than firing or silently passing.
                continue

            if long_v.confident_breach and self._short_window_allows_fire(short_v, tier.burn_rate):
                reason = (
                    f"{tier.long_window} burn lower-bound "
                    f"{long_v.lower_bound_burn:.1f}x >= {tier.burn_rate}x "
                    f"on {long_v.sample_count:.0f} samples "
                    f"({int(self.confidence * 100)}% confidence)"
                )
                if used_fallback:
                    reason += " [adapted from sparse window]"
                return AlertDecision(
                    service=measurement.service,
                    slo=measurement.slo,
                    severity=tier.severity,
                    fired_tier=tier,
                    reason=reason,
                    used_fallback=used_fallback,
                    verdicts=verdicts,
                )

        # Nothing fired. Distinguish "healthy" from "we never had enough data".
        if all(not v.sufficient_data for v in verdicts):
            return AlertDecision(
                measurement.service, measurement.slo, Severity.INSUFFICIENT_DATA,
                None, "no window held enough traffic to evaluate the SLO",
                verdicts=verdicts,
            )

        return AlertDecision(
            measurement.service, measurement.slo, Severity.NONE,
            None, "error budget burn within confident, sustainable bounds",
            verdicts=verdicts,
        )

    def _short_window_allows_fire(self, short_v: WindowVerdict | None, burn_rate: float) -> bool:
        """The short window guards against alerting on a spike that already
        recovered. It must not veto a confident long-window breach just because
        it is too sparse to have an opinion. So it only blocks when it has enough
        data AND its own point estimate shows the burn has dropped below threshold.
        """
        if short_v is None or not short_v.sufficient_data:
            return True
        return short_v.observed_burn >= burn_rate

    def _verdict_with_adaptation(
        self, measurement: MultiWindowMeasurement, window: str, burn_rate: float
    ) -> tuple[WindowVerdict, bool]:
        primary = measurement.get(window)
        verdict = self._window_verdict(primary, window, burn_rate)
        if verdict.sufficient_data:
            return verdict, False

        fallback_name = ADAPTIVE_FALLBACK.get(window, window)
        if fallback_name != window:
            fallback = measurement.get(fallback_name)
            if fallback is not None:
                fb_verdict = self._window_verdict(fallback, fallback_name, burn_rate)
                if fb_verdict.sufficient_data:
                    return fb_verdict, True

        return verdict, False

    def _window_verdict(
        self, m: WindowMeasurement | None, window: str, burn_rate: float
    ) -> WindowVerdict:
        if m is None:
            m = WindowMeasurement(window=window, good_count=0, total_count=0)

        interval = self._interval(m)
        observed_burn = m.error_ratio / self._error_budget
        lower_bound_burn = interval.lower / self._error_budget

        min_samples = min_samples_for_burn(self.target, burn_rate, self.min_expected_errors)
        sufficient = m.total_count >= min_samples
        confident = sufficient and lower_bound_burn >= burn_rate

        return WindowVerdict(
            window=window,
            observed_burn=observed_burn,
            lower_bound_burn=lower_bound_burn,
            interval=interval,
            sample_count=m.total_count,
            min_samples_required=min_samples,
            sufficient_data=sufficient,
            confident_breach=confident,
        )

    def _interval(self, m: WindowMeasurement) -> Interval:
        good = int(round(m.good_count))
        bad = int(round(m.bad_count))
        if self.estimator == Estimator.BAYESIAN:
            return beta_binomial_interval(good, bad, prior=self.prior, confidence=self.confidence)
        return wilson_interval(good, bad, confidence=self.confidence)
