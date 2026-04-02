"""Count-based SLI measurements.

The core difference between this tracker and a textbook burn-rate implementation
is that it carries raw event counts through the whole pipeline, not just the
error ratio. Counts are what let us reason about statistical confidence. A ratio
of 0.5 means something very different at n=2 than at n=20000, and the ratio alone
throws that information away.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WindowMeasurement:
    """Good and total event counts observed over a single time window."""

    window: str          # e.g. "5m", "1h", "6h", "3d"
    good_count: float
    total_count: float

    @property
    def bad_count(self) -> float:
        return max(0.0, self.total_count - self.good_count)

    @property
    def error_ratio(self) -> float:
        if self.total_count <= 0:
            return 0.0
        return self.bad_count / self.total_count

    @property
    def is_empty(self) -> bool:
        return self.total_count <= 0


@dataclass(frozen=True)
class MultiWindowMeasurement:
    """A set of window measurements for one SLO at a single evaluation tick."""

    service: str
    slo: str
    windows: dict[str, WindowMeasurement]

    def get(self, window: str) -> WindowMeasurement | None:
        return self.windows.get(window)
