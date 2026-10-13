#!/usr/bin/env python3
"""Per-stock adaptive strategy: each stock gets the approach that fits it.

Usage:
    python scripts/stock_adaptive.py                 # discovery universe
    python scripts/stock_adaptive.py --holdout       # one logged look at the B/C holdout
    python scripts/stock_adaptive.py --dump FILE     # also write the run (account B) as JSON for the visualizer

PRE-REGISTERED 2026-10-13, committed before the first run and before the
holdout data was downloaded. Do not change rules, thresholds, costs or pass
marks after seeing results.

The idea
--------
Instead of one rule for every stock, the bot profiles each stock every day
from the past year and picks a mode for it:

  character  variance ratio VR = var(10-day returns) / (10 x var(1-day returns))
             over the last 252 days. VR > 1: moves tend to continue
             (trending); VR < 1: moves tend to reverse (mean-reverting).
  evidence   net return of each component's shadow trades ON THIS STOCK that
             closed in the last 252 trading days.

  mode       trend   if VR > 1.10 and the trend component's evidence > 0
             revert  if VR < 0.90 and the revert component's evidence > 0
             cash    otherwise (stand aside on this stock)

Components are those of adaptive_research.py (trend breakout; RSI(2) dip
buying above the 200-day average). Market filter: no new entries while SPY is
below its 200-day average.

Sizing adapts to volatility: each position risks 1% of current equity between
entry and stop, capped by the slot size and available cash; a position under
the $100 minimum is skipped. Free slots go to the candidates with the strongest
evidence (largest trailing shadow return on that stock).

Accounts, costs and pass marks are those of adaptive_research.py: judged on
account B ($3,000, 5 slots of up to $600) against an equal-weight buy-and-hold
of the same universe; discovery at doubled costs, holdout at normal costs; at
least 30 trades.

Holdout universe: S&P 500 members whose ticker starts with "B" or "C",
excluding any already in the discovery data. Stored in data/holdout_stocks_bc/.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adaptive_research import (  # noqa: E402
    ACCOUNTS,
    COMPONENTS,
    DATA_DIR,
    HOLDOUT_LOG,
    MIN_POSITION,
    NOT_STOCKS,
    SLIPPAGE_BPS,
    START,
    equal_weight_hold,
    load,
    order_cost,
    signals,
    stats,
)

HOLDOUT_DIR = DATA_DIR / "holdout_stocks_bc"
HOLDOUT_SYMBOLS = [
    "BALL", "BAX", "BBY", "BDX", "BEN", "BG", "BIIB", "BK", "BKNG", "BKR", "BLDR", "BLK",
    "BMY", "BR", "BRO", "BSX", "BX", "BXP", "CAG", "CAH", "CARR", "CAT", "CB", "CBOE",
    "CBRE", "CCI", "CDNS", "CDW", "CEG", "CF", "CFG", "CHD", "CHRW", "CHTR", "CI", "CINF",
    "CL", "CLX", "CME", "CMG", "CMI", "CMS", "CNC", "CNP", "COF", "COO", "COP", "COR",
    "COST", "CPAY", "CPB", "CPRT", "CPT", "CRL", "CRM", "CSGP", "CSX", "CTAS", "CTRA",
    "CTSH", "CTVA", "CVX",
]

VR_TREND = 1.10
VR_REVERT = 0.90
LOOKBACK = 252
RISK_PCT = 1.0
MODE_CODES = {"cash": 0, "trend": 1, "revert": 2}


def variance_ratio(close: pd.Series, q: int = 10, window: int = LOOKBACK) -> pd.Series:
    r1 = np.log(close).diff()
    rq = np.log(close).diff(q)
    return rq.rolling(window).var() / (q * r1.rolling(window).var())


def shadow_by_stock(df: pd.DataFrame, s: pd.DataFrame) -> dict[str, pd.Series]:
    """Per component, a daily series of shadow-trade net returns booked on their exit day."""
    o, lo = df["open"].to_numpy(), df["low"].to_numpy()
    out = {}
    for name, comp in COMPONENTS.items():
        booked = pd.Series(0.0, index=df.index)
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
                booked.iloc[j] += price / e - 1 - 2 * SLIPPAGE_BPS / 1e4 - 0.002
                i = j + 1
            else:
                i += 1
        out[name] = booked
    return out


def profile(df: pd.DataFrame) -> pd.DataFrame:
    """Signals plus the per-stock mode and evidence, all known at each day's close."""
    s = signals(df)
    s["vr"] = variance_ratio(df["close"])
    shadow = shadow_by_stock(df, s)
    for name in COMPONENTS:
        s[f"{name}_evidence"] = shadow[name].rolling(LOOKBACK, min_periods=LOOKBACK).sum()
    mode = pd.Series("cash", index=df.index)
    mode[(s["vr"] > VR_TREND) & (s["trend_evidence"] > 0)] = "trend"
    mode[(s["vr"] < VR_REVERT) & (s["revert_evidence"] > 0)] = "revert"
    s["mode"] = mode.map(MODE_CODES)
    return s


