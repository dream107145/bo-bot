# Multi-asset strategy overhaul: audit, measurements, redesign

Date: 2026-09-17. Data: 292 archived 5-minute windows (BTC, ETH, SOL, XRP, DOGE)
recorded by the live paper bot between 2026-09-16 13:10 and 2026-09-17 13:50 UTC,
plus 83 live paper fills. Every number below was produced by a script in
`scripts/` from files in `data/`; nothing is estimated from memory.

**The one-paragraph version.** The old bot under-counted the venue fee by
1.75-2.8x, graded half of its own fills with the same signal that produced
them, and traded a model whose only real information advantage over the market
is a few thousandths of a Brier point. On 292 real windows a redesigned engine
shows a positive replay PnL, but clustered by 5-minute epoch (every asset in a
window is one bet) it is positive in only 11 of 22 epochs traded and the
out-of-sample half has a t-statistic below 1. There is **no statistically
defensible edge yet**. The redesign therefore makes the bot a measurement
instrument: it discovers every listed asset, prices with the verified cost
curve, sizes tiny, refuses most trades with a reason code, and reports an
epoch-clustered evidence statistic that must clear 2 before live money is even
discussed. Detecting a 3-5 cent edge at that bar needs roughly 400-1,000
independent traded epochs, i.e. weeks of recording, not the 25 hours on disk.

---

## A. Current strategy (as found)

```
discover:   btc/eth/sol/xrp/doge-updown-5m-<epoch> constructed from the clock (assets hard-coded)
price:      Binance bookTicker microprice, single exchange
strike:     60s trailing TWAP of the Binance proxy at the window open
model:      analytic digital option on the Chainlink 60s-TWAP settlement,
            Student-t(4), sigma from TwoScaleVol (fast EWMA x slow/fast ratio, prior 1.4)
edge:       fair' = 0.5*model + 0.5*mid ;  net = fair' - ask - 0.02*min(ask,1-ask)
filter:     window 230..3s left, model outside [0.15,0.85], ask in [0.03,0.97],
            net >= 0.05 and net >= 1.5 x vol-uncertainty band, one position per market
size:       0.25 Kelly, floored so a WIN nets $1 (raises the stake regardless of edge),
            caps $15/market, $45 gross, $30 daily loss
execution:  FOK limit one tick through the ask, paper engine with measured latency
settlement: venue outcomePrices at close+40s, else the bot's own Binance-proxy TWAP
```

## B. Problems, measured

