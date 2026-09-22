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

## When a window never resolves

The venue outcome does not always arrive. After `SETTLE_MAX_ATTEMPTS` retries
with no TWAP of our own to fall back on, the window is abandoned: the position
is flattened at its last mark, **the risk budget is handed back**, and a ledger
row goes out with `unresolved: true, marked: true`.

That middle part is not a detail. An abandoned window used to keep its entry in
the risk manager's open book forever, and each one consumed a slot out of
`max_concurrent_positions` permanently:

> 2026-09-19, 17:07-17:17 UTC. Ten windows failed to resolve inside one
> ten-minute venue outage. That took the bot to exactly 10 of 10 open
> positions, and it bought nothing for the next **twenty hours** -- 8,985
> `RISK_LIMIT` refusals -- while every feed was green, the book lag was 110 ms
> and the dashboard looked entirely healthy.

`_reap` now carries the same guarantee as a backstop: **nothing leaves it still
on the risk book**, whatever the reason settlement did not run.

The marked PnL is an estimate, so it is kept out of `epoch_pnl` and out of the
win/loss counts -- it moves the balance, because the money really did move, but
it is not evidence of edge in either direction.

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

## Connecting a real Polymarket account (read only)

The venue's Data API serves on-chain positions to anyone who asks: **no key, no
signature, no credentials**. All this needs is a wallet address.

```bash
python -m troll_poly_bot.web --wallet 0xYOUR_PROXY_WALLET
# or
export TPB_POLYMARKET_WALLET=0xYOUR_PROXY_WALLET
```

An "Real balance and trade history" panel then appears with portfolio value,
open positions marked to market, unrealised PnL and recent trades, straight
from the venue. `GET /api/account` is the same data.

Use the **account wallet** shown under your Polymarket profile (a Deposit
Wallet for accounts since May 2026, a Gnosis-Safe proxy for older ones), not
the signer key address. They are different addresses and the API knows nothing
about the latter.

What this deliberately is *not*:

* It **cannot place, cancel or modify an order**. `feeds/account.py` issues GET
  requests to a public endpoint and holds no key; a test parses the module and
  fails if it ever imports anything capable of signing, or builds a request
  with a body.
* It **does not touch the bot**. The panel lives in the dashboard process, so a
  slow HTTP call cannot stall a trading tick, and no strategy, risk or engine
  value is read from or written to the account.
* The paper bot **keeps paper trading**. The two sets of numbers sit side by
  side on purpose: one is what the strategy did on simulated fills, the other
  is what the account actually holds. Reconciling them is the point.

Each section carries its own error, so a rate-limited positions call does not
blank the balance that loaded fine, and the panel says how old the data is
rather than showing a stale number as current. The server caches for 10 s and
the page refreshes every 15 s; the endpoint is free, which is exactly why it
should not be hammered.

## Trading the real account (`--live`)

The same strategy, the same gates, a real venue behind them. Read
[docs/strategy.md](docs/strategy.md) first: the evidence for an edge is thin
and the fee curve is unforgiving. Then, if you still want to:

```bash
python -m pip install -e ".[live]"          # adds polymarket-client, the unified SDK
cp .env.example .env                        # fill in the real-money block, keep it out of git
python -m troll_poly_bot --live --balance 50             # DRY RUN: authenticates, signs, posts nothing
python -m troll_poly_bot --live --armed --balance 50     # real fill-or-kill orders
```

What goes in `.env`: `TPB_POLY_PRIVATE_KEY` (the key that signs; for an email
login it is exported from your profile settings) and `TPB_POLY_WALLET`, the
account wallet shown under your Polymarket profile (older name
`TPB_POLY_FUNDER`). The signer's address and the profile address are
different, and that is how it should be: since May 2026 every account is a
**Deposit Wallet**, a contract the signer controls, and the venue reports the
wallet type itself, so nothing about signature types needs configuring.
`python scripts/live_check.py` prints only addresses and balances and says
whether the account is ready. `--armed` additionally requires
`TPB_LIVE_ACK=I_UNDERSTAND_REAL_MONEY`.

The venue moved to CLOB V2 in April 2026. The bot trades through Polymarket's
unified `polymarket-client` SDK, which signs V2 orders, including the
ERC-1271 signatures a Deposit Wallet needs. Each order goes out as a
fill-or-kill market order with a price bound, the V2 shape of the paper
engine's FOK limit order.

