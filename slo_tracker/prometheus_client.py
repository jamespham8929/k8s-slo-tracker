"""Prometheus HTTP API client for querying SLI metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
import requests


class PrometheusClient:
    def __init__(self, base_url: str, timeout: int = 30):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()

    def query(self, promql: str, time: Optional[datetime] = None) -> float | None:
        """Run an instant query and return a single scalar result."""
        params: dict = {"query": promql}
        if time:
            params["time"] = time.timestamp()

        resp = self._session.get(
            f"{self._base_url}/api/v1/query",
            params=params,
            timeout=self._timeout,
        )
        resp.raise_for_status()

        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {data.get('error', 'unknown')}")

        result = data.get("data", {}).get("result", [])
        if not result:
            return None

        return float(result[0]["value"][1])

    def query_range_average(
        self,
        promql: str,
        start: datetime,
        end: datetime,
        step: str = "5m",
    ) -> float | None:
        """Query a range and return the time-weighted average of all data points."""
        resp = self._session.get(
            f"{self._base_url}/api/v1/query_range",
            params={
                "query": promql,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()

        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if not results:
            return None

        all_values = [float(v[1]) for r in results for v in r.get("values", [])]
        if not all_values:
            return None

        return sum(all_values) / len(all_values)

    def get_availability_sli(
        self, good_query: str, total_query: str, window_hours: int = 1
    ) -> float | None:
        """Compute availability = good_events / total_events over the given window."""
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(hours=window_hours)

        good = self.query_range_average(good_query, start, now)
        total = self.query_range_average(total_query, start, now)

        if good is None or total is None or total == 0:
            return None

        return good / total

    def get_sli_for_windows(
        self, good_query: str, total_query: str
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """Return SLI values for the window, 1h, 6h, and 24h lookback."""
        return (
            self.get_availability_sli(good_query, total_query, window_hours=24 * 30),
            self.get_availability_sli(good_query, total_query, window_hours=1),
            self.get_availability_sli(good_query, total_query, window_hours=6),
            self.get_availability_sli(good_query, total_query, window_hours=24),
        )
