"""Run the confidence-gated engine on a loop and export its decisions.

Rules exported to Prometheus cannot do confidence gating. PromQL has no Wilson
interval, so `generate-alerts` emits plain threshold rules (see ADR 0004). This
module closes that gap. It evaluates every SLO on an interval with the same
engine `evaluate` uses, and exposes each decision as a metric. Alert rules then
only read the decision, so the gating happens in Python and the paging still
goes through Prometheus and Alertmanager. See ADR 0005.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable

from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

from .adaptive import Severity
from .evaluator import EvaluatedSLO, SLOEvaluator
from .models import SLODefinition

logger = logging.getLogger(__name__)

SEVERITIES = [s.value for s in Severity]


class DecisionExporter:
    """Holds the metrics for one daemon. Tests give each one its own registry."""

    def __init__(self, registry: CollectorRegistry | None = None):
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.state = Gauge(
            "slo_tracker_alert_state",
            "1 for the severity the gated engine chose for this SLO, 0 for the others.",
            ["service", "slo", "namespace", "team", "severity"],
            registry=r,
        )
        self.burn_lower = Gauge(
            "slo_tracker_burn_rate_lower_bound",
            "Lower confidence bound of the burn rate in each window.",
            ["service", "slo", "window"],
            registry=r,
        )
        self.burn_observed = Gauge(
            "slo_tracker_burn_rate_observed",
            "Burn rate as observed, before any confidence bound.",
            ["service", "slo", "window"],
            registry=r,
        )
        self.samples = Gauge(
            "slo_tracker_window_samples",
            "Requests counted in each window.",
            ["service", "slo", "window"],
            registry=r,
        )
        self.sufficient = Gauge(
            "slo_tracker_window_sufficient_data",
            "1 when a window held enough traffic to decide, 0 when it did not.",
            ["service", "slo", "window"],
            registry=r,
        )
        self.errors = Counter(
            "slo_tracker_evaluation_errors_total",
            "Evaluations that failed, usually because Prometheus did not answer.",
            ["service", "slo"],
            registry=r,
        )
        self.last_success = Gauge(
            "slo_tracker_last_evaluation_timestamp_seconds",
            "Unix time an SLO was last evaluated successfully.",
            ["service", "slo"],
            registry=r,
        )
        self.cycle_seconds = Gauge(
            "slo_tracker_evaluation_cycle_seconds",
            "How long the last pass over every SLO took.",
            registry=r,
        )

    def record(self, result: EvaluatedSLO, now: float) -> None:
        slo, decision = result.definition, result.decision
        for severity in SEVERITIES:
            self.state.labels(
                service=slo.service, slo=slo.name, namespace=slo.namespace,
                team=slo.team or "", severity=severity,
            ).set(1.0 if decision.severity.value == severity else 0.0)
        for verdict in decision.verdicts:
            ids = {"service": slo.service, "slo": slo.name, "window": verdict.window}
            self.burn_lower.labels(**ids).set(verdict.lower_bound_burn)
            self.burn_observed.labels(**ids).set(verdict.observed_burn)
            self.samples.labels(**ids).set(verdict.sample_count)
            self.sufficient.labels(**ids).set(1.0 if verdict.sufficient_data else 0.0)
        self.last_success.labels(service=slo.service, slo=slo.name).set(now)

    def record_error(self, slo: SLODefinition) -> None:
        # The last decision stays as it was. If Prometheus is struggling during
        # an incident, clearing the state would resolve a real page. The error
        # counter and the staleness alert report the problem instead.
        self.errors.labels(service=slo.service, slo=slo.name).inc()


def evaluate_all(
    evaluator: SLOEvaluator,
    slos: Iterable[SLODefinition],
    exporter: DecisionExporter,
    clock: Callable[[], float] = time.time,
) -> list[EvaluatedSLO]:
    """One pass over every SLO. A failure on one SLO does not stop the rest."""
    started = time.monotonic()
    results = []
    for slo in slos:
        try:
            result = evaluator.evaluate(slo)
        except Exception:
            # Any failure is reported the same way: logged, counted, skipped.
            logger.exception("evaluating %s/%s failed", slo.service, slo.name)
            exporter.record_error(slo)
            continue
        exporter.record(result, now=clock())
        results.append(result)
    exporter.cycle_seconds.set(time.monotonic() - started)
    return results


def serve_forever(
    evaluator: SLOEvaluator,
    slos: list[SLODefinition],
    exporter: DecisionExporter,
    interval_seconds: float,
    stop: threading.Event,
) -> None:
    while not stop.is_set():
        evaluate_all(evaluator, slos, exporter)
        stop.wait(interval_seconds)


def start_metrics_server(exporter: DecisionExporter, port: int) -> None:
    start_http_server(port, registry=exporter.registry)
