#!/usr/bin/env python3

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bot.risk import RiskManager


def make_risk(max_order_attempts_per_bot_per_day: int = 3) -> RiskManager:
    return RiskManager(
        max_concurrent_positions=1,
        max_gross_exposure_pct=80.0,
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=2.0,
        max_trades_per_bot_per_day=1,
        max_order_attempts_per_bot_per_day=max_order_attempts_per_bot_per_day,
        min_position_value=150.0,
        max_position_value=240.0,
    )


def test_entry_allowed_and_filled():
    rm = make_risk()

    decision = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_entry_allowed_and_filled:", decision)

    assert decision.allowed is True
    assert decision.shares > 0
    assert 150.0 <= decision.position_value <= 240.0

    rm.on_order_submitted("STLA", decision.position_value)
    rm.on_order_filled("STLA", decision.position_value)

    assert rm.open_positions == 1
    assert rm.gross_exposure == decision.position_value
    assert rm.reserved_positions == 0
    assert rm.trades_by_bot.get("STLA", 0) == 1

    print("PASS: entry allowed and filled")


def test_reserved_order_blocks_second_bot():
    rm = make_risk()

    first = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    assert first.allowed is True

    rm.on_order_submitted("STLA", first.position_value)

    second = rm.evaluate_entry(
        bot_id="BPER",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_reserved_order_blocks_second_bot:", second)

    assert second.allowed is False
    assert "max_concurrent_positions" in second.reason

    print("PASS: reserved order blocks second bot")


def test_cancelled_order_releases_reservation():
    rm = make_risk()

    first = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    assert first.allowed is True

    rm.on_order_submitted("STLA", first.position_value)

    second = rm.evaluate_entry(
        bot_id="BPER",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    assert second.allowed is False

    rm.on_order_cancelled("STLA", first.position_value)

    third = rm.evaluate_entry(
        bot_id="BPER",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_cancelled_order_releases_reservation:", third)

    assert third.allowed is True
    assert rm.open_positions == 0
    assert rm.reserved_positions == 0

    print("PASS: cancelled order releases reservation")


def test_filled_trade_blocks_same_bot_same_day():
    rm = make_risk()

    first = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    assert first.allowed is True

    rm.on_order_submitted("STLA", first.position_value)
    rm.on_order_filled("STLA", first.position_value)

    while_open = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_filled_trade_blocks_same_bot_same_day while_open:", while_open)

    assert while_open.allowed is False
    assert "max_concurrent_positions" in while_open.reason or "max_trades_per_bot_per_day" in while_open.reason

    rm.on_position_closed("STLA", first.position_value)

    after_close = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_filled_trade_blocks_same_bot_same_day after_close:", after_close)

    assert after_close.allowed is False
    assert "max_trades_per_bot_per_day" in after_close.reason

    print("PASS: filled trade blocks same bot same day")


def test_cancelled_order_does_not_count_as_filled_trade():
    rm = make_risk()

    first = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    assert first.allowed is True

    rm.on_order_submitted("STLA", first.position_value)
    rm.on_order_cancelled("STLA", first.position_value)

    assert rm.trades_by_bot.get("STLA", 0) == 0
    assert rm.order_attempts_by_bot.get("STLA", 0) == 1

    second = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_cancelled_order_does_not_count_as_filled_trade:", second)

    assert second.allowed is True

    print("PASS: cancelled order does not count as filled trade")


def test_max_order_attempts_per_bot():
    rm = make_risk(max_order_attempts_per_bot_per_day=1)

    first = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    assert first.allowed is True

    rm.on_order_submitted("STLA", first.position_value)
    rm.on_order_cancelled("STLA", first.position_value)

    second = rm.evaluate_entry(
        bot_id="STLA",
        equity=300.0,
        entry_price=10.0,
        stop_price=9.8,
    )

    print("test_max_order_attempts_per_bot:", second)

    assert second.allowed is False
    assert "max_order_attempts" in second.reason

    print("PASS: max order attempts per bot enforced")


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
    test_entry_allowed_and_filled()
    test_reserved_order_blocks_second_bot()
    test_cancelled_order_releases_reservation()
    test_filled_trade_blocks_same_bot_same_day()
    test_cancelled_order_does_not_count_as_filled_trade()
    test_max_order_attempts_per_bot()
    test_daily_loss_limit()

    print("\nAll risk manager tests passed.")


if __name__ == "__main__":
    main()
