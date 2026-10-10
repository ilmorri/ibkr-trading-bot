#!/usr/bin/env python3

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd

from bot.config import load_config
from bot.engine import Engine
from bot.logger import setup_logging


def make_bars(rows):
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def opening_range_breakout_bars():
    rows = []
    base = "2026-01-05"

    for i in range(15):
        ts = f"{base} 09:{i:02d}"
        rows.append(
            (
                ts,
                10.00,
                10.05,
                10.00,
                10.02,
                1000,
            )
        )

    rows.append(
        (
            f"{base} 09:15",
            10.10,
            10.20,
            10.08,
            10.18,
            2000,
        )
    )

    return make_bars(rows)


def vwap_reversion_bars():
    rows = []
    base = "2026-01-05"

    for i in range(12):
        ts = f"{base} 09:{i:02d}"
        rows.append(
            (
                ts,
                10.00,
                10.01,
                9.99,
                10.00,
                1000,
            )
        )

    rows.append(
        (
            f"{base} 09:12",
            9.90,
            9.92,
            9.80,
            9.90,
            2000,
        )
    )

    return make_bars(rows)


def main() -> int:
    setup_logging()

    cfg = load_config()

    engine = Engine(cfg)

    enabled_bots = [
        item
        for item in cfg.get("universe", [])
        if item.get("enabled", True)
    ]

    if not enabled_bots:
        print("No enabled bots found in config/config.yaml")
        return 1

    print("=" * 70)
    print("DRY RUN ENGINE")
    print("=" * 70)
    print(f"Equity used for dry run: 300.00 EUR")
    print(f"Enabled bots: {[item['symbol'] for item in enabled_bots]}")
    print("=" * 70)

    bars_by_symbol = {}

    for item in enabled_bots:
        symbol = item["symbol"]
        strategy = item.get("strategy")

        if strategy == "opening_range":
            bars_by_symbol[symbol] = opening_range_breakout_bars()
        elif strategy == "vwap_reversion":
            bars_by_symbol[symbol] = vwap_reversion_bars()
        else:
            bars_by_symbol[symbol] = None

    results = engine.run_once(bars_by_symbol, equity=300.0)

    for result in results:
        print("-" * 70)
        print(f"Bot: {result.bot_id}")
        print(f"Strategy: {result.strategy}")
        print(f"Signal action: {result.action}")
        print(f"Signal reason: {result.reason}")
        print(f"Price: {result.price:.4f}")
        print(f"Stop: {result.stop}")
        print(f"Target: {result.target}")

        if result.risk_allowed is None:
            print(f"Risk: not evaluated")
        elif result.risk_allowed is True:
            print(f"Risk: ALLOWED")
            print(f"Shares: {result.shares}")
            print(f"Position value: {result.position_value_eur:.2f} EUR")
        else:
            print(f"Risk: BLOCKED")
            print(f"Risk reason: {result.risk_reason}")

    print("=" * 70)
    print("Dry run complete.")
    print("Note: because max_concurrent_positions is 1, after the first trade is")
    print("allowed, later bots may be blocked by the risk manager.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
