"""Tests for the command-line surface.

These use click's runner and fake the Prometheus client, so nothing here touches
a network. What they pin is the contract a user sees: exit codes, what lands on
stdout, and whether output is pipeable into the tool it is meant for.
"""

import pytest
import yaml
from click.testing import CliRunner
from rich.console import Console

from slo_tracker import cli as cli_module
from slo_tracker.cli import cli, load_config


@pytest.fixture(autouse=True)
def wide_console(monkeypatch):
    """Pin the console width.

    rich sizes its tables to the terminal, so under a narrow one the assertions
    below would be testing the truncation rather than the output.
    """
    monkeypatch.setattr(cli_module, "console", Console(width=200))

CONFIG = """
services:
  - name: checkout-api
    namespace: payments
    team: payments
    slos:
      - name: availability
        type: availability
        target: 0.999
        window: 30d
        good_query: |
          sum(increase(http_requests_total{service="checkout-api",code=~"2.."}[WINDOW]))
        total_query: |
          sum(increase(http_requests_total{service="checkout-api"}[WINDOW]))
"""

CONFIG_WITH_LATENCY = CONFIG + """
      - name: latency
        type: latency
        target: 0.99
        window: 30d
        query: histogram_quantile(0.99, checkout)
        threshold_ms: 300
"""


def write_config(tmp_path, body=CONFIG):
    path = tmp_path / "slos.yaml"
    path.write_text(body)
    return str(path)


class FakePrometheus:
    """Returns a fixed count for every query, whatever it is."""

    def __init__(self, *_args, **_kwargs):
        self.queries = []

    def query(self, promql, time=None):
        self.queries.append(promql)
        return 10.0 if "code=~" in promql else 1000.0

    def get_sli_for_windows(self, good_query, total_query):
        return (0.9995, 0.999, 0.9992, 0.9994)


class EmptyPrometheus(FakePrometheus):
    def get_sli_for_windows(self, good_query, total_query):
        return (None, None, None, None)


