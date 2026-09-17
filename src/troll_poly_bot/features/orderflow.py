"""Order-flow imbalance from the spot exchanges (Strategy D).

Two components, both bounded in [-1, 1]:

* **Trade-flow imbalance** ``tfi_<w>s``: (taker buy notional - taker sell
  notional) / total, over trailing windows of 5, 15 and 30 s, summed across
  exchanges. Who is hitting the book, and how hard.
* **Book imbalance** ``obi``: (bid size - ask size) / (bid + ask) at the top
  of book, summed across the fresh sources. Who is waiting.

``ofi_score`` averages the 15 s trade flow and the book imbalance. It is the
input to a *drift* term in the pricer (see strategy/engine.py): a positive
score means the next seconds are expected to drift up by
``ofi_drift_bps * score`` basis points, and the digital pricer moves its mean
by that much. On a 300 s window with an 18 bps standard deviation a 1 bp
drift moves P(up) by about 2c at the money and nothing in the tails, which is
the right shape: flow matters when the outcome is still open.

Calibration, live
-----------------
Nothing here is assumed to work. ``OrderFlowCalibration`` records the score
at t and the realised composite return over the next 10 s and 30 s, and
keeps running correlation and regression slope (bps per unit of score). The
engine can be told to use the measured slope once enough samples exist, and
the dashboard shows the numbers either way. The archive did not record trade
flow, so this cannot be replayed on history yet; it must earn its weight on
the live tape.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

FEATURES: tuple[str, ...] = ("tfi_5s", "tfi_15s", "tfi_30s", "obi", "ofi_score",
                             "trade_rate_30s", "notional_30s")


def _imbalance(buy: float, sell: float) -> float:
    tot = buy + sell
    return (buy - sell) / tot if tot > 0 else 0.0


@dataclass(slots=True)
class OrderFlowState:
    """Rolling trade flow and top-of-book state for one asset."""
    keep_s: float = 60.0
    quote_max_age_ms: float = 3_000.0
    _trades: deque = field(default_factory=deque)           # (ts_ms, signed_notional, notional)
    _book: dict[str, tuple[float, float, float]] = field(default_factory=dict)   # exch -> (ts, bid_sz, ask_sz)

    def on_trade(self, notional: float, aggressor_buy: bool, ts_ms: float) -> None:
        if notional <= 0.0:
            return
        self._trades.append((ts_ms, notional if aggressor_buy else -notional, notional))
        cutoff = ts_ms - self.keep_s * 1000.0
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    def on_quote(self, exchange: str, bid_size: float | None, ask_size: float | None, ts_ms: float) -> None:
        if bid_size is None or ask_size is None or bid_size <= 0 or ask_size <= 0:
            return
        self._book[exchange] = (ts_ms, bid_size, ask_size)

    def trade_flow(self, now_ms: float, window_s: float) -> tuple[float, float, int]:
        """(buy notional, sell notional, count) over the trailing window."""
        cutoff = now_ms - window_s * 1000.0
        buy = sell = 0.0
        n = 0
        for ts, signed, notional in reversed(self._trades):
            if ts < cutoff:
                break
            if signed > 0:
                buy += notional
            else:
                sell += notional
            n += 1
        return buy, sell, n

    def book_imbalance(self, now_ms: float) -> float | None:
        bid = ask = 0.0
        for ts, b, a in self._book.values():
            if now_ms - ts <= self.quote_max_age_ms:
                bid += b
                ask += a
        if bid + ask <= 0:
            return None
        return (bid - ask) / (bid + ask)

    def features(self, now_ms: float) -> dict[str, float]:
        out: dict[str, float] = {}
        for w in (5, 15, 30):
            buy, sell, n = self.trade_flow(now_ms, float(w))
            out[f"tfi_{w}s"] = _imbalance(buy, sell) if n > 0 else math.nan
            if w == 30:
                out["trade_rate_30s"] = n / 30.0
                out["notional_30s"] = buy + sell
        obi = self.book_imbalance(now_ms)
        out["obi"] = math.nan if obi is None else obi
        parts = [v for v in (out["tfi_15s"], out["obi"]) if not math.isnan(v)]
        out["ofi_score"] = sum(parts) / len(parts) if parts else math.nan
        return out


@dataclass
class OrderFlowCalibration:
    """Running regression of realised forward return on the score.

    ``record(score, ts, log_price)`` at decision time; ``resolve(ts, log_price)``
    on every later price update settles the pending samples whose horizon has
    passed. Sums are kept per horizon so slope and correlation come for free.
    """
    horizons_s: tuple[int, ...] = (10, 30)
    max_pending: int = 5000
    #: samples are recorded this often; consecutive samples share most of a
    #: horizon, so the effective sample size is n * interval / horizon and the
    #: reported t uses that, not the raw count
    sample_interval_s: float = 1.0
    _pending: deque = field(default_factory=deque)          # (ts, score, x0)
    _sums: dict[int, list[float]] = field(default_factory=dict)   # h -> [n, Ss, Sr, Sss, Srr, Ssr]

    def record(self, score: float, ts_ms: float, log_price: float) -> None:
        if math.isnan(score):
            return
        self._pending.append((ts_ms, score, log_price))
        while len(self._pending) > self.max_pending:
            self._pending.popleft()

    def resolve(self, now_ms: float, log_price: float) -> None:
        if not self._pending:
            return
        longest = max(self.horizons_s) * 1000.0
        keep: deque = deque()
        for ts, score, x0 in self._pending:
            age = now_ms - ts
            done_all = True
            for h in self.horizons_s:
                key = (h, ts)
                if age >= h * 1000.0:
                    if key not in self._seen_keys():
                        self._add(h, score, (log_price - x0) * 1e4)
                        self._mark(key)
                else:
                    done_all = False
            if not done_all and age < longest:
                keep.append((ts, score, x0))
        self._pending = keep

    # settled (h, ts) pairs so a sample is counted once per horizon; bounded
    _settled: set = field(default_factory=set)

    def _seen_keys(self) -> set:
        return self._settled

    def _mark(self, key) -> None:
        self._settled.add(key)
        if len(self._settled) > 20000:
            self._settled = set(list(self._settled)[-10000:])

    def _add(self, h: int, s: float, r: float) -> None:
        acc = self._sums.setdefault(h, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        acc[0] += 1
        acc[1] += s
        acc[2] += r
        acc[3] += s * s
        acc[4] += r * r
        acc[5] += s * r

    def stats(self, h: int) -> dict:
        acc = self._sums.get(h)
        if not acc or acc[0] < 3:
            return {"n": int(acc[0]) if acc else 0}
        n, ss, sr, sss, srr, ssr = acc
        var_s = sss / n - (ss / n) ** 2
        var_r = srr / n - (sr / n) ** 2
        cov = ssr / n - (ss / n) * (sr / n)
        slope = cov / var_s if var_s > 1e-12 else 0.0
        corr = cov / math.sqrt(var_s * var_r) if var_s > 1e-12 and var_r > 1e-12 else 0.0
        n_eff = max(n * self.sample_interval_s / max(h, self.sample_interval_s), 1.0)
        t = corr * math.sqrt(max(n_eff - 2, 1)) / math.sqrt(max(1 - corr * corr, 1e-9))
        return {"n": int(n), "n_eff": int(n_eff), "slope_bps": round(slope, 4), "corr": round(corr, 4),
                "t": round(t, 2)}

    def slope_bps(self, h: int) -> float | None:
        st = self.stats(h)
        return st.get("slope_bps")

    def t_stat(self, h: int) -> float:
        return self.stats(h).get("t", 0.0)

    def n(self, h: int) -> int:
        return self.stats(h).get("n", 0)

    def report(self) -> dict:
        return {f"{h}s": self.stats(h) for h in self.horizons_s}
