# 3. Adapt across windows and surface insufficient data

Date: 2025-03-07

## Status

Accepted

## Context

Confidence gating (ADR 0002) stops false pages, but on its own it can turn into
silence. If the fast windows are always too sparse to decide, a service could
run with no effective alerting and nobody would know. Silence that looks like
health is the worst failure mode in an alerting system.

## Decision

Two mechanisms.

First, when a long window cannot reach its minimum sample size, the engine
borrows the next slower window for that tier before giving up. A 1-hour window
that is too sparse falls back to the 6-hour window. The decision records that it
adapted, so the reason string on the alert is honest about which window actually
fired.

Second, when no window in the ladder can carry a confident decision, the engine
returns an explicit `insufficient_data` severity rather than `none`. This is a
distinct state meaning "this SLO has no coverage right now," which a dashboard
can render differently from a healthy green and a team can choose to alert on at
low priority.

## Consequences

- A starved SLO is visible instead of silently green.
- Operators get a clear signal that a service needs synthetic traffic, service
  aggregation, or a looser SLO, which are the standard remedies for genuinely
  low-traffic services.
- The Beta-Binomial prior is currently static, derived from the SLO target. A
  natural extension is to learn the prior from the service's own history,
  including time-of-day and day-of-week seasonality, so a service that is quiet
  every night is not flagged insufficient every night. That work is not done yet
  and is called out as a limitation in the README.
