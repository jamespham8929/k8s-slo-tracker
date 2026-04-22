# Benchmarks

## false_page_simulation.py

Replays many SLO evaluation ticks against a synthetic low-traffic service and
compares three alerting strategies. See the main README for the headline table.

The simulation is deterministic given `--seed`, so the numbers in the README
reproduce exactly. Traffic is modeled by drawing each request's success or
failure from the configured true error rate (binomial), then aggregating into
the standard window counts. The two scenarios are a healthy service (every page
is false) and a service with a real 2% sustained error rate (every page is
correct).

```bash
# Headline run
PYTHONPATH=. python benchmarks/false_page_simulation.py --rps 0.1 --ticks 3000

# Sweep traffic levels to see where single-window alerting starts flapping
for rps in 0.05 0.1 0.5 1 5; do
  PYTHONPATH=. python benchmarks/false_page_simulation.py --rps $rps --ticks 2000
done
```

What the numbers show, and do not show:

- The confidence-gated engine does not beat the SRE-workbook multi-window method
  on healthy-service false pages. That method is already at zero. The honest
  claim is parity there.
- It does beat single-window threshold alerting, which is what a lot of teams
  actually run, by eliminating the low-traffic false-page stream.
- It detects a real burn on more ticks than either baseline on sparse traffic,
  because it adapts to a slower window instead of failing to confirm.
