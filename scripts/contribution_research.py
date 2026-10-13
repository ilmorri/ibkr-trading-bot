#!/usr/bin/env python3
"""Investing new money: right away, or saved for dips?

Usage:
    python scripts/contribution_research.py

PRE-REGISTERED 2026-10-13, committed before the first run. Do not change
rules, costs or the pass mark after seeing results.

Setup: start with EUR 300, add EUR 100 at the start of every month, never
sell. Whole shares are bought at the day's close; cash earns nothing.
Each purchase pays a fixed fee plus 5 bps slippage. At the end, 26% tax on
the gain (identical treatment for every rule, since none of them sells).

Rules
-----
  immediate   buy whenever cash >= the order threshold
  dip         keep contributions in cash; buy with all cash when the index
              closes at least 10% below its 252-day high, or when the oldest
              uninvested contribution is 12 months old
Thresholds: an order is placed only if its fee is at most 0.5% of it
(EUR 4 fee -> at least EUR 800; EUR 1.25 -> at least EUR 250).

Universes: the S&P 500 (SPY) 2001-2026, and the 19 country index funds in
data/holdout_indices/ (already used once, for Halloween, so this is their
second look).

Pass mark for "dip": its final after-tax wealth beats "immediate" on the
S&P 500 at EUR 4 AND in at least 60% of the country funds at EUR 4.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import index_tilt as it  # noqa: E402

START_CASH = 300.0
MONTHLY = 100.0
SLIPPAGE_BPS = 5.0
TAX = 0.26
MAX_FEE_SHARE = 0.005
DIP = 0.10
MAX_WAIT_MONTHS = 12


def simulate(df: pd.DataFrame, rule: str, fee: float) -> dict:
    close = df["close"].to_numpy()
    high252 = df["close"].rolling(252, min_periods=1).max().to_numpy()
    months = df.index.to_period("M")
    threshold = fee / MAX_FEE_SHARE
    cash, shares, paid_in, fees = START_CASH, 0, START_CASH, 0.0
    waiting_since = 0  # row of the oldest uninvested contribution
    orders = 0
    for i in range(len(df)):
        if i > 0 and months[i] != months[i - 1]:
            if cash < 1:
                waiting_since = i
            cash += MONTHLY
            paid_in += MONTHLY
        price = close[i]
        if rule == "immediate":
            go = cash >= threshold
        else:
            dip = price <= high252[i] * (1 - DIP)
            waited = (months[i] - months[waiting_since]).n >= MAX_WAIT_MONTHS
            go = cash >= threshold and (dip or waited)
        if go:
            n = int((cash - fee) / (price * (1 + SLIPPAGE_BPS / 1e4)))
            if n >= 1:
                cost = fee + n * price * SLIPPAGE_BPS / 1e4
                cash -= n * price + cost
                shares += n
                fees += cost
                orders += 1
                waiting_since = i
    value = cash + shares * close[-1]
    after_tax = value - TAX * max(0.0, value - paid_in)
    return {"paid_in": paid_in, "value": value, "after_tax": after_tax, "fees": fees, "orders": orders}


def main() -> int:
    spy = it.load(it.DATA_DIR / "SPY_daily.csv")
    print(f"S&P 500 {spy.index[0].date()} .. {spy.index[-1].date()}: EUR 300 + EUR 100/month, never sell\n")
    print(f"  {'fee':>5}  {'rule':<10}{'paid in':>10}{'value':>11}{'after tax':>11}{'fees':>8}{'orders':>8}")
    spy_dip_wins = None
    for fee in (1.25, 4.0):
        res = {rule: simulate(spy, rule, fee) for rule in ("immediate", "dip")}
        for rule, r in res.items():
            print(f"  {fee:>5.2f}  {rule:<10}{r['paid_in']:>10,.0f}{r['value']:>11,.0f}{r['after_tax']:>11,.0f}{r['fees']:>8,.0f}{r['orders']:>8}")
        if fee == 4.0:
            spy_dip_wins = res["dip"]["after_tax"] > res["immediate"]["after_tax"]

    print("\nCountry index funds at EUR 4 (after-tax wealth; dip minus immediate):")
    wins, rows = 0, 0
    for sym in it.HOLDOUT_SYMBOLS:
        path = it.HOLDOUT_DIR / f"{sym}_daily.csv"
        if not path.exists():
            continue
        df = it.load(path)
        a, b = simulate(df, "immediate", 4.0), simulate(df, "dip", 4.0)
        diff = b["after_tax"] - a["after_tax"]
        wins += diff > 0
        rows += 1
        print(f"  {sym:<5} immediate {a['after_tax']:>9,.0f}   dip {b['after_tax']:>9,.0f}   {diff:>+8,.0f} ({diff / a['after_tax']:+.1%})")
    passed = spy_dip_wins and rows and wins / rows >= 0.6
    print(f"\n  dip beats immediate in {wins}/{rows} countries; on the S&P 500: {'yes' if spy_dip_wins else 'no'}"
          f"  -> dip {'PASS' if passed else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