Three locks, in order:

1. **Dry run by default.** `--live` without `--armed` does everything except
   POST: it reads the real balance and allowance, reconciles every 30 s, and
   signs each order the strategy wants, logging it as `DRY_RUN`. Run this
   until the log looks like what you expect.
2. **Arming is explicit twice** -- the flag and the acknowledgement variable.
   The dashboard's Start button can never arm the bot.
3. **Caps outside the strategy** (`--max-order-usdc 5`, `--max-open-usdc 25`,
   `--max-daily-loss 10`, `--max-orders-per-hour 60`) plus a **kill file**:
   create `data/KILL` and nothing further is sent. Five consecutive venue
   errors also halt it. Orders are fill-or-kill only, so stopping the process
   never leaves a resting order behind.

Bankroll: `--balance` is what the risk caps scale to, capped to what the
account holds. The bot's balance is that bankroll plus the venue's cash
change since start; the account's real balance is shown separately.

**Redemption.** A winning token pays out only once it is redeemed, and a
Deposit Wallet (or legacy proxy) redeems through Polymarket's relayer. Give
the bot a **Relayer API key** (app: Settings -> API Keys -> Relayer API Keys;
`TPB_POLY_RELAYER_API_KEY` and `TPB_POLY_RELAYER_API_KEY_ADDRESS` in `.env`)
and it redeems each resolved winner itself, retrying while the market is
still settling on-chain. Without one, turn on **Auto-Redeem** in the app.
Either way, until the cash lands the amount shows as *pending redemption*
and is counted in equity; the next reconciliation retires it.

Fees are estimated from the market's schedule per fill; the venue's real
deduction is what the balance reconciliation reflects.

## The dashboard

