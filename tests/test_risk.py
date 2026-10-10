#!/usr/bin/env python3

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bot.risk import RiskManager


def make_risk() -> RiskManager:
    return RiskManager(
        max_concurrent_positions=1,
        max_gross_exposure_pct=80.0,
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=2.0,
        max_trades_per_bot_per_day=1,
        min_position_value_eur=150.0,
        max_position_value_eur=240.0,
    )


def test_entry_allowed():
    rm = make_risk()

    decision = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_entry_allowed:", decision)

    assert decision.allowed is True
    assert decision.shares > 0
    assert 150.0 <= decision.position_value_eur <= 240.0
    print("PASS: entry allowed")


def test_entry_respects_max_position():
    rm = make_risk()

    decision = rm.evaluate_entry(
        bot_id="BPER",
        equity=300.0,
        entry_price=5.0,
        stop_price=4.9,
    )

    print("test_entry_respects_max_position:", decision)

    assert decision.allowed is True
    assert decision.position_value_eur <= 240.0
    print("PASS: max position respected")


def test_max_concurrent_positions():
    rm = make_risk()

    first = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    assert first.allowed is True
    rm.on_entry_opened("STLA", first.position_value_eur)

    second = rm.evaluate_entry(
        bot_id="BPER",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_max_concurrent_positions:", second)

    assert second.allowed is False
    assert "max_concurrent_positions" in second.reason
    print("PASS: max concurrent positions enforced")


def test_max_trades_per_bot_per_day():
    rm = make_risk()

    first = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    assert first.allowed is True
    rm.on_entry_opened("STLA", first.position_value_eur)
    rm.on_position_closed(first.position_value_eur)

    second = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_max_trades_per_bot_per_day:", second)

    assert second.allowed is False
    assert "max_trades_per_bot_per_day" in second.reason
    print("PASS: max trades per bot per day enforced")


def test_daily_loss_limit():
    rm = make_risk()
    rm.start_equity = 300.0

    decision = rm.evaluate_entry(
        bot_id="STLA",
        equity=293.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_daily_loss_limit:", decision)

    assert decision.allowed is False
    assert "daily_loss_limit" in decision.reason
    print("PASS: daily loss limit enforced")


def main():
    test_entry_allowed()
    test_entry_respects_max_position()
    test_max_concurrent_positions()
    test_max_trades_per_bot_per_day()
    test_daily_loss_limit()

    print("\nAll risk manager tests passed.")


if __name__ == "__main__":
    main()
