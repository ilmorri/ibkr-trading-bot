#!/usr/bin/env python3
"""Download 1-minute bars from IBKR into data/ and print screening stats."""
import argparse
from pathlib import Path

import pandas as pd
from ib_insync import IB, Stock, util

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("symbols", nargs="+")
    p.add_argument("--months", type=int, default=3)
    p.add_argument("--port", type=int, default=4002)
    args = p.parse_args()

    ib = IB()
    ib.connect("localhost", args.port, clientId=3)
    rows = []
    try:
        for sym in args.symbols:
            contract = Stock(sym, "SMART", "USD")
            if not ib.qualifyContracts(contract):
                print(f"{sym}: contract not found, skipped")
                continue
            chunks, end = [], ""
            for _ in range(args.months):
                bars = ib.reqHistoricalData(
                    contract, endDateTime=end, durationStr="1 M", barSizeSetting="1 min",
                    whatToShow="TRADES", useRTH=True, formatDate=1,
                )
                if not bars:
                    break
                chunks.append(util.df(bars))
                end = bars[0].date
                ib.sleep(1)
            if not chunks:
                print(f"{sym}: no data")
                continue
            df = pd.concat(chunks).drop_duplicates("date").sort_values("date")
            out = ROOT / "data" / f"{sym}_1min.csv"
            df.to_csv(out, index=False)

            day = df.groupby(df["date"].dt.date).agg(
                high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")
            )
            rows.append({
                "symbol": sym,
                "days": len(day),
                "avg_price": round(day["close"].mean(), 2),
                "avg_range_pct": round(((day["high"] - day["low"]) / day["close"] * 100).mean(), 2),
                "avg_value_musd": round((day["close"] * day["volume"]).mean() / 1e6, 1),
            })
            print(f"{sym}: {len(df)} bars saved to {out.name}")
    finally:
        ib.disconnect()

    if rows:
        print()
        print(pd.DataFrame(rows).sort_values("avg_range_pct", ascending=False).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