| # | Problem | Measurement |
|---|---|---|
| 1 | **Fee model wrong.** Assumed `0.02 * min(p,1-p)`. The venue returns `feeSchedule {rate 0.07, exponent 1, takerOnly}` and the docs give `fee = shares * 0.07 * p(1-p)` (100 shares at 0.50 = $1.75). | Hurdle under-counted 1.75x at 0.50, 2.8x at 0.80. On the 83 live fills the bot charged $3.51; the real schedule charges $7.95. |
| 2 | **Self-graded outcomes.** When the Gamma row had vanished (it does within ~10 min) settlement fell back to the bot's own Binance TWAP: the signal that produced the trade graded the trade. | 37 of 77 graded live fills (48%). 5 of 287 archived outcomes disagree with the venue's own closing book. The archive loader now grades from the closing book. |
| 3 | **Overconfidence on selected trades (winner's curse).** | Venue-confirmed fills only: n=40, paid 0.732, model 0.845, realised 0.713, net **-$6.69**. The headline "+$12.06" over all 77 fills came from self-graded and 13 lucky SOL/XRP fills. Pre-shrink log: model 0.921 vs realised 0.640. |
| 4 | **One-day directional sample.** | 36 UP fills won 39%; 41 DOWN fills won 98%. Per-epoch PnL across assets correlates 0.3-0.6; 13 of 14 multi-asset epochs traded the same direction. |
| 5 | **Entry timing.** | Fills with 150-300s left: model 0.87, realised 0.33 (n=19, -$52). 60-90s: model 0.76, realised 0.19 (n=7, -$24). |
| 6 | **Vol prior too high for non-BTC.** `TwoScaleVol.default_ratio = 1.4` is a sigma ratio (variance ratio 1.96) applied for the first ~15 min. | Archive variance ratio 60s/1s: BTC 1.34, DOGE 1.30, ETH 0.97, SOL 1.00, XRP 1.05. |
| 7 | **Feed lag not in the pricer.** The pricer received local staleness (~0-200 ms) as `info_lag`, not the measured feed latency (~550 ms). | Small (horizon +0.5 s) but the README claimed otherwise. |
| 8 | **Hard-coded assets, one exchange.** | The venue lists BNB and HYPE 5m markets; HYPE is not on Binance spot at all. |
| 9 | **`$1 win target` sizing** raised the stake when Kelly said small. | Removed. Sizing never increases against the edge. |
| 10 | No reason codes, no per-epoch/correlation caps, no drawdown limit, no evidence statistic. | Built (below). |

**Loss attribution on the 40 venue-confirmed live fills** (-$6.69 net, 268 shares):
PREDICTION/CALIBRATION error dominates: paying 0.732 for outcomes that happened
71.3% of the time is -$5.25 gross before any cost. FEES $1.44 as charged
(true schedule: ~$3.9). SLIPPAGE -$5.06 in the paper engine's own accounting
(favourable: FOK rejections filtered adverse moves). LATENCY: the reaction lag
is ~1.0 s (spot 546 ms + half an 888 ms order round trip); the token price
absorbs a spot move inside the same second (lead/lag below), so latency
converts into missed fills rather than bad fills. BAD_MARKET_SELECTION and
REGIME: the loss sits in 150-300s and 60-90s entries and in UP trades on a
down day. None of these can be separated further with n=40.

## C. New strategy

```
Polymarket Gamma ──► AssetRegistry: probe every candidate slug, keep what exists (7 today)
                     MarketMeta per window with the venue's feeSchedule, liquidity, question
Binance+Bybit+Coinbase ──► CompositeSpot: median, inverse-spread weight, deviation gate
                     ──► TwoScaleVol, TwapState, StrikeTracker (60s TWAP at the open)
                     ──► FeatureEngine (24 causal features, same code live and in replay)
                     ──► p_analytic = TWAP digital pricer, Student-t(4)
                     ──► p_used = 0.5 p_analytic + 0.5 market mid        (the calibration layer)
                     ──► CostModel: fee 0.07 p(1-p), slippage 0.005, uncertainty charge
                     ──► gates -> Reason code or Intent
                     ──► RiskManager: fractional Kelly x confidence, caps per market/asset/EPOCH/total,
                         same-direction-in-epoch scaled by (1 - rho), daily loss, drawdown
                     ──► revalidate against the freshest book, FOK one tick through, never chase
                     ──► settlement from the venue; per-asset PnL; EPOCH-clustered evidence t-stat
```

Why the 50/50 blend and not a fitted model: walk-forward over 193 OOS windows,
every fitted model (logistic on 2 to 24 features, gradient boosting, per-asset,
global-plus-asset-adjustment) had a **worse** Brier than the market price.
Only the parameter-free analytic pricer and its 50/50 blend beat the market,
and once bootstrapped over epochs only the blend's interval excludes zero
(-0.0026 Brier, 95% [-0.0048, -0.0006]). Simpler won, by a hair.

## D. Asset analysis

Profiles from the archive (sigma in bps over the horizon; VR = variance ratio
against iid sqrt(t) scaling; spread and one-sided fraction of the Up book):

| asset | windows | s1s | s60s | s300s | VR60 | kurt300 | spread 60-120s | one-sided 30-60s | market Brier 120-180s |
|---|---|---|---|---|---|---|---|---|---|
| BTC | 70 | 0.93 | 8.3 | 17.9 | 1.34 | 4.2 | 0.011 | 0.68 | 0.144 (OOS) |
| ETH | 70 | 1.46 | 11.2 | 23.1 | 0.97 | 4.6 | 0.011 | 0.62 | 0.129 |
| SOL | 70 | 1.54 | 11.9 | 24.8 | 1.00 | 3.8 | 0.015 | 0.63 | 0.149 |
| XRP | 70 | 1.97 | 15.7 | 32.0 | 1.05 | 4.4 | 0.020 | 0.60 | 0.115 |
| DOGE | 7 | 1.05 | 9.3 | 19.4 | 1.30 | 2.9 | 0.032 | 0.48 | 0.140 |

BNB and HYPE were discovered today and have no archive yet. Liquidity at
discovery (Gamma `liquidityNum`): BTC 14.6k, ETH 8.6k, XRP 4.5k, DOGE 3.0k,
SOL 2.5k, BNB 1.6k, HYPE 1.4k.

Replay of the new engine by asset (all 292 windows, 10 shares per trade,
blend50, threshold 0.10; read with section I in mind):

| asset | fills | win | avg fill | net PnL | fees | PnL/share | PF | max DD |
|---|---|---|---|---|---|---|---|---|
| BTC | 14 | 0.57 | 0.388 | +23.49 | 2.21 | +0.168 | 2.03 | -7.01 |
| ETH | 14 | 0.43 | 0.317 | +13.69 | 1.91 | +0.098 | 1.60 | -7.60 |
| SOL | 9 | 0.78 | 0.427 | +30.14 | 1.46 | +0.335 | 4.38 | -5.87 |
| XRP | 12 | 0.58 | 0.375 | +23.36 | 1.64 | +0.195 | 2.53 | -10.14 |
| DOGE | 1 | 1.00 | 0.460 | +5.23 | 0.17 | +0.523 | - | 0 |

## E. Entry analysis (time remaining)

Market calibration and the replay by bucket:

| left | market Brier | model Brier | blend Brier | one-sided (BTC) | new fills | win | PnL/share |
|---|---|---|---|---|---|---|---|
| 240-300s | 0.238 | 0.240 | 0.238 | 0.00 | 13 | 0.92 | +0.546 |
| 180-240s | 0.202 | 0.198 | 0.200 | 0.00 | 8 | 0.63 | +0.125 |
| 120-180s | 0.172 | 0.166 | 0.168 | 0.01 | 10 | 0.60 | +0.211 |
| 60-120s | 0.120 | 0.125 | 0.121 | 0.23 | 10 | 0.50 | +0.075 |
| 30-60s | 0.052 | 0.053 | 0.051 | 0.68 | 4 | 0.25 | -0.158 |
| 10-30s | 0.013 | 0.016 | 0.014 | 0.91 | 3 | 0.00 | -0.231 |
| 0-10s | 0.003 | 0.004 | 0.002 | 0.98 | 2 | 0.00 | -0.021 |

Seven of the thirteen 240-300s fills are two epochs (1789581600: four assets
UP; 1789590900: four assets DOWN). The final minute is untradeable for a
taker (books one-sided) and lost in the replay. **The engine trades 295s to
60s left only.** An earlier hypothesis, that the crowd prices the window
against the opening spot instead of the TWAP strike, was tested and refuted:
the market price correlates 0.89 with the TWAP model and 0.38 with a
spot-strike model in the first 15 seconds.

## F. Regime analysis (new engine, all windows)

| regime | fills | win | PnL/share | note |
|---|---|---|---|---|
| HIGH_VOLATILITY (rv30/sigma >= 1.5) | 8 | 0.88 | +0.461 | |
| NORMAL_VOLATILITY | 34 | 0.59 | +0.188 | |
| LOW_VOLATILITY (<= 0.6) | 8 | 0.25 | -0.063 | the only negative cell; n=8 |
| SIDEWAYS (|ret120| < 1.5 sigma) | 50 | 0.58 | +0.192 | no fill ever fired in a strong trend |
| WIDE_SPREAD (>= 3c) | 2 | 0.50 | +0.306 | |
| LIQUIDITY_COLLAPSE (one-sided) | 0 | - | - | excluded by gate |
| EXTREME_MOVE (|ret30| >= 3 sigma) | 0 of 3 sent | - | - | excluded by gate |

LOW_VOLATILITY is not excluded by default: eight fills is not evidence.
`EngineConfig.excluded_vol` exists for when it is.

## G. Edge analysis

Per-fill rows (`data/research/new_trades.csv`): predicted probability,
market probability, ask seen, fill, fee, net edge, outcome, PnL. First rows:

```
slug                       left side  p_model p_market ask  fill  fee     net_edge won  pnl
btc-updown-5m-1789565700    43   UP   0.446   0.305   0.31 0.31  0.0150  +0.121   0   -3.25
eth-updown-5m-1789570500   278   DOWN 0.627   0.495   0.50 0.50  0.0175  +0.110   0   -5.18
xrp-updown-5m-1789575900   123   DOWN 0.599   0.415   0.42 0.42  0.0171  +0.162   1   +5.63
btc-updown-5m-1789581600   290   UP   0.460   0.275   0.28 0.27  0.0138  +0.166   1   +7.16
```

Does disagreement with the market carry information? OOS rows, analytic model:

| model minus market | rows | windows | realised minus market |
|---|---|---|---|
| below -0.10 | 518 | 95 | -0.029 |
| -0.10..-0.05 | 1181 | 142 | -0.051 |
| -0.05..-0.02 | 1654 | 180 | -0.063 |
| -0.02..+0.02 | 5155 | 193 | +0.012 |
| +0.02..+0.05 | 1449 | 173 | +0.108 |
| +0.05..+0.10 | 1013 | 147 | +0.092 |
| above +0.10 | 417 | 89 | +0.192 |

The sign is right in every bucket, which is why the model beats the market on
Brier. Rows are heavily correlated within windows and epochs; the clustered
tests below are the ones to trust.

Lead/lag: corr(spot 1s return, Up price change) is 0.29 in the same second,
0.10 the next second, and does not accumulate after that. Given |model -
market| > 5c, the market moves toward the model by 0.5c after 1s, 1.4c after
5s and 1.7c after 10s. With a ~1 s reaction lag the fast part is gone before
an order lands; the slow part is smaller than fee plus spread.

## H. Backtest: OLD vs NEW on the same 292 windows

10 shares per trade, fills 1 s after the decision against the recorded touch,
FOK one tick through, real 7% fee curve charged to both:

| | OLD (as shipped) | NEW (blend50, thr 0.10) |
|---|---|---|
| Orders sent | 50 | 83 |
| Fills | 33 | 50 |
| Win rate | 0.79 | 0.58 |
| Gross PnL | +35.90 | +103.30 |
| Fees | 4.79 | 7.39 |
| Slippage | -4.70 (favourable) | -3.80 (favourable) |
| Net PnL | +31.11 | +95.91 |
| Profit factor | 1.68 | 2.37 |
| Max drawdown | -15.46 | -21.03 |
| Average trade | +0.94 | +1.92 |
| Average claimed edge | +0.109 (model - paid) | +0.126 net |
| **Epochs traded** | **16** | **22** |
| **Per-epoch mean (se)** | **+1.94 (1.88), t = 1.03** | **+4.36 (2.29), t = 1.90** |
| **Epochs positive** | **11 / 16** | **11 / 22** |

Note: the slippage sign convention is fill minus seen ask, so a negative
number is a better fill; fill-or-kill rejected the adverse moves (17 for OLD,
33 for NEW). Neither row is evidence of profitability. Both are consistent with zero.

## I. In-sample, validation, out-of-sample

* **Model comparison** (`scripts/research_models.py`): windows ordered by
  open time; the first 34% (99 windows) is the initial training block; the
  remaining 193 windows are predicted in 5 walk-forward blocks, each by a model
  fitted only on earlier windows. OOS Brier: market 0.1347, analytic 0.1316,
  blend50 0.1321, Platt-market 0.1402, blend-logistic 0.1374, full logistic
  0.1452, gradient boosting 0.1456, per-asset 0.1394, hybrid 0.1381.
  Epoch-bootstrapped difference to the market: analytic -0.0031 [-0.0071,
  +0.0010]; blend50 -0.0026 [-0.0048, -0.0006]; GBM +0.0106 [+0.0005, +0.0208].
* **Replay** (`scripts/research_backtest.py`): the analytic model and the blend
  have no fitted parameters, so every window is out of sample for them. The
  only chosen quantity is the edge threshold. It was chosen on the first 146
  windows (validation: blend50 at 0.10, +$61 on 25 fills) and applied to the
  last 146 (test: +$34.72 on 25 fills, pnl/share +0.139 with se 0.094;
  per-epoch t = 0.94, 4 of 11 epochs positive).
* **Robustness on the test half**: pnl/share stays positive across thresholds
  0.02-0.05 and models (0.013 to 0.091, se ~0.045), across cross width 0-2
  ticks and landing delay 1-3 s (+0.11 to +0.16), and flips negative only
  when trading is restricted to the last 120 s (-0.059). Nothing reaches
  2 standard errors.
* **What n would be needed**: a per-share edge of 0.05 at a 0.4-0.6 fill
  price needs ~384 independent traded epochs for 2 sigma; 0.03 needs ~1,070;
  0.02 needs ~2,400. The archive has 71 epochs. At ~30% of epochs traded that
  is 20 bets per day: **three to seven weeks of continuous recording** before
  the question can be answered either way.

## J. When the strategy must NOT trade (reason codes)

| code | rule |
|---|---|
| OUTSIDE_TIME_WINDOW | fewer than 60 s or more than 295 s left (books go one-sided in the last minute; 60-98% of samples) |
| CANNOT_LAND_IN_TIME | seconds left < measured round trip + 500 ms |
| STALE_DATA | composite spot older than 1.5 s or book older than 2.5 s (both widened live to 2.5x the measured p95) |
| DATA_INCONSISTENT | exchanges disagree by more than 15 bps, or fewer sources than required |
| NO_STRIKE / MODEL_NOT_READY | window open missed, TWAP window not fully covered, vol not warmed |
| MODEL_SANITY | analytic fair differs from the mid by more than 6c on average over 200 looks |
| BAD_REGIME | book one-sided (LIQUIDITY_COLLAPSE), or |30 s return| >= 3 sigma (EXTREME_MOVE) |
| NO_OFFER / PRICE_BAND | no ask, or ask outside 0.05-0.95 |
| LOW_LIQUIDITY | spread wider than 3c, or fewer than 20 shares within one tick of the ask |
| EDGE_TOO_SMALL | p_used - ask - fee - 0.005 - uncertainty < 0.05 |
| LOW_CONFIDENCE | edge inside the model's own 25%-sigma error band |
| HIGH_SLIPPAGE | the ask moved past the limit between evaluation and send (never chase) |
| ALREADY_POSITIONED / ORDER_IN_FLIGHT | one position per market |
| RISK_LIMIT | per-market 10%, per-asset 20%, per-epoch 25%, total 40% of the account, 4 concurrent, daily loss 20%, drawdown 30% |
| SIZE_ZERO | Kelly x confidence stake under $2 or under the venue's 5-share minimum |

And the meta-rule: **no real money while the evidence statistic (per-epoch
mean / se) is below 2 or the epoch count is below a few hundred.**

## K. Configuration (`src/troll_poly_bot/config.py`)

```
EngineConfig   trade_window 295 -> 60 s | latency_safety_margin 500 ms
               max_spot_age 1500 ms | max_book_age 2500 ms | max_source_deviation 15 bps
               market_blend 0.5 | sanity_max_bias 0.06 over 200 samples
               min_net_edge 0.02 | expected_slippage 0.005 | uncertainty_charge 0.0 | confidence gate 0.5 bands
               price band 0.05-0.95 | max_spread 0.05 | min_depth 10 shares | cross 1 tick
               order flow: prior slope 0, live-calibrated per asset, needs n >= 1000 and |t| >= 2, cap 3 bps
               excluded: LIQUIDITY_COLLAPSE, EXTREME_MOVE | asset_overrides {}
RiskConfig     kelly 0.25 x confidence | position $10 | asset $20 | epoch $30 | total $50
               concurrent 7 | daily loss $20 | drawdown $30 | min order $2 | epoch_correlation 0.5
               (all dollar caps scale with --balance / 100)
FeedConfig     exchanges binance, bybit, coinbase | 28 candidate assets, re-probed every 30 min
               quote max age 3 s | estimator update interval 100 ms
VolConfig      fast EWMA 1 s grid, 120 s half-life | slow 30 s grid, 3600 s | default ratio 1.15
Fees           read from each market row; verified 0.07 * p(1-p), taker only, 20% maker rebate
```

The gates were loosened on 2026-09-18 to trade more often; section M has the
replay behind the change. Neither setting is a demonstrated optimum.

## L. Code changes

| file | change |
|---|---|
| `market/discovery.py` (new) | probe 28 candidate assets against Gamma, keep what is listed, re-probe |
| `feeds/markets.py` | `MarketMeta` carries the fee schedule, question, liquidity, volume, TWAP lookback; any slug asset accepted |
| `feeds/spot.py` (new) | Binance + Bybit + Coinbase adapters, pure parsers, basis-adjusted composite (per-exchange level offsets, warm-up) with deviation gate |
| `signals/costs.py` (new) | verified fee curve, cost breakdown (fee, half-spread, slippage, uncertainty charge) |
| `signals/regime.py` (new) | vol / trend / liquidity / extreme labels in units of the asset's own sigma |
| `features/engine.py` (new) | 24 causal, time-based features; one code path for live and replay |
| `models/logistic.py`, `models/calibration.py` (new) | numpy logistic, Platt, isotonic, Brier/log-loss/ECE/reliability buckets |
| `strategy/engine.py` (new) | the decision engine with `Reason` codes, revalidation, snapshot |
| `risk/limits.py` (new) | hard caps incl. per-epoch correlated exposure, fractional Kelly x confidence, halts |
| `execution/paper.py` | `FeeModel` charges the venue schedule, per token |
| `pricing/vol.py` | EWMA seeded from a 30-sample mean, not one return (a start-up glitch read as 6-9 bps/s for minutes) |
| `backtest/archive.py`, `backtest/dataset.py`, `backtest/replay.py` (new) | bell-graded loader, 1 s grid, decision dataset, execution-aware replay |
| `analytics/scorecard.py` (new) | scorecards by asset / time / regime / edge / price |
| `live.py` | rewired to discovery, composite spot, engine, risk; archives now store per-exchange prices and depth; evidence statistic |
| `config.py`, `__main__.py` | new config groups; `--assets` restricts, default is everything listed |
| `web/static/*` | decision columns, reason labels, evidence and per-asset PnL |
| `scripts/research_*.py` (new) | every table in this document |
| `tests/test_{costs,models,features,replay,risk,discovery,spot,engine}.py` (new) | 43 new tests; 178 total pass |
| `strategy/taker.py`, `sim.py`, `scripts/replay.py` | kept as the OLD strategy for comparison and the synthetic sim |

## Honest limits

* 25 hours of data from one day of one market regime, five assets, 71 epochs.
* Outcomes graded from the venue's closing book, not from the Chainlink stream
  itself (no public API); the Binance-proxy TWAP disagreed with the recorded
  outcome in ~1% of windows.
* Archives before the sampler fix stop 30-90 s before the close (12 per asset).
* The paper engine fills at the recorded touch with an assumed 500-share
  depth; newer archives record depth within two ticks so this can be tightened.
* The live bot cannot yet run two instances against the same state file; the
  smoke run of the new code used private paths while the previous instance
  kept running.


---

## M. Changes after the report (2026-09-18)

**Errors getting live markets, found in the log and fixed.** A DNS outage
(00:14 to 03:08 UTC) made every feed reconnect every two seconds, about
5,000 warnings. A restart during the tail of the outage probed the venue,
reached only BTC and ETH, and made that the asset list for the next half
hour: "could not ask" had been treated as "not listed". Settlement gave up
after one failed fetch. Now: a failed fetch raises `FetchFailed` and the
registry keeps the asset and re-probes every minute (`market/discovery.py`);
reconnects back off 2 s to 60 s and report health per feed (`feeds` in the
state, "FEEDS DOWN" in the status line); settlement retries five times over
about three minutes before falling back to the proxy; markets whose open was
missed are labelled "missed open" and reaped at close.

**Strategy D, order flow, implemented.** Every exchange adapter now emits
top-of-book sizes and each trade with its aggressor side (`feeds/spot.py`);
`features/orderflow.py` turns them into trade-flow imbalance over 5/15/30 s
and a book imbalance, combined into `ofi_score` in [-1, 1]. The score enters
the pricer as an expected drift of the settling mean (`drift_log` in
`pricing/twap.py`), which is the right shape: a 1 bp drift moves P(up) by
about 2c at the money and nothing in the tails. **The drift is measured, not
assumed.** A live regression of the realised 10 s and 30 s composite return
on the score runs per asset; the tilt is zero until at least 1,000 samples
exist and the overlap-corrected |t| clears 2, and then the measured slope is
used with its measured sign. The first 75 s of live tape already argued for
that caution: on every asset, recent aggressive buying predicted a *negative*
next-10s return (BTC slope -1.1 bps, XRP -1.8, SOL 30 s -3.2), the transient
impact reverting rather than continuing. The archive holds no trade flow, so
none of this can be replayed; it earns its weight on the live tape and the
dashboard shows the running slope, correlation and t per asset.

**Trade frequency.** Replayed on 399 windows with the engine's gates mirrored
(10 shares per fill, real fee curve, fills 1 s after the decision):

| gates | fills | win | PnL/share (se) | net | epochs | positive | t |
|---|---|---|---|---|---|---|---|
| original: net edge 0.05, uncertainty charge 1.0, gate 1.0, spread 0.03 | 74 | 0.50 | +0.148 (0.053) | +$110 | 32 | 14 | 1.95 |
| same, test half | 23 | 0.43 | +0.182 (0.091) | +$42 | 12 | 5 | 1.22 |
| **adopted: 0.02, charge 0.0, gate 0.5, spread 0.05, depth 10** | 332 | 0.52 | +0.045 (0.025) | +$148 | 89 | 51 | 1.32 |
| same, test half | 159 | 0.47 | +0.041 (0.036) | +$65 | 41 | 21 | 0.79 |

Four and a half times the fills at roughly a third of the per-share estimate;
neither setting is statistically distinguishable from zero. The uncertainty
charge was removed because it cut fills without raising EV; the LOW_CONFIDENCE
gate stays at half a band. Risk caps rose with the asset count: 7 concurrent
positions (one per listed asset), 30% of the account per epoch, 50% total.
