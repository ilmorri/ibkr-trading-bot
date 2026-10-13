"""Download daily bars from IBKR for the walk-forward test.

Usage (IB Gateway paper must be running and logged in):
    python scripts/download_daily.py HOOD INTC SOFI --years 10

Writes data/<SYMBOL>_daily.csv with columns date,open,high,low,close,volume.
Prices are split- and dividend-adjusted (ADJUSTED_LAST), so old prices can
differ from what was quoted on the day.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from ib_insync import IB, Stock

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def main() -> int:
    parser = argparse.ArgumentParser(description="Download daily bars from IBKR")
    parser.add_argument("symbols", nargs="+", help="US stock symbols, e.g. HOOD INTC")
    parser.add_argument("--years", type=int, default=10, help="years of history (default 10)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=4002, help="4002 = IB Gateway paper")
    parser.add_argument("--client-id", type=int, default=4)
    parser.add_argument(
        "--what",
        default="ADJUSTED_LAST",
        help="ADJUSTED_LAST (default, adjusted for splits and dividends) or TRADES",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    ib = IB()
    try:
        ib.connect(args.host, args.port, clientId=args.client_id, timeout=15)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not connect to IB Gateway on {args.host}:{args.port}: {exc}")
        return 1

    failed = []
    print(f"{'symbol':<8}{'bars':>7}  {'first':<12}{'last':<12}{'last_close':>11}")
    for symbol in [s.upper() for s in args.symbols]:
        contract = Stock(symbol, "SMART", "USD")
        try:
            if not ib.qualifyContracts(contract):
                raise RuntimeError("contract not found")
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=f"{args.years} Y",
                barSizeSetting="1 day",
                whatToShow=args.what,
                useRTH=True,
                formatDate=1,
                timeout=180,
            )
            if not bars:
                raise RuntimeError("no bars returned")
        except Exception as exc:  # noqa: BLE001
            print(f"{symbol:<8} FAILED: {exc}")
            failed.append(symbol)
            ib.sleep(2)
            continue

        df = pd.DataFrame(
            {
                "date": [str(b.date) for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            }
        )
        df.to_csv(DATA_DIR / f"{symbol}_daily.csv", index=False)
        print(
            f"{symbol:<8}{len(df):>7}  {df['date'].iloc[0]:<12}{df['date'].iloc[-1]:<12}"
            f"{df['close'].iloc[-1]:>11.2f}"
        )
        ib.sleep(2)  # stay well under IBKR's pacing limits

    ib.disconnect()
    if failed:
        print(f"\nFailed: {' '.join(failed)}  (rerun the script with just these symbols)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
