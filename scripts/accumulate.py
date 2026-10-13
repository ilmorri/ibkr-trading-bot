#!/usr/bin/env python3
"""Accumulation bot: buy SPYL on Xetra with spare EUR cash; never sell.

Usage:
    python scripts/accumulate.py                       # paper account, preview only
    python scripts/accumulate.py --execute             # paper account, place the order
    python scripts/accumulate.py --live                # LIVE account, preview only
    python scripts/accumulate.py --live --execute      # LIVE account, place the order

Why this and not a trading strategy: in the research of 2026-10-13 nothing
beat buying and holding after fees and Italian tax (see docs/RESEARCH.md).
This bot does the part that historically worked, cheaply:

  - it buys only when the order is big enough that the fee is at most
    `max_fee_share` of it (EUR 4 fee -> orders of at least EUR 800)
  - it never sells, so the 26% tax is paid once, at the end, not every year
  - whole shares, a limit order a little above the estimated price, DAY only

Pricing: the live account has no Xetra market data. The EUR price is estimated
from SPYL's London price (USD, free delayed data) divided by EUR/USD, and the
limit is set `limit_buffer` above it. A limit that is too low simply does not
fill and the bot tries again next run.

Settings come from the `accumulate` section of config/config.yaml (defaults below).
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ib_insync import IB, Forex, LimitOrder, Stock  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.logger import get_logger  # noqa: E402

logger = get_logger("accumulate")

DEFAULTS = {
    "symbol": "SPYL",
    "exchange": "IBIS2",
    "currency": "EUR",
    "price_symbol": "SPYL",       # same fund on the London Stock Exchange, for pricing
    "price_exchange": "LSEETF",
    "price_currency": "USD",
    "fee_estimate": 4.0,          # EUR per order; set to the real figure from an order preview
    "max_fee_share": 0.005,       # fee at most 0.5% of the order
    "cash_reserve": 0.0,          # EUR always left uninvested
    "max_order_value": 2000.0,    # EUR cap per order (safety, and essential on paper)
    "limit_buffer": 0.005,        # limit 0.5% above the estimated price
    "trading_hours": ["09:05", "17:25"],  # Xetra, Europe/Berlin
}
PAPER_PORT, LIVE_PORT = 4002, 4001


def settings(cfg: dict) -> dict:
    s = dict(DEFAULTS)
    s.update(cfg.get("accumulate", {}) or {})
    return s


def xetra_open(s: dict) -> bool:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    start, end = s["trading_hours"]
    return now.weekday() < 5 and start <= now.strftime("%H:%M") <= end


def eur_cash(ib: IB) -> float:
    """EUR cash that can be spent: the smaller of total cash and available funds (base currency EUR)."""
    ib.sleep(2)  # let the account values arrive after connecting
    values = {v.tag: float(v.value) for v in ib.accountValues()
              if v.currency == "EUR" and v.tag in ("TotalCashValue", "AvailableFunds")}
    if len(values) < 2:
        raise RuntimeError(f"EUR cash values not reported: {values}")
    return min(values.values())


def estimate_eur_price(ib: IB, s: dict) -> tuple[float, str]:
    ib.reqMarketDataType(3)  # delayed is enough for a limit with a buffer
    london = ib.qualifyContracts(Stock(s["price_symbol"], s["price_exchange"], s["price_currency"]))[0]
    fx = ib.qualifyContracts(Forex("EURUSD"))[0]
    bars = ib.reqHistoricalData(london, "", "2 D", "5 mins", "TRADES", True)
    fx_bars = ib.reqHistoricalData(fx, "", "1 D", "5 mins", "MIDPOINT", True)
    if not bars or not fx_bars:
        raise RuntimeError("no recent London price or EUR/USD rate")
    usd, eurusd = float(bars[-1].close), float(fx_bars[-1].close)
    return usd / eurusd, f"London ${usd:.3f} at {bars[-1].date}, EUR/USD {eurusd:.5f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="use the LIVE account (port 4001)")
    parser.add_argument("--execute", action="store_true", help="place the order (default: preview only)")
    parser.add_argument("--max-order", type=float, help="override max_order_value for this run")
    parser.add_argument("--ignore-hours", action="store_true", help="allow outside Xetra hours (order waits for the open)")
    args = parser.parse_args()

    cfg = load_config()
    s = settings(cfg)
    if args.max_order is not None:
        s["max_order_value"] = args.max_order
    port = LIVE_PORT if args.live else PAPER_PORT
    mode = ("LIVE" if args.live else "paper") + (" EXECUTE" if args.execute else " preview")

    ib = IB()
    ib.connect(cfg["ibkr"]["host"], port, clientId=21, timeout=15)
    try:
        accounts = ib.managedAccounts()
        is_paper = all(a.startswith("DU") for a in accounts)
        if args.live == is_paper:
            raise SystemExit(f"Refusing: --live={args.live} but accounts {accounts} are {'paper' if is_paper else 'live'}")
        print(f"[{mode}] accounts {accounts}")

        if ib.openTrades():
            pending = [t for t in ib.openTrades() if t.contract.symbol == s["symbol"]]
            if pending:
                print(f"An order for {s['symbol']} is already working; nothing to do.")
                return 0

        cash = eur_cash(ib)
        spendable = min(cash - s["cash_reserve"], s["max_order_value"])
        threshold = s["fee_estimate"] / s["max_fee_share"]
        print(f"EUR cash {cash:,.2f}; spendable now {spendable:,.2f}; minimum order {threshold:,.2f} "
              f"(fee {s['fee_estimate']:.2f} at most {s['max_fee_share']:.1%})")
        if spendable < threshold:
            print("Not enough cash for an order that keeps the fee small. Waiting for more deposits.")
            return 0

        price, source = estimate_eur_price(ib, s)
        limit = round(price * (1 + s["limit_buffer"]), 2)
        shares = math.floor((spendable - s["fee_estimate"]) / limit)
        value = shares * limit
        print(f"Estimated {s['symbol']} price EUR {price:.3f} ({source}); limit EUR {limit:.2f}")
        if shares < 1 or value < threshold:
            print(f"{shares} shares (EUR {value:,.2f}) is below the minimum order; waiting.")
            return 0

        # SMART routing with Xetra as the primary listing: the API's precautionary
        # settings reject orders routed directly to one exchange (error 10311).
        contract = ib.qualifyContracts(Stock(s["symbol"], "SMART", s["currency"], primaryExchange=s["exchange"]))[0]
        print(f"Order: BUY {shares} {s['symbol']} (SMART, primary {s['exchange']}) limit EUR {limit:.2f} DAY "
              f"(about EUR {value:,.2f} + fee)")

        if not args.execute:
            print("Preview only. Run again with --execute to place it.")
            return 0
        if not xetra_open(s) and not args.ignore_hours:
            print("Xetra is closed now; run during trading hours (or pass --ignore-hours).")
            return 0

        order = LimitOrder("BUY", shares, limit, tif="DAY", orderRef=f"accumulate-{datetime.now():%Y%m%d}")
        trade = ib.placeOrder(contract, order)
        ib.sleep(5)
        print(f"Placed: status {trade.orderStatus.status}, filled {trade.orderStatus.filled:g} "
              f"at {trade.orderStatus.avgFillPrice or '-'}")
        for entry in trade.log:
            if entry.message:
                print(f"  {entry.status}: {entry.message}")
        logger.info("accumulate_order mode=%s shares=%s limit=%.2f status=%s", mode, shares, limit, trade.orderStatus.status)
    finally:
        ib.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
