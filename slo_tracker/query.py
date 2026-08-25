"""Window substitution for the PromQL templates in the SLO config.

Queries in the config carry a literal WINDOW token that gets replaced per alert
window, so one query definition serves every window in the ladder. Both the live
evaluator and the rule generator go through here, which is what stops the rules
Prometheus evaluates from measuring something different than the tracker does.
"""

from __future__ import annotations

WINDOW_TOKEN = "WINDOW"


def render(template: str, window: str) -> str:
    """Substitute the window into a query template."""
    return template.replace(WINDOW_TOKEN, window)


def validate(template: str | None, field: str, slo: str) -> str:
    """Check a query is usable before it is turned into rules.

    A query without the WINDOW token silently produces the same expression for
    every window, which turns a multi-window alert into the single-window alert
    it was designed to replace. That is worth failing on rather than shipping.
    """
    if not template or not template.strip():
        raise ValueError(f"{slo}: {field} is required to generate rules")
    if WINDOW_TOKEN not in template:
        raise ValueError(
            f"{slo}: {field} has no {WINDOW_TOKEN} token, so every window would "
            f"evaluate the same expression"
        )
    return template.strip()
