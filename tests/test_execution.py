#!/usr/bin/env python3

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bot.engine import Candidate
from bot.execution import ExecutionEngine, MockExecutionClient
from bot.risk import RiskManager
from bot.strategies.base import Action, Signal


def make_candidate(
    bot_id: str,
    price: float = 10.0,
    stop: float = 9.9,
    target: float = 10.2,
) -> Candidate:
    signal = Signal(
        action=Action.BUY,
        price=price,
        stop=stop,
        target=target,
        reason="test_signal",
        net_rr=2.0,
        score=2.0,
        score_reason="test_score",
    )

    return Candidate(
        bot_id=bot_id,
        strategy="test",
        priority=1,
        signal=signal,
    )


def make_risk(max_order_attempts_per_bot_per_day: int = 3) -> RiskManager:
    return RiskManager(
        max_concurrent_positions=1,
        max_gross_exposure_pct=80.0,
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=2.0,
        max_trades_per_bot_per_day=1,
        max_order_attempts_per_bot_per_day=max_order_attempts_per_bot_per_day,
        min_position_value_eur=150.0,
        max_position_value_eur=240.0,
    )


def test_cancelled_order_releases_risk_and_next_bot_trades():
    risk = make_risk()

    client = MockExecutionClient(
        outcomes={
            "STLA": ["CANCELLED"],
            "BPER": ["FILLED"],
        }
    )

    execution = ExecutionEngine(
        risk=risk,
        client=client,
        order_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )

    candidates = [
        make_candidate("STLA"),
        make_candidate("BPER"),
    ]

    attempts = execution.execute_candidates(candidates, equity=300.0)

    print("attempts:", attempts)

    assert attempts[0].status == "CANCELLED"
    assert attempts[0].filled is False
    assert attempts[0].trade_opened is False

    assert attempts[1].status == "FILLED"
    assert attempts[1].filled is True
    assert attempts[1].trade_opened is True

    assert risk.open_positions == 1
    assert risk.reserved_positions == 0
    assert risk.gross_exposure_eur > 0

    assert risk.trades_by_bot.get("STLA", 0) == 0
    assert risk.trades_by_bot.get("BPER", 0) == 1

    assert risk.order_attempts_by_bot.get("STLA", 0) == 1
    assert risk.order_attempts_by_bot.get("BPER", 0) == 1

    print("PASS: cancelled order releases risk and next bot trades")


def test_filled_position_blocks_second_bot():
    risk = make_risk()

    client = MockExecutionClient(
        outcomes={
            "STLA": ["FILLED"],
            "BPER": ["FILLED"],
        }
    )

    execution = ExecutionEngine(
        risk=risk,
        client=client,
        order_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )

    candidates = [
        make_candidate("STLA"),
        make_candidate("BPER"),
    ]

    attempts = execution.execute_candidates(candidates, equity=300.0)

    print("attempts:", attempts)

    assert attempts[0].status == "FILLED"
    assert attempts[0].trade_opened is True

    assert attempts[1].status == "SKIPPED"
    assert attempts[1].trade_opened is False

    assert risk.open_positions == 1
    assert risk.reserved_positions == 0

    print("PASS: filled position blocks second bot")


def test_cancelled_order_allows_retry_if_attempts_remaining():
    risk = make_risk(max_order_attempts_per_bot_per_day=3)

    client = MockExecutionClient(
        outcomes={
            "STLA": ["CANCELLED", "FILLED"],
        }
    )

    execution = ExecutionEngine(
        risk=risk,
        client=client,
        order_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )

    candidates = [
        make_candidate("STLA"),
        make_candidate("STLA"),
    ]

    attempts = execution.execute_candidates(candidates, equity=300.0)

    print("attempts:", attempts)

    assert attempts[0].status == "CANCELLED"
    assert attempts[0].trade_opened is False

    assert attempts[1].status == "FILLED"
    assert attempts[1].trade_opened is True

    assert risk.trades_by_bot.get("STLA", 0) == 1
    assert risk.order_attempts_by_bot.get("STLA", 0) == 2

    print("PASS: cancelled order allows retry if attempts remaining")


def test_max_order_attempts_blocks_retry():
    risk = make_risk(max_order_attempts_per_bot_per_day=1)

    client = MockExecutionClient(
        outcomes={
            "STLA": ["CANCELLED", "FILLED"],
        }
    )

    execution = ExecutionEngine(
        risk=risk,
        client=client,
        order_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )

    candidates = [
        make_candidate("STLA"),
        make_candidate("STLA"),
    ]

    attempts = execution.execute_candidates(candidates, equity=300.0)

    print("attempts:", attempts)

    assert attempts[0].status == "CANCELLED"
    assert attempts[1].status == "SKIPPED"
    assert attempts[1].trade_opened is False

    assert risk.order_attempts_by_bot.get("STLA", 0) == 1

    print("PASS: max order attempts blocks retry")


def main():
    test_cancelled_order_releases_risk_and_next_bot_trades()
    test_filled_position_blocks_second_bot()
    test_cancelled_order_allows_retry_if_attempts_remaining()
    test_max_order_attempts_blocks_retry()

    print("\nAll execution lifecycle tests passed.")


if __name__ == "__main__":
    main()
