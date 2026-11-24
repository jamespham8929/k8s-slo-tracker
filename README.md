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
- [`slo_tracker/alert_generator.py`](slo_tracker/alert_generator.py) - exports
  the ladder as Prometheus rules and as a PrometheusRule manifest
- [`slo_tracker/query.py`](slo_tracker/query.py) - the window substitution both
  the evaluator and the generator go through
- [`docs/adr/`](docs/adr/) - architecture decision records explaining the
  statistical choices

## Configuration

Define SLOs in YAML (see [`config/slos.yaml`](config/slos.yaml)):

```yaml
services:
  - name: checkout-api
    namespace: payments
    team: payments
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

# Put the thresholds in front of Prometheus itself
slo-tracker generate-alerts --config config/slos.yaml | kubectl apply -f -

# Or a plain rules file, for a rule_files entry or for promtool
slo-tracker generate-alerts --config config/slos.yaml --format rules --output alerts.yaml
```

## Getting the rules into Prometheus

`generate-alerts` turns each SLO into recording rules and burn-rate alerts, using
the same ladder the live engine uses, so the two cannot drift apart on
thresholds or windows.

```yaml
- record: slo:sli_error:ratio_rate1h
  expr: |
    1 - (
      sum(increase(http_requests_total{service="checkout-api",code=~"2.."}[1h]))
      /
      sum(increase(http_requests_total{service="checkout-api"}[1h]))
    )
  labels: {service: checkout-api, slo: availability, namespace: payments, team: payments}

- alert: SLOErrorBudgetBurnCritical
  expr: |
    (
      slo:sli_error:ratio_rate1h{slo="availability", service="checkout-api"} > 0.0144
      and
      slo:sli_error:ratio_rate5m{slo="availability", service="checkout-api"} > 0.0144
    )
    or
    (
      slo:sli_error:ratio_rate6h{slo="availability", service="checkout-api"} > 0.006
      and
      slo:sli_error:ratio_rate30m{slo="availability", service="checkout-api"} > 0.006
    )
```

Four things about that output are deliberate, and
[ADR 0004](docs/adr/0004-exporting-rules-to-prometheus.md) covers why:

- **The expressions come from your config, not from a metric name baked into the
  generator.** A gRPC service gets rules that measure gRPC.
- **Recording rules and the alerts that read them share a group.** Prometheus
  runs groups concurrently and a group's rules in order, so splitting them leaves
  every alert reading a stale evaluation.
- **The default output is a PrometheusRule carrying a selector label.** The
  operator ignores a manifest its `ruleSelector` does not match, which applies
  cleanly, reports success, and then does nothing forever.
- **The alert names match the rest of the platform's alerting**, so the same
  routing, inhibition, and runbooks apply either way, and every alert carries the
  owning `team` label Alertmanager routes on. An alert without one reaches
  whatever catch-all receiver exists, which is nobody's pager.

What does not survive the export is the confidence gating. PromQL has no
reasonable way to express a Wilson lower bound, so these rules behave like the
textbook method: on a service doing six requests a minute, a single failed
request will page.

### Keeping the gating with the daemon

`slo-tracker serve` closes that gap. It runs the gated engine every minute and
serves each decision on `:9105/metrics`. `generate-alerts --engine gated` then
writes alerts that only read the decision, under the same names and labels as
the static rules, so routing and runbooks stay the same.

```bash
slo-tracker serve --config config/slos.yaml --prometheus http://prometheus:9090
slo-tracker generate-alerts --config config/slos.yaml --engine gated | kubectl apply -f -
```

```yaml
- alert: SLOErrorBudgetBurnCritical
  expr: |
    max without (instance, pod, job)
      (slo_tracker_alert_state{service="checkout-api", slo="availability", severity="critical"}) == 1
  for: 1m
```

A failed evaluation keeps the last decision, so a slow Prometheus during an
incident cannot resolve a real page. A daemon that stops evaluating fires
`SLOTrackerStale`, because while it is down no gated alert can page. The
[Dockerfile](Dockerfile) and [deploy/daemon.yaml](deploy/daemon.yaml) run it
next to kube-prometheus-stack. See
[ADR 0005](docs/adr/0005-a-daemon-for-gated-alerts.md).

## Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v --cov=slo_tracker
```

The tests in [`tests/test_adaptive.py`](tests/test_adaptive.py) encode the
behavior that motivates the project: a single error on a sparse service must not
page, and a sustained burn must.

CI runs the suite on Python 3.10 through 3.13 with a 95% coverage floor, lints
with ruff, and runs the false-page simulation as a smoke check so the benchmark
cannot rot. A separate job generates rules from the shipped config and hands them
to `promtool check rules` and the PrometheusRule to `kubeconform`, because "the
generator produced YAML" and "Prometheus will load it" are different claims.

## Limitations

- Confidence gating adds detection latency on the very first burst of a fast,
  severe outage, because it waits for enough samples to be sure. For high-traffic
  services this is sub-second and irrelevant. For low-traffic services it is the
  correct tradeoff, you were never going to get a statistically sound fast signal
  from sparse data anyway.
- The Beta-Binomial prior is currently per-SLO, not learned from history. Learning
  a seasonal prior is future work (see [ADR 0003](docs/adr/0003-sparse-traffic-handling.md)).
- Static exported rules cannot do confidence gating. The gated engine keeps it,
  at the cost of one more process to run and watch.
- Latency SLOs are carried in the config and evaluated nowhere. `generate-alerts`
  reports them as skipped rather than emitting alerts that could never fire.

## License

MIT
