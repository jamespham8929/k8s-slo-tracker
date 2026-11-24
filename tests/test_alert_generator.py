"""Tests for Prometheus rule generation.

The generated rules are the artifact this repo hands to a cluster, so these
tests assert the shape Prometheus and the operator actually require, not just
that YAML came out.
"""

import shutil
import subprocess

import pytest
import yaml

from slo_tracker.adaptive import DEFAULT_LADDER, BurnWindow, Severity
from slo_tracker.alert_generator import (
    CRITICAL_ALERT,
    WARNING_ALERT,
    AlertRuleGenerator,
    ladder_windows,
    partition_supported,
    window_seconds,
)
from slo_tracker.models import SLODefinition, SLOType


def make_availability_slo(
    service="my-service", target=0.999, metric="http_requests_total", team="platform"
):
    return SLODefinition(
        name="availability",
        service=service,
        namespace="production",
        slo_type=SLOType.AVAILABILITY,
        target=target,
        window_days=30,
        team=team,
        good_query=f'sum(increase({metric}{{service="{service}",code=~"2.."}}[WINDOW]))',
        total_query=f'sum(increase({metric}{{service="{service}"}}[WINDOW]))',
    )


def make_latency_slo(service="my-service"):
    return SLODefinition(
        name="latency",
        service=service,
        namespace="production",
        slo_type=SLOType.LATENCY,
        target=0.99,
        window_days=30,
        latency_query=f'histogram_quantile(0.99, {service})',
        latency_threshold_ms=300,
    )


def rules_of(output, index=0):
    return yaml.safe_load(output)["groups"][index]["rules"]


class TestWindows:
    def test_window_seconds(self):
        assert window_seconds("30s") == 30
        assert window_seconds("5m") == 300
        assert window_seconds("6h") == 21600
        assert window_seconds("3d") == 259200
        assert window_seconds("1w") == 604800

    def test_unknown_unit_is_rejected(self):
        with pytest.raises(ValueError, match="unrecognised window"):
            window_seconds("5y")

    def test_ladder_windows_are_derived_and_ordered(self):
        # Derived from the ladder rather than listed, so a new tier cannot leave
        # its recording rule unwritten.
        assert ladder_windows() == ["5m", "30m", "1h", "6h", "3d"]

    def test_ladder_windows_follow_a_custom_ladder(self):
        ladder = (BurnWindow("2h", "10m", 10.0, Severity.CRITICAL),)
        assert ladder_windows(ladder) == ["10m", "2h"]

    def test_only_windows_the_ladder_uses_are_emitted(self):
        # 1d is in the evaluator's REQUIRED_WINDOWS for adaptive fallback, and no
        # tier references it, so it should not become a recording rule nobody
        # reads.
        assert "1d" not in ladder_windows()


class TestRecordingRules:
    def test_expressions_come_from_the_configured_queries(self):
        # The point of the rewrite: a gRPC service must not get rules that
        # measure an HTTP metric.
        slo = make_availability_slo(service="notifier", metric="grpc_server_handled_total")
        rules = rules_of(AlertRuleGenerator().generate([slo]))

        recordings = [r for r in rules if "record" in r]
        assert recordings, "expected recording rules"
        for rule in recordings:
            assert "grpc_server_handled_total" in rule["expr"]
            assert "http_requests_total" not in rule["expr"]

    def test_one_recording_rule_per_ladder_window(self):
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo()]))
        records = [r["record"] for r in rules if "record" in r]
        assert records == [f"slo:sli_error:ratio_rate{w}" for w in ladder_windows()]

    def test_window_token_is_substituted(self):
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo()]))
        by_record = {r["record"]: r["expr"] for r in rules if "record" in r}

        assert "[6h]" in by_record["slo:sli_error:ratio_rate6h"]
        assert "WINDOW" not in by_record["slo:sli_error:ratio_rate6h"]

    def test_recording_rules_carry_identifying_labels(self):
        slo = make_availability_slo(service="payment-api")
        rules = rules_of(AlertRuleGenerator().generate([slo]))
        for rule in (r for r in rules if "record" in r):
            assert rule["labels"] == {
                "service": "payment-api",
                "slo": "availability",
                "namespace": "production",
                "team": "platform",
            }

    def test_recording_rules_precede_the_alerts_that_read_them(self):
        # Same group, recordings first. Prometheus runs a group's rules in order,
        # so an alert placed above its recording rule reads a stale evaluation.
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo()]))
        kinds = ["record" if "record" in r else "alert" for r in rules]
        assert kinds == sorted(kinds, key=lambda k: 0 if k == "record" else 1)
        assert kinds.count("alert") == 2