def simulate(bars, profiles, spy, account, cost_mult):
    equity0, slots, slot_value = account
    dates = spy.index[spy.index >= START]
    spy_ok = (spy["close"] > spy["close"].rolling(200).mean()).reindex(dates).fillna(False).to_numpy(bool)
    frames = {s: {k: v.to_numpy(float) for k, v in df.reindex(dates).items()} for s, df in bars.items()}
    prof = {s: {k: v.to_numpy(float) for k, v in p.reindex(dates).astype(float).items()} for s, p in profiles.items()}

    cash, positions, trades, curve, costs = equity0, {}, [], [], 0.0
    code_to_mode = {v: k for k, v in MODE_CODES.items()}

    def close_position(sym, row, price, reason):
        nonlocal cash, costs
        p = positions.pop(sym)
        fee = order_cost(p["shares"], price, cost_mult)
        cash += p["shares"] * price - fee
        costs += fee
        trades.append({
            "symbol": sym, "component": p["component"], "entry_date": str(dates[p["start"]].date()),
            "exit_date": str(dates[row].date()), "entry": round(p["entry"], 4), "exit": round(price, 4),
            "shares": p["shares"], "reason": reason,
            "pnl": round((price - p["entry"]) * p["shares"] - p["fee_in"] - fee, 2),
        })

    for row in range(1, len(dates)):
        for sym in list(positions):
            p, f = positions[sym], frames[sym]
            if math.isnan(f["open"][row]):
                continue
            comp = COMPONENTS[p["component"]]
            if f["open"][row] <= p["stop"]:
                close_position(sym, row, f["open"][row], "stop_gap")
            elif prof[sym][comp["exit"]][row - 1] == 1.0:
                close_position(sym, row, f["open"][row], "signal")
            elif comp["max_hold"] and row - p["start"] >= comp["max_hold"]:
                close_position(sym, row, f["open"][row], "max_hold")

        if len(positions) < slots and spy_ok[row - 1]:
            equity_now = curve[-1] if curve else equity0
            candidates = []
            for sym, s in prof.items():
                r = row - 1
                if sym in positions or math.isnan(frames[sym]["open"][row]) or math.isnan(s["mode"][r]):
                    continue
                mode = code_to_mode[int(s["mode"][r])]
                if mode == "cash":
                    continue
                comp = COMPONENTS[mode]
                if s[comp["entry"]][r] != 1.0 or math.isnan(s[comp["stop"]][r]):
                    continue
                candidates.append((s[f"{mode}_evidence"][r], sym, mode, s[comp["stop"]][r]))
            candidates.sort(key=lambda x: (-x[0], x[1]))

            for _, sym, mode, stop_dist in candidates:
                if len(positions) >= slots:
                    break
                price = frames[sym]["open"][row]
                risk_budget = equity_now * RISK_PCT / 100
                value_by_risk = risk_budget / (stop_dist / price) if stop_dist > 0 else 0
                budget = min(slot_value, cash, value_by_risk)
                shares = int(budget // price)
                if shares < 1 or shares * price < MIN_POSITION:
                    continue
                fee = order_cost(shares, price, cost_mult)
                if shares * price + fee > cash:
                    continue
                cash -= shares * price + fee
                costs += fee
                positions[sym] = {"component": mode, "shares": shares, "entry": price,
                                  "stop": price - stop_dist, "start": row, "fee_in": fee}

        for sym in list(positions):
            p, low = positions[sym], frames[sym]["low"][row]
            if not math.isnan(low) and low <= p["stop"]:
                close_position(sym, row, p["stop"], "stop")

        curve.append(cash + sum(p["shares"] * frames[s]["close"][row] for s, p in positions.items()
                                if not math.isnan(frames[s]["close"][row])))

    return pd.Series(curve, index=dates[1:]), trades, costs


def load_universe(holdout: bool) -> dict:
    if holdout:
        files = [HOLDOUT_DIR / f"{s}_daily.csv" for s in HOLDOUT_SYMBOLS]
        files = [f for f in files if f.exists()]
    else:
        files = [f for f in sorted(DATA_DIR.glob("*_daily.csv")) if f.name.replace("_daily.csv", "") not in NOT_STOCKS]
    return {f.name.replace("_daily.csv", ""): load(f) for f in files}


def passes(trades, s, bench) -> bool:
    return len(trades) >= 30 and s["sharpe"] > bench["sharpe"] and s["cagr"] > 0


def dump(path: Path, label, bars, profiles, spy, curve, trades, bench_curve, result, bench):
    dates = curve.index
    stocks = {}
    for sym, df in bars.items():
        close = df["close"].reindex(dates)
        mode = profiles[sym]["mode"].reindex(dates)
        stocks[sym] = {
            "close": [None if math.isnan(x) else round(float(x), 4) for x in close],
            "mode": [None if math.isnan(x) else int(x) for x in mode],
        }
    spy_ok = (spy["close"] > spy["close"].rolling(200).mean()).reindex(dates).fillna(False)
    payload = {
        "title": f"Per-stock adaptive strategy - {label}",
        "universe": label,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": {"equity": ACCOUNTS["B"][0], "slots": ACCOUNTS["B"][1], "slot_value": ACCOUNTS["B"][2]},
        "dates": [str(d.date()) for d in dates],
        "equity": [round(float(x), 2) for x in curve],
        "benchmark": [round(float(x), 2) for x in bench_curve.reindex(dates).ffill()],
        "market_ok": [bool(x) for x in spy_ok],
        "stats": {"strategy": result, "benchmark": bench},
        "modes": {str(v): k for k, v in MODE_CODES.items()},
        "stocks": stocks,
        "trades": trades,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--dump", type=Path)
    args = parser.parse_args()

    spy = load(DATA_DIR / "SPY_daily.csv")
    discovery = load_universe(False)
    label = "discovery"
    bars = discovery

    if args.holdout:
        name = "stock_adaptive"
        if HOLDOUT_LOG.exists() and any(r["strategy"] == name for r in csv.DictReader(HOLDOUT_LOG.open())):
            raise SystemExit(f"{name} already had its holdout look; see {HOLDOUT_LOG}")
        dprof = {s: profile(df) for s, df in discovery.items()}
        d2_curve, d2_trades, _ = simulate(discovery, dprof, spy, ACCOUNTS["B"], 2.0)
        if not passes(d2_trades, stats(d2_curve), equal_weight_hold(discovery, spy)):
            raise SystemExit("stock_adaptive did not pass discovery; the holdout stays sealed.")
        bars = {s: df for s, df in load_universe(True).items() if s not in discovery}
        label = "holdout"

    profiles = {s: profile(df) for s, df in bars.items()}
    bench = equal_weight_hold(bars, spy)
    print(f"{label.upper()}: {len(bars)} stocks, {START.date()} .. {spy.index[-1].date()}")
    print(f"  equal-weight hold  CAGR {bench['cagr']:6.1%}  Sharpe {bench['sharpe']:.2f}  maxDD {bench['max_dd']:6.1%}\n")

    header = f"  {'acct':>5}{'trades':>7}{'CAGR':>7}{'@2x':>7}{'Sharpe':>8}{'@2x':>6}{'maxDD':>8}{'end $':>9}{'costs':>8}  verdict"
    print(header)
    for account in ("B", "A"):
        c1, t1, cost1 = simulate(bars, profiles, spy, ACCOUNTS[account], 1.0)
        c2, t2, _ = simulate(bars, profiles, spy, ACCOUNTS[account], 2.0)
        s1, s2 = stats(c1), stats(c2)
        ok = passes(t2, s2, bench) if label == "discovery" else passes(t1, s1, bench)
        verdict = ("PASS" if ok else "FAIL") if account == "B" else "(info)"
        comps = pd.Series([t["component"] for t in t1]).value_counts().to_dict()
        print(f"  {account:>5}{len(t1):>7}{s1['cagr']:>7.1%}{s2['cagr']:>7.1%}{s1['sharpe']:>8.2f}{s2['sharpe']:>6.2f}"
              f"{s1['max_dd']:>8.1%}{s1['end']:>9,.0f}{cost1:>8,.0f}  {verdict}  {comps}")

        if account == "B":
            if label == "holdout":
                new = not HOLDOUT_LOG.exists()
                with HOLDOUT_LOG.open("a", newline="") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["time", "strategy", "cagr", "sharpe", "spy_sharpe", "verdict"])
                    w.writerow([datetime.now(timezone.utc).isoformat(), "stock_adaptive", f"{s1['cagr']:.4f}",
                                f"{s1['sharpe']:.3f}", f"{bench['sharpe']:.3f}", "PASS" if ok else "FAIL"])
            if args.dump:
                closes = pd.DataFrame({s: df["close"] for s, df in bars.items()}).reindex(c1.index)
                eligible = closes.columns[closes.iloc[0].notna()]
                bench_curve = (closes[eligible] / closes[eligible].iloc[0]).mean(axis=1) * ACCOUNTS["B"][0]
                clean = lambda d: {k: round(float(v), 4) for k, v in d.items()}  # noqa: E731
                dump(args.dump, label, bars, profiles, spy, c1, t1, bench_curve, clean(s1), clean(bench))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
