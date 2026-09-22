# 15-minute windows: what a month of history says

Study: `scripts/fetch_15m_history.py` + `scripts/research_15m.py`, run 2026-09-22.
Data: 2026-08-22 .. 2026-09-22, **10,560 windows** (every window for BTC and
ETH, every third for SOL, XRP, DOGE, BNB, HYPE), 210,737 CLOB price points of
the UP token at 1-minute fidelity, and 1-minute Coinbase candles for all seven
assets. Outcomes are the venue's own. Report: `data/research/15m/report.json`.

## The method, and its handicap

At every in-window market observation the bot's **own pricer** (Student-t(4),
TWAP variance, `pricing/`) is handed the last *completed* 1-minute candle and
scored against the market mid and the outcome. That spot is 0..59 s older than
what the market has already priced, so every comparison here is biased
**against** the model; the live bot's spot is ~1 s old. The first version of
the study made the opposite mistake, giving the model spot up to 90 s newer
than the book, and reported +0.2/share at t = 15 -- a leak, not an edge. Treat
every number below as the order of a parameter, not its third decimal.

## 1. Volatility: sqrt(t) carries from 5m to 15m

| asset | σ 1m (bps) | VR(5) | VR(15) | VR15/VR5 | kurt 15m | t-df 15m | \|r15\| p50/p90/p99 bps |
|---|---|---|---|---|---|---|---|
| BTC | 5.2 | 1.01 | 0.98 | 0.97 | 13 | 2.8 | 9 / 29 / 69 |
| ETH | 7.0 | 0.99 | 0.98 | 0.99 | 24 | 2.8 | 12 / 38 / 94 |
| SOL | 8.9 | 0.99 | 0.95 | 0.96 | 8 | 3.1 | 16 / 50 / 116 |
| XRP | 10.7 | 0.98 | 0.92 | 0.93 | 21 | 2.9 | 18 / 59 / 135 |
| DOGE | 10.5 | 1.00 | 0.98 | 0.98 | 14 | 2.9 | 18 / 59 / 138 |
| BNB | 6.2 | 1.00 | 1.01 | 1.00 | 4.5 | 4.0 | 13 / 38 / 76 |
| HYPE | 10.7 | 0.98 | 0.93 | 0.95 | 3.8 | 4.1 | 21 / 63 / 129 |

VR(h) = var(r_h) / (h · var(r_1)); 1.0 means returns scale as sqrt(t). The
additional scaling from a 5-minute to a 15-minute horizon is 0.93..1.00, so
**the vol estimator is unchanged**. Tails at 15m fit a Student-t with df ≈ 3
on five assets, but the pricer's Brier score with t(3), t(4) and Gaussian
differ in the fourth decimal (0.0362 / 0.0362 / 0.0371 on |z| > 1.5), so
**the pricer is unchanged** too.

Half of a window's final move is realised by minute 6; 75% by minute 10; 96%
by minute 14.

## 2. The market: when the book goes one-sided

| seconds left | n | mean \|mid − 0.5\| | saturated (<0.05 or >0.95) | Brier (market) |
|---|---|---|---|---|
| 720–900 | 31,626 | 0.10 | 0.0% | 0.232 |
| 540–720 | 31,592 | 0.19 | 0.8% | 0.194 |
| 360–540 | 31,609 | 0.25 | 7.8% | 0.157 |
| 180–360 | 31,616 | 0.32 | 27.5% | 0.122 |
| 60–180 | 21,080 | 0.39 | **53.3%** | 0.083 |
| 0–60 | 10,530 | 0.44 | **77.8%** | 0.037 |

The first in-window mid is 0.499 (base rate up 0.493). Over half the books
are already outside the tradeable band with two to three minutes left, so
**`trade_window_end_s` moves from 60 to 120** at 15m.

Volume per window, p10 / p50 / p90 USDC: BTC 14.7k / 25.1k / 46.6k · ETH 2.0k
/ 3.9k / 7.6k · SOL 0.6k / 1.3k / 2.6k · XRP 0.4k / 0.9k / 1.8k · DOGE 0.14k /
0.37k / 0.88k · BNB 0.10k / 0.33k / 0.80k · HYPE **0.03k** / 0.22k / 0.52k.
The depth gates (`min_depth_shares`, `max_depth_fraction`) are what make the
thin five tradeable at all; per-asset PnL in the paper rule did not single
any of them out as worse than BTC/ETH.

## 3. Model vs market: the pricer transfers, the blend still wins

Brier score by seconds left (lower is better; 0.25 is a coin):

| left | market | model | blend 0.3 | blend 0.5 | blend 0.7 | model − mid |
|---|---|---|---|---|---|---|
| 720–900 | 0.2315 | 0.2338 | 0.2323 | 0.2317 | **0.2313** | +0.001 |
| 540–720 | 0.1941 | 0.1965 | 0.1946 | 0.1940 | **0.1937** | +0.000 |
| 360–540 | 0.1574 | 0.1581 | 0.1565 | **0.1561** | 0.1562 | −0.001 |
| 180–360 | 0.1213 | 0.1211 | 0.1195 | **0.1192** | 0.1195 | −0.000 |
| 60–180 | 0.0831 | 0.0837 | 0.0814 | **0.0808** | 0.0811 | +0.000 |

With a 0..59 s spot handicap the model alone matches the market and shows
**zero mean bias** at every bucket; the blend beats both at every bucket
below 720 s, exactly the 5m finding.

## 4. The paper rule after costs

Buy the side the blend prices at least `e` above ask + fee (spread 0.02, one
tick through, 7% · p(1−p) taker fee), first qualifying observation per
window, hold to settlement. Validation = first 15 days, test = last 16.
PnL per share, t clustered by 900 s epoch.

| blend | e | valid n | valid pnl | t | test n | test pnl | t |
|---|---|---|---|---|---|---|---|
| 0.5 | 0.02 *(5m value)* | 2,777 | −0.003 | −0.2 | 2,796 | **−0.022** | −2.2 |
| 0.5 | 0.03 | 2,130 | +0.017 | 1.4 | 2,086 | −0.010 | −0.9 |
| 0.5 | 0.05 | 1,254 | +0.042 | 2.4 | 1,140 | −0.006 | −0.4 |
| 0.7 | 0.02 | 1,085 | +0.046 | 2.4 | 978 | −0.010 | −0.6 |
| 0.7 | 0.03 | 736 | +0.096 | 4.0 | 579 | −0.004 | −0.2 |
| **0.7** | **0.05** | 438 | +0.211 | 6.1 | 240 | **+0.079** | **2.4** |
| 0.7 | 0.08 | 257 | +0.252 | 5.5 | 86 | +0.059 | 1.1 |

The 5m configuration (0.5 / 0.02) **loses money at 15m** in the test half.
Blend 0.7 with a required edge of 0.05 is the only cell positive in both
halves. And the edge is latency-sensitive in the right direction -- PnL by
how stale the model's spot was at entry (the live bot sits at the left):

| rule | lag 0–10 s | 10–20 s | 20–40 s | 40–60 s |
|---|---|---|---|---|
| 0.5 / 0.02 | +0.022 (n 769) | −0.008 (4,797) | +0.016 (1,754) | −0.015 (38) |
| 0.7 / 0.03 | **+0.207** (217, t 5.1) | +0.059 (1,040) | +0.107 (442) | −0.092 (11) |
| 0.7 / 0.05 | **+0.269** (159, t 5.8) | +0.175 (555) | +0.202 (232) | −0.023 (3) |

So **`market_blend` 0.5 → 0.7 and `min_net_edge` 0.02 → 0.05** at 15m.
Expect far fewer trades than at 5m: ~240 entries in 16 days across seven
assets in the test half, before the depth and regime gates.

## 5. Exits: take-profit is a loser at 15m, measured

From the 5,573 entries of the as-is rule, the best bid seen afterwards was
+0.14 above entry at the median and +0.52 at p90; **89% of eventual winners
touched +0.05** on the way to 1.00. Rule variants (second fee charged; when
both a stop and a target are hit the stop is assumed first):

| exit rule | pnl/share | t |
|---|---|---|
| hold to settlement | −0.012 | −1.7 |
| stop −0.10 | +0.027 | 5.5 |
| stop −0.20 *(5m value)* | +0.017 | 2.8 |
| **take-profit +0.05** *(5m value)* | **−0.096** | **−21.6** |
| take-profit +0.08 | −0.089 | −19.0 |
| take-profit +0.12 | −0.075 | −15.6 |

The 5m note said "measure it before believing it". Measured: a +0.05 target
gives away ~0.08/share at 15m. **`take_profit_enabled` → off.** The stop-loss
stays at 0.20: a tighter stop looked better in-sample, but the mechanism --
entries that move against us fast are the ones where the study's stale spot
was wrong, not the market -- is exactly the artefact a live 1 s spot removes.

## 6. What changes, what does not

Applied by `DURATION_PROFILES[15]` in `config.py` when the primary window is 15m:

| parameter | 5m | 15m | why |
|---|---|---|---|
| `engine.market_blend` | 0.5 | **0.7** | §3, §4 |
| `engine.min_net_edge` | 0.02 | **0.05** | §4 |
| `engine.trade_window_end_s` | 60 | **120** | §2 |
| `engine.take_profit_enabled` | on | **off** | §5 |
| `engine.take_profit_min_secs_left` | 10 | **120** | §2 |
| `engine.max_book_age_ms` | 2500 | **5000** | live run: quiet ≠ stale |
| `risk.max_entries_per_market` | 1 | **3** | scale in, same side, inside the cap (judgment) |
| `risk.reentry_cooldown_s` | 30 | **45** | not the same print twice (judgment) |

Unchanged on evidence: the vol estimator and its prior ratio (§1), the
pricer's tails (§1), `stop_loss_delta` (§5), Kelly fraction and every loss
limit. `trade_window_start_s` keeps its "5 s after the open" meaning (895 of
900). The per-epoch cap buckets at 900 s (`apply_durations`).

## 7. What this study cannot see

- Spread and depth at 15m. 0.02 is assumed; `prices-history` carries no book.
  The live `HIGH_SLIPPAGE` / `LOW_LIQUIDITY` counters are the check.
- The strike is a candle-mean proxy for the Chainlink 60 s TWAP.
- One month, one regime. The validation/test split disagreed at the small
  edges; the parameters chosen are the ones that agreed.
