"""Multi-window burn rate helpers following the Google SRE workbook approach.

The canonical alert ladder lives in `adaptive.DEFAULT_LADDER`. This module
re-exposes it in the older `BurnRateWindow` shape for the alert-rule generator
and keeps the stateless burn-rate math helpers. Keeping a single source of truth
for the ladder means the generated Prometheus rules and the live engine can never
disagree about thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass

from .adaptive import DEFAULT_LADDER


@dataclass
class BurnRateWindow:
    long_window: str   # e.g. "1h"
    short_window: str  # e.g. "5m"
    burn_rate_threshold: float
    severity: str


# Derived from the engine's ladder so the two cannot drift. At a 99.9% SLO these
# thresholds exhaust the budget in 50 hours (14.4x), 5 days (6.0x), 30 days (1.0x).
STANDARD_WINDOWS: list[BurnRateWindow] = [
    BurnRateWindow(
        long_window=tier.long_window,
        short_window=tier.short_window,
        burn_rate_threshold=tier.burn_rate,
        severity=tier.severity.value,
    )
    for tier in DEFAULT_LADDER
]


def burn_rate_to_exhaustion_hours(burn_rate: float, window_days: int = 30) -> float:
    """
    Given a burn rate multiplier, return how many hours until the error budget
    is exhausted over the given window.
    """
    if burn_rate <= 0:
        return float("inf")
    return (window_days * 24) / burn_rate


def is_paging_alert(burn_rate_1h: float, burn_rate_6h: float) -> bool:
    """
    True when both the 1h and 6h burn rates are at the page-level threshold.
    Requiring two windows prevents false positives from short-lived spikes.
    """
    page_threshold = STANDARD_WINDOWS[0].burn_rate_threshold
    return burn_rate_1h >= page_threshold or burn_rate_6h >= STANDARD_WINDOWS[1].burn_rate_threshold


def compute_multi_window_alert_severity(
    burn_rate_1h: float,
    burn_rate_5m: float,
    burn_rate_6h: float,
    burn_rate_30m: float,
    burn_rate_3d: float,
    burn_rate_6h_slow: float,
) -> str:
    """
    Evaluate all three standard alert windows and return the highest severity.
    Returns "critical", "warning", or "none".
    """
    if (burn_rate_1h >= STANDARD_WINDOWS[0].burn_rate_threshold and
            burn_rate_5m >= STANDARD_WINDOWS[0].burn_rate_threshold):
        return "critical"

    if (burn_rate_6h >= STANDARD_WINDOWS[1].burn_rate_threshold and
            burn_rate_30m >= STANDARD_WINDOWS[1].burn_rate_threshold):
        return "critical"

    if (burn_rate_3d >= STANDARD_WINDOWS[2].burn_rate_threshold and
            burn_rate_6h_slow >= STANDARD_WINDOWS[2].burn_rate_threshold):
        return "warning"

    return "none"
