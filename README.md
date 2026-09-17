# troll-poly-bot

Latency-aware **paper trading** bot for Polymarket's 5-minute crypto up/down
markets, across **every asset the venue lists** (today: BTC, ETH, SOL, XRP,
DOGE, BNB, HYPE -- discovered, not hard-coded).

No live trading path is wired. `BotConfig.from_env` refuses any mode but `paper`.

```bash
python -m pip install -e ".[dev]"
python -m pytest -q                                   # 207 tests

python -m troll_poly_bot --balance 100                # paper-trade every listed asset
python -m troll_poly_bot --balance 100 --assets BTC,ETH
python -m troll_poly_bot.web                          # dashboard on http://127.0.0.1:8765

python scripts/research_profiles.py                   # asset profiles, market calibration, lead/lag
python scripts/research_models.py                     # walk-forward model comparison (OOS)
python scripts/research_backtest.py                   # execution-aware replay, OLD vs NEW
python scripts/research_diagnose.py                   # epoch-clustered stats, strike test
python scripts/research_cluster_check.py              # evidence and sample-size arithmetic
```

**Read [docs/strategy.md](docs/strategy.md) first.** It is the audit, the
measurements, the redesign and the honest verdict: on 292 recorded windows no
strategy shows a statistically defensible edge after the venue's real fee
curve, and the bot is now built to *collect that evidence* while refusing most
trades with a reason code.

## What the bot does

```
AssetRegistry     probe 28 candidate slugs on Gamma, trade what exists, re-probe every 30 min
MarketMeta        each window with the venue's own feeSchedule (0.07 * p(1-p), taker only)
CompositeSpot     Binance + Bybit + Coinbase, basis-adjusted median, cross-exchange deviation gate
OrderFlow         trade-flow and book imbalance per asset -> a measured, sign-agnostic drift in the pricer
Strike            60 s trailing TWAP of the composite at the window open (what Chainlink settles on)
Model             analytic TWAP digital pricer, Student-t(4), TwoScaleVol, blended 50/50 with the mid
Costs             fee curve, slippage, an uncertainty charge for the vol estimate
Feeds             exponential reconnect backoff, per-feed health in the state; a failed venue
                  probe keeps an asset instead of delisting it
Engine            every gate emits a Reason: EDGE_TOO_SMALL, LOW_LIQUIDITY, HIGH_SLIPPAGE,
                  STALE_DATA, DATA_INCONSISTENT, BAD_REGIME, RISK_LIMIT, LOW_CONFIDENCE, ...
Risk              fractional Kelly x confidence; caps per market, per asset, per 300 s EPOCH
                  (all assets in a window are one bet), total, daily loss, drawdown
Execution         revalidate against the freshest book, FOK one tick through, never chase
Settlement        the venue's own outcome; per-asset PnL; an epoch-clustered evidence t-stat
Recorder          every window archived to data/charts/<slug>.json with touch, depth, per-exchange spot
```

State goes to `data/live_state.json` (dashboard), fills and settlements to
`data/live_trades.jsonl`, closed windows to `data/charts/`.

## The one thing to understand first

A 5m up/down market is a **cash-or-nothing digital option on an oracle print**.
The market resolves

```
Up  iff  TWAP60(t_close) >= TWAP60(t_open)         (Chainlink 60-second TWAP; ties are Up)
```

Work in per-second vol. BTC at ~0.9 bps/s, ETH 1.5, SOL 1.5, XRP 2.0 (measured
on the archive). The strike is the trailing 60 s average at the open, not the
opening spot, and inside the last minute most of the settling average is already
locked in, so uncertainty collapses far faster than a spot-settled option's.
Derivation and Monte Carlo check in [pricing/twap.py](src/troll_poly_bot/pricing/twap.py)
and [tests/test_twap.py](tests/test_twap.py).

## What the data says (Sept 2026, 292 windows, 25 hours)

* **The market knows the rules.** Ten seconds after the open its price
  correlates 0.89 with the TWAP model and 0.38 with a naive spot-strike model.
* **The market is roughly calibrated** at every horizon; its Brier goes
  0.238 -> 0.202 -> 0.172 -> 0.120 -> 0.052 -> 0.013 -> 0.003 through the window.
* **The analytic model beats it by 0.003 Brier**, the 50/50 blend by 0.0026
  with an epoch-bootstrap interval that excludes zero. Every fitted model
  (logistic, gradient boosting, per-asset, hybrid) is worse than the market.
* **The token price absorbs a spot move within the same second.** With a
  ~1 s reaction lag there is no latency edge to harvest from a home connection.
