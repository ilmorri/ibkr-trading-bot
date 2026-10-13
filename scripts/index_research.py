#!/usr/bin/env python3
"""Index and multi-asset strategies, tested with a sealed holdout.

Usage:
    python scripts/index_research.py                 # discovery period only
    python scripts/index_research.py --holdout NAME  # one look at the holdout, logged

PRE-REGISTERED 2026-10-13, committed before the first run. Do not change the
rules, parameters, costs or pass marks after seeing results; a new idea is a
new entry with its own test.

Periods
-------
  discovery  start of data .. 2016-10-14  (about 15 years)
  holdout    2016-10-17 .. today          (about 10 years, sealed)

The holdout is only evaluated for a strategy that passed discovery, once,
and every look is appended to data/holdout_log.csv. A strategy that fails
its holdout is finished.

Strategies (published standard versions, no parameter search)
-------------------------------------------------------------
  turn_of_month   SPY: buy at the open of the last trading day of the month,
                  sell at the close of the 3rd trading day of the next month
  pre_holiday     SPY: buy at the open of the trading day before an NYSE
                  holiday, sell at that day's close
  rsi2_index      SPY: close above its 200-day average and 2-day RSI below 10:
                  buy at the next open; sell at the next open after a close
                  above the 5-day average
  dual_momentum   Monthly. If SPY's 12-month return is above zero, hold
                  whichever of SPY and EFA has the higher 12-month return;
                  otherwise hold AGG (bonds). (The published version compares
                  with T-bills; no T-bill series is available, so zero is used.)
  trend_10m       Monthly. 25% each in SPY, EFA, AGG, GLD; each held only while
                  its month-end close is above its 10-month average, else cash.

Costs: 10 bps per side (5 commission + 5 slippage); doubled for the pass mark.
Cash earns nothing.

Pass marks
----------
  discovery  at least 30 trades (single-asset) or 60 months (monthly),
             Sharpe at DOUBLED costs above SPY buy-and-hold's Sharpe,
             and CAGR at doubled costs above zero
  holdout    Sharpe at normal costs above SPY buy-and-hold's Sharpe,
             and CAGR above zero

Note: a higher Sharpe than buy-and-hold with a lower CAGR is less risk per
unit of return, not more money. The report shows both.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bot.market_calendar import MarketCalendar  # noqa: E402

DATA_DIR = ROOT / "data"
HOLDOUT_START = pd.Timestamp("2016-10-17")
HOLDOUT_LOG = DATA_DIR / "holdout_log.csv"
COST_BPS = 10.0


def load(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / f"{symbol}_daily.csv", parse_dates=["date"]).set_index("date")
    return df[["open", "high", "low", "close"]].astype(float)


def rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


# ----------------------------------------------------------------------
# Single-asset strategies: a list of trades (entry row, entry field, exit row, exit field)
# ----------------------------------------------------------------------
def turn_of_month(df: pd.DataFrame) -> list[tuple]:
    month = df.index.to_period("M")
    trades = []
    for i in range(len(df) - 1):
        if month[i] != month[i + 1]:  # i is the last trading day of its month
            exit_row = i + 3
            if exit_row < len(df):
                trades.append((i, "open", exit_row, "close"))
    return trades


def pre_holiday(df: pd.DataFrame) -> list[tuple]:
    cal = MarketCalendar()
    trades = []
    for i, day in enumerate(df.index.date):
        nxt = cal.next_trading_day(day)
        gap_has_holiday = any(
            not cal.is_trading_day(d) and d.weekday() < 5
            for d in pd.date_range(day, nxt, inclusive="neither").date
        )
        if gap_has_holiday:
            trades.append((i, "open", i, "close"))
    return trades


def rsi2_index(df: pd.DataFrame) -> list[tuple]:
    close = df["close"]
    entry = (close > close.rolling(200).mean()) & (rsi(close, 2) < 10)
    exit_ = close > close.rolling(5).mean()
    trades, i, n = [], 200, len(df)
    while i < n - 1:
        if entry.iloc[i]:
            start = i + 1
            j = start
            while j < n - 1 and not exit_.iloc[j]:
                j += 1
            end = min(j + 1, n - 1)
            trades.append((start, "open", end, "open"))
            i = end
        else:
            i += 1
    return trades


def trades_to_returns(df: pd.DataFrame, trades: list[tuple], cost_bps: float) -> pd.Series:
    """Daily account returns, marked at the close; cash days return zero."""
    ret = pd.Series(0.0, index=df.index)
    cost = cost_bps / 1e4
    for start, start_field, end, end_field in trades:
        mark = df[start_field].iloc[start]
        for row in range(start, end + 1):
            if row == end:
                price = df[end_field].iloc[row]
            else:
                price = df["close"].iloc[row]
            r = price / mark - 1
            if row == start:
                r -= cost
            if row == end:
                r -= cost
            ret.iloc[row] = (1 + ret.iloc[row]) * (1 + r) - 1
            mark = price
    return ret


# ----------------------------------------------------------------------
# Monthly portfolio strategies: target weights decided at a month-end close,
# applied from the next day's close.
# ----------------------------------------------------------------------
def month_end_rows(index: pd.DatetimeIndex) -> list[int]:
    period = index.to_period("M")
    return [i for i in range(len(index) - 1) if period[i] != period[i + 1]]


def weights_to_returns(closes: pd.DataFrame, weights: dict[int, dict[str, float]], cost_bps: float):
    daily = closes.pct_change().fillna(0.0)
    ret = pd.Series(0.0, index=closes.index)
    current: dict[str, float] = {}
    changes = 0
    for row in range(1, len(closes)):
        ret.iloc[row] = sum(w * daily[s].iloc[row] for s, w in current.items())
        if row - 1 in weights:  # decided at the previous close, traded at this close
            target = weights[row - 1]
            turnover = sum(abs(target.get(s, 0) - current.get(s, 0)) for s in set(target) | set(current))
            if turnover > 1e-9:
                changes += 1
            ret.iloc[row] -= turnover * cost_bps / 1e4
            current = target
    return ret, changes


def dual_momentum(closes: pd.DataFrame) -> dict[int, dict[str, float]]:
    weights = {}
    for row in month_end_rows(closes.index):
        if row < 252:
            continue
        mom = closes.iloc[row] / closes.iloc[row - 252] - 1
        if mom["SPY"] > 0:
            weights[row] = {"SPY" if mom["SPY"] >= mom["EFA"] else "EFA": 1.0}
        else:
            weights[row] = {"AGG": 1.0}
    return weights


def trend_10m(closes: pd.DataFrame) -> dict[int, dict[str, float]]:
    ends = month_end_rows(closes.index)
    weights = {}
    for k, row in enumerate(ends):
        if k < 10:
            continue
        window = [ends[k - m] for m in range(10)]
        target = {}
        for s in ["SPY", "EFA", "AGG", "GLD"]:
            avg = closes[s].iloc[window].mean()
            if closes[s].iloc[row] > avg:
                target[s] = 0.25
        weights[row] = target
    return weights


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------
def stats(ret: pd.Series) -> dict:
    ret = ret.dropna()
    curve = (1 + ret).cumprod()
    years = (ret.index[-1] - ret.index[0]).days / 365.25
    cagr = curve.iloc[-1] ** (1 / years) - 1
    sharpe = ret.mean() / ret.std() * math.sqrt(252) if ret.std() > 0 else 0.0
    dd = (curve / curve.cummax() - 1).min()
    exposure = (ret != 0).mean()
    return {"cagr": cagr, "sharpe": sharpe, "max_dd": dd, "exposure": exposure}


def evaluate(name: str, period: str, spy: pd.DataFrame, closes: pd.DataFrame):
    """Returns (normal-cost stats, doubled-cost stats, trade count, trades per year) for a period."""
    def window(series: pd.Series) -> pd.Series:
        if period == "discovery":
            return series[series.index < HOLDOUT_START]
        return series[series.index >= HOLDOUT_START]

    if name in SINGLE:
        trades = SINGLE[name](spy)
        r1 = window(trades_to_returns(spy, trades, COST_BPS))
        r2 = window(trades_to_returns(spy, trades, 2 * COST_BPS))
        lo, hi = r1.index[0], r1.index[-1]
        count = sum(1 for t in trades if lo <= spy.index[t[0]] <= hi)
    else:
        weights = MONTHLY[name](closes)
        r1, _ = weights_to_returns(closes, weights, COST_BPS)
        r2, _ = weights_to_returns(closes, weights, 2 * COST_BPS)
        first = closes.index[min(weights)]
        r1, r2 = window(r1[r1.index > first]), window(r2[r2.index > first])
        count = len([r for r in weights if window(pd.Series([0], index=[closes.index[r]])).size])
    years = (r1.index[-1] - r1.index[0]).days / 365.25
    return stats(r1), stats(r2), count, count / years, (r1.index[0], r1.index[-1])


SINGLE = {"turn_of_month": turn_of_month, "pre_holiday": pre_holiday, "rsi2_index": rsi2_index}
MONTHLY = {"dual_momentum": dual_momentum, "trend_10m": trend_10m}
MIN_TRADES = {"single": 30, "monthly": 60}


def benchmark(spy: pd.DataFrame, lo, hi) -> dict:
    ret = spy["close"].pct_change()
    return stats(ret[(ret.index >= lo) & (ret.index <= hi)])


def breakeven_fee(cagr_2x: float, cagr_1x: float, trades_per_year: float, equity: float) -> str:
    """Rough fixed fee per order at which the normal-cost CAGR falls to zero."""
    if trades_per_year <= 0 or cagr_1x <= 0:
        return "-"
    yearly_profit = cagr_1x * equity
    return f"${yearly_profit / (2 * trades_per_year):.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--holdout", help="evaluate ONE strategy on the sealed holdout (logged)")
    args = parser.parse_args()

    spy = load("SPY")
    closes = pd.DataFrame({s: load(s)["close"] for s in ["SPY", "EFA", "AGG", "GLD"]}).dropna()

    if args.holdout:
        name = args.holdout
        if name not in SINGLE and name not in MONTHLY:
            raise SystemExit(f"Unknown strategy {name}")
        d1, d2, dn, _, (dlo, dhi) = evaluate(name, "discovery", spy, closes)
        bench_d = benchmark(spy, dlo, dhi)
        kind = "single" if name in SINGLE else "monthly"
        if not (dn >= MIN_TRADES[kind] and d2["sharpe"] > bench_d["sharpe"] and d2["cagr"] > 0):
            raise SystemExit(f"{name} did not pass discovery; the holdout stays sealed for it.")

        previous = []
        if HOLDOUT_LOG.exists():
            previous = list(csv.DictReader(HOLDOUT_LOG.open()))
        if any(r["strategy"] == name for r in previous):
            raise SystemExit(f"{name} already had its holdout look; see {HOLDOUT_LOG}")

        h1, h2, hn, tpy, (lo, hi) = evaluate(name, "holdout", spy, closes)
        bench = benchmark(spy, lo, hi)
        passed = h1["sharpe"] > bench["sharpe"] and h1["cagr"] > 0
        print(f"HOLDOUT {name}  {lo.date()} .. {hi.date()}  (look #{len(previous) + 1} overall)")
        print(f"  strategy  CAGR {h1['cagr']:6.1%}  Sharpe {h1['sharpe']:.2f}  maxDD {h1['max_dd']:6.1%}  "
              f"in market {h1['exposure']:.0%}  trades {hn}")
        print(f"  @2x cost  CAGR {h2['cagr']:6.1%}  Sharpe {h2['sharpe']:.2f}")
        print(f"  SPY hold  CAGR {bench['cagr']:6.1%}  Sharpe {bench['sharpe']:.2f}  maxDD {bench['max_dd']:6.1%}")
        print(f"  verdict: {'PASS' if passed else 'FAIL'}")

        new = not HOLDOUT_LOG.exists()
        with HOLDOUT_LOG.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "strategy", "cagr", "sharpe", "spy_sharpe", "verdict"])
            w.writerow([datetime.now(timezone.utc).isoformat(), name, f"{h1['cagr']:.4f}",
                        f"{h1['sharpe']:.3f}", f"{bench['sharpe']:.3f}", "PASS" if passed else "FAIL"])
        return 0

    print("DISCOVERY period only (holdout sealed). Costs 10 bps/side; pass judged at 20 bps/side.\n")
    header = (f"{'strategy':<15}{'period':<25}{'trades':>7}{'/yr':>6}{'CAGR':>7}{'@2x':>7}"
              f"{'Sharpe':>8}{'@2x':>6}{'SPY Sh':>8}{'maxDD':>8}{'inMkt':>7}{'fee@$300':>10}{'fee@$3k':>9}  verdict")
    print(header)
    print("-" * len(header))
    for name in list(SINGLE) + list(MONTHLY):
        d1, d2, n, tpy, (lo, hi) = evaluate(name, "discovery", spy, closes)
        bench = benchmark(spy, lo, hi)
        kind = "single" if name in SINGLE else "monthly"
        passed = n >= MIN_TRADES[kind] and d2["sharpe"] > bench["sharpe"] and d2["cagr"] > 0
        print(f"{name:<15}{str(lo.date()) + '..' + str(hi.date()):<25}{n:>7}{tpy:>6.1f}{d1['cagr']:>7.1%}"
              f"{d2['cagr']:>7.1%}{d1['sharpe']:>8.2f}{d2['sharpe']:>6.2f}{bench['sharpe']:>8.2f}"
              f"{d1['max_dd']:>8.1%}{d1['exposure']:>7.0%}{breakeven_fee(d2['cagr'], d1['cagr'], tpy, 300):>10}"
              f"{breakeven_fee(d2['cagr'], d1['cagr'], tpy, 3000):>9}  {'PASS' if passed else 'FAIL'}")
    print("\nfee@$X: rough fixed commission per order at which the strategy's yearly profit on an $X account"
          "\nis used up entirely. IBKR quoted $6 (SPYL, LSE) and EUR 4 (IWDA) on the paper account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
