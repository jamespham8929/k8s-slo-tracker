"""Data models for SLO definitions and evaluation results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SLOType(str, Enum):
    AVAILABILITY = "availability"
    LATENCY = "latency"


@dataclass
class SLODefinition:
    name: str
    service: str
    namespace: str
    slo_type: SLOType
    target: float  # 0.0 - 1.0, e.g. 0.999 for 99.9%
    window_days: int
    good_query: Optional[str] = None
    total_query: Optional[str] = None
    latency_query: Optional[str] = None
    latency_threshold_ms: Optional[float] = None

    @property
    def error_budget_total(self) -> float:
        """Total error budget as a fraction (1 - target)."""
        return 1.0 - self.target

    @property
    def window_seconds(self) -> int:
        return self.window_days * 86400

    def __str__(self) -> str:
        return f"{self.service}/{self.name} ({self.target * 100:.3f}%)"


@dataclass
class SLOStatus:
    definition: SLODefinition
    current_sli: float  # measured value 0.0 - 1.0
    error_budget_remaining: float  # fraction remaining (1.0 = full, 0.0 = exhausted)
    burn_rate_1h: float
    burn_rate_6h: float
    burn_rate_24h: float

    @property
    def is_breaching(self) -> bool:
        return self.current_sli < self.definition.target

    @property
    def budget_minutes_remaining(self) -> float:
        total_minutes = self.definition.window_days * 24 * 60 * self.definition.error_budget_total
        return total_minutes * self.error_budget_remaining

    @property
    def alert_severity(self) -> str:
        if self.burn_rate_1h >= 14.4:
            return "critical"
        if self.burn_rate_6h >= 6.0:
            return "warning"
        return "none"


@dataclass
class ServiceSLOConfig:
    name: str
    namespace: str
    slos: list[SLODefinition] = field(default_factory=list)
