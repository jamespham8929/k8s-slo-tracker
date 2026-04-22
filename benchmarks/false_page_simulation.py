"""Simulation harness for low-traffic burn-rate alerting.

Two experiments:

  1. false-page test - a service whose TRUE error rate is healthy (well inside
     its SLO). Every page here is a false positive caused by sampling noise.

  2. detection test - a service with a genuine sustained burn. Every strategy
     should page. We record whether each one catches it.

Three strategies are compared:

  naive-single  A single 5-minute error-ratio threshold. Common in practice
                ("alert if 5m error rate > X"). Cheap and very noisy on low
                traffic.
  naive-multi   The Google SRE workbook multi-window, multi-burn-rate alert.
                The industry best practice. Robust against false pages already.
  gated         This project: confidence-gated, traffic-adaptive burn rate.

The point of the project is not to beat naive-multi on healthy-service false
pages, it already does well there. It is to (a) match it without the operational
cost of hand-tuning windows per service, (b) eliminate the single-window false
pages many teams actually suffer, and (c) emit an explicit insufficient-data
state so a starved SLO is visible instead of silently green.

Run:
    PYTHONPATH=. python benchmarks/false_page_simulation.py
"""

from __future__ import annotations

import argparse
import random

from slo_tracker.adaptive import AdaptiveBurnRateEngine, DEFAULT_LADDER
from slo_tracker.measurement import MultiWindowMeasurement, WindowMeasurement

WINDOW_SECONDS = {"5m": 300, "30m": 1800, "1h": 3600, "6h": 21600, "1d": 86400, "3d": 259200}
ALL_WINDOWS = ("5m", "30m", "1h", "6h", "1d", "3d")


def simulate_window(rps: float, window: str, true_error_rate: float) -> WindowMeasurement:
    n = int(rps * WINDOW_SECONDS[window])
    if n < 200000:
        errors = sum(1 for _ in range(n) if random.random() < true_error_rate)
    else:
        errors = max(0, int(random.gauss(n * true_error_rate, (n * true_error_rate) ** 0.5)))
    return WindowMeasurement(window=window, good_count=n - errors, total_count=n)


def tick(rps: float, true_error_rate: float) -> MultiWindowMeasurement:
    windows = {w: simulate_window(rps, w, true_error_rate) for w in ALL_WINDOWS}
    return MultiWindowMeasurement("sim", "availability", windows)


def naive_single(m: MultiWindowMeasurement, target: float) -> bool:
    budget = 1.0 - target
    sw = m.get("5m")
    return bool(sw and sw.total_count > 0 and sw.error_ratio / budget >= 14.4)


def naive_multi(m: MultiWindowMeasurement, target: float) -> bool:
    budget = 1.0 - target
    for tier in DEFAULT_LADDER:
        if tier.severity.value != "critical":
            continue
        lw, sw = m.get(tier.long_window), m.get(tier.short_window)
        if not lw or not sw:
            continue
        if lw.error_ratio / budget >= tier.burn_rate and sw.error_ratio / budget >= tier.burn_rate:
            return True
    return False


def run(rps: float, true_error_rate: float, ticks: int, target: float, seed: int):
    random.seed(seed)
    engine = AdaptiveBurnRateEngine(target=target)
    counts = {"naive-single": 0, "naive-multi": 0, "gated": 0}
    for _ in range(ticks):
        m = tick(rps, true_error_rate)
        counts["naive-single"] += naive_single(m, target)
        counts["naive-multi"] += naive_multi(m, target)
        counts["gated"] += engine.evaluate(m).should_page
    return {k: v / ticks for k, v in counts.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rps", type=float, default=0.1, help="requests per second")
    parser.add_argument("--ticks", type=int, default=3000)
    parser.add_argument("--target", type=float, default=0.999)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    print(f"\nSLO {args.target:.3%}, {args.rps} rps ({args.rps * 60:.0f} req/min), "
          f"{args.ticks} ticks per scenario\n")

    print("Experiment 1: healthy service (true error 0.05%) - all pages are false")
    healthy = run(args.rps, 0.0005, args.ticks, args.target, args.seed)
    for k in ("naive-single", "naive-multi", "gated"):
        print(f"  {k:<14} false-page rate {healthy[k]:6.2%}")

    print("\nExperiment 2: real sustained burn (true error 2%) - every page is correct")
    burning = run(args.rps, 0.02, args.ticks, args.target, args.seed)
    for k in ("naive-single", "naive-multi", "gated"):
        print(f"  {k:<14} detection rate  {burning[k]:6.2%}")
    print()


if __name__ == "__main__":
    main()
