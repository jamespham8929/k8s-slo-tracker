"""Turns SLO definitions into Prometheus rules, and into a PrometheusRule manifest.

The rules exported here are the textbook multi-window multi-burn-rate form. They
are not the confidence-gated engine: PromQL has no reasonable way to express a
Wilson lower bound, so a rule file cannot make the low-traffic judgement that
`slo-tracker evaluate` makes. What it can do is put the same thresholds, on the
same windows, under the same record names, in front of Prometheus itself. See
docs/adr/0004-exporting-rules-to-prometheus.md.

Every expression is built from the queries in the SLO config rather than from a
metric name hardcoded here, so the rules Prometheus evaluates measure exactly
what the tracker measures.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from .adaptive import DEFAULT_LADDER, BurnWindow
from .models import SLODefinition, SLOType
from .query import render, validate

DEFAULT_RUNBOOK_BASE = "https://runbooks.example.com/slo"

# Alert names are shared with the rest of the platform's alerting, so the same
# Alertmanager routing, inhibition rules, and runbooks apply whether the rules
# came from here or were written by hand.
CRITICAL_ALERT = "SLOErrorBudgetBurnCritical"
WARNING_ALERT = "SLOErrorBudgetBurnWarning"

class _BlockDumper(yaml.SafeDumper):
    """Dumps multi-line strings as block scalars.

    Without this every PromQL expression comes out as an escaped one-line string
    wrapped at the page width, which is unreadable in a manifest a human is
    expected to review before applying.
    """


def _represent_str(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockDumper.add_representer(str, _represent_str)


def _dump(document: dict) -> str:
    return yaml.dump(
        document, Dumper=_BlockDumper, default_flow_style=False, sort_keys=False, width=1000
    )


# Multiplier per unit, for ordering windows shortest-first.
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def window_seconds(window: str) -> int:
    """Length of a Prometheus duration like 30m or 3d, in seconds."""
    unit = window[-1]
    if unit not in _UNIT_SECONDS:
        raise ValueError(f"unrecognised window {window!r}")
    return int(window[:-1]) * _UNIT_SECONDS[unit]


def ladder_windows(ladder: tuple[BurnWindow, ...] = DEFAULT_LADDER) -> list[str]:
    """Every window the ladder references, shortest first.

    Derived from the ladder rather than listed separately, so a tier added to the
    engine cannot leave its recording rule unwritten. Windows the ladder does not
    use are not emitted: an unevaluated recording rule is pure cost.
    """
    windows = {w for tier in ladder for w in (tier.long_window, tier.short_window)}
    return sorted(windows, key=window_seconds)


@dataclass
class UnsupportedSLO:
    slo: SLODefinition
    reason: str


def partition_supported(
    slos: list[SLODefinition],
) -> tuple[list[SLODefinition], list[UnsupportedSLO]]:
    """Split SLOs into the ones rules can be generated for and the ones that cannot.

    Latency SLOs are carried in the config and evaluated nowhere yet. Emitting
    alerts for them was the previous behaviour and it was worse than emitting
    nothing, because the alerts referenced recording rules that were never
    written and so could never fire.
    """
    supported: list[SLODefinition] = []
    unsupported: list[UnsupportedSLO] = []
    for slo in slos:
        if slo.slo_type != SLOType.AVAILABILITY:
            unsupported.append(
                UnsupportedSLO(slo, f"{slo.slo_type.value} SLOs have no rule template yet")
            )
            continue
        supported.append(slo)
    return supported, unsupported


# Rule engines. "static" computes burn rates in PromQL from the raw counters.
# "gated" reads the decision `slo-tracker serve` already made with confidence
# gating, so Prometheus only routes it.
ENGINES = ("static", "gated")

# How stale the daemon's last evaluation may get before its decisions stop
# being trusted. Several missed cycles at the default 60s interval.
GATED_STALE_SECONDS = 600


class AlertRuleGenerator:
    def __init__(
        self,
        ladder: tuple[BurnWindow, ...] = DEFAULT_LADDER,
        runbook_base_url: str = DEFAULT_RUNBOOK_BASE,
        engine: str = "static",
    ):
        if engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
        self._ladder = ladder
        self._runbook_base_url = runbook_base_url.rstrip("/")
        self._windows = ladder_windows(ladder)
        self._engine = engine

    def rule_groups(self, slos: list[SLODefinition]) -> list[dict]:
        """Build one rule group per SLO.

        Recording rules and the alerts that read them share a group on purpose.
        Prometheus evaluates groups concurrently but the rules inside a group in
        order, so splitting them would leave each alert reading whichever
        evaluation the recording group last finished.
        """
        groups = []
        for slo in slos:
            if slo.slo_type != SLOType.AVAILABILITY:
                raise ValueError(
                    f"{slo.service}/{slo.name}: cannot generate rules for a "
                    f"{slo.slo_type.value} SLO"
                )
            if self._engine == "gated":
                rules = self._gated_alert_rules(slo)
            else:
                rules = self._recording_rules(slo) + self._alert_rules(slo)
            groups.append({"name": f"slo.{slo.service}.{slo.name}", "rules": rules})
        if self._engine == "gated" and slos:
            groups.append(self._gated_health_group(slos))
        return groups

    def generate(self, slos: list[SLODefinition]) -> str:
        """A plain Prometheus rules file, the form promtool and rule_files expect."""
        return _dump({"groups": self.rule_groups(slos)})

    def generate_prometheus_rule(
        self,
        slos: list[SLODefinition],
        name: str = "slo-tracker-rules",
        namespace: str = "monitoring",
        labels: dict[str, str] | None = None,
    ) -> str:
        """The same rules wrapped as a PrometheusRule for the prometheus-operator.

        The label matters as much as the rules: the operator only picks up a
        PrometheusRule its ruleSelector matches, so a manifest with no labels
        applies cleanly and is then ignored forever.
        """
        manifest = {
            "apiVersion": "monitoring.coreos.com/v1",
            "kind": "PrometheusRule",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels or {},
            },
            "spec": {"groups": self.rule_groups(slos)},
        }
        return _dump(manifest)

    @staticmethod
    def _identity_labels(slo: SLODefinition) -> dict[str, str]:
        labels = {
            "service": slo.service,
            "slo": slo.name,
            "namespace": slo.namespace,
        }
        if slo.team:
            labels["team"] = slo.team
        return labels

    def _recording_rules(self, slo: SLODefinition) -> list[dict]:
        identity = f"{slo.service}/{slo.name}"
        good = validate(slo.good_query, "good_query", identity)
        total = validate(slo.total_query, "total_query", identity)

        rules = []
        for window in self._windows:
            # The config wraps counters in increase(), so this is a ratio of
            # counts rather than of rates. The two are the same number, and the
            # rate-flavoured record name is the convention every SLO tool uses.
            expr = (
                "1 - (\n"
                f"  {render(good, window)}\n"
                "  /\n"
                f"  {render(total, window)}\n"
                ")"
            )
            rules.append({
                "record": f"slo:sli_error:ratio_rate{window}",
                "expr": expr,
                "labels": self._identity_labels(slo),
            })
        return rules

    def _alert_rules(self, slo: SLODefinition) -> list[dict]:
        rules = []
        for severity, alert_name, wait in (
            ("critical", CRITICAL_ALERT, "2m"),
            ("warning", WARNING_ALERT, "15m"),
        ):
            tiers = [tier for tier in self._ladder if tier.severity.value == severity]
            if not tiers:
                continue
            rules.append({
                "alert": alert_name,
                "expr": self._burn_expr(slo, tiers),
                "for": wait,
                "labels": {"severity": severity, **self._identity_labels(slo)},
                "annotations": self._annotations(slo, tiers, severity),
            })
        return rules

    def _gated_alert_rules(self, slo: SLODefinition) -> list[dict]:
        """Alerts that read the daemon's decision instead of recomputing it.

        The engine has already waited for enough evidence before choosing a
        severity, so `for` is short. It only absorbs one flapping cycle.
        """
        rules = []
        for severity, alert_name, wait in (
            ("critical", CRITICAL_ALERT, "1m"),
            ("warning", WARNING_ALERT, "5m"),
        ):
            tiers = [tier for tier in self._ladder if tier.severity.value == severity]
            if not tiers:
                continue
            selector = (
                f'{{service="{slo.service}", slo="{slo.name}", severity="{severity}"}}'
            )
            annotations = self._annotations(slo, tiers, severity)
            pairs = ", ".join(f"{t.burn_rate}x over {t.long_window}" for t in tiers)
            annotations["description"] = (
                f"The lower confidence bound of {slo.service}/{slo.name}'s burn "
                f"rate is above {pairs}. slo_tracker_burn_rate_lower_bound "
                "shows the evidence per window."
            )
            rules.append({
                "alert": alert_name,
                "expr": (
                    "max without (instance, pod, job) "
                    f"(slo_tracker_alert_state{selector}) == 1"
                ),
                "for": wait,
                "labels": {"severity": severity, "engine": "gated", **self._identity_labels(slo)},
                "annotations": annotations,
            })
        return rules

    def _gated_health_group(self, slos: list[SLODefinition]) -> dict:
        """Alerts on the daemon itself.

        With gated rules, a daemon that stops evaluating means no SLO can page.
        That has to be loud on its own.
        """
        names = "|".join(sorted({slo.service for slo in slos}))
        return {
            "name": "slo-tracker.daemon",
            "rules": [
                {
                    "alert": "SLOTrackerStale",
                    "expr": (
                        "time() - max by (service, slo) "
                        f'(slo_tracker_last_evaluation_timestamp_seconds{{service=~"{names}"}})'
                        f" > {GATED_STALE_SECONDS}"
                        "\nor\n"
                        "absent(slo_tracker_last_evaluation_timestamp_seconds)"
                    ),
                    "for": "5m",
                    "labels": {"severity": "critical"},
                    "annotations": {
                        "summary": "slo-tracker has stopped evaluating SLOs",
                        "description": (
                            "No successful evaluation for over "
                            f"{GATED_STALE_SECONDS // 60} minutes. While this "
                            "fires, the gated SLO alerts cannot page."
                        ),
                        "runbook_url": f"{self._runbook_base_url}/slo-tracker",
                    },
                },
                {
                    "alert": "SLOTrackerEvaluationErrors",
                    "expr": "increase(slo_tracker_evaluation_errors_total[15m]) > 0",
                    "for": "15m",
                    "labels": {"severity": "warning"},
                    "annotations": {
                        "summary": (
                            "slo-tracker cannot evaluate "
                            "{{ $labels.service }}/{{ $labels.slo }}"
                        ),
                        "description": (
                            "Evaluations keep failing, usually because a query "
                            "errors or Prometheus does not answer. The last "
                            "decision stays in place until one succeeds."
                        ),
                        "runbook_url": f"{self._runbook_base_url}/slo-tracker",
                    },
                },
            ],
        }

    def _burn_expr(self, slo: SLODefinition, tiers: list[BurnWindow]) -> str:
        """One expression per severity, its tiers joined by `or`.

        Each tier needs its long window and its short window over the same
        threshold at once. The long window is the evidence budget is being spent,
        the short one is the evidence it still is, which is what stops an alert
        outliving the incident that caused it.
        """
        selector = f'{{slo="{slo.name}", service="{slo.service}"}}'
        clauses = []
        for tier in tiers:
            threshold = tier.burn_rate * slo.error_budget_total
            clauses.append(
                "(\n"
                f"  slo:sli_error:ratio_rate{tier.long_window}{selector} > {threshold:.6g}\n"
                "  and\n"
                f"  slo:sli_error:ratio_rate{tier.short_window}{selector} > {threshold:.6g}\n"
                ")"
            )
        return "\nor\n".join(clauses)

    def _annotations(
        self, slo: SLODefinition, tiers: list[BurnWindow], severity: str
    ) -> dict[str, str]:
        pairs = ", ".join(
            f"{tier.burn_rate}x over {tier.long_window}/{tier.short_window}" for tier in tiers
        )
        pace = (
            "fast enough to exhaust it in days"
            if severity == "critical"
            else "fast enough to exhaust it this month"
        )
        return {
            "summary": f"{slo.service}/{slo.name} is burning its error budget {pace}",
            "description": (
                f"{slo.service} is failing "
                "{{ $value | humanizePercentage }}"
                f" of requests against a {slo.target * 100:.3f}% objective. "
                f"Triggered by {pairs}."
            ),
            "runbook_url": f"{self._runbook_base_url}/{slo.service}",
        }
