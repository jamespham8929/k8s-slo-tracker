# k8s-slo-tracker

SLO error-budget tracking and burn-rate alerting for Kubernetes services, built
around one problem the textbook approach handles badly: **low-traffic and bursty
services.**

## The problem

The standard for SLO alerting is the multi-window, multi-burn-rate method from
the Google SRE workbook. It works well when a service handles thousands of
requests per window. It assumes the observed error ratio is a trustworthy
estimate of the true error rate.

On a service doing a few requests per minute that assumption falls apart. A
single failed request in a 5-minute window can be a 3% error ratio, which at a
99.9% SLO reads as a 30x burn rate, far past any paging threshold. The common
reactions both make things worse:

- Run a simple "5-minute error rate over X" alert. It pages on noise. At 6
  requests per minute this fires on roughly 1.5% of evaluation ticks for a
  perfectly healthy service, which is dozens of false pages a month.
- Mute the low-traffic services. Now they have no SLO coverage at all, and a
  real outage goes unnoticed.

Neither is acceptable. The interesting engineering question is how to keep
coverage on a sparse service without paging someone at 3am because one health
check timed out.

## The approach

Carry raw event counts through the whole pipeline instead of pre-computed ratios,
then reason about how much to trust each measurement.

1. **Confidence gating.** For each window, compute a confidence interval on the
   true error rate (Wilson score interval, or a Bayesian Beta-Binomial posterior
   when you want to encode a prior). Alert on the *lower bound* of the burn rate,
   not the point estimate. One error in 25 requests has a wide interval whose
   lower bound is near zero, so it does not page. A sustained burn tightens the
   interval until the lower bound crosses the threshold, so a real problem still
   pages, with statistical justification attached to the alert.

2. **Sample-size awareness.** A window that cannot hold enough events to detect a
   given burn is marked insufficient rather than evaluated. The minimum is
   derived from the SLO and the burn rate (see [ADR 0002](docs/adr/0002-confidence-gated-burn-rate.md)).

3. **Traffic adaptation.** When a fast window is too sparse to decide, the engine
   borrows the next slower window instead of going blind. When every window is
   starved it returns an explicit `insufficient_data` state, so a starved SLO is
   visible on a dashboard instead of showing a misleading green.

## Results

From [`benchmarks/false_page_simulation.py`](benchmarks/false_page_simulation.py),
a 99.9% SLO on a 6 req/min service, 3000 evaluation ticks, fixed seed:

| Strategy | False pages (healthy service) | Detection (real 2% burn) |
|----------|------------------------------|--------------------------|
| Single 5m threshold | 1.57% | 44.97% |
| SRE-workbook multi-window | 0.00% | 91.70% |
| Confidence-gated (this project) | 0.00% | 100.00% |

Two honest takeaways. The confidence-gated engine matches the best-practice
multi-window method on false pages, it does not beat something already at zero.
Where it pulls ahead is detection on sparse traffic: the textbook method
sometimes cannot confirm a real burn on its short window and misses ticks, while
the adaptive engine borrows a slower window and catches every one. Reproduce with:

```bash
PYTHONPATH=. python benchmarks/false_page_simulation.py --rps 0.1 --ticks 3000
```

## Architecture

```
Prometheus  ->  counts per window  ->  AdaptiveBurnRateEngine  ->  AlertDecision
                (good/total)            confidence + adaptation     severity + reason
                                                                    + per-window verdicts
```

- [`slo_tracker/confidence.py`](slo_tracker/confidence.py) - Wilson and
  Beta-Binomial interval estimators, no scipy required
- [`slo_tracker/adaptive.py`](slo_tracker/adaptive.py) - the engine
- [`slo_tracker/measurement.py`](slo_tracker/measurement.py) - count-based
  measurement types
- [`slo_tracker/alert_generator.py`](slo_tracker/alert_generator.py) - emits
  Prometheus recording and alerting rules
- [`docs/adr/`](docs/adr/) - architecture decision records explaining the
  statistical choices

## Configuration

Define SLOs in YAML (see [`config/slos.yaml`](config/slos.yaml)):

```yaml
services:
  - name: checkout-api
    namespace: payments
    slos:
      - name: availability
        type: availability
        target: 0.999
        window: 30d
        good_query: sum(increase(http_requests_total{service="checkout-api",code=~"2.."}[WINDOW]))
        total_query: sum(increase(http_requests_total{service="checkout-api"}[WINDOW]))
```

The queries use `increase(...)` over counters so the tracker gets event counts,
not rates. `WINDOW` is substituted per evaluation window.

## Usage

```bash
pip install -r requirements.txt

# Confidence-gated, traffic-adaptive evaluation. This is the path that stays
# sane on low-traffic services. Output shows severity, the burn-rate lower
# bound, the sample count behind the decision, and a human-readable reason.
python -m slo_tracker.cli evaluate --config config/slos.yaml --prometheus http://prometheus:9090

# Same, with a Bayesian estimator and a tighter confidence level
python -m slo_tracker.cli evaluate --config config/slos.yaml --estimator bayesian --confidence 0.99

# Simple ratio-based status table (the naive view, kept for comparison)
python -m slo_tracker.cli status --config config/slos.yaml --prometheus http://prometheus:9090

# Generate Prometheus alert rules (thresholds derive from the same ladder)
python -m slo_tracker.cli generate-alerts --config config/slos.yaml --output alerts.yaml
```

## Tests

```bash
pip install pytest
PYTHONPATH=. pytest tests/ -v
```

The tests in [`tests/test_adaptive.py`](tests/test_adaptive.py) encode the
behavior that motivates the project: a single error on a sparse service must not
page, and a sustained burn must.

## Limitations

- Confidence gating adds detection latency on the very first burst of a fast,
  severe outage, because it waits for enough samples to be sure. For high-traffic
  services this is sub-second and irrelevant. For low-traffic services it is the
  correct tradeoff, you were never going to get a statistically sound fast signal
  from sparse data anyway.
- The Beta-Binomial prior is currently per-SLO, not learned from history. Learning
  a seasonal prior is future work (see [ADR 0003](docs/adr/0003-sparse-traffic-handling.md)).

## License

MIT
