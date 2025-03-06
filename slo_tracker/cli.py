"""CLI entry point for k8s-slo-tracker."""

import time
import yaml
import click
from rich.console import Console
from rich.table import Table

from .models import SLODefinition, SLOType, ServiceSLOConfig
from .calculator import ErrorBudgetCalculator
from .prometheus_client import PrometheusClient
from .alert_generator import AlertRuleGenerator

console = Console()


def load_config(path: str) -> list[ServiceSLOConfig]:
    with open(path) as f:
        raw = yaml.safe_load(f)

    configs = []
    for svc in raw.get("services", []):
        slos = []
        for s in svc.get("slos", []):
            slos.append(SLODefinition(
                name=s["name"],
                service=svc["name"],
                namespace=svc["namespace"],
                slo_type=SLOType(s["type"]),
                target=s["target"],
                window_days=_parse_window(s.get("window", "30d")),
                good_query=s.get("good_query"),
                total_query=s.get("total_query"),
                latency_query=s.get("query"),
                latency_threshold_ms=s.get("threshold_ms"),
            ))
        configs.append(ServiceSLOConfig(name=svc["name"], namespace=svc["namespace"], slos=slos))
    return configs


def _parse_window(window: str) -> int:
    if window.endswith("d"):
        return int(window[:-1])
    if window.endswith("w"):
        return int(window[:-1]) * 7
    return 30


@click.group()
def cli():
    pass


@cli.command()
@click.option("--config", required=True, help="Path to SLO config YAML")
@click.option("--prometheus", default="http://localhost:9090", show_default=True)
def status(config, prometheus):
    """Print current error budget status for all configured SLOs."""
    services = load_config(config)
    prom = PrometheusClient(prometheus)
    calc = ErrorBudgetCalculator()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Service")
    table.add_column("SLO")
    table.add_column("Target")
    table.add_column("Current SLI")
    table.add_column("Budget left")
    table.add_column("Burn 1h")
    table.add_column("Severity")

    for svc in services:
        for slo in svc.slos:
            window_sli, sli_1h, sli_6h, sli_24h = prom.get_sli_for_windows(
                slo.good_query, slo.total_query
            )
            if window_sli is None:
                continue

            st = calc.compute_status(slo, window_sli, sli_1h or window_sli, sli_6h or window_sli, sli_24h or window_sli)
            color = {"critical": "red", "warning": "yellow", "none": "green"}.get(st.alert_severity, "white")

            table.add_row(
                svc.name,
                slo.name,
                f"{slo.target * 100:.3f}%",
                f"{st.current_sli * 100:.4f}%",
                f"{st.error_budget_remaining * 100:.1f}%",
                f"{st.burn_rate_1h:.2f}x",
                f"[{color}]{st.alert_severity}[/{color}]",
            )

    console.print(table)


@cli.command("generate-alerts")
@click.option("--config", required=True, help="Path to SLO config YAML")
@click.option("--output", default=None, help="Output file path (default: stdout)")
def generate_alerts(config, output):
    """Generate Prometheus alerting rules from SLO definitions."""
    services = load_config(config)
    gen = AlertRuleGenerator()

    all_slos = [slo for svc in services for slo in svc.slos]
    rules_yaml = gen.generate(all_slos)

    if output:
        with open(output, "w") as f:
            f.write(rules_yaml)
        console.print(f"[green]Wrote alert rules to {output}[/green]")
    else:
        console.print(rules_yaml)


@cli.command()
@click.option("--config", required=True)
@click.option("--prometheus", default="http://localhost:9090")
@click.option("--interval", default=60, show_default=True, help="Refresh interval in seconds")
def watch(config, prometheus, interval):
    """Continuously refresh SLO status display."""
    while True:
        console.clear()
        console.rule(f"[bold]SLO Status[/bold] — refreshing every {interval}s")
        try:
            status.callback(config=config, prometheus=prometheus)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        time.sleep(interval)


if __name__ == "__main__":
    cli()
