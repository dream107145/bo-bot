"""Run the paper bot against the synthetic market across latency profiles.

    python scripts/run_sim.py
    python scripts/run_sim.py --windows 300 --maker-lag 300
"""
from __future__ import annotations

import argparse
import sys

from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import PROFILES
from troll_poly_bot.sim import SimConfig, compare_profiles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=120)
    ap.add_argument("--maker-lag", type=float, default=600.0,
                    help="how stale the opposing maker's view is, in ms")
    ap.add_argument("--maker-bias", type=float, default=0.08,
                    help="favourite-longshot bias; 0 = an unbiased opponent")
    ap.add_argument("--half-spread", type=float, default=0.015)
    ap.add_argument("--min-edge", type=float, default=None)
    ap.add_argument("--fee-rate", type=float, default=0.02)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    sim_cfg = SimConfig(
        n_windows=args.windows,
        maker_lag_ms=args.maker_lag,
        maker_bias=args.maker_bias,
        half_spread=args.half_spread,
        seed=args.seed,
    )
    bot_cfg = BotConfig()
    bot_cfg.fees.rate = args.fee_rate
    if args.min_edge is not None:
        bot_cfg.strategy.min_edge = args.min_edge

    print(f"\n{sim_cfg.n_windows} windows x {sim_cfg.window_s:.0f}s  "
          f"| maker lag {sim_cfg.maker_lag_ms:.0f}ms  "
          f"| maker bias {sim_cfg.maker_bias:.0%}  "
          f"| half-spread {sim_cfg.half_spread:.3f}  "
          f"| fee {bot_cfg.fees.rate:.1%}  "
          f"| min edge {bot_cfg.strategy.min_edge:.3f}")
    print(f"starting balance {bot_cfg.starting_balance:.2f}\n")

    results = compare_profiles(sim_cfg, bot_cfg)

    header = (f"{'profile':<16} {'react':>9}  {'pnl':>9}  {'trades':>10}  "
              f"{'fill':>6} {'slip':>9} {'fees':>8} {'late':>5} {'fok_rej':>8}")
    print(header)
    print("-" * len(header))
    for r in results:
        print(r.summary())

    print("\ncalibration -- did the model's probabilities come true?")
    ch = (f"{'profile':<16} {'fills':>7} {'predicted':>10} {'realised':>9} "
          f"{'gap':>8} {'edge@dec':>9} {'pnl/share':>10}")
    print(ch)
    print("-" * len(ch))
    for r in results:
        c = r.calibration()
        if not c:
            print(f"{r.profile_name:<16} {'(none)':>7}")
            continue
        print(f"{r.profile_name:<16} {int(c['n_fills']):>7} "
              f"{c['predicted_win_rate']:>10.3f} {c['realised_win_rate']:>9.3f} "
              f"{c['realised_win_rate'] - c['predicted_win_rate']:>8.3f} "
              f"{c['edge_at_decision']:>9.3f} {c['realised_pnl_per_share']:>10.4f}")
    print("  gap < 0 means the model was overconfident on the fills it ACTUALLY")
    print("  got. That is the signature of adverse selection rather than a bad")
    print("  pricer: the orders that survive to fill are selected against you.")

    print("\nwhy trades were skipped (last profile):")
    for code, n in sorted(results[-1].skips.items(), key=lambda kv: -kv[1]):
        print(f"  {code:<32} {n:>8}")

    print("\nread this as: the spread between INSTANT and your real profile is")
    print("the part of the PnL that is infrastructure, not insight.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
