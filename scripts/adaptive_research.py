#!/usr/bin/env python3
"""Adaptive multi-strategy portfolio, tested with a fresh-stock holdout.

Usage:
    python scripts/adaptive_research.py                  # discovery universe
    python scripts/adaptive_research.py --holdout NAME   # one logged look at the holdout universe

PRE-REGISTERED 2026-10-13, committed before the first run and before the
holdout data was downloaded. Do not change rules, thresholds, costs or pass
marks after seeing results.

Universes
---------
  discovery  the stocks already in data/*_daily.csv (excluding index/bond/gold
             funds). Already seen in earlier research, so only a filter.
  holdout    HOLDOUT_SYMBOLS below: S&P 500 members whose ticker starts with
             "A", chosen mechanically, not yet downloaded when this file was
             committed. Stored in data/holdout_stocks/. Excluding any already
             used in discovery.

Components (daily bars, long only; signals on the close, orders at the next open)
----------------------------------------------------------------------------------
  trend   entry: close above the prior 20-day high and above its 50-day average
          exit:  close below the prior 10-day low; stop 3 x ATR(14) below entry
  revert  entry: close above its 200-day average and 2-day RSI below 10
          exit:  close above its 5-day average, or after 10 days; stop 8% below entry

Market filter (all variants): no new entries while SPY closes below its 200-day
average. Open positions keep their own exits.

Variants
--------
  regime_switch       per stock, Kaufman efficiency ratio over 20 days:
                      ER >= 0.35 -> only trend may enter; ER < 0.25 -> only revert;
                      in between -> no entry
  performance_switch  both components also run as shadow strategies on every stock
                      without capital limits; a component may enter only while
                      the average net return of its last 10 shadow trades that have
                      already closed (across all stocks) is above zero
  ensemble            both components may always enter (the control)

Portfolio: free slots are filled with the candidates with the highest 126-day
return. Whole shares. Account A: $300, 1 slot of up to $240, minimum $100.
Account B: $3,000, 5 slots of up to $600, minimum $100. Costs per order:
max($0.35, $0.0035/share) capped at 1% of value, plus 5 bps slippage; doubled
for the pass mark. Cash earns nothing.

Pass mark (judged on account B, where fees do not dominate; account A is
reported for practicality):
  discovery  at least 30 trades, Sharpe at DOUBLED costs above the Sharpe of an
             equal-weight buy-and-hold of the same universe, CAGR at doubled
             costs above zero
  holdout    same, at normal costs. One look per variant, logged in
             data/holdout_log.csv.
"""
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HOLDOUT_DIR = DATA_DIR / "holdout_stocks"
HOLDOUT_LOG = DATA_DIR / "holdout_log.csv"

NOT_STOCKS = {"SPY", "QQQ", "IWM", "EFA", "EEM", "DIA", "AGG", "TLT", "IEF", "SHY", "GLD"}

HOLDOUT_SYMBOLS = [
    "A", "AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "ADM", "ADP", "ADSK", "AEE", "AEP",
    "AES", "AFL", "AIG", "AIZ", "AJG", "AKAM", "ALB", "ALGN", "ALL", "AMAT", "AMCR", "AME",
    "AMGN", "AMP", "AMT", "AMZN", "ANET", "AON", "AOS", "APA", "APD", "APH", "ARE", "ATO",
    "AVB", "AVGO", "AVY", "AWK", "AXP", "AZO",
]

VARIANTS = ["regime_switch", "performance_switch", "ensemble"]
ACCOUNTS = {"A": (300.0, 1, 240.0), "B": (3000.0, 5, 600.0)}
MIN_POSITION = 100.0
SLIPPAGE_BPS = 5.0
START = pd.Timestamp("2017-10-16")  # one year of warm-up after the first stock bars


# ----------------------------------------------------------------------
# Data and indicators
# ----------------------------------------------------------------------
def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return df[["open", "high", "low", "close"]].astype(float)


def rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(gain.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def efficiency_ratio(close: pd.Series, n: int = 20) -> pd.Series:
    change = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n).sum()
    return change / path.replace(0, np.nan)


