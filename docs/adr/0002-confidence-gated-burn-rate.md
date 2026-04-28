# 2. Gate burn-rate alerts on a confidence lower bound

Date: 2025-02-21

## Status

Accepted

## Context

The multi-window burn-rate method compares an observed error ratio against a
threshold. The observed ratio is a maximum-likelihood estimate of the true error
rate, and like any MLE it has variance that depends on sample size. At high
traffic the variance is negligible. At low traffic it dominates.

Concretely, at a 99.9% SLO the 14.4x paging threshold corresponds to a 1.44%
error rate. A 5-minute window on a 6 req/min service holds about 30 requests. A
single error is a 3.3% observed rate, which trips the threshold, even though one
error in 30 tells you almost nothing about the true rate.

## Decision

Estimate a confidence interval on the true error rate for each window and alert
on the lower bound of the implied burn rate, not the point estimate.

Default estimator is the Wilson score interval. It is well behaved at small n and
at proportions near 0 or 1, where the normal approximation produces nonsense like
negative lower bounds. A Bayesian Beta-Binomial posterior is available when a
team wants to encode a prior belief about the service's error rate.

Pair this with a minimum sample size per window. To detect a burn of rate `B` at
SLO target `t`, the expected error rate at threshold is `B * (1 - t)`. We require
at least `min_expected_errors` (default 5) expected failures in the window before
we trust it:

    n_min = min_expected_errors / (B * (1 - t))

Below `n_min` the window is marked insufficient rather than evaluated.

## Consequences

- A single error on a sparse service no longer pages. The lower bound stays low
  until errors are sustained enough to tighten the interval.
- Real burns still page, a beat later than a naive threshold, with the sample
  count and confidence level attached to the alert for the responder.
- There is a detection-latency cost on the first moments of a fast severe
  outage. For high-traffic services this is sub-second. For low-traffic services
  a statistically sound fast signal was never available from the data anyway, so
  this is the honest tradeoff rather than a regression.
- The benchmark confirms the engine matches the SRE-workbook multi-window method
  on healthy-service false pages (both at 0%) and exceeds it on detection of a
  real burn on sparse traffic (100% vs 91.7% of ticks).
