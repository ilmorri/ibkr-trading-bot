"""Walk-forward test of daily-bar swing strategies.

Usage:
    python scripts/walk_forward.py                 # every data/*_daily.csv
    python scripts/walk_forward.py HOOD INTC SOFI

How it works
------------
The history is cut into consecutive folds. For each fold the script picks the
parameter set that made the most money on the TRAIN window (the 2 years before
the fold), then trades only that set on the TEST window (the next 6 months),
which the choice never saw. Only the test-window trades are reported, so the
result cannot be improved by tuning: there is nothing to tune by hand.

If no parameter set was profitable on a train window, the strategy stays out of
the market for that fold.

Rules shared by all strategies: long or short (see SIDES), one position per symbol, signal on
the daily close, entry at the next day's open, whole shares, fixed position
value. A stop is checked against each day's low (filled at the open if the
price gaps through it).

Pass mark (fixed in advance): at least 20 out-of-sample trades, positive net
P&L, and still positive with costs doubled.
"""
from __future__ import annotations

import argparse
import itertools
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

WARMUP = 200          # bars needed before the slowest indicator is valid
MIN_TRAIN_TRADES = 5  # a parameter set needs this many train trades to be eligible
MIN_OOS_TRADES = 20   # pass mark


# --------------------------------------------------------------------------
# Indicators (all causal: the value at bar t uses bars up to and including t)
# --------------------------------------------------------------------------
def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(gain.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


# --------------------------------------------------------------------------
# Strategies: each returns (entry signal, exit signal, stop distance, max hold)
# Signals are evaluated on the close of bar t and acted on at the open of t+1.
# --------------------------------------------------------------------------
def trend_pullback(df: pd.DataFrame, trend_n: int, rsi_max: int, max_hold: int, stop_pct: float):
    """Buy a short-term dip inside a long-term uptrend; sell on the bounce."""
    close = df["close"]
    entry = (close > sma(close, trend_n)) & (rsi(close, 2) < rsi_max)
    exit_ = close > sma(close, 5)
    stop_dist = close * stop_pct / 100.0
    return entry, exit_, stop_dist, max_hold


def breakout(df: pd.DataFrame, entry_n: int, exit_n: int, atr_mult: float):
    """Buy a new N-day high; sell on a new M-day low or an ATR stop."""
    close = df["close"]
    entry = close > df["high"].shift(1).rolling(entry_n).max()
    exit_ = close < df["low"].shift(1).rolling(exit_n).min()
    stop_dist = atr(df) * atr_mult
    return entry, exit_, stop_dist, None


# Short-side mirrors of the long rules (added 2026-10-13, grids fixed before the first run).
def downtrend_rally(df: pd.DataFrame, trend_n: int, rsi_min: int, max_hold: int, stop_pct: float):
    """Short a short-term rally inside a long-term downtrend; cover on the drop."""
    close = df["close"]
    entry = (close < sma(close, trend_n)) & (rsi(close, 2) > rsi_min)
    exit_ = close < sma(close, 5)
    stop_dist = close * stop_pct / 100.0
    return entry, exit_, stop_dist, max_hold


def breakdown(df: pd.DataFrame, entry_n: int, exit_n: int, atr_mult: float):
    """Short a new N-day low; cover on a new M-day high or an ATR stop."""
    close = df["close"]
    entry = close < df["low"].shift(1).rolling(entry_n).min()
    exit_ = close > df["high"].shift(1).rolling(exit_n).max()
    stop_dist = atr(df) * atr_mult
    return entry, exit_, stop_dist, None


# +1 buys, -1 sells short.
SIDES = {"trend_pullback": 1, "breakout": 1, "downtrend_rally": -1, "breakdown": -1}

STRATEGIES = {
    "trend_pullback": (
        trend_pullback,
        {"trend_n": [100, 200], "rsi_max": [5, 10, 20], "max_hold": [5, 10], "stop_pct": [5.0, 8.0]},
    ),
    "breakout": (
        breakout,
        {"entry_n": [20, 55], "exit_n": [10, 20], "atr_mult": [2.0, 3.0]},
    ),
    "downtrend_rally": (
        downtrend_rally,
        {"trend_n": [100, 200], "rsi_min": [80, 90, 95], "max_hold": [5, 10], "stop_pct": [5.0, 8.0]},
    ),
    "breakdown": (
        breakdown,
        {"entry_n": [20, 55], "exit_n": [10, 20], "atr_mult": [2.0, 3.0]},
    ),
}


def param_grid(grid: dict) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*(grid[k] for k in keys))]


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------
def simulate(df: pd.DataFrame, entry, exit_, stop_dist, max_hold, costs: dict, side: int = 1) -> list[dict]:
    """side +1 is long, -1 is short (stop above entry, checked against the high)."""
    o, low, c = df["open"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    high = df["high"].to_numpy()
    entry = entry.fillna(False).to_numpy(dtype=bool)
    exit_ = exit_.fillna(False).to_numpy(dtype=bool)
    stop_dist = stop_dist.to_numpy(dtype=float)
    n = len(df)
    trades: list[dict] = []
    pos = None  # dict while a position is open

    def close_position(i: int, price: float, reason: str) -> None:
        nonlocal pos
        shares = pos["shares"]
        gross = side * (price - pos["entry"]) * shares
        commission = 2 * max(costs["commission_min"], costs["commission_per_share"] * shares)
        slippage = costs["slippage_bps"] / 1e4 * (pos["entry"] + price) * shares
        if side < 0:
            # Stock loan fee, charged per calendar day held; trading days x 365/252.
            days = (i - pos["i"]) * 365 / 252
            commission += costs["borrow_bps_per_year"] / 1e4 * pos["entry"] * shares * days / 365
        trades.append(
            {
                "entry_i": pos["i"],
                "exit_i": i,
                "entry": pos["entry"],
                "exit": price,
                "shares": shares,
                "gross": gross,
                "cost": commission + slippage,
                "risk": abs(pos["entry"] - pos["stop"]) * shares,
                "reason": reason,
            }
        )
        pos = None

    def stop_touched(i: int) -> bool:
        return low[i] <= pos["stop"] if side > 0 else high[i] >= pos["stop"]

    def stop_gapped(i: int) -> bool:
        return o[i] <= pos["stop"] if side > 0 else o[i] >= pos["stop"]

    for i in range(max(WARMUP, 1), n):
        if pos is not None:
            if stop_gapped(i):
                close_position(i, o[i], "stop_gap")
            elif exit_[i - 1]:
                close_position(i, o[i], "signal")
            elif max_hold is not None and i - pos["i"] >= max_hold:
                close_position(i, o[i], "max_hold")
            elif stop_touched(i):
                close_position(i, pos["stop"], "stop")
            continue  # no new entry on a day that started with a position

        if entry[i - 1] and not math.isnan(stop_dist[i - 1]) and stop_dist[i - 1] > 0:
            price = o[i]
            shares = int(costs["position_value"] // price)
            if shares < 1 or shares * price < costs["min_position_value"]:
                continue
            pos = {"i": i, "entry": price, "shares": shares, "stop": price - side * stop_dist[i - 1]}
            if stop_touched(i):
                close_position(i, pos["stop"], "stop")

    if pos is not None:
        close_position(n - 1, c[n - 1], "end_of_data")
    return trades


# --------------------------------------------------------------------------
# Walk-forward
# --------------------------------------------------------------------------
def walk_forward(df: pd.DataFrame, strategy: str, costs: dict, train: int, test: int):
    """Returns (out-of-sample trades, fold log, best in-sample net over the whole history)."""
    func, grid = STRATEGIES[strategy]
    runs = []
    for params in param_grid(grid):
        entry, exit_, stop_dist, max_hold = func(df, **params)
        trades = simulate(df, entry, exit_, stop_dist, max_hold, costs, SIDES[strategy])
        runs.append((params, trades))

    def net_between(trades, start, end):
        picked = [t for t in trades if start <= t["entry_i"] < end]
        return sum(t["gross"] - t["cost"] for t in picked), picked

    oos, folds = [], []
    n = len(df)
    start = WARMUP + train
    fold = 0
    while start < n:
        end = min(start + test, n)
        fold += 1
        best = None
        for params, trades in runs:
            net, picked = net_between(trades, start - train, start)
            # a trade still open at the end of the train window is not known yet
            picked = [t for t in picked if t["exit_i"] < start]
            net = sum(t["gross"] - t["cost"] for t in picked)
            if len(picked) >= MIN_TRAIN_TRADES and net > 0 and (best is None or net > best[0]):
                best = (net, params, trades)
        dates = f"{df['date'].iloc[start]} .. {df['date'].iloc[end - 1]}"
        if best is None:
            folds.append({"fold": fold, "test": dates, "params": "stayed out", "train_net": 0.0, "test_net": 0.0, "trades": 0})
        else:
            test_net, picked = net_between(best[2], start, end)
            for t in picked:
                oos.append({**t, "fold": fold, "params": str(best[1])})
            folds.append({"fold": fold, "test": dates, "params": str(best[1]), "train_net": best[0], "test_net": test_net, "trades": len(picked)})
        start = end

    first_test = WARMUP + train
    in_sample_best = max(
        (net_between(trades, first_test, n)[0] for _, trades in runs), default=0.0
    )
    return oos, folds, in_sample_best


def buy_and_hold(df: pd.DataFrame, start: int, costs: dict) -> float:
    if start >= len(df):
        return 0.0
    price = df["open"].iloc[start]
    shares = int(costs["position_value"] // price)
    return (df["close"].iloc[-1] - price) * shares if shares >= 1 else 0.0


def summarise(trades: list[dict]) -> dict:
    gross = sum(t["gross"] for t in trades)
    cost = sum(t["cost"] for t in trades)
    wins = sum(1 for t in trades if t["gross"] - t["cost"] > 0)
    return {
        "trades": len(trades),
        "win_pct": 100.0 * wins / len(trades) if trades else 0.0,
        "gross": gross,
        "costs": cost,
        "net": gross - cost,
        "net_2x_cost": gross - 2 * cost,
        "worst_trade": min((t["gross"] - t["cost"] for t in trades), default=0.0),
    }


def verdict(s: dict) -> str:
    if s["trades"] < MIN_OOS_TRADES:
        return "too few"
    return "PASS" if s["net"] > 0 and s["net_2x_cost"] > 0 else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward test on daily bars")
    parser.add_argument("symbols", nargs="*", help="default: every data/*_daily.csv")
    parser.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    parser.add_argument("--train-days", type=int, default=504, help="train window in trading days (default 504 = 2 years)")
    parser.add_argument("--test-days", type=int, default=126, help="test window in trading days (default 126 = 6 months)")
    parser.add_argument("--equity", type=float, default=300.0)
    parser.add_argument("--position-value", type=float, default=240.0)
    parser.add_argument("--min-position-value", type=float, default=150.0)
    parser.add_argument("--commission-per-share", type=float, default=0.0035)
    parser.add_argument("--commission-min", type=float, default=0.35)
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="per side, on entry and exit")
    parser.add_argument("--borrow-bps-per-year", type=float, default=100.0,
                        help="stock loan fee on short positions (easy-to-borrow names cost less)")
    parser.add_argument("--folds", action="store_true", help="print the fold-by-fold log")
    args = parser.parse_args()

    costs = {
        "position_value": args.position_value,
        "min_position_value": args.min_position_value,
        "commission_per_share": args.commission_per_share,
        "commission_min": args.commission_min,
        "slippage_bps": args.slippage_bps,
        "borrow_bps_per_year": args.borrow_bps_per_year,
    }

    symbols = [s.upper() for s in args.symbols] or sorted(
        p.name.replace("_daily.csv", "") for p in DATA_DIR.glob("*_daily.csv")
    )
    if not symbols:
        print("No data/*_daily.csv files found. Run scripts/download_daily.py first.")
        return 1

    rows, all_trades, by_strategy = [], [], {s: [] for s in args.strategies}
    for symbol in symbols:
        path = DATA_DIR / f"{symbol}_daily.csv"
        if not path.exists():
            print(f"{symbol}: {path.name} not found, skipped")
            continue
        df = pd.read_csv(path)
        first_test = WARMUP + args.train_days
        if len(df) < first_test + args.test_days:
            print(f"{symbol}: only {len(df)} bars, need {first_test + args.test_days}, skipped")
            continue
        if df["close"].iloc[first_test:].min() > args.position_value:
            print(f"{symbol}: one share always costs more than ${args.position_value:.0f}, skipped")
            continue
        hold = buy_and_hold(df, first_test, costs)
        for strategy in args.strategies:
            oos, folds, in_sample_best = walk_forward(df, strategy, costs, args.train_days, args.test_days)
            s = summarise(oos)
            rows.append({"symbol": symbol, "strategy": strategy, **s, "in_sample_best": in_sample_best, "buy_hold": hold, "verdict": verdict(s)})
            by_strategy[strategy].extend(oos)
            for t in oos:
                all_trades.append(
                    {
                        "symbol": symbol,
                        "strategy": strategy,
                        "fold": t["fold"],
                        "params": t["params"],
                        "entry_date": df["date"].iloc[t["entry_i"]],
                        "exit_date": df["date"].iloc[t["exit_i"]],
                        "entry": round(t["entry"], 4),
                        "exit": round(t["exit"], 4),
                        "shares": t["shares"],
                        "gross": round(t["gross"], 2),
                        "cost": round(t["cost"], 2),
                        "net": round(t["gross"] - t["cost"], 2),
                        "risk_pct_of_equity": round(100 * t["risk"] / args.equity, 2),
                        "reason": t["reason"],
                    }
                )
            if args.folds:
                print(f"\n{symbol} {strategy}")
                for f in folds:
                    print(f"  fold {f['fold']:>2}  {f['test']}  train {f['train_net']:>8.2f}  test {f['test_net']:>8.2f}  trades {f['trades']:>2}  {f['params']}")

    if not rows:
        print("Nothing to report.")
        return 1

    print("\nOut-of-sample results per symbol (dollars; test windows only)")
    header = f"{'symbol':<7}{'strategy':<16}{'trades':>7}{'win%':>6}{'gross':>9}{'costs':>8}{'net':>9}{'net@2x':>9}{'worst':>8}{'tuned':>9}{'buyhold':>9}  verdict"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['symbol']:<7}{r['strategy']:<16}{r['trades']:>7}{r['win_pct']:>6.0f}{r['gross']:>9.2f}{r['costs']:>8.2f}"
            f"{r['net']:>9.2f}{r['net_2x_cost']:>9.2f}{r['worst_trade']:>8.2f}{r['in_sample_best']:>9.2f}{r['buy_hold']:>9.2f}  {r['verdict']}"
        )

    print("\nOut-of-sample results per strategy, all symbols together (the number to trust most)")
    header = f"{'strategy':<16}{'trades':>7}{'win%':>6}{'gross':>9}{'costs':>8}{'net':>9}{'net@2x':>9}{'worst':>8}  verdict"
    print(header)
    print("-" * len(header))
    for strategy, trades in by_strategy.items():
        s = summarise(trades)
        print(
            f"{strategy:<16}{s['trades']:>7}{s['win_pct']:>6.0f}{s['gross']:>9.2f}{s['costs']:>8.2f}"
            f"{s['net']:>9.2f}{s['net_2x_cost']:>9.2f}{s['worst_trade']:>8.2f}  {verdict(s)}"
        )

    print(
        "\nColumns: 'tuned' is the best any single parameter set did over the same period with hindsight"
        "\n(what a plain parameter sweep would have reported). 'buyhold' is buying the same dollar amount"
        "\nat the start of the test period and never selling. 'worst' is the worst single trade."
    )
    if all_trades:
        risks = [t["risk_pct_of_equity"] for t in all_trades]
        print(f"Planned risk per trade: median {np.median(risks):.1f}% of equity, max {max(risks):.1f}%.")

    DATA_DIR.mkdir(exist_ok=True)
    pd.DataFrame(all_trades).to_csv(DATA_DIR / "walkforward_trades.csv", index=False)
    pd.DataFrame(rows).round(2).to_csv(DATA_DIR / "walkforward_summary.csv", index=False)
    print("Saved data/walkforward_trades.csv and data/walkforward_summary.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
