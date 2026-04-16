"""Ties Prometheus event counts to the adaptive burn-rate engine.

Fetches good/total counts for each window in the alert ladder using instant
`increase(...)` queries, then hands the multi-window measurement to the engine.
The SLO config supplies the queries with a literal WINDOW token that is
substituted per window.
"""

from __future__ import annotations

from dataclasses import dataclass

from .adaptive import AdaptiveBurnRateEngine, AlertDecision, Estimator
from .measurement import MultiWindowMeasurement, WindowMeasurement
from .models import SLODefinition
from .prometheus_client import PrometheusClient

# Every window the ladder and its adaptive fallbacks can reference.
REQUIRED_WINDOWS = ("5m", "30m", "1h", "6h", "1d", "3d")


@dataclass
class EvaluatedSLO:
    definition: SLODefinition
    decision: AlertDecision


class SLOEvaluator:
    def __init__(
        self,
        prometheus: PrometheusClient,
        confidence: float = 0.95,
        estimator: Estimator = Estimator.WILSON,
    ):
        self._prom = prometheus
        self._confidence = confidence
        self._estimator = estimator

    def evaluate(self, slo: SLODefinition) -> EvaluatedSLO:
        engine = AdaptiveBurnRateEngine(
            target=slo.target,
            confidence=self._confidence,
            estimator=self._estimator,
        )
        windows: dict[str, WindowMeasurement] = {}
        for window in REQUIRED_WINDOWS:
            good = self._count(slo.good_query, window)
            total = self._count(slo.total_query, window)
            windows[window] = WindowMeasurement(
                window=window,
                good_count=good or 0.0,
                total_count=total or 0.0,
            )

        measurement = MultiWindowMeasurement(
            service=slo.service, slo=slo.name, windows=windows
        )
        return EvaluatedSLO(definition=slo, decision=engine.evaluate(measurement))

    def _count(self, query_template: str | None, window: str) -> float | None:
        if not query_template:
            return None
        promql = query_template.replace("WINDOW", window)
        return self._prom.query(promql)
