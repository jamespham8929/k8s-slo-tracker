# 5. A daemon so paging keeps the confidence gating

Date: 2026-11-23

## Status

Accepted

## Context

ADR 0004 got the SLOs into Prometheus as plain burn-rate rules. That made
paging work at 3am, but it gave up the main idea of this project. PromQL cannot
compute a Wilson or Beta-Binomial bound, so the exported rules fire on the raw
ratio. On a service doing six requests a minute, one failed request pages.
`evaluate` does the gating, but only while someone runs it.

So the choice was between rules that page reliably on a noisy signal, and an
engine with a good signal that nothing runs.

## Decision

Add `slo-tracker serve`. It runs the same `SLOEvaluator` on an interval (60
seconds by default) and exports each decision on `/metrics`:

- `slo_tracker_alert_state{severity}` is 1 for the severity the engine chose and
  0 for the others. Exactly one is 1 per SLO.
- `slo_tracker_burn_rate_lower_bound`, `slo_tracker_burn_rate_observed`,
  `slo_tracker_window_samples`, and `slo_tracker_window_sufficient_data` show
  the evidence per window. A responder can see why it paged, or why it did not.

`generate-alerts --engine gated` emits alerts that read the state metric. They
keep the names, labels, and runbooks of the static rules, so Alertmanager
routing and inhibition do not change. The engine has already waited for enough
evidence, so `for` drops to 1 minute for critical and 5 for warning.

Two failure rules matter more than the happy path.

1. A failed evaluation keeps the last decision. Prometheus is often slow during
   the incident that is paging. If a failed query cleared the state, the page
   would resolve exactly when it matters. The failure is counted in
   `slo_tracker_evaluation_errors_total` and alerts on its own.
2. A daemon that stops is loud. With gated rules, no SLO can page while the
   daemon is down. `SLOTrackerStale` pages when no evaluation has succeeded for
   10 minutes, or when the metric is absent.

## Consequences

- Paging now depends on one more process. The staleness alert is the price of
  that, and it routes like any other critical alert.
- Detection takes up to one interval longer than a rule evaluated every 30
  seconds. For the services gating helps most, low-traffic ones, the bound needs
  minutes of data anyway.
- The two engines are a choice per environment, not a mix. Running both for the
  same SLO would send two alerts with the same name.