* **Books go one-sided once a window is decided**: 60-70% of samples in the
  last 60 s, 93-98% in the last 10 s. Nothing for a taker to buy.
* **Fees are 0.07 * p(1-p) per share, taker only** -- 1.75c at 0.50. The old
  code assumed 2% * min(p, 1-p) and under-counted by 1.75-2.8x.
* **The replay of the new engine is positive but not evidence**: +$96 on 50
  fills, yet positive in only 11 of 22 epochs and t = 0.94 on the held-out
  half. Showing a 5c edge at 2 sigma needs ~400 independent traded epochs;
  3c needs ~1,000. That is weeks of recording.

## The four bugs the first live runs exposed (kept for the record)

1. **The system clock was ~1 s fast.** Fixed NTP-style against the venue.
2. **The volatility estimator was ~2x too low**: sqrt(t) scaling of 1 s
   returns on trending prices. `TwoScaleVol` measures the horizon correction.
3. **The information lag was double-counted in the TWAP mean**, producing a
   fair of 1.000 on a 1-cent token. The mean is now bounded by its inputs.
4. **The first strike came from a partial TWAP window.** The bot refuses to
   strike until the 60 s window is fully covered.

Plus two found in this overhaul: the fee model (above) and **self-graded
settlement** -- when the Gamma row had vanished, the bot graded its own trade
with the same Binance TWAP that produced it (48% of live fills). The archive
loader now grades from the venue's closing book.

**A large edge against a liquid book is a bug report, not an opportunity.**
The `MODEL_SANITY` gate halts an asset when the analytic fair persistently
disagrees with the mid by more than 6c.

## How the paper engine models the network

Three clocks: `decision_ts` (the strategy saw a book `md_book` ms old and a
spot `md_spot` ms old), `exchange_ts = decision + submit` (the order lands and
matches against the book *as it is then*), `ack_ts` (the strategy learns the
result). Latencies are measured live and fed back as an empirical bootstrap.
Pessimistic where ambiguous: top-of-book contention, resting orders fill only
when traded through, late orders are rejected.
See [execution/paper.py](src/troll_poly_bot/execution/paper.py).

Measured from this machine: spot feed p50 ~545 ms, book ~430 ms, order round
trip ~890 ms, reaction lag ~1.0 s.

## The dashboard

`python -m troll_poly_bot.web` serves the console on loopback. Start / Stop /
Restart run the trader as a child process. Panels: equity and KPIs (now with
the **evidence** t-stat, today's PnL and drawdown, PnL by asset), token price
chart with fill markers, **open markets with the engine decision on each**
(model p, market p, net edge, regime, reason), model vs market with sources
and regime, trades, the **reason-code bar chart** ("why it is not trading"),
and the on-disk history across restarts.

## Layout

```
src/troll_poly_bot/
  config.py                EngineConfig / RiskConfig / FeedConfig / VolConfig (+ legacy sim config)
  live.py                  the live paper trader
  market/discovery.py      probe which assets are listed
  feeds/markets.py         Gamma parsing incl. fee schedule; strike tracker
  feeds/spot.py            Binance / Bybit / Coinbase adapters, composite price
  feeds/polymarket.py      CLOB book parsing (worst-first!)
  pricing/                 vol, digital pricer, TWAP pricer
  features/engine.py       24 causal features, shared by live and replay
  models/                  logistic, Platt, isotonic, calibration metrics
  signals/costs.py         fee curve and cost breakdown
  signals/regime.py        regime labels
  strategy/engine.py       the decision engine (Reason codes)
  strategy/taker.py        the ORIGINAL strategy, kept for the sim and the comparison
  risk/limits.py           caps, Kelly x confidence, halts
  execution/               latency model, paper matching engine
  backtest/                archive loader (bell-graded), decision dataset, execution replay
  analytics/scorecard.py   scorecards
  web/                     dashboard
scripts/research_*.py      every table in docs/strategy.md
scripts/smoke_*.py         live probes of discovery and the spot feeds
tests/                     207 tests
```

## Status

Done: multi-asset discovery, composite spot, verified fee curve, feature engine,
model comparison harness, execution-aware replay, decision engine with reason
codes, risk manager with epoch-correlated caps, dashboard, recorder with depth
and per-exchange prices, evidence statistic.

Not done, by design: live order signing. It stays out until the evidence
statistic clears 2 over a few hundred traded epochs -- see
[docs/strategy.md](docs/strategy.md) section I for the arithmetic.