class TestLoadConfig:
    def test_parses_services_and_slos(self, tmp_path):
        configs = load_config(write_config(tmp_path))
        assert len(configs) == 1
        slo = configs[0].slos[0]
        assert slo.service == "checkout-api"
        assert slo.namespace == "payments"
        assert slo.team == "payments"
        assert slo.target == 0.999
        assert slo.window_days == 30
        assert "WINDOW" in slo.good_query

    def test_window_units(self, tmp_path):
        for window, days in (("30d", 30), ("4w", 28), ("bogus", 30)):
            path = tmp_path / f"{window}.yaml"
            path.write_text(CONFIG.replace("window: 30d", f"window: {window}"))
            assert load_config(str(path))[0].slos[0].window_days == days

    def test_slo_level_team_overrides_the_service_team(self, tmp_path):
        body = CONFIG.replace("        window: 30d", "        window: 30d\n        team: sre")
        assert load_config(write_config(tmp_path, body))[0].slos[0].team == "sre"

    def test_empty_config(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("services: []\n")
        assert load_config(str(path)) == []


class TestGenerateAlerts:
    def test_emits_a_prometheus_rule_by_default(self, tmp_path):
        result = CliRunner().invoke(cli, ["generate-alerts", "--config", write_config(tmp_path)])
        assert result.exit_code == 0, result.output

        manifest = yaml.safe_load(result.output)
        assert manifest["kind"] == "PrometheusRule"
        assert manifest["metadata"]["labels"] == {"release": "kube-prometheus-stack"}

    def test_output_is_pipeable(self, tmp_path):
        # stdout has to be the manifest and nothing else, or `| kubectl apply -f -`
        # fails on decoration.
        result = CliRunner().invoke(cli, ["generate-alerts", "--config", write_config(tmp_path)])
        assert result.output.startswith("apiVersion:")
        assert "[green]" not in result.output

    def test_rules_format_omits_the_manifest_wrapper(self, tmp_path):
        result = CliRunner().invoke(
            cli, ["generate-alerts", "--config", write_config(tmp_path), "--format", "rules"]
        )
        assert result.exit_code == 0, result.output
        parsed = yaml.safe_load(result.output)
        assert "groups" in parsed
        assert "kind" not in parsed

    def test_name_namespace_and_labels_are_settable(self, tmp_path):
        result = CliRunner().invoke(cli, [
            "generate-alerts", "--config", write_config(tmp_path),
            "--name", "team-rules", "--namespace", "observability",
            "--label", "release=prom", "--label", "team=payments",
        ])
        assert result.exit_code == 0, result.output
        metadata = yaml.safe_load(result.output)["metadata"]
        assert metadata["name"] == "team-rules"
        assert metadata["namespace"] == "observability"
        assert metadata["labels"] == {"release": "prom", "team": "payments"}

    def test_malformed_label_is_rejected(self, tmp_path):
        result = CliRunner().invoke(
            cli, ["generate-alerts", "--config", write_config(tmp_path), "--label", "nope"]
        )
        assert result.exit_code != 0
        assert "key=value" in result.output

    def test_runbook_base_is_threaded_through(self, tmp_path):
        result = CliRunner().invoke(cli, [
            "generate-alerts", "--config", write_config(tmp_path),
            "--runbook-base", "https://wiki.internal/rb",
        ])
        assert "https://wiki.internal/rb/checkout-api" in result.output

    def test_writes_to_a_file(self, tmp_path):
        out = tmp_path / "rules.yaml"
        result = CliRunner().invoke(cli, [
            "generate-alerts", "--config", write_config(tmp_path), "--output", str(out),
        ])
        assert result.exit_code == 0, result.output
        assert yaml.safe_load(out.read_text())["kind"] == "PrometheusRule"
        assert "Wrote" in result.output

    def test_latency_slos_are_skipped_with_a_warning(self, tmp_path):
        # The old behaviour emitted alerts for latency SLOs that referenced
        # recording rules it never wrote, so they could never fire.
        out = tmp_path / "rules.yaml"
        result = CliRunner().invoke(cli, [
            "generate-alerts", "--config", write_config(tmp_path, CONFIG_WITH_LATENCY),
            "--format", "rules", "--output", str(out),
        ])
        assert result.exit_code == 0, result.output
        assert "skipping checkout-api/latency" in result.output

        groups = yaml.safe_load(out.read_text())["groups"]
        assert [g["name"] for g in groups] == ["slo.checkout-api.availability"]

    def test_a_config_with_nothing_generatable_fails(self, tmp_path):
        body = CONFIG_WITH_LATENCY.replace("type: availability", "type: latency")
        result = CliRunner().invoke(
            cli, ["generate-alerts", "--config", write_config(tmp_path, body)]
        )
        assert result.exit_code != 0
        assert "no SLOs" in result.output

    def test_missing_config_fails_cleanly(self):
        result = CliRunner().invoke(cli, ["generate-alerts", "--config", "/no/such/file.yaml"])
        assert result.exit_code != 0


class TestStatus:
    def test_renders_a_row_per_slo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_module, "PrometheusClient", FakePrometheus)
        result = CliRunner().invoke(cli, ["status", "--config", write_config(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "checkout-api" in result.output
        assert "availability" in result.output

    def test_skips_slos_with_no_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_module, "PrometheusClient", EmptyPrometheus)
        result = CliRunner().invoke(cli, ["status", "--config", write_config(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "checkout-api" not in result.output


class TestEvaluate:
    def test_reports_a_severity_and_a_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_module, "PrometheusClient", FakePrometheus)
        result = CliRunner().invoke(cli, ["evaluate", "--config", write_config(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "checkout-api" in result.output

    def test_estimator_choice_is_validated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_module, "PrometheusClient", FakePrometheus)
        result = CliRunner().invoke(cli, [
            "evaluate", "--config", write_config(tmp_path), "--estimator", "vibes",
        ])
        assert result.exit_code != 0

    def test_bayesian_estimator_runs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_module, "PrometheusClient", FakePrometheus)
        result = CliRunner().invoke(cli, [
            "evaluate", "--config", write_config(tmp_path), "--estimator", "bayesian",
        ])
        assert result.exit_code == 0, result.output


class TestWatch:
    def test_refreshes_then_stops(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_module, "PrometheusClient", FakePrometheus)

        ticks = {"n": 0}

        def fake_sleep(_seconds):
            ticks["n"] += 1
            if ticks["n"] >= 2:
                raise KeyboardInterrupt
        monkeypatch.setattr(cli_module.time, "sleep", fake_sleep)

        result = CliRunner().invoke(cli, ["watch", "--config", write_config(tmp_path)])
        assert isinstance(result.exception, (KeyboardInterrupt, SystemExit))
        assert ticks["n"] == 2

    def test_a_failed_refresh_does_not_end_the_loop(self, tmp_path, monkeypatch):
        # A watch that dies on the first Prometheus blip is a watch nobody trusts.
        def exploding_client(*_args, **_kwargs):
            raise RuntimeError("prometheus unreachable")
        monkeypatch.setattr(cli_module, "PrometheusClient", exploding_client)
        monkeypatch.setattr(
            cli_module.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt)
        )

        result = CliRunner().invoke(cli, ["watch", "--config", write_config(tmp_path)])
        assert "prometheus unreachable" in result.output


class TestGatedGeneration:
    def test_engine_flag_switches_to_gated_rules(self, tmp_path):
        result = CliRunner().invoke(cli, [
            "generate-alerts", "--config", write_config(tmp_path),
            "--format", "rules", "--engine", "gated",
        ])
        assert result.exit_code == 0, result.output
        assert "slo_tracker_alert_state" in result.output
        assert "slo:sli_error:ratio_rate" not in result.output


class TestServe:
    def test_starts_the_server_and_loops(self, tmp_path, monkeypatch):
        calls = {}
        monkeypatch.setattr(cli_module, "PrometheusClient", FakePrometheus)
        monkeypatch.setattr(
            cli_module, "start_metrics_server",
            lambda exporter, port: calls.setdefault("port", port),
        )

        def fake_loop(evaluator, slos, exporter, interval, stop):
            calls["slos"] = [s.name for s in slos]
            calls["interval"] = interval

        monkeypatch.setattr(cli_module, "serve_forever", fake_loop)
        # Keep pytest's own Ctrl-C handling.
        monkeypatch.setattr(cli_module.signal, "signal", lambda *_: None)
        result = CliRunner().invoke(cli, [
            "serve", "--config", write_config(tmp_path, CONFIG_WITH_LATENCY),
            "--port", "9200", "--interval", "30",
        ])

        assert result.exit_code == 0, result.output
        assert calls == {"port": 9200, "slos": ["availability"], "interval": 30}
        assert "skipping" in result.output  # the latency SLO

    def test_refuses_a_config_with_nothing_to_evaluate(self, tmp_path, monkeypatch):
        latency_only = """
services:
  - name: checkout-api
    namespace: payments
    slos:
      - name: latency
        type: latency
        target: 0.99
        query: histogram_quantile(0.99, checkout)
        threshold_ms: 300
"""
        monkeypatch.setattr(cli_module, "start_metrics_server", lambda *_: None)
        result = CliRunner().invoke(
            cli, ["serve", "--config", write_config(tmp_path, latency_only)]
        )
        assert result.exit_code != 0
        assert "no SLOs" in result.output
