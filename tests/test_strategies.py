#!/usr/bin/env python3

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd

from bot.strategies.opening_range import OpeningRange
from bot.strategies.vwap_reversion import VwapReversion


def make_bars(rows):
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def test_opening_range_breakout():
    rows = [
        ("2026-01-05 09:00", 100.0, 100.5, 99.9, 100.2, 1000),
        ("2026-01-05 09:01", 100.2, 101.0, 100.1, 100.8, 1000),
        ("2026-01-05 09:02", 100.8, 101.5, 100.5, 101.2, 1000),
        ("2026-01-05 09:03", 101.2, 101.8, 100.9, 101.5, 1000),
        ("2026-01-05 09:04", 101.5, 102.0, 101.1, 101.8, 1000),
        ("2026-01-05 09:05", 101.8, 102.2, 101.4, 102.0, 1000),
        ("2026-01-05 09:06", 102.0, 102.3, 101.6, 102.1, 1000),
        ("2026-01-05 09:07", 102.1, 102.4, 101.7, 102.2, 1000),
        ("2026-01-05 09:08", 102.2, 102.5, 101.8, 102.3, 1000),
        ("2026-01-05 09:09", 102.3, 102.6, 101.9, 102.4, 1000),
        ("2026-01-05 09:10", 102.4, 102.7, 102.0, 102.5, 1000),
        ("2026-01-05 09:11", 102.5, 102.8, 102.1, 102.6, 1000),
        ("2026-01-05 09:12", 102.6, 102.9, 102.2, 102.7, 1000),
        ("2026-01-05 09:13", 102.7, 103.0, 102.3, 102.8, 1000),
        ("2026-01-05 09:14", 102.8, 103.1, 102.4, 102.9, 1000),
        ("2026-01-05 09:15", 102.9, 103.2, 102.5, 103.0, 1000),
        ("2026-01-05 09:16", 103.0, 104.0, 102.8, 103.8, 2000),
    ]

    strat = OpeningRange("RACE", {"range_minutes": 15})
    signal = strat.evaluate(make_bars(rows))

    print("OpeningRange breakout signal:", signal)
    assert signal.action.value == "BUY"
    assert signal.stop is not None
    assert signal.target is not None
    print("PASS: opening_range breakout")


def test_opening_range_no_breakout():
    rows = [
        ("2026-01-05 09:00", 100.0, 100.5, 99.9, 100.2, 1000),
        ("2026-01-05 09:01", 100.2, 100.6, 100.0, 100.4, 1000),
        ("2026-01-05 09:02", 100.4, 100.8, 100.2, 100.5, 1000),
        ("2026-01-05 09:03", 100.5, 100.9, 100.3, 100.6, 1000),
        ("2026-01-05 09:04", 100.6, 101.0, 100.4, 100.7, 1000),
        ("2026-01-05 09:05", 100.7, 101.1, 100.5, 100.8, 1000),
        ("2026-01-05 09:06", 100.8, 101.2, 100.6, 100.9, 1000),
        ("2026-01-05 09:07", 100.9, 101.3, 100.7, 101.0, 1000),
        ("2026-01-05 09:08", 101.0, 101.4, 100.8, 101.1, 1000),
        ("2026-01-05 09:09", 101.1, 101.5, 100.9, 101.2, 1000),
        ("2026-01-05 09:10", 101.2, 101.6, 101.0, 101.3, 1000),
        ("2026-01-05 09:11", 101.3, 101.7, 101.1, 101.4, 1000),
        ("2026-01-05 09:12", 101.4, 101.8, 101.2, 101.5, 1000),
        ("2026-01-05 09:13", 101.5, 101.9, 101.3, 101.6, 1000),
        ("2026-01-05 09:14", 101.6, 102.0, 101.4, 101.7, 1000),
        ("2026-01-05 09:15", 101.7, 102.1, 101.5, 101.8, 1000),
        ("2026-01-05 09:16", 101.8, 102.2, 101.6, 102.0, 1000),
    ]

    strat = OpeningRange("RACE", {"range_minutes": 15})
    signal = strat.evaluate(make_bars(rows))

    print("OpeningRange no breakout signal:", signal)
    assert signal.action.value == "FLAT"
    print("PASS: opening_range no breakout")


def test_vwap_reversion_buy():
    rows = []

    for i in range(12):
        minute = i
        rows.append(
            (
                f"2026-01-05 09:{minute:02d}",
                102.0,
                102.1,
                101.9,
                102.0,
                1000,
            )
        )

    rows.append(
        (
            "2026-01-05 09:12",
            100.0,
            100.2,
            99.8,
            99.9,
            2000,
        )
    )

    strat = VwapReversion("ENIE", {"z_entry": 2.0, "min_bars": 10})
    signal = strat.evaluate(make_bars(rows))

    print("VwapReversion signal:", signal)
    assert signal.action.value == "BUY"
    assert signal.stop is not None
    assert signal.target is not None
    print("PASS: vwap_reversion buy")


def test_vwap_reversion_no_signal():
    rows = [
        ("2026-01-05 09:00", 102.0, 102.1, 101.9, 102.0, 1000),
        ("2026-01-05 09:01", 102.0, 102.2, 101.9, 102.1, 1000),
        ("2026-01-05 09:02", 102.1, 102.3, 102.0, 102.2, 1000),
        ("2026-01-05 09:03", 102.2, 102.4, 102.1, 102.3, 1000),
        ("2026-01-05 09:04", 102.3, 102.5, 102.2, 102.4, 1000),
        ("2026-01-05 09:05", 102.4, 102.6, 102.3, 102.5, 1000),
        ("2026-01-05 09:06", 102.5, 102.7, 102.4, 102.6, 1000),
        ("2026-01-05 09:07", 102.6, 102.8, 102.5, 102.7, 1000),
        ("2026-01-05 09:08", 102.7, 102.9, 102.6, 102.8, 1000),
        ("2026-01-05 09:09", 102.8, 103.0, 102.7, 102.9, 1000),
        ("2026-01-05 09:10", 102.9, 103.1, 102.8, 103.0, 1000),
        ("2026-01-05 09:11", 103.0, 103.2, 102.9, 103.1, 1000),
    ]

    strat = VwapReversion("ENIE", {"z_entry": 2.0, "min_bars": 10})
    signal = strat.evaluate(make_bars(rows))

    print("VwapReversion no signal:", signal)
    assert signal.action.value == "FLAT"
    print("PASS: vwap_reversion no signal")


def main():
    test_opening_range_breakout()
    test_opening_range_no_breakout()
    test_vwap_reversion_buy()
    test_vwap_reversion_no_signal()
    print("\nAll offline strategy tests passed.")


if __name__ == "__main__":
    main()