class TestAlertRules:
    def test_one_alert_per_severity_with_distinct_names(self):
        # Previously every tier produced an alert with the same name and the same
        # labels, so two of them were indistinguishable to Alertmanager.
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo()]))
        alerts = [r for r in rules if "alert" in r]

        names = [r["alert"] for r in alerts]
        assert names == [CRITICAL_ALERT, WARNING_ALERT]
        assert len(set(names)) == len(names)

    def test_alerts_carry_the_owning_team(self):
        # Alertmanager routes on this label. Without it an alert reaches whatever
        # catch-all receiver exists, which is nobody's pager.
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo(team="payments")]))
        for rule in (r for r in rules if "alert" in r):
            assert rule["labels"]["team"] == "payments"

    def test_an_slo_with_no_team_omits_the_label(self):
        # An empty team label routes no better than a missing one and reads as
        # deliberate, so leave it off entirely.
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo(team=None)]))
        for rule in (r for r in rules if "alert" in r):
            assert "team" not in rule["labels"]

    def test_severity_labels_match_the_ladder(self):
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo()]))
        alerts = {r["alert"]: r for r in rules if "alert" in r}
        assert alerts[CRITICAL_ALERT]["labels"]["severity"] == "critical"
        assert alerts[WARNING_ALERT]["labels"]["severity"] == "warning"

    def test_critical_expression_covers_every_critical_tier(self):
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo(target=0.999)]))
        expr = next(r for r in rules if r.get("alert") == CRITICAL_ALERT)["expr"]

        critical_tiers = [t for t in DEFAULT_LADDER if t.severity is Severity.CRITICAL]
        assert expr.count("\nor\n") == len(critical_tiers) - 1
        for tier in critical_tiers:
            threshold = tier.burn_rate * 0.001
            assert f"ratio_rate{tier.long_window}" in expr
            assert f"ratio_rate{tier.short_window}" in expr
            assert f"{threshold:.6g}" in expr

    def test_each_tier_pairs_a_long_and_a_short_window(self):
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo()]))
        expr = next(r for r in rules if r.get("alert") == CRITICAL_ALERT)["expr"]
        # Every parenthesised clause is "long > t and short > t".
        for clause in expr.split("\nor\n"):
            assert clause.count(" and\n") == 1

    def test_threshold_scales_with_the_objective(self):
        strict = rules_of(AlertRuleGenerator().generate([make_availability_slo(target=0.9999)]))
        loose = rules_of(AlertRuleGenerator().generate([make_availability_slo(target=0.99)]))

        strict_expr = next(r for r in strict if r.get("alert") == CRITICAL_ALERT)["expr"]
        loose_expr = next(r for r in loose if r.get("alert") == CRITICAL_ALERT)["expr"]

        assert "0.00144" in strict_expr
        assert "0.144" in loose_expr

    def test_alerts_select_on_slo_and_service(self):
        # The recording rules for two SLOs on one service share a record name, so
        # selecting on the service alone would match both.
        rules = rules_of(AlertRuleGenerator().generate([make_availability_slo("payment-api")]))
        expr = next(r for r in rules if r.get("alert") == CRITICAL_ALERT)["expr"]
        assert 'slo="availability"' in expr
        assert 'service="payment-api"' in expr

    def test_annotations_are_actionable(self):
        slo = make_availability_slo(service="payment-api")
        rules = rules_of(AlertRuleGenerator().generate([slo]))
        for rule in (r for r in rules if "alert" in r):
            annotations = rule["annotations"]
            assert "payment-api" in annotations["summary"]
            assert "{{ $value | humanizePercentage }}" in annotations["description"]
            assert "99.900%" in annotations["description"]
            assert annotations["runbook_url"].endswith("/payment-api")

    def test_runbook_base_is_configurable(self):
        gen = AlertRuleGenerator(runbook_base_url="https://wiki.internal/runbooks/")
        rules = rules_of(gen.generate([make_availability_slo("payment-api")]))
        alert = next(r for r in rules if "alert" in r)
        assert alert["annotations"]["runbook_url"] == "https://wiki.internal/runbooks/payment-api"

    def test_a_ladder_without_warnings_emits_no_warning_alert(self):
        ladder = (BurnWindow("1h", "5m", 14.4, Severity.CRITICAL),)
        rules = rules_of(AlertRuleGenerator(ladder=ladder).generate([make_availability_slo()]))
        assert [r["alert"] for r in rules if "alert" in r] == [CRITICAL_ALERT]


class TestGroups:
    def test_one_group_per_slo(self):
        slos = [make_availability_slo("svc-a"), make_availability_slo("svc-b")]
        parsed = yaml.safe_load(AlertRuleGenerator().generate(slos))
        assert [g["name"] for g in parsed["groups"]] == [
            "slo.svc-a.availability",
            "slo.svc-b.availability",
        ]

    def test_expressions_are_readable_block_scalars(self):
        # A manifest a human reviews before applying should not be a wall of
        # escaped \n.
        output = AlertRuleGenerator().generate([make_availability_slo()])
        assert "\\n" not in output
        assert "expr: |" in output


