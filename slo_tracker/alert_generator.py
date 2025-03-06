"""Generates Prometheus/Alertmanager alert rules from SLO definitions."""

from __future__ import annotations

import yaml
from .models import SLODefinition, SLOType

# Multi-window burn rate thresholds from Google SRE workbook
ALERT_WINDOWS = [
    {"severity": "critical", "long_window": "1h",  "short_window": "5m",  "burn_rate": 14.4},
    {"severity": "critical", "long_window": "6h",  "short_window": "30m", "burn_rate": 6.0},
    {"severity": "warning",  "long_window": "3d",  "short_window": "6h",  "burn_rate": 1.0},
]


class AlertRuleGenerator:
    def generate(self, slos: list[SLODefinition]) -> str:
        """Generate a YAML string containing Prometheus recording and alerting rules."""
        groups = []

        for slo in slos:
            recording_rules = self._recording_rules(slo)
            alert_rules = self._alert_rules(slo)
            groups.append({
                "name": f"slo.{slo.service}.{slo.name}",
                "rules": recording_rules + alert_rules,
            })

        return yaml.dump({"groups": groups}, default_flow_style=False, sort_keys=False)

    def _recording_rules(self, slo: SLODefinition) -> list[dict]:
        if slo.slo_type != SLOType.AVAILABILITY:
            return []

        rules = []
        for window in ["5m", "30m", "1h", "6h", "1d", "3d"]:
            label_selector = f'service="{slo.service}", namespace="{slo.namespace}"'
            rules.append({
                "record": f"slo:sli_error:ratio_rate{window}",
                "expr": (
                    f"1 - (\n"
                    f"  sum(rate(http_requests_total{{{label_selector},code=~\"2..\"}}[{window}]))\n"
                    f"  /\n"
                    f"  sum(rate(http_requests_total{{{label_selector}}}[{window}]))\n"
                    f")"
                ),
                "labels": {
                    "service": slo.service,
                    "slo": slo.name,
                    "namespace": slo.namespace,
                },
            })
        return rules

    def _alert_rules(self, slo: SLODefinition) -> list[dict]:
        rules = []
        error_budget = 1.0 - slo.target

        for w in ALERT_WINDOWS:
            burn = w["burn_rate"]
            long_w = w["long_window"]
            short_w = w["short_window"]
            severity = w["severity"]
            threshold = burn * error_budget

            rules.append({
                "alert": f"SLOBurnRate{slo.service.replace('-', '_').title()}",
                "expr": (
                    f"slo:sli_error:ratio_rate{long_w}{{service=\"{slo.service}\"}} > {threshold:.6f}\n"
                    f"and\n"
                    f"slo:sli_error:ratio_rate{short_w}{{service=\"{slo.service}\"}} > {threshold:.6f}"
                ),
                "for": "2m",
                "labels": {
                    "severity": severity,
                    "service": slo.service,
                    "slo": slo.name,
                    "namespace": slo.namespace,
                },
                "annotations": {
                    "summary": f"SLO burn rate alert: {slo.service}/{slo.name}",
                    "description": (
                        f"{slo.service} is burning error budget at {burn}x the sustainable rate "
                        f"(target: {slo.target * 100:.3f}%). "
                        f"Measured over {long_w} and {short_w} windows."
                    ),
                    "runbook_url": f"https://runbooks.example.com/slo/{slo.service}",
                },
            })

        return rules
