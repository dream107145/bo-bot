# troll-poly-bot

Latency-aware **paper trading** bot for Polymarket's 5-minute crypto up/down
markets, across **every asset the venue lists** (today: BTC, ETH, SOL, XRP,
DOGE, BNB, HYPE -- discovered, not hard-coded).

No live trading path is wired. `BotConfig.from_env` refuses any mode but `paper`.

```bash
python3.11 -m venv .venv && . .venv/bin/activate      # 3.11+ required
python -m pip install -e ".[dev]"
python -m pytest -q                                   # 207 tests

python -m troll_poly_bot --balance 100                # paper-trade every listed asset
python -m troll_poly_bot --balance 100 --assets BTC,ETH
python -m troll_poly_bot.web                          # dashboard on http://127.0.0.1:8765

pm2 start ecosystem.config.js                         # or run both as services

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
TakeProfit        sell into the bid once it is 0.05 above our average entry and the
                  exit clears both fees; frees the epoch's risk budget early
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

## Taking profit before settlement

A 5m up/down market is a digital: it pays 1.0 or 0.0. A position that has moved
our way is worth something *now*, on the bid, and worth a coin flip at close.
The engine sells into the bid when all of the following hold:

```
best bid  >=  average entry + take_profit_delta        default 0.05
realised PnL after BOTH fees  >=  take_profit_min_net  default 0.005/share
bid depth at the touch        >=  our whole position   (FOK is all-or-nothing)
seconds left                  >   take_profit_min_secs_left   default 10
```

`--take-profit 0.08` moves the threshold; `--take-profit 0` turns it off. The
exit is a full close, and the window is then left alone -- re-entering a book
we just de-risked into would pay the spread twice to undo the exit.

The profit test is computed after the exit fee *and* the entry fee already
paid, because the venue's curve (`0.07 * p(1-p)` per share, each way) is large
enough at mid prices to turn a 1-tick rise into a loss. The dashboard shows
these as `banked +x.xx` rather than `won`, and the fill row as `UP exit`.

**This reduces variance, not risk-adjusted return.** It gives up the rest of
the move whenever the position would have won, and pays a second taker fee to
do it. Nothing here has been measured over enough epochs to say whether that
trade is worth making -- `scripts/research_backtest.py` is where that question
gets answered, and until it is, treat the feature as a variance knob rather
than an improvement.

The other half is the stop: sell when the bid falls `stop_loss_delta` (default
0.20) **below** entry. The band is deliberately wide, because digitals revert
and a stop that fires on noise pays the spread to lock in a loss the window
would have taken back. It is a circuit breaker for windows that have already
decided against us, not a scalp. `--stop-loss 0` disables it. The take-profit
is checked first, and its fee guard never applies to a stop -- refusing to cut
a loss because the exit costs a fee is how a small loss becomes the whole
stake.

## Trading more often without risking more

The frequency limit was never the signal, it was the arithmetic:
`max_epoch_exposure / max_position` is the ceiling on how many 300 s windows
the bot can be in at once, and at `30/10` that was **three**.

Sizing is therefore small rather than the caps being loose:

```
kelly_fraction            0.25 -> 0.15
max_position_usdc         10   -> 5        per market
max_asset_exposure_usdc   20   -> 15
max_concurrent_positions  7    -> 10
max_epoch_exposure_usdc   30      UNCHANGED
max_total_exposure_usdc   50      UNCHANGED
max_daily_loss_usdc       20      UNCHANGED
max_drawdown_usdc         30      UNCHANGED
```

Six windows per epoch instead of three, with the same money at risk. Not one
aggregate cap moved, and `max_concurrent_positions * max_position_usdc` is
still <= `max_total_exposure_usdc`, so the position count cannot outrun the
money cap. The evidence statistic is clustered per epoch and needs *epochs* to
reach `|t| ~ 2`, so more, smaller windows is also the only way that number ever
becomes meaningful.

**`min_net_edge` was deliberately left at 0.02.** It is the largest reason code
by far (~60% of evaluations) and the obvious way to trade more, but
docs/strategy.md already measured 0.02 against 0.05: 4.5x the fills at
+0.056/share (t 1.6) against +0.148/share (t 1.9). Lowering it further buys
frequency with thinner and less proven edge, which is the opposite of safer.
`--min-edge 0.015` is there if you want to make that trade explicitly.

## The dashboard

`python -m troll_poly_bot.web` serves the console on loopback. Start / Stop /
Restart run the trader as a child process. Panels: equity and KPIs (now with
the **evidence** t-stat, today's PnL and drawdown, PnL by asset), token price
chart with fill markers, **open markets with the engine decision on each**
(model p, market p, net edge, regime, reason), model vs market with sources
and regime, trades, the **reason-code bar chart** ("why it is not trading"),
and the on-disk history across restarts.

The chart redraws **10x a second**: the bot writes `data/live_state.json` every
100 ms and the page polls at the same cadence. That is only affordable because
`/api/live` sends the drawn market's history and nothing else, and `?since=<t>`
trims it again to the points the client is missing -- a steady tick is ~5 KB
against ~9.6 MB if the whole state went out. If the chart ever feels sluggish,
check the response size before touching the interval; the poll is guarded by an
`inFlight` flag, so a payload that takes longer than a tick to arrive silently
becomes the real refresh rate.

A trader the console did not spawn is still *detected* -- from the mtime of
`data/live_state.json` -- and shown as running, but the console says so plainly
instead of offering a Stop it cannot honour. That is the case when pm2 owns the
trader; see below.

## Running it as a service (pm2)

`ecosystem.config.js` registers two apps: `tpb-web` (the console) and `tpb-bot`
(the paper trader). Two, not one, for the same reason `server.py` runs the
trader as a child process -- it owns long-lived websockets and its own asyncio
loop, and a hung feed must never take the console down with it.

```bash
pm2 start ecosystem.config.js     # both apps
pm2 status                        # tpb-web, tpb-bot
pm2 logs tpb-bot                  # follow the trader
pm2 restart tpb-bot               # cycle the trader
pm2 stop all && pm2 delete all    # unregister

