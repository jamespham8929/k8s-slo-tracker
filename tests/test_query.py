"""Tests for window substitution.

Small surface, but both the live evaluator and the rule generator go through it,
which is what stops the rules Prometheus evaluates from measuring something
different than the tracker does.
"""

import pytest

from slo_tracker.query import WINDOW_TOKEN, render, validate


class TestRender:
    def test_substitutes_every_occurrence(self):
        template = "sum(increase(a[WINDOW])) / sum(increase(b[WINDOW]))"
        assert render(template, "6h") == "sum(increase(a[6h])) / sum(increase(b[6h]))"

    def test_leaves_a_query_without_the_token_alone(self):
        assert render("up", "6h") == "up"


class TestValidate:
    def test_returns_the_stripped_template(self):
        assert validate("  sum(increase(a[WINDOW]))\n", "good_query", "svc/slo") == \
            "sum(increase(a[WINDOW]))"

    def test_missing_query_is_rejected(self):
        with pytest.raises(ValueError, match="good_query is required"):
            validate(None, "good_query", "svc/slo")

    def test_blank_query_is_rejected(self):
        with pytest.raises(ValueError, match="total_query is required"):
            validate("   \n", "total_query", "svc/slo")

    def test_query_without_the_token_is_rejected(self):
        # Otherwise every window evaluates the same expression, turning a
        # multi-window alert back into the single-window one it replaced.
        with pytest.raises(ValueError, match=WINDOW_TOKEN):
            validate("sum(increase(a[5m]))", "good_query", "svc/slo")

    def test_the_error_names_the_slo(self):
        with pytest.raises(ValueError, match="checkout-api/availability"):
            validate(None, "good_query", "checkout-api/availability")
