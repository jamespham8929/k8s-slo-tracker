"""Error budget calculation and burn rate analysis."""

from __future__ import annotations

from .models import SLODefinition, SLOStatus


class ErrorBudgetCalculator:
    """
    Calculates error budget consumption and burn rates from raw SLI measurements.

    Burn rate is expressed as a multiple of the sustainable consumption rate.
    A burn rate of 1.0 means the budget is being consumed at exactly the rate
    that exhausts it over the full window. A burn rate of 14.4 exhausts the
    monthly budget in 50 hours.
    """

    def compute_status(
        self,
        definition: SLODefinition,
        current_sli: float,
        sli_1h: float,
        sli_6h: float,
        sli_24h: float,
    ) -> SLOStatus:
        budget_remaining = self._budget_remaining(definition, current_sli)
        burn_1h = self._burn_rate(definition, sli_1h)
        burn_6h = self._burn_rate(definition, sli_6h)
        burn_24h = self._burn_rate(definition, sli_24h)

        return SLOStatus(
            definition=definition,
            current_sli=current_sli,
            error_budget_remaining=budget_remaining,
            burn_rate_1h=burn_1h,
            burn_rate_6h=burn_6h,
            burn_rate_24h=burn_24h,
        )

    def _budget_remaining(self, definition: SLODefinition, current_sli: float) -> float:
        """
        Fraction of the error budget remaining over the measurement window.
        Returns a value in [0, 1]. Values above 1 are clamped to 1 (budget over-delivered).
        """
        error_rate = 1.0 - current_sli
        total_budget = definition.error_budget_total

        if total_budget <= 0:
            return 1.0

        consumed_fraction = error_rate / total_budget
        remaining = 1.0 - consumed_fraction
        return max(0.0, min(1.0, remaining))

    def _burn_rate(self, definition: SLODefinition, window_sli: float) -> float:
        """
        How fast the budget is burning relative to sustainable rate.
        burn_rate = (1 - window_sli) / (1 - target)
        """
        error_rate = 1.0 - window_sli
        budget_per_unit_time = definition.error_budget_total

        if budget_per_unit_time <= 0:
            return 0.0

        return error_rate / budget_per_unit_time

    def minutes_until_exhausted(self, status: SLOStatus) -> float | None:
        """
        Estimate minutes until the error budget is fully consumed, based on 1h burn rate.
        Returns None if the budget is not currently burning.
        """
        if status.burn_rate_1h <= 1.0:
            return None  # burning slower than sustainable

        total_budget_minutes = (
            status.definition.window_days * 24 * 60 * status.definition.error_budget_total
        )
        remaining_budget_minutes = total_budget_minutes * status.error_budget_remaining

        if remaining_budget_minutes <= 0:
            return 0.0

        # At this burn rate, how long until zero?
        rate_per_minute = status.burn_rate_1h / 60
        return remaining_budget_minutes / rate_per_minute
