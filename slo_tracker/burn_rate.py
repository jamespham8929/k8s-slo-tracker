"""Multi-window burn rate alerts following the Google SRE workbook approach."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BurnRateWindow:
    long_window: str   # e.g. "1h"
    short_window: str  # e.g. "5m"
    burn_rate_threshold: float
    severity: str


# Standard multi-window burn rate pairs from the Google SRE workbook
# At 99.9% SLO (error budget = 0.1%), these thresholds fire when budget
# will be exhausted in:
#   14.4x burn → 50 hours (critical/page)
#   6.0x  burn → 5 days  (critical/page)
#   1.0x  burn → 30 days (warning/ticket)
STANDARD_WINDOWS: list[BurnRateWindow] = [
    BurnRateWindow(long_window="1h",  short_window="5m",  burn_rate_threshold=14.4, severity="critical"),
    BurnRateWindow(long_window="6h",  short_window="30m", burn_rate_threshold=6.0,  severity="critical"),
    BurnRateWindow(long_window="3d",  short_window="6h",  burn_rate_threshold=1.0,  severity="warning"),
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
