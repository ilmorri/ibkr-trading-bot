#!/usr/bin/env python3
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bot.config import load_config
from ib_insync import IB, Stock


def clean(x):
    if x is None or (isinstance(x, float) and math.isnan(x)) or x == -1:
        return None
    return x


def main() -> int:
    cfg = load_config()
    host = cfg["ibkr"]["host"]
    port = int(cfg["ibkr"]["port"])
    client_id = int(cfg["ibkr"]["clientId"])
    exchange = cfg["market"]["exchange"]
    currency = cfg["market"]["currency"]
    universe = [b for b in cfg["universe"] if b.get("enabled", True)]

    print(f"Connecting to {host}:{port} clientId={client_id}")
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=10)
    ok = 0
    try:
        print("Connected. Accounts:", ib.managedAccounts())
        ib.reqMarketDataType(4)  # delayed quotes when there is no live subscription

        for item in universe:
            symbol = item["symbol"]
            print(f"\n{symbol} ({item.get('name', symbol)})")
            details = ib.reqContractDetails(Stock(symbol, "SMART", currency))
            if not details:
                print(f"  FAIL: no {currency} stock with symbol {symbol}")
                continue
            for d in details:
                c = d.contract
                print(f"  match: conId={c.conId} primaryExchange={c.primaryExchange} name={d.longName}")
            chosen = next(
                (d.contract for d in details if d.contract.primaryExchange == exchange),
                details[0].contract,
            )
            if exchange != "SMART" and chosen.primaryExchange != exchange:
                print(f"  NOTE: none listed on {exchange}; using {chosen.primaryExchange}")
            tick = ib.reqMktData(chosen, "", False, False)
            ib.sleep(4)
            last, bid, ask, close = (clean(v) for v in (tick.last, tick.bid, tick.ask, tick.close))
            print(f"  last={last} bid={bid} ask={ask} close={close}")
            if any(v is not None for v in (last, bid, ask, close)):
                print("  STATUS: OK")
                ok += 1
            else:
                print("  STATUS: no market data")
            ib.cancelMktData(chosen)
    finally:
        ib.disconnect()

    print(f"\nSymbols with data: {ok}/{len(universe)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
