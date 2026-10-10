#!/usr/bin/env python3

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bot.config import load_config
from ib_insync import IB, Stock


def main() -> int:
    cfg = load_config()

    host = cfg["ibkr"]["host"]
    port = int(cfg["ibkr"]["port"])
    client_id = int(cfg["ibkr"]["clientId"])
    exchange = cfg["market"]["exchange"]
    currency = cfg["market"]["currency"]

    universe = [b for b in cfg["universe"] if b.get("enabled", True)]

    print("=" * 60)
    print("IBKR smoke test")
    print("=" * 60)
    print(f"Host: {host}")
    print(f"Port: {port}")
    print(f"ClientId: {client_id}")
    print(f"Exchange: {exchange}")
    print(f"Currency: {currency}")
    print(f"Symbols: {[b['symbol'] for b in universe]}")
    print("-" * 60)

    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=10)

    print("Connected to IBKR")
    print("Account:", ib.account())

    ib.reqMarketDataType(1)

    ok_count = 0
    warn_count = 0

    for item in universe:
        symbol = item["symbol"]
        name = item.get("name", symbol)
        contract = Stock(symbol, exchange, currency)

        try:
            contract = ib.qualifyContracts(contract)
        except Exception as exc:
            print(f"FAIL {symbol} ({name}): contract qualification failed: {exc}")
            continue

        tick = ib.reqMktData(contract, "", False, False)
        ib.sleep(2.0)

        last = getattr(tick, "last", None)
        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)

        last_val = last.value if last is not None else None
        bid_val = bid.value if bid is not None else None
        ask_val = ask.value if ask is not None else None

        print(f"\n{symbol} ({name})")
        print(f"  Last: {last_val}")
        print(f"  Bid:  {bid_val}")
        print(f"  Ask:  {ask_val}")

        if last_val or bid_val or ask_val:
            print("  STATUS: OK")
            ok_count += 1
        else:
            print("  STATUS: WARNING - no market data yet")
            print("  Possible causes: IBKR data package, account approval, delayed data, wrong contract.")
            warn_count += 1

        ib.cancelMktData(contract)

    print("-" * 60)
    print(f"OK symbols: {ok_count}")
    print(f"Warning symbols: {warn_count}")
    print("IBKR smoke test complete.")

    ib.disconnect()

    if warn_count == len(universe):
        print("Result: FAIL - no market data received for any symbol")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
