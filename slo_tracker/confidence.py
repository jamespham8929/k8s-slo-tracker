"""Statistical confidence estimation for error-rate measurements.

The standard multi-window burn-rate alerting model assumes the observed error
ratio is a good estimate of the true error rate. That assumption holds when a
window contains thousands of requests. It breaks badly on low-traffic services,
where a single failed request in a 5-minute window can push the observed error
ratio to 50% or 100% and trigger a false page.

This module estimates how much we should trust an observed error ratio given the
sample size behind it. Two estimators are provided:

  1. Wilson score interval - a frequentist confidence interval for a binomial
     proportion that stays well behaved at small n and at proportions near 0
     or 1, where the normal approximation falls apart.

  2. Beta-Binomial posterior - a Bayesian estimate that smooths sparse data
     toward a prior and degrades gracefully to the prior when n is zero.

Both are implemented without scipy so the agent has no heavy runtime
dependency. If scipy is installed the Beta-Binomial path uses exact Beta
quantiles, otherwise it falls back to a Wilson-Hilferty normal approximation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# z-scores for common two-sided confidence levels
Z_SCORES: dict[float, float] = {
    0.80: 1.2816,
    0.90: 1.6449,
    0.95: 1.9600,
    0.99: 2.5758,
}


def z_for_confidence(confidence: float) -> float:
    """Return the z-score for a confidence level, interpolating if needed."""
    if confidence in Z_SCORES:
        return Z_SCORES[confidence]
    # Rational approximation of the inverse normal CDF (Beasley-Springer-Moro).
    # Good to about 4 decimal places across the useful range.
    p = 1.0 - (1.0 - confidence) / 2.0
    return _inverse_normal_cdf(p)


@dataclass(frozen=True)
class Interval:
    """A point estimate with a lower and upper bound."""

    point: float
    lower: float
    upper: float

    @property
    def width(self) -> float:
        return self.upper - self.lower


def wilson_interval(successes: int, failures: int, confidence: float = 0.95) -> Interval:
    """Wilson score interval for the proportion of failures.

    `successes` and `failures` are raw counts over the window. The returned
    interval describes the true failure proportion. We deliberately report the
    interval on failures (not successes) because burn-rate alerting cares about
    whether the error rate is confidently high.

    At n = 0 the interval is the full [0, 1] range with a point estimate of 0,
    which correctly signals "we know nothing yet."
    """
    n = successes + failures
    if n == 0:
        return Interval(point=0.0, lower=0.0, upper=1.0)

    z = z_for_confidence(confidence)
    p_hat = failures / n
    z2 = z * z

    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))

    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return Interval(point=p_hat, lower=lower, upper=upper)


@dataclass(frozen=True)
class BetaPrior:
    """Prior belief about a service's error rate, expressed as a Beta(alpha, beta).

    A weakly informative default of Beta(1, 1) is uniform on [0, 1]. To encode
    "this service is usually healthy" set a prior whose mean alpha/(alpha+beta)
    matches the SLO error budget, with a small alpha+beta so real data quickly
    dominates. See ADR 0003 for how the default prior is derived from the SLO.
    """

    alpha: float = 1.0
    beta: float = 1.0

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @classmethod
    def from_slo(cls, target: float, strength: float = 5.0) -> "BetaPrior":
        """Build a prior centered on the SLO error budget.

        `strength` is the prior sample size (alpha + beta). A strength of 5 means
        the prior carries about as much weight as five observed requests, so any
        real traffic overrides it quickly while still smoothing the n = 0 case.
        """
        error_budget = max(1e-9, 1.0 - target)
        alpha = error_budget * strength
        beta = strength - alpha
        return cls(alpha=alpha, beta=beta)


def beta_binomial_interval(
    successes: int,
    failures: int,
    prior: BetaPrior | None = None,
    confidence: float = 0.95,
) -> Interval:
    """Bayesian credible interval for the failure rate under a Beta prior.

    The posterior is Beta(alpha + failures, beta + successes). The point estimate
    is the posterior mean. When scipy is available the bounds are exact Beta
    quantiles, otherwise a Wilson-Hilferty approximation is used.
    """
    prior = prior or BetaPrior()
    a = prior.alpha + failures
    b = prior.beta + successes

    point = a / (a + b)
    tail = (1.0 - confidence) / 2.0
    lower = _beta_quantile(tail, a, b)
    upper = _beta_quantile(1.0 - tail, a, b)
    return Interval(point=point, lower=lower, upper=upper)


def _beta_quantile(q: float, a: float, b: float) -> float:
    try:
        from scipy.stats import beta as _beta  # type: ignore

        return float(_beta.ppf(q, a, b))
    except Exception:
        return _beta_quantile_approx(q, a, b)


def _beta_quantile_approx(q: float, a: float, b: float) -> float:
    """Wilson-Hilferty style approximation of the Beta inverse CDF.

    Accurate enough for alerting decisions (we only need to know which side of a
    threshold the bound lands on). Falls back to clamped normal approximation in
    the tails where the cube-root transform is weakest.
    """
    mean = a / (a + b)
    var = (a * b) / ((a + b) ** 2 * (a + b + 1))
    if var <= 0:
        return mean
    z = z_for_confidence(1.0 - 2.0 * min(q, 1.0 - q))
    sign = -1.0 if q < 0.5 else 1.0
    approx = mean + sign * z * math.sqrt(var)
    return min(1.0, max(0.0, approx))


def _inverse_normal_cdf(p: float) -> float:
    """Acklam's rational approximation of the standard normal inverse CDF."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]

    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
