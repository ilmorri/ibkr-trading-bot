#!/usr/bin/env python3
"""Portfolio test of monthly momentum rotation, with and without an index trend filter.

Usage:
    python scripts/rotation_test.py                     # $300, whole shares
    python scripts/rotation_test.py --equity 3000
    python scripts/rotation_test.py --fractional        # hypothetical: IBKR's API refuses fractional orders

How it works
------------
One account, one portfolio, all stocks in data/*_daily.csv except the filter
index. On the last trading day of each month every stock with enough history
is ranked by its momentum: the return from `lookback` days ago to 21 days ago
(the most recent month is skipped, as in the academic definition). The top N
are held in equal dollar slots. Orders go at the next day's open.

  - A stock that drops out of the top N is sold; a new one is bought into the
    free slot. Holdings that stay are not resized, to keep turnover low.
  - Whole shares: if one share costs more than a slot, the next stock down
    the ranking is taken.
  - Trend filter (optional): when the index closes below its 200-day average
    at month end, everything is sold and the account sits in cash.
  - Costs: IBKR tiered commission per order, max($0.35, $0.0035/share), capped
    at 1% of the order value; plus slippage per side. Cash earns nothing.

Variants and pass mark are FIXED BEFORE THE FIRST RUN (2026-10-13). Do not
change them after seeing results. A variant passes if, after costs, it beats
the equal-weight buy-and-hold of the same universe on CAGR and still ends
positive with costs doubled. The rule as a whole passes only if at least 6 of
the 8 variants pass AND the majority pass in both halves of the period.

Biases to keep in mind: the universe is today's listed stocks, chosen today,
so it leaves out companies that went bust (survivorship). The equal-weight
benchmark shares that bias, which is why it is the comparison that counts.
"""
from __future__ import annotations

import argparse
import itertools
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

INDEX = "SPY"
NOT_STOCKS = {"SPY", "QQQ", "IWM", "EFA", "EEM", "DIA", "AGG", "TLT", "IEF", "SHY", "GLD"}
SKIP_RECENT = 21
TREND_N = 200

# Fixed before the first run.
LOOKBACKS = [126, 252]
TOP_NS = [3, 5]
FILTERS = [False, True]


@dataclass
class Costs:
    per_share: float = 0.0035
    minimum: float = 0.35
    max_pct: float = 1.0
    slippage_bps: float = 5.0
    multiplier: float = 1.0

    def order(self, shares: float, price: float) -> float:
        value = abs(shares) * price
        commission = min(max(self.minimum, self.per_share * abs(shares)), value * self.max_pct / 100)
        return (commission + value * self.slippage_bps / 1e4) * self.multiplier


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    opens, closes = {}, {}
    for path in sorted(DATA_DIR.glob("*_daily.csv")):
        symbol = path.name.replace("_daily.csv", "")
        if symbol in NOT_STOCKS and symbol != INDEX:
            continue
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
        opens[symbol], closes[symbol] = df["open"], df["close"]
    opens, closes = pd.DataFrame(opens).sort_index(), pd.DataFrame(closes).sort_index()
    index = closes.pop(INDEX)
    opens.pop(INDEX)
    return opens, closes, index


def month_end_rows(dates: pd.DatetimeIndex) -> list[int]:
    period = dates.to_period("M")
    return [i for i in range(len(dates) - 1) if period[i] != period[i + 1]]


def run(opens, closes, index, start_row, lookback, top_n, use_filter, equity, fractional, costs):
    dates = closes.index
    index_sma = index.rolling(TREND_N).mean()
    rebalance_rows = [r for r in month_end_rows(dates) if r >= start_row]

    cash, holdings = equity, {}  # symbol -> shares
    curve, total_cost, orders = [], 0.0, 0
    rebalance_at = set(rebalance_rows)
    pending = None  # target list decided at a month-end close, executed at the next open

    def value_at(row, prices):
        return cash + sum(sh * prices.iloc[row][s] for s, sh in holdings.items() if not math.isnan(prices.iloc[row][s]))

    for row in range(start_row, len(dates)):
        if pending is not None:
            px = opens.iloc[row]
            target = pending
            pending = None

            top, target = target
            for symbol in [s for s in holdings if s not in top]:
                shares = holdings.pop(symbol)
                price = px[symbol]
                fee = costs.order(shares, price)
                cash += shares * price - fee
                total_cost += fee
                orders += 1

            free_slots = top_n - len(holdings)
            if free_slots > 0:
                slot = value_at(row, opens) / top_n
                for symbol in target:
                    if free_slots == 0:
                        break
                    if symbol in holdings:
                        continue
                    price = px[symbol]
                    budget = min(slot, cash)
                    shares = budget / price * (1 - 0.002) if fractional else math.floor(budget / price)
                    if shares <= 0 or (fractional and shares * price < 1.0):
                        continue
                    fee = costs.order(shares, price)
                    if shares * price + fee > cash:
                        if fractional:
                            shares = (cash - fee) / price
                        else:
                            shares -= 1
                        if shares <= 0:
                            continue
                    cash -= shares * price + fee
                    holdings[symbol] = shares
                    total_cost += fee
                    orders += 1
                    free_slots -= 1

        curve.append(value_at(row, closes))

        if row in rebalance_at:
            if use_filter and not (index.iloc[row] > index_sma.iloc[row]):
                pending = (set(), [])
                continue
            past = closes.iloc[row - lookback]
            recent = closes.iloc[row - SKIP_RECENT]
            momentum = (recent / past - 1).dropna()
            momentum = momentum[closes.iloc[row].notna()]
            ranked = list(momentum.sort_values(ascending=False).index)
            # Keep holdings still in the top N; fill the rest down the ranking (skipping unaffordable ones happens at execution).
            keep = [s for s in ranked[:top_n] if s in holdings]
            # Sell anything outside the top N; fill free slots down the ranking.
            pending = (set(ranked[:top_n]), keep + [s for s in ranked if s not in keep][: top_n * 3])

    curve = pd.Series(curve, index=dates[start_row:])
    return curve, total_cost, orders


