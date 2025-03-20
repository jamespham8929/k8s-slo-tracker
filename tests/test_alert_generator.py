"""Tests for Prometheus alert rule generation."""

import yaml
import pytest

from slo_tracker.alert_generator import AlertRuleGenerator
from slo_tracker.models import SLODefinition, SLOType


def make_availability_slo(service="my-service", target=0.999):
    return SLODefinition(
        name="availability",
        service=service,
        namespace="production",
        slo_type=SLOType.AVAILABILITY,
        target=target,
        window_days=30,
        good_query=f'sum(rate(http_requests_total{{service="{service}",code=~"2.."}}[5m]))',
        total_query=f'sum(rate(http_requests_total{{service="{service}"}}[5m]))',
    )


class TestAlertGeneration:
    def test_generates_valid_yaml(self):
        gen = AlertRuleGenerator()
        slo = make_availability_slo()
        output = gen.generate([slo])
        parsed = yaml.safe_load(output)
        assert "groups" in parsed

    def test_generates_one_group_per_slo(self):
        gen = AlertRuleGenerator()
        slos = [make_availability_slo("svc-a"), make_availability_slo("svc-b")]
        output = gen.generate(slos)
        parsed = yaml.safe_load(output)
        assert len(parsed["groups"]) == 2

    def test_generates_alert_rules_for_each_burn_window(self):
        gen = AlertRuleGenerator()
        slo = make_availability_slo()
        output = gen.generate([slo])
        parsed = yaml.safe_load(output)

        alert_rules = [r for r in parsed["groups"][0]["rules"] if "alert" in r]
        assert len(alert_rules) == 3  # one per ALERT_WINDOWS entry

    def test_critical_alert_has_correct_threshold(self):
        gen = AlertRuleGenerator()
        slo = make_availability_slo(target=0.999)
        output = gen.generate([slo])
        parsed = yaml.safe_load(output)

        critical_rules = [
            r for r in parsed["groups"][0]["rules"]
            if r.get("labels", {}).get("severity") == "critical"
        ]
        assert len(critical_rules) >= 1
        # Error budget = 0.001, burn 14.4x → threshold ≈ 0.0144
        rule_expr = critical_rules[0]["expr"]
        assert "0.014400" in rule_expr or "14.4" in str(critical_rules[0])

    def test_recording_rules_emitted_for_availability_slo(self):
        gen = AlertRuleGenerator()
        slo = make_availability_slo()
        output = gen.generate([slo])
        parsed = yaml.safe_load(output)

        recording_rules = [r for r in parsed["groups"][0]["rules"] if "record" in r]
        assert len(recording_rules) == 6  # one per time window

    def test_alert_annotations_include_service(self):
        gen = AlertRuleGenerator()
        slo = make_availability_slo(service="payment-api")
        output = gen.generate([slo])
        parsed = yaml.safe_load(output)

        alert_rules = [r for r in parsed["groups"][0]["rules"] if "alert" in r]
        for rule in alert_rules:
            assert "payment-api" in rule["annotations"]["summary"]
