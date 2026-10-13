#!/usr/bin/env python3
"""Partial fills, the cancel race, exits, reconciliation and saved state."""

import sys
from datetime import date, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bot.execution import ExecutionEngine, MockExecutionClient
from bot.market_calendar import MarketCalendar
from bot.risk import RiskManager
from bot.state import StateStore

from test_execution import make_candidate, make_risk


def make_engine(client: MockExecutionClient, risk: RiskManager | None = None) -> ExecutionEngine:
    return ExecutionEngine(
        risk=risk or make_risk(),
        client=client,
        order_timeout_seconds=0.02,
        poll_interval_seconds=0.001,
        cancel_timeout_seconds=0.02,
    )


def open_one(engine: ExecutionEngine, bot_id: str = "SOFI"):
    attempts = engine.execute_candidates([make_candidate(bot_id)], equity=300.0)
    assert attempts[0].trade_opened is True
    return engine.open_trades[bot_id]


# ----------------------------------------------------------------------
# Entries
# ----------------------------------------------------------------------
def test_partial_fill_opens_trade_for_filled_shares_only():
    client = MockExecutionClient(outcomes={"SOFI": ["PARTIAL:5"]})
    engine = make_engine(client)

    attempts = engine.execute_candidates([make_candidate("SOFI")], equity=300.0)
    requested = client.submitted[0].shares

    assert attempts[0].trade_opened is True
    assert attempts[0].shares == 5
    assert attempts[0].note == f"partial_fill_5_of_{requested}"
    assert client.resized == {attempts[0].order_id: 5}

    risk = engine.risk
    assert risk.open_positions == 1
    assert risk.reserved_positions == 0
    assert risk.reserved_gross == 0.0
    assert risk.gross_exposure == 5 * 10.0


def test_fill_during_cancel_is_not_lost():
    client = MockExecutionClient(outcomes={"SOFI": ["FILLS_ON_CANCEL"]})
    engine = make_engine(client)

    attempts = engine.execute_candidates([make_candidate("SOFI")], equity=300.0)

    assert client.cancelled == [attempts[0].order_id]
    assert attempts[0].trade_opened is True
    assert "SOFI" in engine.open_trades
    assert engine.risk.open_positions == 1


def test_unconfirmed_cancel_keeps_reservation_until_resolved():
    client = MockExecutionClient(outcomes={"SOFI": ["STUCK"]})
    engine = make_engine(client)

    attempts = engine.execute_candidates([make_candidate("SOFI")], equity=300.0)
    order_id = attempts[0].order_id

    assert attempts[0].status == "UNRESOLVED"
    assert order_id in engine.pending_orders
    assert engine.risk.reserved_positions == 1

    # A second bot cannot use the slot while the first order's fate is unknown.
    blocked = engine.execute_candidates([make_candidate("F")], equity=300.0)
    assert blocked[0].status == "SKIPPED"

    # The broker later reports the cancel; the reservation is released.
    client._orders[order_id]["outcome"] = "CANCELLED"
    assert engine.resolve_pending() == []
    assert engine.pending_orders == {}
    assert engine.risk.reserved_positions == 0


# ----------------------------------------------------------------------
# Exits and the daily loss limit
# ----------------------------------------------------------------------
def test_stop_exit_closes_position_and_records_loss():
    client = MockExecutionClient()
    engine = make_engine(client)
    trade = open_one(engine)

    assert engine.check_exits() == []

    client.set_exit(trade.order_ref, price=9.9, reason="stop", commission=0.70)
    closed = engine.check_exits()

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop"
    expected = (9.9 - 10.0) * trade.shares - 0.70
    assert abs(closed[0].pnl - expected) < 1e-9

    risk = engine.risk
    assert risk.open_positions == 0
    assert risk.gross_exposure == 0.0
    assert risk.open_by_bot == {}
    assert abs(risk.realized_pnl - expected) < 1e-9


def test_realized_losses_trigger_daily_loss_limit():
    risk = make_risk()
    risk.max_trades_per_bot_per_day = 5

    first = risk.evaluate_entry(bot_id="SOFI", equity=300.0, entry_price=10.0, stop_price=9.8)
    assert first.allowed is True
    risk.on_order_submitted("SOFI", first.position_value)
    risk.on_order_filled("SOFI", first.position_value)
    # 2% of 300 is 6: lose exactly that.
    risk.on_position_closed("SOFI", pnl=-6.0)

    after = risk.evaluate_entry(bot_id="F", equity=300.0, entry_price=10.0, stop_price=9.8)

    assert after.allowed is False
    assert "daily_loss_limit" in after.reason


def test_flatten_sells_open_positions():
    client = MockExecutionClient()
    engine = make_engine(client)
    trade = open_one(engine)

    closed = engine.flatten_all()

    assert [c.exit_reason for c in closed] == ["flatten"]
    assert client.closed == [trade.order_ref]
    assert engine.open_trades == {}
    assert engine.risk.open_positions == 0


