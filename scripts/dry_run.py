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
from bot.execution import ExecutionEngine, MockExecutionClient
from bot.logger import setup_logging
from bot.risk import RiskManager


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

    print("=" * 75)
    print("DRY RUN ENGINE + ORDER LIFECYCLE")
    print("=" * 75)
    print("Equity used for dry run: 300.00 EUR")
    print(f"Enabled bots: {[item['symbol'] for item in enabled_bots]}")
    print("=" * 75)

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

    result = engine.evaluate(bars_by_symbol)

    print("-" * 75)
    print("Candidates after score filter:")
    print("-" * 75)

    if not result.candidates:
        print("No candidates passed the score filter.")
    else:
        for candidate in result.candidates:
            signal = candidate.signal
            print(f"bot={candidate.bot_id}")
            print(f"strategy={candidate.strategy}")
            print(f"priority={candidate.priority}")
            print(f"price={signal.price:.4f}")
            print(f"stop={signal.stop}")
            print(f"target={signal.target}")
            print(f"score={signal.score:.3f}")
            print(f"net_rr={signal.net_rr:.3f}")
            print()

    risk = RiskManager.from_config(cfg)

    client = MockExecutionClient(
        outcomes={
            "STLA": ["CANCELLED"],
            "BPER": ["FILLED"],
            "ENI": ["FILLED"],
            "ISP": ["FILLED"],
        },
        default="FILLED",
    )

    execution = ExecutionEngine(
        risk=risk,
        client=client,
        order_timeout_seconds=0.2,
        poll_interval_seconds=0.01,
    )

    attempts = execution.execute_candidates(result.candidates, equity=300.0)

    print("-" * 75)
    print("Order attempts:")
    print("-" * 75)

    for attempt in attempts:
        print(f"bot={attempt.bot_id}")
        print(f"strategy={attempt.strategy}")
        print(f"order_id={attempt.order_id}")
        print(f"status={attempt.status}")
        print(f"shares={attempt.shares}")
        print(f"price={attempt.price:.4f}")
        print(f"position_value_eur={attempt.position_value_eur:.2f}")
        print(f"filled={attempt.filled}")
        print(f"trade_opened={attempt.trade_opened}")
        print(f"exit_reason={attempt.exit_reason}")
        print(f"note={attempt.note}")
        print()

    print("=" * 75)
    print("Final risk state:")
    print("=" * 75)
    print(f"open_positions: {risk.open_positions}")
    print(f"gross_exposure_eur: {risk.gross_exposure_eur:.2f}")
    print(f"reserved_positions: {risk.reserved_positions}")
    print(f"reserved_gross_eur: {risk.reserved_gross_eur:.2f}")
    print(f"trades_by_bot: {risk.trades_by_bot}")
    print(f"order_attempts_by_bot: {risk.order_attempts_by_bot}")

    print("=" * 75)
    print("Dry run complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