class TestUnsupportedSLOs:
    def test_latency_slos_are_partitioned_out(self):
        availability = make_availability_slo()
        latency = make_latency_slo()
        supported, unsupported = partition_supported([availability, latency])

        assert supported == [availability]
        assert len(unsupported) == 1
        assert unsupported[0].slo is latency
        assert "latency" in unsupported[0].reason

    def test_generating_for_a_latency_slo_raises(self):
        # Better than the old behaviour, which emitted alerts referencing
        # recording rules it never wrote.
        with pytest.raises(ValueError, match="cannot generate rules"):
            AlertRuleGenerator().generate([make_latency_slo()])

    def test_missing_query_is_rejected(self):
        slo = make_availability_slo()
        slo.total_query = None
        with pytest.raises(ValueError, match="total_query is required"):
            AlertRuleGenerator().generate([slo])

    def test_query_without_a_window_token_is_rejected(self):
        # Every window would otherwise evaluate the same expression, quietly
        # turning a multi-window alert back into a single-window one.
        slo = make_availability_slo()
        slo.good_query = 'sum(increase(http_requests_total[5m]))'
        with pytest.raises(ValueError, match="no WINDOW token"):
            AlertRuleGenerator().generate([slo])


class TestPrometheusRuleManifest:
    def test_manifest_shape(self):
        manifest = yaml.safe_load(
            AlertRuleGenerator().generate_prometheus_rule([make_availability_slo()])
        )
        assert manifest["apiVersion"] == "monitoring.coreos.com/v1"
        assert manifest["kind"] == "PrometheusRule"
        assert manifest["metadata"]["name"] == "slo-tracker-rules"
        assert manifest["metadata"]["namespace"] == "monitoring"
        assert manifest["spec"]["groups"]

    def test_manifest_carries_the_selector_label(self):
        # The operator ignores a PrometheusRule its ruleSelector does not match,
        # which fails by doing nothing at all.
        manifest = yaml.safe_load(
            AlertRuleGenerator().generate_prometheus_rule(
                [make_availability_slo()],
                name="custom",
                namespace="observability",
                labels={"release": "kube-prometheus-stack"},
            )
        )
        assert manifest["metadata"]["name"] == "custom"
        assert manifest["metadata"]["namespace"] == "observability"
        assert manifest["metadata"]["labels"] == {"release": "kube-prometheus-stack"}

    def test_manifest_and_rules_file_hold_the_same_groups(self):
        gen = AlertRuleGenerator()
        slos = [make_availability_slo()]
        assert (
            yaml.safe_load(gen.generate_prometheus_rule(slos))["spec"]["groups"]
            == yaml.safe_load(gen.generate(slos))["groups"]
        )


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
class TestPromtoolAccepts:
    """Prometheus' own parser is the only authority on whether these rules load."""

    def test_generated_rules_pass_promtool(self, tmp_path):
        slos = [
            make_availability_slo("checkout-api"),
            make_availability_slo("notifier", metric="grpc_server_handled_total"),
        ]
        path = tmp_path / "rules.yaml"
        path.write_text(AlertRuleGenerator().generate(slos))

        result = subprocess.run(
            ["promtool", "check", "rules", str(path)],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SUCCESS" in result.stdout


class TestGatedEngine:
    def test_alerts_read_the_daemon_decision(self):
        gen = AlertRuleGenerator(engine="gated")
        rules = rules_of(gen.generate([make_availability_slo()]))

        assert [r["alert"] for r in rules] == [CRITICAL_ALERT, WARNING_ALERT]
        assert all("record" not in r for r in rules)
        critical = rules[0]
        assert 'slo_tracker_alert_state{service="my-service", slo="availability", ' \
               'severity="critical"}' in critical["expr"]
        assert critical["labels"]["engine"] == "gated"
        assert critical["labels"]["team"] == "platform"

    def test_adds_health_alerts_for_the_daemon(self):
        gen = AlertRuleGenerator(engine="gated")
        groups = yaml.safe_load(gen.generate([make_availability_slo()]))["groups"]
        health = groups[-1]
        assert health["name"] == "slo-tracker.daemon"
        assert {r["alert"] for r in health["rules"]} == {
            "SLOTrackerStale", "SLOTrackerEvaluationErrors",
        }
        stale = next(r for r in health["rules"] if r["alert"] == "SLOTrackerStale")
        assert "absent(slo_tracker_last_evaluation_timestamp_seconds)" in stale["expr"]

    def test_rejects_an_unknown_engine(self):
        with pytest.raises(ValueError):
            AlertRuleGenerator(engine="magic")

    @pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
    def test_gated_rules_pass_promtool(self, tmp_path):
        path = tmp_path / "gated.yaml"
        path.write_text(
            AlertRuleGenerator(engine="gated").generate([make_availability_slo()])
        )
        result = subprocess.run(
            ["promtool", "check", "rules", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr
