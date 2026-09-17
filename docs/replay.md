# Replay: the strategy on real recorded data, with network delay

`python scripts/replay.py`

This is the test the synthetic simulator cannot be. The token prices are the
actual Polymarket mids the live bot recorded (`data/charts/*.json`), the
outcomes are the venue's own resolutions, and the delay is this machine's
**measured** latency (spot p50 603 ms, book p50 494 ms, order round trip 978 ms)
run through the same three-clock engine the live bot uses: the strategy sees a
book `md_book` ms stale, its order lands `submit` ms later and is matched
against the book as it is *then*, and it learns the result `ack` ms after that.

The pricer is fed the spot as it **arrives**, not as it truly is. Feeding it the
true price at the true time would let the model see data it never had — a
flattering bug, and the opposite of what the test is for.

## The verdict, on 28 windows

Same strategy, same real prices, only the network differs:

| profile | reaction lag | pnl | filled | paid | model | realised |
|---|---|---|---|---|---|---|
| instant | 0 ms | **−9.35** | 10 / 10 | 0.796 | 0.861 | 0.692 |
| colocated | 5 ms | −16.16 | 6 / 10 | 0.788 | 0.858 | 0.472 |
| nearby VPS | 53 ms | −16.16 | 6 / 10 | 0.788 | 0.858 | 0.472 |
| **this machine** | 1090 ms | −7.21 | 6 / 9 | 0.773 | 0.859 | 0.640 |
| this machine ×1.5 | 1635 ms | −15.18 | 7 / 14 | 0.777 | 0.866 | 0.531 |

**The strategy loses on real prices even at zero latency.** The defect is the
one that has appeared in every test this project has run: the model claims
0.861, reality is 0.692, and it paid 0.796 — it buys outcomes at 80¢ that
happen 69% of the time. That is the winner's curse measured cleanly on real
data at zero delay, which isolates it from latency entirely. The
`market_shrink=0.5` correction is on and is not correcting enough of it.

The delay rows do not form a curve. Measured delay lost *less* than instant
because FOK rejections happened to filter out bad trades. With 6–10 fills per
row, the differences between rows are one or two trades flipping. Do not read
a latency→PnL relationship from this table.

### A correction

An earlier run of this harness on 20 windows showed **+$8.02 on 4 fills, 4/4
won**, and was reported as "edge at zero latency". Eight more windows turned it
into −$9.35 on 10 fills. Four fills were never evidence of anything; the rule
that n was too small had been stated all session and was not applied to a
number that looked good. It is recorded here so it is not repeated.

## Cross width: refuted

Hypothesis: with ~1 s of delay the book often moves a tick during flight, so a
1-tick fill-or-kill cross gets rejected; crossing wider would recover the
fills the delay was killing.

| cross | pnl | fill rate | FOK rejected |
|---|---|---|---|
| 1 tick | −7.21 | 66.7% | 3 |
| 2 ticks | −16.21 | 75.0% | 2 |
| 3 ticks | −16.21 | 75.0% | 2 |
| 4 ticks | −14.00 | 88.9% | 1 |

Wider crosses fill more and **lose more**. Because the underlying trades are
negative EV, the FOK rejections were *protecting* the account. Filling more of
a bad selection at a worse price only realises more of the loss. The parameter
exists (`cross_ticks`) and stays at 1.

## Conservatism: the shape of the loss, not a setting

| shrink / min edge | profile | pnl | fills | model | realised |
|---|---|---|---|---|---|
| 0.50 / 0.05 | instant | −9.35 | 10 | 0.861 | 0.692 |
| 0.50 / 0.05 | measured | −7.21 | 6 | 0.859 | 0.640 |
| 0.65 / 0.05 | measured | +2.20 | 1 | 0.806 | 1.000 |
| 0.50 / 0.08 | instant | +6.15 | 2 | 0.787 | 1.000 |
| 0.50 / 0.08 | measured | +6.91 | 2 | 0.795 | 1.000 |
| 0.65 / 0.08, 0.80 / 0.05 | both | 0.00 | 0 | — | — |

Raising the bar collapses fills from 10 → 2 → 1 → 0, and the sign flips
positive on the last one or two trades that happened to win. Two fills going
2/2 is a coin flip landing twice. What this shows is where the loss *comes
from* — the ~10 overconfident trades at the default — and that tightening
removes them. It does not show that `min_edge = 0.08` is a good setting, and it
is not adopted. Picking the row that came out positive is exactly how a bot is
tuned to look profitable on the sample it was tuned on.

## Sizing worked; selection did not

The `$1` win-target sizing did what it says: the winning window netted
**+$1.16 per fill**. The losing window lost **−$10.69**. Sizing scales the
outcome of the selection; it cannot fix the selection. No sizing rule creates
edge.

## The final minute: refuted at the execution layer

This was the one structurally-motivated candidate left. The TWAP settlement
collapses uncertainty 108× at 10 s out, so a naive market would leave a large
mispricing in the last minute. It could not be replayed, because every archive
stopped 30–90 s before close. Finding out *why* answered the question.

Every archived window ended on the same last sample — `up 0.985 / down 0.015`,
the canonical decided-market book — and then went silent. Probing the venue's
**raw REST book** through a live window's final two minutes:

```
 86s left | UP bids=2  best 0.02   asks=97  best 0.03 | DOWN bids=97  best 0.97  asks=2  best 0.98
 76s left | UP bids=0  best None   asks=99  best 0.01 | DOWN bids=99  best 0.99  asks=0  best None
 ... identical through the bell and 20 s past it ...
```

The books are not empty. **They are one-sided in a complementary way: the
winner has ~100 levels of bids up to 0.99 and no asks; the loser has ~100
levels of asks from 0.01 and no bids.** Nobody will sell the winner. Nobody
will buy the loser. That is why `mid` was `None` for both tokens — and why a
fix that recovered one token's price from the other had nothing to recover
from. The sampler now uses the side that *is* quoted (the loser's best ask,
the winner's best bid; they sum to 1.00) and records through the bell.

The strategic content matters more than the chart. **There is nothing for a
taker to buy in the final minute of a decided window.** The market prices the
uncertainty collapse faster than a taker can act — this window was 0.97/0.98
with two thin levels at 86 s out — and then withdraws the offer entirely. The
residual before withdrawal is ~2¢ on a 98¢ token, which is the "risk $49 to
win $1" regime the price band excludes on purpose. Windows that are *not*
decided a minute out are coin flips, where the model has no edge by
construction.

Observed on every window seen so far (nine archived, one probed live at the
raw book). More windows would firm up "every" into a rate, but the mechanism
is not in doubt: it was read directly off the venue.

## Honest limits of the data

- **n is small** — 28 windows, 6–10 fills per configuration. This validates
  the machinery and shows what delay and cross width cost on real prices. It
  is not a profitability estimate in either direction.
- **Archives recorded before the sampler fix stop 30–90 s before close.** The
  first fix (`_recover_mids`) did not work live, for the reason above; the
  second (`_token_price`) is verified by unit tests against the exact book the
  venue served and **confirmed live** — archives now reach 0.0 s. From that
  change on, each point also records the touch itself (`ub/ua/db/da`, each
  `None` when absent), so a decided window replays as the one-sided book the
  venue actually served and a taker cannot buy a winner nobody was offering.
  Archives *without* the touch rebuild as a 1-tick two-sided book, which is
  faithful until a window is decided and flattering after: a replay could
  "buy" a winner at 0.99. The price band (0.03–0.97) blocks that trade
  anyway, so the verdict is unaffected — but on those archives it is blocked
  by the band rather than by the missing ask, which is the venue's reason.
- **Archives hold mids, not depth.** Books are rebuilt as a 1-tick market
  around the mid (the live books were 1 tick wide) with an assumed 500 shares
  resting — far more than our ≤20-share orders ask for, so contention is not
  the binding constraint here.
- The `TwoScaleVol` slow estimator needs ~15 minutes to warm; in a replay it
  uses its default ratio, as a freshly started live bot would.

## What the harness fixed in itself

Two artefacts were found by reading the first run rather than trusting it:

1. Three FOK rejections at *instant* delay — impossible at true zero latency.
   The loop stepped the engine before submitting, so a 0 ms order was not
   matched until the next recorded sample ~500 ms on, silently adding half a
   second to every profile. Fixed with a post-submit step.
2. `spot_too_stale` at 8% — the live bot widens its staleness gates to 2.5× the
   measured p95; the replay had left them at 800 ms. Fixed by probing the
   latency model the same way the live bot samples the wire.

Both made the replay harsher than reality, so fixing them was the honest
direction.

## Conclusion

This taker strategy has no demonstrated edge on these markets at any latency
this machine can reach, and the one hypothesis that survived the replay is
unexecutable at the venue. That is the end of this strategy family, not a
reason to tune it further.
