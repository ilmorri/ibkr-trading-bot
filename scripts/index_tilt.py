#!/usr/bin/env python3
"""Core-plus-tactical: hold an index, tilt how much of it by volatility, trend and dips.

Usage:
    python scripts/index_tilt.py                    # discovery (S&P 500, 2001-2016)
    python scripts/index_tilt.py --holdout          # one logged look at the country-index holdout
    python scripts/index_tilt.py --dump FILE        # write the S&P 500 run (EUR 3,000, EUR 4) for the visualizer

PRE-REGISTERED 2026-10-13, committed before the first run and before the
holdout data was downloaded. Do not change rules, thresholds, costs or pass
marks after seeing results.

Instrument
----------
Live it would trade SPYL on Xetra (EUR), which the live account can trade.
SPYL tracks the S&P 500, so SPY's daily bars stand in for it. The test runs in
the index's own currency; EUR/USD moves affect strategy and buy-and-hold alike.

Rule (evaluated on each day's close; the order fills at the next day's close)
-----------------------------------------------------------------------------
  volatility   vol20 = standard deviation of the last 20 daily returns x sqrt(252)
               scale = min(1, max(0.25, 0.15 / vol20))
  trend        x 0.5 while the close is below its 200-day average
  dip          if the close is above its 200-day average and RSI(2) < 10: 100%
  exposure     the result rounded to the nearest of 25 / 50 / 75 / 100%
  orders       only when the rounded exposure changes; the rest is cash (earns 0)

Costs: a fixed fee per order plus 5 bps slippage on the traded amount.
  fee scenarios  EUR 1.25 (tiered minimum) and EUR 4 (paper-account quote)
  accounts       EUR 300 (the live plan) and EUR 3,000

Pass marks
----------
  discovery  S&P 500 from the first valid signal to 2016-10-14, account
             EUR 3,000 at EUR 4 per order with costs DOUBLED: Sharpe above
             buy-and-hold's, CAGR above zero, at least 20 orders
  holdout    HOLDOUT_SYMBOLS (country index funds, full history, never looked
             at before), EUR 3,000 at EUR 4, normal costs: at least 60% of the
             countries with Sharpe above their own buy-and-hold and CAGR above
             zero. One look, logged in data/holdout_log.csv.

A higher Sharpe than buy-and-hold with a lower CAGR means less risk per unit
of return, not more money; the report shows both.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HOLDOUT_DIR = DATA_DIR / "holdout_indices"
HOLDOUT_LOG = DATA_DIR / "holdout_log.csv"
DISCOVERY_END = pd.Timestamp("2016-10-14")

HOLDOUT_SYMBOLS = [
    "EWJ", "EWG", "EWU", "EWC", "EWA", "EWZ", "EWW", "EWS", "EWH", "EWT",
    "EWY", "EWI", "EWP", "EWQ", "EWL", "EWN", "EWD", "EWK", "EWO",
]

TARGET_VOL = 0.15
LEVELS = np.array([0.25, 0.5, 0.75, 1.0])
SLIPPAGE_BPS = 5.0
FEES = [1.25, 4.0]
ACCOUNTS = [300.0, 3000.0]
JUDGE = (3000.0, 4.0)


def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return df[["open", "high", "low", "close"]].astype(float)


def rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(gain.notna())


def target_exposure(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    ret = close.pct_change()
    vol20 = ret.rolling(20).std() * math.sqrt(252)
    scale = (TARGET_VOL / vol20).clip(lower=0.25, upper=1.0)
    sma200 = close.rolling(200).mean()
    above = close > sma200
    raw = scale * np.where(above, 1.0, 0.5)
    dip = above & (rsi(close, 2) < 10)
    raw = raw.where(~dip, 1.0)
    valid = sma200.notna() & vol20.notna()
    level = pd.Series(np.nan, index=df.index)
    level[valid] = [LEVELS[np.abs(LEVELS - x).argmin()] for x in raw[valid]]
    reason = pd.Series("", index=df.index)
    reason[valid & dip] = "dip"
    reason[valid & ~dip & ~above] = "below 200-day average"
    reason[valid & ~dip & above & (scale < 1)] = "high volatility"
    reason[valid & ~dip & above & (scale >= 1)] = "calm uptrend"
    return pd.DataFrame({"level": level, "reason": reason, "vol20": vol20, "sma200": sma200})


def simulate(df: pd.DataFrame, equity0: float, fee: float, cost_mult: float = 1.0):
    """Daily account values; exposure decided at close t, traded at close t+1."""
    sig = target_exposure(df)
    close = df["close"].to_numpy()
    level = sig["level"].to_numpy()
    first = int(np.argmax(~np.isnan(level)))
    cash, units = equity0, 0.0
    current = 0.0
    values, exposure, orders = [], [], []
    for i in range(first + 1, len(df)):
        want = level[i - 1]
        if not math.isnan(want) and want != current:
            value = cash + units * close[i]
            target_units = value * want / close[i]
            traded = abs(target_units - units) * close[i]
            cost = (fee + traded * SLIPPAGE_BPS / 1e4) * cost_mult
            cash += (units - target_units) * close[i] - cost
            orders.append({"date": str(df.index[i].date()), "from": current, "to": float(want),
                           "price": round(float(close[i]), 4), "reason": sig["reason"].iloc[i - 1],
                           "cost": round(cost, 2)})
            units, current = target_units, float(want)
        values.append(cash + units * close[i])
        exposure.append(current)
    idx = df.index[first + 1:]
    return pd.Series(values, index=idx), pd.Series(exposure, index=idx), orders


def stats(curve: pd.Series) -> dict:
    ret = curve.pct_change().dropna()
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    return {
        "cagr": (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1,
        "sharpe": ret.mean() / ret.std() * math.sqrt(252) if ret.std() > 0 else 0.0,
        "max_dd": (curve / curve.cummax() - 1).min(),
    }


def window(df: pd.DataFrame, end: pd.Timestamp | None) -> pd.DataFrame:
    return df[df.index <= end] if end is not None else df


def evaluate(df: pd.DataFrame, equity0: float, fee: float, mult: float):
    curve, exposure, orders = simulate(df, equity0, fee, mult)
    hold = df["close"].reindex(curve.index)
    return stats(curve), stats(hold), len(orders), exposure.mean(), curve, exposure, orders


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--dump", type=Path)
    args = parser.parse_args()

    spy = load(DATA_DIR / "SPY_daily.csv")
    disc = window(spy, DISCOVERY_END)

    # ---------------------------------------------------------------- discovery
    print(f"DISCOVERY: S&P 500 (SPY) {disc.index[0].date()} .. {disc.index[-1].date()}\n")
    print(f"  {'account':>8}{'fee':>7}{'orders':>8}{'avg exp':>9}{'CAGR':>7}{'@2x':>7}{'Sharpe':>8}{'@2x':>6}{'maxDD':>8}   hold: CAGR / Sharpe / maxDD")
    judged = None
    for equity0 in ACCOUNTS:
        for fee in FEES:
            s1, h, n, avg, *_ = evaluate(disc, equity0, fee, 1.0)
            s2, *_ = evaluate(disc, equity0, fee, 2.0)
            mark = ""
            if (equity0, fee) == JUDGE:
                judged = n >= 20 and s2["sharpe"] > h["sharpe"] and s2["cagr"] > 0
                mark = "  <- judged: " + ("PASS" if judged else "FAIL")
            print(f"  {equity0:>8,.0f}{fee:>7.2f}{n:>8}{avg:>9.0%}{s1['cagr']:>7.1%}{s2['cagr']:>7.1%}{s1['sharpe']:>8.2f}"
                  f"{s2['sharpe']:>6.2f}{s1['max_dd']:>8.1%}   {h['cagr']:.1%} / {h['sharpe']:.2f} / {h['max_dd']:.1%}{mark}")

    if args.dump:
        s1, h, n, avg, curve, exposure, orders = evaluate(spy, *JUDGE, 1.0)
        hold_curve = spy["close"].reindex(curve.index) / spy["close"].reindex(curve.index).iloc[0] * JUDGE[0]
        level_code = {0.25: 0, 0.5: 1, 0.75: 2, 1.0: 3}
        payload = {
            "kind": "tilt",
            "title": "Index tilt on the S&P 500 (SPYL stand-in) - full history",
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "account": {"equity": JUDGE[0], "fee": JUDGE[1]},
            "note": f"Discovery ends {DISCOVERY_END.date()}; later years overlap periods already examined in earlier research.",
            "dates": [str(d.date()) for d in curve.index],
            "equity": [round(float(x), 2) for x in curve],
            "benchmark": [round(float(x), 2) for x in hold_curve],
            "market_ok": [bool(x) for x in (spy["close"] > spy["close"].rolling(200).mean()).reindex(curve.index)],
            "stats": {"strategy": {k: round(float(v), 4) for k, v in stats(curve).items()},
                      "benchmark": {k: round(float(v), 4) for k, v in stats(hold_curve).items()}},
            "mode_names": {"0": "25% invested", "1": "50% invested", "2": "75% invested", "3": "100% invested"},
            "stocks": {"S&P 500": {
                "close": [round(float(x), 4) for x in spy["close"].reindex(curve.index)],
                "mode": [level_code.get(float(x)) for x in exposure],
                "exposure": [float(x) for x in exposure],
            }},
            "orders": orders,
            "trades": [],
        }
        args.dump.parent.mkdir(parents=True, exist_ok=True)
        args.dump.write_text(json.dumps(payload, separators=(",", ":")))
        print(f"\nwrote {args.dump}")

    if not args.holdout:
        return 0

    # ---------------------------------------------------------------- holdout
    if not judged:
        raise SystemExit("\nDid not pass discovery; the holdout stays sealed.")
    if HOLDOUT_LOG.exists() and any(r["strategy"] == "index_tilt" for r in csv.DictReader(HOLDOUT_LOG.open())):
        raise SystemExit(f"index_tilt already had its holdout look; see {HOLDOUT_LOG}")

    rows = []
    for sym in HOLDOUT_SYMBOLS:
        path = HOLDOUT_DIR / f"{sym}_daily.csv"
        if not path.exists():
            continue
        df = load(path)
        s1, h, n, avg, *_ = evaluate(df, *JUDGE, 1.0)
        ok = s1["sharpe"] > h["sharpe"] and s1["cagr"] > 0
        rows.append((sym, df.index[0].date(), n, avg, s1, h, ok))

    print(f"\nHOLDOUT: {len(rows)} country index funds, EUR 3,000 at EUR 4 per order\n")
    print(f"  {'fund':<6}{'from':<12}{'orders':>7}{'avg exp':>9}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>8}   hold: CAGR / Sharpe / maxDD")
    for sym, start, n, avg, s1, h, ok in rows:
        print(f"  {sym:<6}{str(start):<12}{n:>7}{avg:>9.0%}{s1['cagr']:>7.1%}{s1['sharpe']:>8.2f}{s1['max_dd']:>8.1%}"
              f"   {h['cagr']:.1%} / {h['sharpe']:.2f} / {h['max_dd']:.1%}  {'beats' if ok else '-'}")
    wins = sum(r[-1] for r in rows)
    passed = rows and wins / len(rows) >= 0.6
    print(f"\n  {wins}/{len(rows)} beat their own buy-and-hold on Sharpe -> {'PASS' if passed else 'FAIL'} (needs 60%)")
    print(f"  median CAGR: strategy {np.median([r[4]['cagr'] for r in rows]):.1%}, hold {np.median([r[5]['cagr'] for r in rows]):.1%}")
    print(f"  median maxDD: strategy {np.median([r[4]['max_dd'] for r in rows]):.1%}, hold {np.median([r[5]['max_dd'] for r in rows]):.1%}")

    new = not HOLDOUT_LOG.exists()
    with HOLDOUT_LOG.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "strategy", "cagr", "sharpe", "spy_sharpe", "verdict"])
        w.writerow([datetime.now(timezone.utc).isoformat(), "index_tilt",
                    f"{np.median([r[4]['cagr'] for r in rows]):.4f}", f"{np.median([r[4]['sharpe'] for r in rows]):.3f}",
                    f"{np.median([r[5]['sharpe'] for r in rows]):.3f}", "PASS" if passed else "FAIL"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
