"""Tests for the Prometheus HTTP API client.

Every number this project reports comes through here, so the parts worth pinning
are the ones that decide whether a result is trustworthy: an empty result is not
a zero, a failed query is not an empty one, and a zero denominator is not an
availability of zero.
"""

from datetime import datetime, timezone

import pytest
import requests

from slo_tracker.prometheus_client import PrometheusClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class RecordingSession:
    """Stands in for requests.Session, recording calls and replaying responses."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self._responses:
            raise AssertionError(f"unexpected extra request to {url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client_with(*responses, base_url="http://prom:9090/"):
    client = PrometheusClient(base_url)
    session = RecordingSession(*responses)
    client._session = session
    return client, session


def instant(value):
    return FakeResponse({
        "status": "success",
        "data": {"result": [{"metric": {}, "value": [1700000000, str(value)]}]},
    })


def empty_instant():
    return FakeResponse({"status": "success", "data": {"result": []}})


def range_of(*values):
    return FakeResponse({
        "status": "success",
        "data": {"result": [{"metric": {}, "values": [[i, str(v)] for i, v in enumerate(values)]}]},
    })


class TestQuery:
    def test_returns_the_scalar(self):
        client, session = client_with(instant(42.5))
        assert client.query("up") == 42.5
        assert session.calls[0]["url"] == "http://prom:9090/api/v1/query"
        assert session.calls[0]["params"]["query"] == "up"

    def test_trailing_slash_is_stripped_from_the_base_url(self):
        client, session = client_with(instant(1))
        client.query("up")
        assert "//api" not in session.calls[0]["url"]

    def test_no_data_is_none_not_zero(self):
        # A zero here would read as "no errors" and silently mark a dead service
        # perfectly healthy.
        client, _ = client_with(empty_instant())
        assert client.query("up") is None

    def test_explicit_evaluation_time_is_passed_through(self):
        client, session = client_with(instant(1))
        at = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        client.query("up", time=at)
        assert session.calls[0]["params"]["time"] == at.timestamp()

    def test_time_is_omitted_when_not_given(self):
        client, session = client_with(instant(1))
        client.query("up")
        assert "time" not in session.calls[0]["params"]

    def test_prometheus_error_status_raises(self):
        client, _ = client_with(FakeResponse({"status": "error", "error": "parse error"}))
        with pytest.raises(RuntimeError, match="parse error"):
            client.query("not a query")

    def test_error_without_a_message_still_raises(self):
        client, _ = client_with(FakeResponse({"status": "error"}))
        with pytest.raises(RuntimeError, match="unknown"):
            client.query("up")

    def test_http_error_propagates(self):
        client, _ = client_with(FakeResponse({}, status_code=503))
        with pytest.raises(requests.HTTPError):
            client.query("up")

    def test_connection_error_propagates(self):
        client, _ = client_with(requests.ConnectionError("refused"))
        with pytest.raises(requests.ConnectionError):
            client.query("up")


class TestQueryRangeAverage:
    def test_averages_every_point(self):
        client, session = client_with(range_of(1, 2, 3, 4))
        assert client.query_range_average(
            "up", datetime(2026, 1, 15, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 1, tzinfo=timezone.utc),
        ) == 2.5
        assert session.calls[0]["url"].endswith("/api/v1/query_range")
        assert session.calls[0]["params"]["step"] == "5m"

    def test_averages_across_series(self):
        response = FakeResponse({
            "status": "success",
            "data": {"result": [
                {"metric": {"pod": "a"}, "values": [[0, "10"]]},
                {"metric": {"pod": "b"}, "values": [[0, "20"]]},
            ]},
        })
        client, _ = client_with(response)
        start = datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert client.query_range_average("up", start, start) == 15.0

    def test_no_series_is_none(self):
        client, _ = client_with(FakeResponse({"status": "success", "data": {"result": []}}))
        start = datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert client.query_range_average("up", start, start) is None

    def test_series_with_no_points_is_none(self):
        response = FakeResponse({
            "status": "success",
            "data": {"result": [{"metric": {}, "values": []}]},
        })
        client, _ = client_with(response)
        start = datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert client.query_range_average("up", start, start) is None

    def test_step_is_configurable(self):
        client, session = client_with(range_of(1))
        start = datetime(2026, 1, 15, tzinfo=timezone.utc)
        client.query_range_average("up", start, start, step="1m")
        assert session.calls[0]["params"]["step"] == "1m"


class TestAvailabilitySLI:
    def test_divides_good_by_total(self):
        client, session = client_with(range_of(99), range_of(100))
        assert client.get_availability_sli("good", "total") == 0.99
        assert len(session.calls) == 2

    def test_zero_denominator_is_none(self):
        # No traffic is not an availability of zero. Returning 0.0 here would
        # report a perfectly idle service as totally broken.
        client, _ = client_with(range_of(0), range_of(0))
        assert client.get_availability_sli("good", "total") is None

    def test_missing_numerator_is_none(self):
        client, _ = client_with(
            FakeResponse({"status": "success", "data": {"result": []}}), range_of(100)
        )
        assert client.get_availability_sli("good", "total") is None

    def test_missing_denominator_is_none(self):
        client, _ = client_with(
            range_of(99), FakeResponse({"status": "success", "data": {"result": []}})
        )
        assert client.get_availability_sli("good", "total") is None

    def test_window_hours_control_the_range(self):
        client, session = client_with(range_of(1), range_of(1))
        client.get_availability_sli("good", "total", window_hours=6)
        params = session.calls[0]["params"]
        assert params["end"] - params["start"] == pytest.approx(6 * 3600, abs=1)


class TestSLIForWindows:
    def test_returns_window_1h_6h_and_24h(self):
        # Two calls per window, in the order the tuple documents.
        responses = []
        for good in (95, 96, 97, 98):
            responses.extend([range_of(good), range_of(100)])
        client, session = client_with(*responses)

        assert client.get_sli_for_windows("good", "total") == (0.95, 0.96, 0.97, 0.98)

        spans = [
            call["params"]["end"] - call["params"]["start"]
            for call in session.calls[::2]
        ]
        assert [round(s / 3600) for s in spans] == [24 * 30, 1, 6, 24]