pm2 save                          # remember the current process list
pm2 startup                       # print the boot command to install (systemd)
```

`pm2 startup` prints a command to run; that command installs the unit that
replays `pm2 save` on boot. Undo with `pm2 unstartup systemd`.

Both apps run `.venv/bin/python` directly (`interpreter: 'none'`) with `cwd`
pinned to the repo root, because the bot resolves `data/` relative to the
working directory. `PYTHONUNBUFFERED=1` is set or pm2's logs lag a buffer
behind. `kill_timeout` is 15 s so websockets close and state flushes before
SIGKILL -- the trader handles SIGINT and SIGTERM and shuts down cleanly. Logs
land in `~/.pm2/logs/tpb-{web,bot}-{out,error}.log`.

**While pm2 owns `tpb-bot`, use pm2 for its lifecycle, not the dashboard's
Start / Stop buttons.** The console refuses to spawn a second trader on top of
one it did not start -- two processes writing `data/live_state.json` would
corrupt the record. Everything else in the UI (state, history, sims) is
unaffected. If you would rather drive the trader from the console, run
`pm2 delete tpb-bot` and keep only `tpb-web` under pm2.

The dashboard **has no auth**. `do_POST` will start the trader, queue sim jobs
and write files for anyone who can reach the port, so the bind address is the
only access control there is.

`ecosystem.config.js` is set to `--host 0.0.0.0` for remote access on a VPS.
That publishes the unauthenticated API to every interface; pair it with a
reverse proxy that requires a password, or a firewall rule limiting port 8765
to known source addresses. To keep it closed instead, set `--host 127.0.0.1`
and forward the port over SSH:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@host     # then browse 127.0.0.1:8765 locally
```

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
ecosystem.config.js        pm2 apps: tpb-web (console) and tpb-bot (trader)
```

## Status

Done: multi-asset discovery, composite spot, verified fee curve, feature engine,
model comparison harness, execution-aware replay, decision engine with reason
codes, risk manager with epoch-correlated caps, dashboard, recorder with depth
and per-exchange prices, evidence statistic.

Not done, by design: live order signing. It stays out until the evidence
statistic clears 2 over a few hundred traded epochs -- see
[docs/strategy.md](docs/strategy.md) section I for the arithmetic.
