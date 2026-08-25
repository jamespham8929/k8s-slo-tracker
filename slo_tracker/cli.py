"""CLI entry point for k8s-slo-tracker."""

import time

import click
import yaml
from rich.console import Console
from rich.table import Table

from .adaptive import Estimator
from .alert_generator import AlertRuleGenerator, partition_supported
from .calculator import ErrorBudgetCalculator
from .evaluator import SLOEvaluator
from .models import ServiceSLOConfig, SLODefinition, SLOType
from .prometheus_client import PrometheusClient

console = Console()

_SEVERITY_COLOR = {
    "critical": "red",
    "warning": "yellow",
    "none": "green",
    "insufficient_data": "dim",
}


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

            st = calc.compute_status(
                slo, window_sli, sli_1h or window_sli, sli_6h or window_sli, sli_24h or window_sli
            )
            color = {"critical": "red", "warning": "yellow", "none": "green"}.get(
                st.alert_severity, "white"
            )

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


@cli.command()
@click.option("--config", required=True, help="Path to SLO config YAML")
@click.option("--prometheus", default="http://localhost:9090", show_default=True)
@click.option(
    "--confidence", default=0.95, show_default=True, help="Confidence level for burn-rate gating"
)
@click.option("--estimator", default="wilson", type=click.Choice(["wilson", "bayesian"]))
def evaluate(config, prometheus, confidence, estimator):
    """Evaluate SLOs with the confidence-gated, traffic-adaptive engine.

    Unlike `status`, this path uses event counts and the AdaptiveBurnRateEngine,
    so it is the one that stays sane on low-traffic services.
    """
    services = load_config(config)
    evaluator = SLOEvaluator(
        PrometheusClient(prometheus),
        confidence=confidence,
        estimator=Estimator(estimator),
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Service")
    table.add_column("SLO")
    table.add_column("Severity")
    table.add_column("Burn (lower bound)", justify="right")
    table.add_column("Samples", justify="right")
    table.add_column("Reason")

    for svc in services:
        for slo in svc.slos:
            result = evaluator.evaluate(slo)
            decision = result.decision
            color = _SEVERITY_COLOR.get(decision.severity.value, "white")

            fired = next((v for v in decision.verdicts if v.confident_breach), None)
            burn = f"{fired.lower_bound_burn:.1f}x" if fired else "-"
            samples = f"{max((v.sample_count for v in decision.verdicts), default=0):.0f}"

            table.add_row(
                svc.name,
                slo.name,
                f"[{color}]{decision.severity.value}[/{color}]",
                burn,
                samples,
                decision.reason,
            )

    console.print(table)


@cli.command("generate-alerts")
@click.option("--config", required=True, help="Path to SLO config YAML")
@click.option("--output", default=None, help="Output file path (default: stdout)")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["prometheus-rule", "rules"]),
    default="prometheus-rule",
    show_default=True,
    help="prometheus-rule for kubectl apply, rules for a plain rule_files entry",
)
@click.option("--name", default="slo-tracker-rules", show_default=True,
              help="PrometheusRule object name")
@click.option("--namespace", default="monitoring", show_default=True,
              help="Namespace for the PrometheusRule")
@click.option(
    "--label",
    "labels",
    multiple=True,
    default=("release=kube-prometheus-stack",),
    show_default=True,
    help="Label on the PrometheusRule, as key=value. Repeatable. The operator "
         "ignores a manifest its ruleSelector does not match.",
)
@click.option("--runbook-base", default=None, help="Base URL for runbook_url annotations")
def generate_alerts(config, output, output_format, name, namespace, labels, runbook_base):
    """Generate Prometheus recording and alerting rules from SLO definitions.

    The thresholds and windows come from the same ladder the live engine uses, so
    the rules Prometheus evaluates cannot drift from what `evaluate` decides.
    """
    services = load_config(config)
    all_slos = [slo for svc in services for slo in svc.slos]

    supported, unsupported = partition_supported(all_slos)
    for item in unsupported:
        console.print(
            f"[yellow]skipping {item.slo.service}/{item.slo.name}: {item.reason}[/yellow]",
            highlight=False,
        )
    if not supported:
        raise click.ClickException("no SLOs in the config can be turned into rules")

    kwargs = {}
    if runbook_base:
        kwargs["runbook_base_url"] = runbook_base
    gen = AlertRuleGenerator(**kwargs)

    if output_format == "rules":
        rendered = gen.generate(supported)
    else:
        rendered = gen.generate_prometheus_rule(
            supported, name=name, namespace=namespace, labels=_parse_labels(labels)
        )

    if output:
        with open(output, "w") as f:
            f.write(rendered)
        console.print(f"[green]Wrote {output_format} for {len(supported)} SLOs to {output}[/green]")
    else:
        # print() rather than console.print(), so piping into kubectl or promtool
        # gets the YAML and nothing else.
        print(rendered, end="")


def _parse_labels(pairs: tuple[str, ...]) -> dict[str, str]:
    labels = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise click.BadParameter(f"expected key=value, got {pair!r}", param_hint="--label")
        labels[key.strip()] = value.strip()
    return labels


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