def test_day_roll_keeps_overnight_position():
    client = MockExecutionClient()
    engine = make_engine(client)
    open_one(engine)

    risk = engine.risk
    risk.current_date = risk.current_date - timedelta(days=1)
    risk.realized_pnl = -3.0

    decision = risk.evaluate_entry(bot_id="F", equity=300.0, entry_price=10.0, stop_price=9.8)

    assert risk.realized_pnl == 0.0
    assert risk.trades_by_bot == {}
    assert risk.open_positions == 1
    assert "SOFI" in risk.open_by_bot
    assert decision.allowed is False
    assert "max_concurrent_positions" in decision.reason


# ----------------------------------------------------------------------
# Reconciliation
# ----------------------------------------------------------------------
def test_reconcile_blocks_unknown_broker_position():
    client = MockExecutionClient(broker_positions={"HOOD": 2})
    engine = make_engine(client)

    problems = engine.reconcile()

    assert len(problems) == 1
    assert "HOOD" in problems[0]
    assert "HOOD" in engine.risk.blocked_bots

    decision = engine.risk.evaluate_entry(bot_id="HOOD", equity=300.0, entry_price=10.0, stop_price=9.8)
    assert decision.allowed is False
    assert "bot_blocked" in decision.reason


def test_reconcile_records_exit_that_happened_while_offline():
    client = MockExecutionClient()
    engine = make_engine(client)
    trade = open_one(engine)

    client.set_exit(trade.order_ref, price=10.3, reason="target")

    assert engine.reconcile() == []
    assert engine.open_trades == {}
    assert engine.closed_trades[-1].exit_reason == "target"
    assert engine.risk.realized_pnl > 0


def test_reconcile_flags_position_closed_outside_bot():
    client = MockExecutionClient()
    engine = make_engine(client)
    open_one(engine)

    client.broker_positions.clear()
    problems = engine.reconcile()

    assert len(problems) == 1
    assert "closed outside the bot" in problems[0]
    assert engine.open_trades == {}
    assert engine.risk.open_positions == 0


# ----------------------------------------------------------------------
# Saved state
# ----------------------------------------------------------------------
def test_state_survives_restart(tmp_path):
    store = StateStore(tmp_path / "state.json")

    client = MockExecutionClient()
    engine = make_engine(client)
    store.attach(engine)
    trade = open_one(engine)

    # A fresh process: new risk manager, new engine, same broker.
    restarted = make_engine(client)
    assert store.load(restarted) is True

    assert restarted.open_trades["SOFI"] == trade
    assert restarted.risk.open_positions == 1
    assert restarted.risk.trades_by_bot == {"SOFI": 1}
    assert restarted.reconcile() == []

    again = restarted.execute_candidates([make_candidate("SOFI")], equity=300.0)
    assert again[0].status == "SKIPPED"


def test_pending_order_is_saved_before_its_outcome_is_known(tmp_path):
    store = StateStore(tmp_path / "state.json")
    client = MockExecutionClient(outcomes={"SOFI": ["STUCK"]})
    engine = make_engine(client)
    store.attach(engine)

    engine.execute_candidates([make_candidate("SOFI")], equity=300.0)

    restarted = make_engine(client)
    store.load(restarted)

    assert len(restarted.pending_orders) == 1
    assert restarted.risk.reserved_positions == 1


def test_missing_state_file_loads_nothing(tmp_path):
    engine = make_engine(MockExecutionClient())
    assert StateStore(tmp_path / "none.json").load(engine) is False


# ----------------------------------------------------------------------
# Market calendar
# ----------------------------------------------------------------------
def test_calendar_2026_holidays_and_half_days():
    cal = MarketCalendar()

    for holiday in ("2026-01-01", "2026-04-03", "2026-07-03", "2026-11-26", "2026-12-25"):
        assert cal.is_trading_day(date.fromisoformat(holiday)) is False

    assert cal.session_close(date(2026, 11, 27)) == time(13, 0)
    assert cal.session_close(date(2026, 12, 24)) == time(13, 0)
    assert cal.session_close(date(2026, 10, 13)) == time(16, 0)


def test_flatten_moves_earlier_on_half_day():
    cal = MarketCalendar()

    assert cal.flatten_time(date(2026, 11, 27), time(15, 55)) == time(12, 55)
    assert cal.flatten_time(date(2026, 11, 30), time(15, 55)) == time(15, 55)
    assert cal.flatten_time(date(2026, 11, 26), time(15, 55)) is None


def test_saturday_new_year_is_not_observed_on_friday():
    # 1 Jan 2028 is a Saturday; the NYSE stays open on Friday 31 Dec 2027.
    assert MarketCalendar().is_trading_day(date(2027, 12, 31)) is True


def test_extra_holiday_from_config():
    cal = MarketCalendar.from_config({"market": {"extra_holidays": ["2026-10-14"]}})
    assert cal.is_trading_day(date(2026, 10, 14)) is False
    assert cal.next_trading_day(date(2026, 10, 13)) == date(2026, 10, 15)
