# 4. Exporting rules to Prometheus

Date: 2026-08-24

## Status

Accepted

## Context

The tracker decides whether to page by pulling event counts and running them
through the confidence-gated engine. That decision lives in the process, which
means it exists only while someone is running `slo-tracker evaluate`. Nothing in
Prometheus knows about these SLOs, so nothing fires at 3am.

The repo already had a `generate-alerts` command producing a rules file, and it
had three problems that made it closer to a demo than an export.

It hardcoded `http_requests_total` and a `code=~"2.."` selector, ignoring the
`good_query` and `total_query` in the config. One of the shipped example services
is measured on gRPC metrics, so the rules generated for it described a metric it
does not emit. The rules and the engine could disagree about what an error even
was, silently.

It emitted one alert per burn-rate tier, all three named `SLOBurnRate<Service>`
and carrying identical labels. Two of them were critical tiers, so a service in
trouble produced two alerts Alertmanager could not tell apart.

It generated alert rules for latency SLOs and recording rules for none of them,
so those alerts referenced records that were never written and could not fire.

## Decision

Generate from the config, and emit a PrometheusRule by default.

Every expression is built by substituting each window into the `good_query` and
`total_query` from the SLO config, through the same `query.render` the live
evaluator uses. A query missing the `WINDOW` token is rejected rather than
rendered identically for every window, which would quietly turn a multi-window
alert back into the single-window alert it was designed to replace.

Windows are derived from the ladder rather than listed separately, so a tier
added to the engine cannot leave its recording rule unwritten. Windows no tier
references are not emitted, because an unevaluated recording rule is pure cost.

One alert per severity, its tiers joined by `or`, named
`SLOErrorBudgetBurnCritical` and `SLOErrorBudgetBurnWarning`. Those names are
shared with the hand-written alerting in observability-bootstrap, so the same
Alertmanager routing, the same inhibition rules, and the same runbooks apply
whichever side the rules came from.

Recording rules and the alerts that read them go in one group. Prometheus
evaluates groups concurrently and the rules inside a group sequentially, so
splitting them leaves each alert reading whichever evaluation the recording group
last finished.

Latency SLOs are partitioned out and reported, not silently dropped and not
half-generated.

The default output is a PrometheusRule with a `release=kube-prometheus-stack`
label, because the operator ignores a PrometheusRule its `ruleSelector` does not
match. That failure applies cleanly, reports success, and then does nothing
forever, which is the worst shape a failure can take. `--format rules` still
emits a plain rules file for a `rule_files` entry or for promtool.

## Consequences

`slo-tracker generate-alerts --config slos.yaml | kubectl apply -f -` puts the
thresholds in front of Prometheus. CI checks the output with promtool and the
manifest with kubeconform, so "the generator produced YAML" and "Prometheus will
load it" stop being the same claim.

What does not survive the export is the thing this project is about. PromQL has
no reasonable way to express a Wilson lower bound, so exported rules cannot make
the low-traffic judgement described in
[ADR 0002](0002-confidence-gated-burn-rate.md). A service doing six requests a
minute will page on a single failed request under these rules, exactly as the
textbook method does.

That is a real limitation and not a temporary one. The exported rules are for
teams that want the thresholds in Prometheus and accept the standard behaviour.
`evaluate`, and eventually a daemon mode, is where the confidence gating lives.
Anything else would be claiming the export does something it cannot.

The error ratio is whatever the config's queries define. The shipped examples use
`1 - (2xx / total)`, which counts a 4xx against the budget. Whether client errors
should burn a service's budget is a judgement per SLO, and pushing it into the
config is the reason the generator no longer has an opinion. It does mean two
teams can define availability differently and both be doing what the tool told
them to.