`python -m troll_poly_bot.web` serves the console on loopback. Start / Stop /
Restart run the trader as a child process. Panels: equity and KPIs (now with
the **evidence** t-stat, today's PnL and drawdown, PnL by asset), token price
chart with fill markers, **open markets with the engine decision on each**
(model p, market p, net edge, regime, reason), model vs market with sources
and regime, trades, the **reason-code bar chart** ("why it is not trading"),
the on-disk history across restarts.

### Five pages, one connection

The console is split into tabs on the URL hash, so a link to `#ledger` or
`#saved` opens there:

| Page | What is on it |
|---|---|
| **Market** (`#market`) | equity and KPIs, the three window charts, open markets with the engine's decision, model vs market, why it is not trading, and the run's trades |
| **Earnings** (`#earnings`) | realised PnL by calendar day, week or month |
| **Ledger** (`#ledger`) | one row per position across all runs, with its buy and sell times, and the read-only venue account |
| **Saved markets** (`#saved`) | the `data/charts` archive of closed windows |
| **Bot variables** (`#variables`) | the tuning panel |

This is not only tidier. The market page redraws ten times a second, and the
ledger and account polls run on their own timers; all of that is now gated on
the page actually being open, so reading the archive costs nothing in the
trading view's CPU. Switching to a page refreshes it on arrival rather than
waiting for its next tick.

### Ledger: round trips, not events

`data/live_trades.jsonl` is an event stream -- one line per fill, one per
settlement. A single position routinely produces three or four of them,
because a window is often entered in two partial fills, then sold, then
settled. Listed flat, with no BUY/SELL marking, those lines look like the same
trade written out several times. They never were duplicates; they were the
parts of one round trip.

So `ledger.py` folds them: one row per position carrying **when it was bought,
when it was sold or settled**, the average price at each end, how long it was
held, the fees and the result. A trip closes when the position goes flat, so
buying the same window again after an exit starts a new one, and both sides of
one window stay separate trades. The raw stream is one toggle away
(`?view=events`), and there BUY and SELL are now named and coloured.

Every ledger row is also stamped with the bot that wrote it. A paper bot and a
real-money bot share this file when both run from one directory, and without
the tag their trades read as one interleaved history; a Money filter appears
once more than one kind has written.

### Earnings (daily / weekly / monthly)

Realised PnL bucketed into calendar days, weeks or months in the machine's own
timezone, read from `data/earnings.jsonl`. That file is **append-only and never
reset**: the run ledger (`data/live_trades.jsonl`) is cleared on a paper restart
because the balance resets with it, which is right for the run view and useless
for "what did I earn last month". Every settled window appends one line tagged
with the mode that produced it, so **paper money and real money are counted
separately and never summed** -- the All / Paper / Real toggle picks which.

The chart is a diverging column chart on a zero baseline: the question is
whether a period made money or lost it, which is polarity, not magnitude.
Profit and loss wear the same two hues as UP and DOWN (blue and orange), which
validate at CVD ΔE 24.7 light and 26.8 dark; the conventional green/red pair
fails the same check at 4.1 and is unreadable for the commonest colour
blindness. Periods with no trading are drawn as gaps rather than closed up,
and a table view sits under the chart. `?period=week&scope=live` links a
particular view.

### Bot variables

A tuning panel that writes `data/controls.json`; a running bot re-reads it
within about two seconds and applies the values to the live config objects --
no restart, and it works even for a bot this server did not start. Roughly
thirty settings across edge, book filters, timing, exits, sizing, circuit
breakers and the real-money caps. Every value is clamped to a range that
cannot wedge the bot, unknown keys are reported rather than applied, and
combinations that would make trading impossible (an order cap below the
minimum order, a trade window that closes before it opens) come back as a
warning on save.

Because running a paper bot and a real-money bot out of one directory is
normal here, the file is scoped: `all`, `paper` and `live`, with a bot applying
`all` then its own section. The **Halt orders** button creates and removes
`data/KILL`, which stops every order without touching open positions.

Bankroll, assets and spot exchanges stay start-time arguments -- change them in
the header and press Restart. Nothing on this page can arm real money:
`--live --armed` remains a command-line act.

### Saved markets

`data/charts/` holds one JSON per closed window -- the whole 200 ms sample
path, the strike, our fills and how the venue graded it -- next to the PNG the
dashboard rendered at the time. That is ~175 MB after a few days, so the page
never reads it to build a list: `archive.py` summarises each window once and
caches the summary against the file's size and mtime in
`data/charts_index.json`. A rescan then costs one `stat` per file and parses
only genuinely new windows (1.8 s for the first 876, 0.02 s after). Delete the
cache and the next listing rebuilds it.

Filter by asset, outcome, or whether we actually traded it; sort by date, PnL
or the size of the move. The PNG is the thumbnail because it already exists;
opening a window fetches that one JSON and redraws it live, with the fills
marked on the path, which a picture cannot do. `?window=<slug>` links a
particular window. Slugs are matched against the market-slug pattern rather
than sanitised, so no crafted name reaches outside the archive.

The chart is **pushed, not polled**. The page opens a websocket to `/ws` and
the server sends the state as the bot writes it -- measured 9.5 pushes/sec at
~6 KB each, a 3 ms first paint. Polling `/api/live` stays wired as the
fallback: identical payloads, so if the socket will not open or drops, the page
carries on without noticing. The subscription rides on the URL
(`/ws?chart=<slug>`) so the first push is already the right market, and
`{"chart": ...}` switches it without reconnecting.

`web/ws.py` is a small hand-rolled RFC 6455 server rather than a dependency,
because the dashboard runs on `http.server` and must stay on ONE port -- a
second listener would need a second hole in the firewall. It implements the
subset the page needs and *refuses* the rest (continuation frames, unmasked
client frames, frames over 64 KiB) instead of half-supporting them.

Two things were capping the refresh rate before the transport ever mattered:

* `price_history` is keyed by slug and `_reap` never pruned it. Each market's
  deque is capped; the NUMBER of markets was not. 244 markets and 154k points
  had piled into a **41 MB** state file, and writing that takes ~1 s -- so the
  dashboard ran at **1 Hz** no matter what `state_loop`'s interval said. Closed
  windows are archived to `data/charts/`, so pruning loses nothing. Now 0.14 MB.
* The 10 Hz write used `json.dumps`. It is `orjson` now (~8x faster on this
  payload, and already a dependency), with `OPT_NON_STR_KEYS` because
  `epoch_pnl` is keyed by int epoch.

If the chart ever feels sluggish again, **measure the state file first**: its
size and write rate cap everything downstream, and no transport can beat the
rate at which the data is produced.

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