def equal_weight_hold(opens, closes, start_row, equity):
    first = opens.iloc[start_row]
    eligible = first.dropna().index
    later = [s for s in closes.columns if s not in eligible]
    shares = (equity / len(eligible)) / first[eligible]
    curve = (closes.iloc[start_row:][eligible] * shares).sum(axis=1)
    return curve, len(eligible), later


def stats(curve: pd.Series) -> dict:
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1
    drawdown = (curve / curve.cummax() - 1).min()
    daily = curve.pct_change().dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0.0
    return {"cagr": cagr, "max_dd": drawdown, "sharpe": sharpe, "end": curve.iloc[-1]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--equity", type=float, default=300.0)
    parser.add_argument("--fractional", action="store_true")
    parser.add_argument("--exclude", nargs="*", default=[], help="symbols to leave out (robustness checks)")
    parser.add_argument("--listed-at-start", action="store_true",
                        help="only stocks already trading when the test starts, like the benchmark")
    args = parser.parse_args()

    opens, closes, index = load_panel()
    start_row = max(LOOKBACKS) + TREND_N  # same start for every variant
    drop = set(args.exclude)
    if args.listed_at_start:
        drop |= set(closes.columns[opens.iloc[start_row].isna()])
    if drop:
        opens, closes = opens.drop(columns=sorted(drop)), closes.drop(columns=sorted(drop))
        print(f"Excluded: {', '.join(sorted(drop))}")
    dates = closes.index
    mid = dates[start_row + (len(dates) - start_row) // 2]

    bench, n_bench, late = equal_weight_hold(opens, closes, start_row, args.equity)
    spy = index.iloc[start_row:] / index.iloc[start_row] * args.equity

    def halves(curve):
        return stats(curve[curve.index < mid]), stats(curve[curve.index >= mid])

    b_all, (b_h1, b_h2) = stats(bench), halves(bench)
    s_all = stats(spy)

    mode = "fractional (hypothetical)" if args.fractional else "whole shares"
    print(f"Period {dates[start_row].date()} .. {dates[-1].date()}  | equity ${args.equity:,.0f} | {mode}")
    print(f"Universe {closes.shape[1]} stocks; equal-weight benchmark holds the {n_bench} listed at the start "
          f"(later listings: {', '.join(late) or 'none'})")
    print(f"Halves split at {mid.date()}\n")

    header = f"{'variant':<26}{'CAGR':>7}{'maxDD':>8}{'Sharpe':>8}{'end $':>9}{'costs':>8}{'orders':>7}{'@2x':>8}  {'1st half':>8}{'2nd half':>9}  verdict"
    print(header)
    print("-" * len(header))
    print(f"{'equal-weight buy&hold':<26}{b_all['cagr']:>7.1%}{b_all['max_dd']:>8.1%}{b_all['sharpe']:>8.2f}"
          f"{b_all['end']:>9,.0f}{'':>8}{'':>7}{'':>8}  {b_h1['cagr']:>8.1%}{b_h2['cagr']:>9.1%}")
    print(f"{'S&P 500 (SPY) buy&hold':<26}{s_all['cagr']:>7.1%}{s_all['max_dd']:>8.1%}{s_all['sharpe']:>8.2f}"
          f"{s_all['end']:>9,.0f}")

    passes, passes_h1, passes_h2 = 0, 0, 0
    rows = []
    for lookback, top_n, use_filter in itertools.product(LOOKBACKS, TOP_NS, FILTERS):
        name = f"mom{lookback // 21}m top{top_n}" + (" +filter" if use_filter else "")
        curve, cost, orders = run(opens, closes, index, start_row, lookback, top_n, use_filter,
                                  args.equity, args.fractional, Costs())
        curve2, _, _ = run(opens, closes, index, start_row, lookback, top_n, use_filter,
                           args.equity, args.fractional, Costs(multiplier=2.0))
        st, (h1, h2) = stats(curve), halves(curve)
        ok = st["cagr"] > b_all["cagr"] and curve2.iloc[-1] > args.equity
        passes += ok
        passes_h1 += h1["cagr"] > b_h1["cagr"]
        passes_h2 += h2["cagr"] > b_h2["cagr"]
        rows.append({"variant": name, **st, "costs": cost, "orders": orders, "end_2x": curve2.iloc[-1],
                     "h1_cagr": h1["cagr"], "h2_cagr": h2["cagr"], "pass": ok})
        print(f"{name:<26}{st['cagr']:>7.1%}{st['max_dd']:>8.1%}{st['sharpe']:>8.2f}{st['end']:>9,.0f}"
              f"{cost:>8,.0f}{orders:>7}{curve2.iloc[-1]:>8,.0f}  {h1['cagr']:>8.1%}{h2['cagr']:>9.1%}  "
              f"{'PASS' if ok else 'FAIL'}")

    overall = passes >= 6 and passes_h1 > 4 and passes_h2 > 4
    print(f"\nVariants passing: {passes}/8 | beating the benchmark in 1st half {passes_h1}/8, 2nd half {passes_h2}/8")
    print(f"Rule verdict: {'PASS' if overall else 'FAIL'}  (needs >= 6/8, and > 4/8 in each half)")

    suffix = f"{int(args.equity)}{'_frac' if args.fractional else ''}{'_restricted' if drop else ''}"
    pd.DataFrame(rows).to_csv(DATA_DIR / f"rotation_summary_{suffix}.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
