#!/usr/bin/env python3
"""Two very-low-turnover strategies, each with its own sealed holdout.

Usage:
    python scripts/low_turnover_research.py                       # discovery for both
    python scripts/low_turnover_research.py --holdout halloween   # one logged look (country funds)
    python scripts/low_turnover_research.py --holdout low_vol     # one logged look (B/C stocks)
    python scripts/low_turnover_research.py --dump-halloween FILE # S&P 500 run for the visualizer

PRE-REGISTERED 2026-10-13, committed before the first run. Neither holdout
had been downloaded or looked at when this file was committed. Do not change
rules, costs or pass marks after seeing results.

Why low turnover: every earlier strategy lost mainly to fixed per-order fees
(EUR 1.25-4 on Xetra, $0.35+ on US stocks) and to time out of the market.

halloween (index seasonality; Bouman & Jacobsen 2002)
  Hold the index from the first trading day of November (buy at that day's
  close) to the last trading day of April (sell at that day's close); cash
  from May to October. Two orders a year.
  Costs: EUR 4 per order + 5 bps slippage (EUR 1.25 also reported).
  Discovery: S&P 500 (SPY) to 2016-10-14, account EUR 3,000, costs DOUBLED:
    Sharpe above buy-and-hold's, CAGR above zero, at least 20 orders.
  Holdout: the 19 country index funds of index_tilt.py (still sealed), full
    history, normal costs: at least 60% with Sharpe above their own
    buy-and-hold and CAGR above zero.

low_vol (low-volatility anomaly; Baker, Bradley & Wurgler 2011)
  On the last trading day of each quarter, rank every stock with a full year
  of history by the standard deviation of its last 252 daily returns; hold
  the 5 lowest in equal slots of up to $600 (account $3,000, whole shares),
  bought at the next open. Stocks still in the 5 are kept, not resized.
  Costs: max($0.35, $0.0035/share) capped at 1%, + 5 bps slippage.
  Discovery: the stocks of adaptive_research.py's discovery universe,
    costs DOUBLED: Sharpe above the equal-weight buy-and-hold of the same
    universe, CAGR above zero, at least 20 trades.
  Holdout: the S&P 500 "B"/"C" stocks of stock_adaptive.py (still sealed),
    normal costs, same test.
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
import adaptive_research as ar  # noqa: E402
import index_tilt as it  # noqa: E402
import stock_adaptive as sa  # noqa: E402

DATA_DIR = ar.DATA_DIR
HOLDOUT_LOG = ar.HOLDOUT_LOG
SLIPPAGE_BPS = 5.0


# ----------------------------------------------------------------------
# halloween
# ----------------------------------------------------------------------
def halloween_in_market(index: pd.DatetimeIndex) -> np.ndarray:
    return np.isin(index.month, [11, 12, 1, 2, 3, 4])


def simulate_halloween(df: pd.DataFrame, equity0: float, fee: float, mult: float = 1.0):
    """Trades at the close of the first November day and of the last April day."""
    close = df["close"].to_numpy()
    inside = halloween_in_market(df.index)
    cash, units = equity0, 0.0
    values, exposure, orders = [], [], []
    for i in range(len(df)):
        want = bool(inside[i])
        holding = units > 0
        last_april_day = inside[i] and i + 1 < len(df) and not inside[i + 1]
        if want and not holding and not last_april_day:
            cost = (fee + cash * SLIPPAGE_BPS / 1e4) * mult
            units = (cash - cost) / close[i]
            orders.append({"date": str(df.index[i].date()), "from": 0.0, "to": 1.0,
                           "price": round(float(close[i]), 4), "reason": "November: into the market", "cost": round(cost, 2)})
            cash = 0.0
        elif holding and last_april_day:
            value = units * close[i]
            cost = (fee + value * SLIPPAGE_BPS / 1e4) * mult
            cash, units = value - cost, 0.0
            orders.append({"date": str(df.index[i].date()), "from": 1.0, "to": 0.0,
                           "price": round(float(close[i]), 4), "reason": "end of April: to cash", "cost": round(cost, 2)})
        values.append(cash + units * close[i])
        exposure.append(1.0 if units > 0 else 0.0)
    return pd.Series(values, index=df.index), pd.Series(exposure, index=df.index), orders


def eval_halloween(df, equity0, fee, mult):
    curve, exposure, orders = simulate_halloween(df, equity0, fee, mult)
    start = curve.index[0]
    hold = df["close"][df.index >= start]
    return it.stats(curve), it.stats(hold), len(orders), curve, exposure, orders


# ----------------------------------------------------------------------
# low_vol
# ----------------------------------------------------------------------
def quarter_end_rows(index: pd.DatetimeIndex) -> set[int]:
    q = index.to_period("Q")
    return {i for i in range(len(index) - 1) if q[i] != q[i + 1]}


def simulate_low_vol(bars: dict, spy: pd.DataFrame, mult: float, n_hold: int = 5,
                     equity0: float = 3000.0, slot_value: float = 600.0):
    dates = spy.index[spy.index >= ar.START]
    opens = pd.DataFrame({s: df["open"] for s, df in bars.items()}).reindex(dates)
    closes = pd.DataFrame({s: df["close"] for s, df in bars.items()}).reindex(dates)
    vol = closes.pct_change(fill_method=None).rolling(252, min_periods=252).std()
    rebalance = quarter_end_rows(dates)
    cash, holdings, trades, values = equity0, {}, 0, []
    pending = None
    o, c, v = opens.to_numpy(), closes.to_numpy(), vol.to_numpy()
    cols = list(closes.columns)

    def fee_for(shares, price):
        value = shares * price
        return (min(max(0.35, 0.0035 * shares), 0.01 * value) + value * SLIPPAGE_BPS / 1e4) * mult

    for row in range(len(dates)):
        if pending is not None:
            top, pending = pending
            for sym in [s for s in holdings if s not in top]:
                k = cols.index(sym)
                price = o[row, k]
                if math.isnan(price):
                    continue
                shares = holdings.pop(sym)
                cash += shares * price - fee_for(shares, price)
                trades += 1
            for sym in pending:
                if len(holdings) >= n_hold:
                    break
                if sym in holdings:
                    continue
                k = cols.index(sym)
                price = o[row, k]
                if math.isnan(price):
                    continue
                shares = int(min(slot_value, cash) // price)
                if shares < 1 or shares * price < ar.MIN_POSITION:
                    continue
                cost = fee_for(shares, price)
                if shares * price + cost > cash:
                    continue
                cash -= shares * price + cost
                holdings[sym] = shares
                trades += 1
            pending = None
        values.append(cash + sum(sh * c[row, cols.index(s)] for s, sh in holdings.items()
                                 if not math.isnan(c[row, cols.index(s)])))
        if row in rebalance:
            ranked = sorted((v[row, k], cols[k]) for k in range(len(cols))
                            if not math.isnan(v[row, k]) and not math.isnan(c[row, k]))
            top = {s for _, s in ranked[:n_hold]}
            keep = [s for s in top if s in holdings]
            # Sell anything outside the top n; fill free slots down the ranking (whole shares may skip some).
            pending = (top, keep + [s for _, s in ranked if s not in keep][: n_hold * 3])
    return pd.Series(values, index=dates), trades


# ----------------------------------------------------------------------
def log_holdout(name, cagr, sharpe, bench_sharpe, passed):
    new = not HOLDOUT_LOG.exists()
    with HOLDOUT_LOG.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "strategy", "cagr", "sharpe", "spy_sharpe", "verdict"])
        w.writerow([datetime.now(timezone.utc).isoformat(), name, f"{cagr:.4f}", f"{sharpe:.3f}",
                    f"{bench_sharpe:.3f}", "PASS" if passed else "FAIL"])


def already_looked(name) -> bool:
    return HOLDOUT_LOG.exists() and any(r["strategy"] == name for r in csv.DictReader(HOLDOUT_LOG.open()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--holdout", choices=["halloween", "low_vol"])
    parser.add_argument("--dump-halloween", type=Path)
    args = parser.parse_args()

    spy = it.load(DATA_DIR / "SPY_daily.csv")
    disc_spy = it.window(spy, it.DISCOVERY_END)

    # ---- halloween discovery
    print(f"HALLOWEEN discovery: S&P 500 {disc_spy.index[0].date()} .. {disc_spy.index[-1].date()}")
    print(f"  {'account':>8}{'fee':>7}{'orders':>8}{'CAGR':>7}{'@2x':>7}{'Sharpe':>8}{'@2x':>6}{'maxDD':>8}   hold: CAGR / Sharpe / maxDD")
    halloween_pass = None
    for equity0 in it.ACCOUNTS:
        for fee in it.FEES:
            s1, h, n, *_ = eval_halloween(disc_spy, equity0, fee, 1.0)
            s2, *_ = eval_halloween(disc_spy, equity0, fee, 2.0)
            mark = ""
            if (equity0, fee) == it.JUDGE:
                halloween_pass = n >= 20 and s2["sharpe"] > h["sharpe"] and s2["cagr"] > 0
                mark = "  <- judged: " + ("PASS" if halloween_pass else "FAIL")
            print(f"  {equity0:>8,.0f}{fee:>7.2f}{n:>8}{s1['cagr']:>7.1%}{s2['cagr']:>7.1%}{s1['sharpe']:>8.2f}{s2['sharpe']:>6.2f}"
                  f"{s1['max_dd']:>8.1%}   {h['cagr']:.1%} / {h['sharpe']:.2f} / {h['max_dd']:.1%}{mark}")

    # ---- low_vol discovery
    disc_bars = ar.load_universe(False)
    s1c, n1 = simulate_low_vol(disc_bars, spy, 1.0)
    s2c, _ = simulate_low_vol(disc_bars, spy, 2.0)
    s1, s2 = it.stats(s1c), it.stats(s2c)
    bench = ar.equal_weight_hold(disc_bars, spy)
    low_vol_pass = n1 >= 20 and s2["sharpe"] > bench["sharpe"] and s2["cagr"] > 0
    print(f"\nLOW_VOL discovery: {len(disc_bars)} stocks, {ar.START.date()} .. {spy.index[-1].date()}, $3,000, 5 stocks")
    print(f"  trades {n1}  CAGR {s1['cagr']:.1%} (@2x {s2['cagr']:.1%})  Sharpe {s1['sharpe']:.2f} (@2x {s2['sharpe']:.2f})  "
          f"maxDD {s1['max_dd']:.1%}   equal-weight hold: {bench['cagr']:.1%} / {bench['sharpe']:.2f} / {bench['max_dd']:.1%}"
          f"  -> {'PASS' if low_vol_pass else 'FAIL'}")

    if args.dump_halloween:
        s1, h, n, curve, exposure, orders = eval_halloween(spy, *it.JUDGE, 1.0)
        hold_curve = spy["close"] / spy["close"].iloc[0] * it.JUDGE[0]
        payload = {
            "kind": "tilt",
            "title": "Halloween seasonality on the S&P 500 (SPYL stand-in) - full history",
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "account": {"equity": it.JUDGE[0], "fee": it.JUDGE[1]},
            "note": f"Discovery ends {it.DISCOVERY_END.date()}",
            "dates": [str(d.date()) for d in curve.index],
            "equity": [round(float(x), 2) for x in curve],
            "benchmark": [round(float(x), 2) for x in hold_curve],
            "market_ok": [True] * len(curve),
            "stats": {"strategy": {k: round(float(v), 4) for k, v in it.stats(curve).items()},
                      "benchmark": {k: round(float(v), 4) for k, v in it.stats(hold_curve).items()}},
            "mode_names": {"3": "Invested (November to April)"},
            "stocks": {"S&P 500": {
                "close": [round(float(x), 4) for x in spy["close"]],
                "mode": [3 if x > 0 else None for x in exposure],
                "exposure": [float(x) for x in exposure],
            }},
            "orders": orders,
            "trades": [],
        }
        args.dump_halloween.parent.mkdir(parents=True, exist_ok=True)
        args.dump_halloween.write_text(json.dumps(payload, separators=(",", ":")))
        print(f"\nwrote {args.dump_halloween}")

    # ---- holdouts
    if args.holdout == "halloween":
        if not halloween_pass:
            raise SystemExit("\nhalloween did not pass discovery; the holdout stays sealed.")
        if already_looked("halloween"):
            raise SystemExit("halloween already had its holdout look.")
        rows = []
        for sym in it.HOLDOUT_SYMBOLS:
            path = it.HOLDOUT_DIR / f"{sym}_daily.csv"
            if path.exists():
                df = it.load(path)
                s1, h, n, *_ = eval_halloween(df, *it.JUDGE, 1.0)
                rows.append((sym, df.index[0].date(), n, s1, h, s1["sharpe"] > h["sharpe"] and s1["cagr"] > 0))
        print(f"\nHALLOWEEN HOLDOUT: {len(rows)} country funds, EUR 3,000 at EUR 4")
        for sym, start, n, s1, h, ok in rows:
            print(f"  {sym:<5} from {start}  orders {n:>3}  CAGR {s1['cagr']:6.1%}  Sharpe {s1['sharpe']:5.2f}  maxDD {s1['max_dd']:6.1%}"
                  f"   hold {h['cagr']:6.1%} / {h['sharpe']:5.2f} / {h['max_dd']:6.1%}  {'beats' if ok else '-'}")
        wins = sum(r[-1] for r in rows)
        passed = bool(rows) and wins / len(rows) >= 0.6
        med = lambda key, i: float(np.median([r[i][key] for r in rows]))  # noqa: E731
        print(f"  {wins}/{len(rows)} beat their own buy-and-hold on Sharpe -> {'PASS' if passed else 'FAIL'} (needs 60%)")
        print(f"  median CAGR {med('cagr', 3):.1%} vs hold {med('cagr', 4):.1%}; median maxDD {med('max_dd', 3):.1%} vs hold {med('max_dd', 4):.1%}")
        log_holdout("halloween", med("cagr", 3), med("sharpe", 3), med("sharpe", 4), passed)

    if args.holdout == "low_vol":
        if not low_vol_pass:
            raise SystemExit("\nlow_vol did not pass discovery; the holdout stays sealed.")
        if already_looked("low_vol"):
            raise SystemExit("low_vol already had its holdout look.")
        held = {s: df for s, df in sa.load_universe(True).items() if s not in disc_bars}
        c1, n1 = simulate_low_vol(held, spy, 1.0)
        s1 = it.stats(c1)
        bench = ar.equal_weight_hold(held, spy)
        passed = n1 >= 20 and s1["sharpe"] > bench["sharpe"] and s1["cagr"] > 0
        print(f"\nLOW_VOL HOLDOUT: {len(held)} B/C stocks  trades {n1}  CAGR {s1['cagr']:.1%}  Sharpe {s1['sharpe']:.2f}  "
              f"maxDD {s1['max_dd']:.1%}   equal-weight hold {bench['cagr']:.1%} / {bench['sharpe']:.2f} / {bench['max_dd']:.1%}"
              f"  -> {'PASS' if passed else 'FAIL'}")
        log_holdout("low_vol", s1["cagr"], s1["sharpe"], bench["sharpe"], passed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