def signals(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    out = pd.DataFrame(index=df.index)
    out["trend_entry"] = (c > df["high"].shift(1).rolling(20).max()) & (c > c.rolling(50).mean())
    out["trend_exit"] = c < df["low"].shift(1).rolling(10).min()
    out["trend_stop"] = 3 * atr(df)
    out["revert_entry"] = (c > c.rolling(200).mean()) & (rsi(c, 2) < 10)
    out["revert_exit"] = c > c.rolling(5).mean()
    out["revert_stop"] = c * 0.08
    out["er"] = efficiency_ratio(c)
    out["mom126"] = c / c.shift(126) - 1
    return out


COMPONENTS = {
    "trend": {"entry": "trend_entry", "exit": "trend_exit", "stop": "trend_stop", "max_hold": None},
    "revert": {"entry": "revert_entry", "exit": "revert_exit", "stop": "revert_stop", "max_hold": 10},
}


# ----------------------------------------------------------------------
# Costs
# ----------------------------------------------------------------------
def order_cost(shares: int, price: float, mult: float) -> float:
    value = shares * price
    commission = min(max(0.35, 0.0035 * shares), 0.01 * value)
    return (commission + value * SLIPPAGE_BPS / 1e4) * mult


# ----------------------------------------------------------------------
# Shadow trades for performance_switch: each component on each stock, no capital limit
# ----------------------------------------------------------------------
def shadow_trades(bars: dict, sigs: dict) -> dict[str, list[tuple]]:
    """Per component, a list of (exit_date, net_return) sorted by exit date."""
    out = {name: [] for name in COMPONENTS}
    for symbol, df in bars.items():
        s = sigs[symbol]
        o, lo, c = df["open"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
        for name, comp in COMPONENTS.items():
            entry = s[comp["entry"]].fillna(False).to_numpy(bool)
            exit_ = s[comp["exit"]].fillna(False).to_numpy(bool)
            stop_dist = s[comp["stop"]].to_numpy(float)
            i, n = 1, len(df)
            while i < n:
                if entry[i - 1] and not math.isnan(stop_dist[i - 1]):
                    e, stop, start = o[i], o[i] - stop_dist[i - 1], i
                    j, price = i, None
                    while j < n:
                        if j > start and o[j] <= stop:
                            price = o[j]
                        elif j > start and exit_[j - 1]:
                            price = o[j]
                        elif j > start and comp["max_hold"] and j - start >= comp["max_hold"]:
                            price = o[j]
                        elif lo[j] <= stop:
                            price = stop
                        if price is not None:
                            break
                        j += 1
                    if price is None:
                        break
                    out[name].append((df.index[j], price / e - 1 - 2 * SLIPPAGE_BPS / 1e4 - 0.002))
                    i = j + 1
                else:
                    i += 1
    for name in out:
        out[name].sort()
    return out


def component_active(shadow: list[tuple], day: pd.Timestamp) -> bool:
    closed = [r for d, r in shadow if d < day][-10:]
    return len(closed) == 10 and sum(closed) / 10 > 0


# ----------------------------------------------------------------------
# Portfolio simulation
# ----------------------------------------------------------------------
@dataclass
class Position:
    symbol: str
    component: str
    shares: int
    entry: float
    stop: float
    start: int


def simulate(variant, bars, sigs, spy, account, cost_mult, shadow=None):
    equity0, slots, slot_value = account
    dates = spy.index[spy.index >= START]
    spy_ok = (spy["close"] > spy["close"].rolling(200).mean()).reindex(dates)
    frames = {s: {k: v.to_numpy(float) for k, v in df.reindex(dates).items()} for s, df in bars.items()}
    sig = {s: {k: v.to_numpy(float) for k, v in x.reindex(dates).astype(float).items()} for s, x in sigs.items()}
    spy_ok = spy_ok.fillna(False).to_numpy(bool)

    cash, positions, trades, curve, costs = equity0, {}, [], [], 0.0
    active_cache = {}

    def close_position(sym, row, price, reason):
        nonlocal cash, costs
        p = positions.pop(sym)
        fee = order_cost(p.shares, price, cost_mult)
        cash += p.shares * price - fee
        costs += fee
        trades.append({"symbol": sym, "component": p.component, "entry_date": dates[p.start],
                       "exit_date": dates[row], "entry": p.entry, "exit": price, "shares": p.shares,
                       "reason": reason})

    for row in range(1, len(dates)):
        # 1. exits at the open, from yesterday's close
        for sym in list(positions):
            p, f = positions[sym], frames[sym]
            if math.isnan(f["open"][row]):
                continue
            comp = COMPONENTS[p.component]
            if f["open"][row] <= p.stop:
                close_position(sym, row, f["open"][row], "stop_gap")
            elif sig[sym][comp["exit"]][row - 1] == 1.0:
                close_position(sym, row, f["open"][row], "signal")
            elif comp["max_hold"] and row - p.start >= comp["max_hold"]:
                close_position(sym, row, f["open"][row], "max_hold")

        # 2. entries at the open, from yesterday's close
        if len(positions) < slots and spy_ok[row - 1]:
            day = dates[row]
            if variant == "performance_switch":
                if day not in active_cache:
                    active_cache[day] = {n: component_active(shadow[n], day) for n in COMPONENTS}
                allowed = active_cache[day]
            else:
                allowed = {n: True for n in COMPONENTS}

            candidates = []
            for sym, s in sig.items():
                if sym in positions:
                    continue
                r = row - 1
                if math.isnan(s["mom126"][r]) or math.isnan(frames[sym]["open"][row]):
                    continue
                for name, comp in COMPONENTS.items():
                    if not allowed[name] or s[comp["entry"]][r] != 1.0 or math.isnan(s[comp["stop"]][r]):
                        continue
                    if variant == "regime_switch":
                        er = s["er"][r]
                        if math.isnan(er):
                            continue
                        if name == "trend" and er < 0.35:
                            continue
                        if name == "revert" and er >= 0.25:
                            continue
                    candidates.append((s["mom126"][r], sym, name, s[comp["stop"]][r]))
                    break  # one component per stock per day; trend is checked first
            candidates.sort(key=lambda x: (-x[0], x[1]))

            for _, sym, name, stop_dist in candidates:
                if len(positions) >= slots:
                    break
                price = frames[sym]["open"][row]
                budget = min(slot_value, cash)
                shares = int(budget // price)
                if shares < 1 or shares * price < MIN_POSITION:
                    continue
                fee = order_cost(shares, price, cost_mult)
                if shares * price + fee > cash:
                    continue
                cash -= shares * price + fee
                costs += fee
                positions[sym] = Position(sym, name, shares, price, price - stop_dist, row)

        # 3. intraday stops
        for sym in list(positions):
            p, low = positions[sym], frames[sym]["low"][row]
            if not math.isnan(low) and low <= p.stop:
                close_position(sym, row, p.stop, "stop")

        value = cash + sum(p.shares * frames[s]["close"][row] for s, p in positions.items()
                           if not math.isnan(frames[s]["close"][row]))
        curve.append(value)

    curve = pd.Series(curve, index=dates[1:])
    return curve, trades, costs


def stats(curve: pd.Series) -> dict:
    ret = curve.pct_change().dropna()
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    return {
        "cagr": (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1,
        "sharpe": ret.mean() / ret.std() * math.sqrt(252) if ret.std() > 0 else 0.0,
        "max_dd": (curve / curve.cummax() - 1).min(),
        "end": curve.iloc[-1],
    }


def equal_weight_hold(bars: dict, spy: pd.DataFrame) -> dict:
    dates = spy.index[spy.index >= START]
    closes = pd.DataFrame({s: df["close"] for s, df in bars.items()}).reindex(dates)
    eligible = closes.columns[closes.iloc[0].notna()]
    curve = (closes[eligible] / closes[eligible].iloc[0]).mean(axis=1)
    return stats(curve)


def load_universe(holdout: bool) -> dict:
    if holdout:
        files = [HOLDOUT_DIR / f"{s}_daily.csv" for s in HOLDOUT_SYMBOLS]
        files = [f for f in files if f.exists()]
    else:
        files = [f for f in sorted(DATA_DIR.glob("*_daily.csv")) if f.name.replace("_daily.csv", "") not in NOT_STOCKS]
    return {f.name.replace("_daily.csv", ""): load(f) for f in files}


def run_variant(variant, bars, sigs, spy, shadow, account, mult):
    curve, trades, costs = simulate(variant, bars, sigs, spy, ACCOUNTS[account], mult, shadow)
    return stats(curve), trades, costs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--holdout", choices=VARIANTS)
    args = parser.parse_args()

    spy = load(DATA_DIR / "SPY_daily.csv")
    discovery = load_universe(False)
    universes = {"discovery": discovery}
    if args.holdout:
        name = f"adaptive:{args.holdout}"
        if HOLDOUT_LOG.exists() and any(r["strategy"] == name for r in csv.DictReader(HOLDOUT_LOG.open())):
            raise SystemExit(f"{name} already had its holdout look; see {HOLDOUT_LOG}")
        dsigs = {s: signals(df) for s, df in discovery.items()}
        dshadow = shadow_trades(discovery, dsigs)
        dbench = equal_weight_hold(discovery, spy)
        d2, dtrades, _ = run_variant(args.holdout, discovery, dsigs, spy, dshadow, "B", 2.0)
        if not (len(dtrades) >= 30 and d2["sharpe"] > dbench["sharpe"] and d2["cagr"] > 0):
            raise SystemExit(f"{args.holdout} did not pass discovery; the holdout stays sealed for it.")
        held = load_universe(True)
        held = {s: df for s, df in held.items() if s not in discovery}
        universes = {"holdout": held}

    for label, bars in universes.items():
        sigs = {s: signals(df) for s, df in bars.items()}
        shadow = shadow_trades(bars, sigs)
        bench = equal_weight_hold(bars, spy)
        spy_stats = stats(spy["close"][spy.index >= START])
        print(f"{label.upper()}: {len(bars)} stocks, {START.date()} .. {spy.index[-1].date()}")
        print(f"  equal-weight hold  CAGR {bench['cagr']:6.1%}  Sharpe {bench['sharpe']:.2f}  maxDD {bench['max_dd']:6.1%}")
        print(f"  SPY hold           CAGR {spy_stats['cagr']:6.1%}  Sharpe {spy_stats['sharpe']:.2f}  maxDD {spy_stats['max_dd']:6.1%}\n")

        variants = [args.holdout] if args.holdout else VARIANTS
        header = f"  {'variant':<20}{'acct':>5}{'trades':>7}{'CAGR':>7}{'@2x':>7}{'Sharpe':>8}{'@2x':>6}{'maxDD':>8}{'end $':>9}{'costs':>8}  verdict"
        print(header)
        for variant in variants:
            for account in ("B", "A"):
                s1, trades, costs = run_variant(variant, bars, sigs, spy, shadow, account, 1.0)
                s2, _, _ = run_variant(variant, bars, sigs, spy, shadow, account, 2.0)
                if label == "discovery":
                    ok = len(trades) >= 30 and s2["sharpe"] > bench["sharpe"] and s2["cagr"] > 0
                else:
                    ok = len(trades) >= 30 and s1["sharpe"] > bench["sharpe"] and s1["cagr"] > 0
                verdict = ("PASS" if ok else "FAIL") if account == "B" else "(info)"
                comps = pd.Series([t["component"] for t in trades]).value_counts().to_dict()
                print(f"  {variant:<20}{account:>5}{len(trades):>7}{s1['cagr']:>7.1%}{s2['cagr']:>7.1%}"
                      f"{s1['sharpe']:>8.2f}{s2['sharpe']:>6.2f}{s1['max_dd']:>8.1%}{s1['end']:>9,.0f}"
                      f"{costs:>8,.0f}  {verdict}  {comps}")
                if label == "holdout" and account == "B":
                    new = not HOLDOUT_LOG.exists()
                    with HOLDOUT_LOG.open("a", newline="") as f:
                        w = csv.writer(f)
                        if new:
                            w.writerow(["time", "strategy", "cagr", "sharpe", "spy_sharpe", "verdict"])
                        w.writerow([datetime.now(timezone.utc).isoformat(), f"adaptive:{variant}",
                                    f"{s1['cagr']:.4f}", f"{s1['sharpe']:.3f}", f"{bench['sharpe']:.3f}",
                                    "PASS" if ok else "FAIL"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
