# k8s-slo-tracker

A CLI tool for defining, tracking, and alerting on SLOs (Service Level Objectives) for services running on Kubernetes. It reads a YAML configuration, queries Prometheus for current SLI values, calculates error budgets, and generates Alertmanager-compatible alert rules.

## Concepts

- **SLI** (Service Level Indicator): a metric measuring service behavior, like request success rate or latency at p99
- **SLO**: a target for an SLI, like "99.9% of requests must succeed over a 30-day rolling window"
- **Error budget**: the margin of allowed failures. At 99.9% availability, the monthly error budget is ~43 minutes.

## Configuration

Define your SLOs in a YAML file:

```yaml
services:
  - name: checkout-api
    namespace: payments
    slos:
      - name: availability
        type: availability
        target: 0.999
        window: 30d
        good_query: |
          sum(rate(http_requests_total{service="checkout-api",code=~"2.."}[5m]))
        total_query: |
          sum(rate(http_requests_total{service="checkout-api"}[5m]))

      - name: latency-p99
        type: latency
        target: 0.95
        window: 30d
        threshold_ms: 300
        query: |
          histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service="checkout-api"}[5m])) by (le))
```

## Usage

```bash
# Install dependencies
pip install -r requirements.txt

# Check current error budget burn for all SLOs
python -m slo_tracker.cli status --config config/slos.yaml --prometheus http://prometheus:9090

# Generate Alertmanager rules and write to file
python -m slo_tracker.cli generate-alerts --config config/slos.yaml --output alerts.yaml

# Watch mode: refresh every 60 seconds
python -m slo_tracker.cli watch --config config/slos.yaml --prometheus http://prometheus:9090 --interval 60
```

## Alert generation

The tool generates multi-window burn rate alerts following the Google SRE workbook approach. For each SLO it emits:

- **Page** (critical): 14x burn rate over 1h + 5h windows
- **Ticket** (warning): 6x burn rate over 6h + 3d windows

## Running tests

```bash
pip install pytest pytest-mock
pytest tests/ -v
```

## License

MIT
