"""Parameter schema driving the web UI.

Single source of truth: the UI form is generated from this, so a control can
never drift from the dataclass field it writes to. Adding a knob here is the
only step needed to expose it.

``help`` text is shown in the UI. Keep it to the *why*, not the *what* -- the
label already says what it is.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..config import BotConfig
from ..execution.latency import PROFILES
from ..sim import SimConfig


def _f(key, label, kind="number", **kw) -> dict[str, Any]:
    return {"key": key, "label": label, "kind": kind, **kw}


#: group -> fields. ``target`` says which config object the value belongs to.
GROUPS: list[dict[str, Any]] = [
    {
        "id": "market",
        "title": "Market & opponent",
        "blurb": "The synthetic venue you are trading against.",
        "target": "sim",
        "fields": [
            _f("n_windows", "Windows", min=10, max=3000, step=10,
               help="Each is one 5-minute market. Under ~200 the result is mostly noise."),
            _f("sigma_per_sec", "Volatility (bps/sec)", min=0.1, max=5.0, step=0.05,
               scale=1e-4,
               help="BTC at ~50% annualised is about 0.9 bps/sec."),
            _f("maker_lag_ms", "Opponent lag (ms)", min=0, max=3000, step=50,
               help="How stale the opposing quotes are. The gap between this and "
                    "your own reaction lag is the speed component of any edge."),
            _f("maker_bias", "Favourite-longshot bias", min=0.0, max=0.3, step=0.01,
               help="How far the opponent shrinks extreme probabilities toward 0.50. "
                    "This is the edge that survives being slow."),
            _f("half_spread", "Half-spread", min=0.005, max=0.10, step=0.005,
               help="Half the opponent's quoted spread, in probability units."),
            _f("depth", "Depth per level (shares)", min=25, max=5000, step=25),
            _f("seed", "Price path seed", min=0, max=99999, step=1,
               help="Same seed means the same price path, so two runs are comparable."),
        ],
    },
    {
        "id": "edge",
        "title": "Entry rules",
        "blurb": "When the bot is allowed to cross the spread.",
        "target": "strategy",
        "fields": [
            _f("min_edge", "Minimum edge", min=0.0, max=0.30, step=0.005,
               help="Required edge after fees, in probability units. A 0.05 edge "
                    "is 5 cents per share."),
            _f("min_edge_sigmas", "Edge vs model uncertainty", min=0.0, max=5.0, step=0.1,
               help="Multiples of the pricer's own error bar. Stops the bot "
                    "trading its own volatility-estimate noise."),
            _f("market_shrink", "Shrink toward market price", min=0.0, max=1.0, step=0.05,
               help="Winner's-curse correction. We only trade when our estimate "
                    "says the market is wrong, which selects for estimates that "
                    "are too high. Live: paid 0.844, model claimed 0.923, "
                    "realised 0.800. 0 trusts the model, 1 defers entirely."),
            _f("min_trade_price", "Minimum price", min=0.01, max=0.20, step=0.01,
               help="Never buy cheaper than this. Penny longshots are where the "
                    "tail estimate is least reliable and the favourite-longshot "
                    "bias works hardest against a buyer."),
            _f("min_fair_for_trade", "Lower fair bound", min=0.01, max=0.49, step=0.01,
               help="Below this counts as a tail and is tradeable."),
            _f("max_fair_for_trade", "Upper fair bound", min=0.51, max=0.99, step=0.01,
               help="Between the bounds there is no model edge, only variance."),
            _f("trade_window_start_s", "Start looking (s left)", min=10, max=300, step=5),
            _f("trade_window_end_s", "Stop looking (s left)", min=0, max=60, step=1,
               help="Fair value moves fastest late, but you cannot trade to the bell."),
            _f("use_twap", "Price against the 60s TWAP", kind="toggle",
               help="The live venue settles on a Chainlink 60s TWAP. The synthetic "
                    "market here settles on closing spot, so this is off for the "
                    "simulator and on for live."),
        ],
    },
    {
        "id": "sizing",
        "title": "Sizing & risk",
        "target": "strategy",
        "fields": [
            _f("kelly_fraction", "Kelly fraction", min=0.01, max=1.0, step=0.01,
               help="Full Kelly assumes the probability is known. It is not."),
            _f("max_position_usdc", "Max per market", min=5, max=500, step=5),
            _f("max_gross_usdc", "Max gross exposure", min=10, max=2000, step=10),
            _f("max_shares_per_order", "Max shares per order", min=5, max=2000, step=5),
            _f("min_order_usdc", "Min order size", min=1, max=50, step=1),
        ],
    },
    {
        "id": "latency",
        "title": "Network",
        "target": "strategy",
        "fields": [
            _f("latency_safety_margin_ms", "Safety margin (ms)", min=0, max=3000, step=50,
               help="Refuse to send an order that cannot land before resolution."),
            _f("max_spot_age_ms", "Max spot age (ms)", min=50, max=5000, step=50,
               help="Staleness feeds straight into the fair value."),
            _f("max_book_age_ms", "Max book age (ms)", min=50, max=5000, step=50),
        ],
    },
    {
        "id": "vol",
        "title": "Volatility estimator",
        "target": "vol",
        "fields": [
            _f("halflife_s", "EWMA half-life (s)", min=10, max=600, step=10,
               help="Short half-lives track regime changes but make the tails jumpy, "
                    "and the tails are where the money is."),
            _f("grid_ms", "Sampling grid (ms)", min=250, max=5000, step=250,
               help="Returns are sampled on a fixed grid; tick returns are "
                    "dominated by bid-ask bounce."),
        ],
    },
    {
        "id": "costs",
        "title": "Costs & capital",
        "target": "top",
        "fields": [
            _f("fee_rate", "Fee rate", min=0.0, max=0.10, step=0.001,
               help="Charged as rate x min(p, 1-p) x size, so the tails are cheaper "
                    "than the middle. Verify against current Polymarket docs."),
            _f("starting_balance", "Starting balance", min=100, max=100000, step=100),
            _f("latency_seed", "Network seed", min=0, max=99999, step=1,
               help="Seeded so a strategy change is the only thing moving the PnL."),
        ],
    },
]


def build_schema() -> dict[str, Any]:
    bot, sim = BotConfig(), SimConfig()
    sim_d, strat_d = asdict(sim), asdict(bot.strategy)
    vol_d = asdict(bot.vol)
    top_d = {
        "fee_rate": bot.fees.rate,
        "starting_balance": bot.starting_balance,
        "latency_seed": bot.latency_seed,
    }
    source = {"sim": sim_d, "strategy": strat_d, "vol": vol_d, "top": top_d}

    groups = []
    for g in GROUPS:
        fields = []
        for f in g["fields"]:
            val = source[g["target"]].get(f["key"])
            if val is not None and f.get("scale"):
                val = val / f["scale"]
            fields.append({**f, "value": val, "target": g["target"]})
        groups.append({**g, "fields": fields})

    return {
        "groups": groups,
        "profiles": [
            {
                "name": name,
                "reaction_lag_ms": round(p.reaction_lag_ms(), 1),
                "round_trip_ms": round(p.round_trip_ms(), 1),
                "contention": p.contention,
            }
            for name, p in PROFILES.items()
        ],
    }


def apply_params(params: dict[str, Any]) -> tuple[SimConfig, BotConfig]:
    """Turn a flat {target: {key: value}} payload into configs.

    Unknown keys are ignored rather than raising: the UI and the schema are
    versioned together, but a stale browser tab should degrade to defaults
    instead of erroring.
    """
    sim, bot = SimConfig(), BotConfig()
    scales = {
        f["key"]: f.get("scale")
        for g in GROUPS for f in g["fields"] if f.get("scale")
    }

    def coerce(obj, key, raw):
        if not hasattr(obj, key):
            return
        current = getattr(obj, key)
        if isinstance(current, bool):
            setattr(obj, key, bool(raw))
            return
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return
        if scales.get(key):
            val *= scales[key]
        setattr(obj, key, int(val) if isinstance(current, int) else val)

    for key, raw in (params.get("sim") or {}).items():
        coerce(sim, key, raw)
    for key, raw in (params.get("strategy") or {}).items():
        coerce(bot.strategy, key, raw)
    for key, raw in (params.get("vol") or {}).items():
        coerce(bot.vol, key, raw)

    top = params.get("top") or {}
    if "fee_rate" in top:
        bot.fees.rate = float(top["fee_rate"])
    if "starting_balance" in top:
        bot.starting_balance = float(top["starting_balance"])
    if "latency_seed" in top:
        bot.latency_seed = int(float(top["latency_seed"]))

    # Guard the invariants the dataclasses cannot express.
    sim.n_windows = max(1, min(int(sim.n_windows), 3000))
    bot.strategy.max_fair_for_trade = max(
        bot.strategy.max_fair_for_trade, bot.strategy.min_fair_for_trade + 0.02
    )
    bot.strategy.trade_window_start_s = max(
        bot.strategy.trade_window_start_s, bot.strategy.trade_window_end_s + 1.0
    )
    return sim, bot
