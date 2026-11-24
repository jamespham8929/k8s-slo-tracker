"""Tests for the daemon that exports gated decisions as metrics."""

import threading

import pytest
from prometheus_client import CollectorRegistry

from slo_tracker.adaptive import Estimator
from slo_tracker.daemon import DecisionExporter, evaluate_all, serve_forever
from slo_tracker.evaluator import SLOEvaluator
from tests.test_alert_generator import make_availability_slo


class CountsPrometheus:
    """Answers every good query with `good` and every total query with `total`."""

    def __init__(self, good, total):
        self.good, self.total = good, total
        self.calls = 0

    def query(self, promql, time=None):
        self.calls += 1
        return self.good if "code=~" in promql else self.total


class BrokenPrometheus:
    def query(self, promql, time=None):
        raise RuntimeError("Prometheus query failed: timeout")


def value(exporter, metric, **labels):
    return exporter.registry.get_sample_value(metric, labels)


def state(exporter, slo, severity):
    return value(
        exporter, "slo_tracker_alert_state",
        service=slo.service, slo=slo.name, namespace=slo.namespace,
        team=slo.team, severity=severity,
    )


@pytest.fixture
def exporter():
    return DecisionExporter(CollectorRegistry())


def evaluator_for(prom):
    return SLOEvaluator(prom, confidence=0.95, estimator=Estimator.WILSON)


class TestEvaluateAll:
    def test_a_heavy_outage_pages(self, exporter):
        # 10% errors on a 99.9% target is a 100x burn on plenty of traffic.
        slo = make_availability_slo()
        evaluate_all(evaluator_for(CountsPrometheus(90_000, 100_000)), [slo], exporter,
                     clock=lambda: 1_800_000_000)

        assert state(exporter, slo, "critical") == 1
        assert state(exporter, slo, "none") == 0
        assert value(exporter, "slo_tracker_last_evaluation_timestamp_seconds",
                     service=slo.service, slo=slo.name) == 1_800_000_000
        assert value(exporter, "slo_tracker_burn_rate_lower_bound",
                     service=slo.service, slo=slo.name, window="1h") > 14.4

    def test_a_healthy_service_does_not_page(self, exporter):
        slo = make_availability_slo()
        evaluate_all(evaluator_for(CountsPrometheus(100_000, 100_000)), [slo], exporter)
        assert state(exporter, slo, "none") == 1
        assert state(exporter, slo, "critical") == 0

    def test_too_little_traffic_reports_insufficient_data(self, exporter):
        # Two failures out of three requests is a scary ratio and no evidence.
        slo = make_availability_slo()
        evaluate_all(evaluator_for(CountsPrometheus(1, 3)), [slo], exporter)
        assert state(exporter, slo, "insufficient_data") == 1
        assert state(exporter, slo, "critical") == 0
        assert value(exporter, "slo_tracker_window_sufficient_data",
                     service=slo.service, slo=slo.name, window="1h") == 0

    def test_exactly_one_severity_is_set(self, exporter):
        slo = make_availability_slo()
        evaluate_all(evaluator_for(CountsPrometheus(90_000, 100_000)), [slo], exporter)
        total = sum(
            state(exporter, slo, s) for s in ("critical", "warning", "none", "insufficient_data")
        )
        assert total == 1

    def test_a_failed_query_keeps_the_last_decision(self, exporter):
        # Prometheus often struggles during the incident that is paging. A
        # failed evaluation must not resolve the page.
        slo = make_availability_slo()
        evaluate_all(evaluator_for(CountsPrometheus(90_000, 100_000)), [slo], exporter,
                     clock=lambda: 100.0)
        evaluate_all(evaluator_for(BrokenPrometheus()), [slo], exporter, clock=lambda: 200.0)

        assert state(exporter, slo, "critical") == 1
        assert value(exporter, "slo_tracker_evaluation_errors_total",
                     service=slo.service, slo=slo.name) == 1
        assert value(exporter, "slo_tracker_last_evaluation_timestamp_seconds",
                     service=slo.service, slo=slo.name) == 100.0

    def test_one_failure_does_not_stop_the_others(self, exporter):
        good = make_availability_slo(service="good-svc")
        bad = make_availability_slo(service="bad-svc")

        class PartlyBroken(CountsPrometheus):
            def query(self, promql, time=None):
                if "bad-svc" in promql:
                    raise RuntimeError("boom")
                return super().query(promql, time)

        results = evaluate_all(evaluator_for(PartlyBroken(100_000, 100_000)), [bad, good],
                               exporter)
        assert [r.definition.service for r in results] == ["good-svc"]
        assert state(exporter, good, "none") == 1
        assert value(exporter, "slo_tracker_evaluation_errors_total",
                     service="bad-svc", slo="availability") == 1

    def test_cycle_time_is_recorded(self, exporter):
        evaluate_all(evaluator_for(CountsPrometheus(1, 1)), [make_availability_slo()], exporter)
        assert value(exporter, "slo_tracker_evaluation_cycle_seconds") >= 0


class TestServeForever:
    def test_runs_until_stopped(self, exporter):
        prom = CountsPrometheus(100_000, 100_000)
        stop = threading.Event()

        class StopAfterTwo(SLOEvaluator):
            passes = 0

            def evaluate(self, slo):
                StopAfterTwo.passes += 1
                if StopAfterTwo.passes == 2:
                    stop.set()
                return super().evaluate(slo)

        serve_forever(StopAfterTwo(prom), [make_availability_slo()], exporter,
                      interval_seconds=0, stop=stop)
        assert StopAfterTwo.passes == 2
