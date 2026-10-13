#!/usr/bin/env python3
"""
Offline checks of IbkrExecutionClient against a fake IB built from real
ib_insync objects. They prove the bot's own logic, not IBKR's behaviour:
that still needs the paper account.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ib_insync import IB, CommissionReport, Execution, Fill, OrderStatus, Stock, Trade

from bot.ibkr_client import IbkrExecutionClient
from bot.orders import OrderRequest


class FakeClient:
    def __init__(self):
        self.next_id = 100

    def getReqId(self) -> int:
        self.next_id += 1
        return self.next_id


class FakeIB:
    bracketOrder = IB.bracketOrder

    def __init__(self):
        self.client = FakeClient()
        self._trades: list[Trade] = []
        self._fills: list[Fill] = []
        self._positions = []

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def placeOrder(self, contract, order):
        for trade in self._trades:
            if order.orderId and trade.order.orderId == order.orderId:
                trade.order = order
                return trade

        if not order.orderId:
            order.orderId = self.client.getReqId()

        trade = Trade(
            contract=contract,
            order=order,
            orderStatus=OrderStatus(orderId=order.orderId, status="Submitted"),
        )
        self._trades.append(trade)

        if order.orderType == "MKT":
            self.fill(trade, order.totalQuantity, 10.05, commission=0.35)

        return trade

    def fill(self, trade: Trade, shares: float, price: float, commission: float = 0.0):
        side = "BOT" if trade.order.action == "BUY" else "SLD"
        self._fills.append(
            Fill(
                trade.contract,
                Execution(
                    orderId=trade.order.orderId,
                    orderRef=trade.order.orderRef,
                    side=side,
                    shares=shares,
                    price=price,
                ),
                CommissionReport(commission=commission),
                datetime.now(timezone.utc),
            )
        )
        trade.orderStatus.filled += shares
        trade.orderStatus.avgFillPrice = price
        if trade.orderStatus.filled >= trade.order.totalQuantity:
            trade.orderStatus.status = "Filled"

    def trades(self):
        return list(self._trades)

    def openTrades(self):
        return [trade for trade in self._trades if not trade.isDone()]

    def fills(self):
        return list(self._fills)

    def cancelOrder(self, order):
        for trade in self._trades:
            if trade.order.orderId == order.orderId:
                trade.orderStatus.status = "Cancelled"

    def positions(self):
        return self._positions

    def sleep(self, seconds):
        pass


def make_request(shares: int = 10) -> OrderRequest:
    return OrderRequest(
        bot_id="SOFI",
        symbol="SOFI",
        strategy="test",
        action="BUY",
        shares=shares,
        price=10.004,
        stop=9.8049,
        target=10.4551,
        score=2.0,
        net_rr=1.5,
        score_reason="",
        order_ref="SOFI-1",
        position_value=shares * 10.0,
    )


def submit(ib: FakeIB) -> tuple[IbkrExecutionClient, str, list[Trade]]:
    client = IbkrExecutionClient(ib)
    order_id = client.submit_entry(make_request())
    return client, order_id, ib.trades()


def test_bracket_is_tagged_rounded_and_transmitted_once():
    ib = FakeIB()
    client, order_id, (parent, take_profit, stop_loss) = submit(ib)

    assert order_id == str(parent.order.orderId)
    assert parent.order.lmtPrice == 10.0
    assert take_profit.order.lmtPrice == 10.46
    assert stop_loss.order.auxPrice == 9.8

    assert (parent.order.orderRef, take_profit.order.orderRef, stop_loss.order.orderRef) == (
        "SOFI-1",
        "SOFI-1:tp",
        "SOFI-1:sl",
    )
    assert parent.order.tif == "DAY"
    assert take_profit.order.tif == stop_loss.order.tif == "GTC"
    assert take_profit.order.parentId == stop_loss.order.parentId == parent.order.orderId
    assert [t.order.transmit for t in (parent, take_profit, stop_loss)] == [False, False, True]


def test_entry_state_reports_partial_fill_then_cancel():
    ib = FakeIB()
    client, order_id, (parent, _, _) = submit(ib)

    assert client.entry_state(order_id).status == "PENDING"

    ib.fill(parent, 4, 10.0)
    client.cancel_order(order_id)
    report = client.entry_state(order_id)

    assert report.status == "CANCELLED"
    assert report.filled_shares == 4
    assert report.avg_fill_price == 10.0


def test_entry_state_after_restart_uses_fills():
    ib = FakeIB()
    client, order_id, (parent, _, _) = submit(ib)
    ib.fill(parent, 10, 9.99)

    # After a restart a finished order is not among the session's trades.
    ib._trades.clear()
    report = client.entry_state(order_id)

    assert report.status == "FILLED"
    assert report.filled_shares == 10
    assert report.avg_fill_price == 9.99


def test_stop_fill_is_reported_as_stop_exit():
    ib = FakeIB()
    client, order_id, (parent, _, stop_loss) = submit(ib)
    ib.fill(parent, 10, 10.0)

    assert client.exit_fill("SOFI-1", 10) is None

    ib.fill(stop_loss, 10, 9.79, commission=0.35)
    report = client.exit_fill("SOFI-1", 10)

    assert report.reason == "stop"
    assert report.exit_price == 9.79
    assert report.shares == 10
    assert report.commission == 0.35


def test_resize_replaces_protection_cancelled_with_parent():
    ib = FakeIB()
    client, order_id, (parent, take_profit, stop_loss) = submit(ib)

    ib.fill(parent, 4, 10.0)
    for trade in (parent, take_profit, stop_loss):
        trade.orderStatus.status = "Cancelled"

    client.resize_protection(order_id, 4)

    new_orders = [t.order for t in ib.openTrades()]
    assert sorted(o.orderType for o in new_orders) == ["LMT", "STP"]
    assert all(o.totalQuantity == 4 and o.action == "SELL" for o in new_orders)
    assert all(o.ocaGroup == "SOFI-1" and o.ocaType == 1 for o in new_orders)
    assert {o.orderRef for o in new_orders} == {"SOFI-1:tp", "SOFI-1:sl"}


def test_resize_shrinks_live_protection():
    ib = FakeIB()
    client, order_id, (parent, take_profit, stop_loss) = submit(ib)
    ib.fill(parent, 4, 10.0)

    client.resize_protection(order_id, 4)

    assert take_profit.order.totalQuantity == stop_loss.order.totalQuantity == 4
    assert len(ib.trades()) == 3


def test_flatten_cancels_protection_before_selling():
    ib = FakeIB()
    client, order_id, (parent, take_profit, stop_loss) = submit(ib)
    ib.fill(parent, 10, 10.0)

    report = client.close_position("SOFI-1", "SOFI", 10)

    assert take_profit.orderStatus.status == stop_loss.orderStatus.status == "Cancelled"
    market = ib.trades()[-1].order
    assert (market.orderType, market.action, market.totalQuantity, market.orderRef) == (
        "MKT",
        "SELL",
        10,
        "SOFI-1:flat",
    )
    assert report.reason == "flatten"
    assert report.shares == 10


def test_flatten_sells_only_what_a_partial_stop_left():
    ib = FakeIB()
    client, order_id, (parent, _, stop_loss) = submit(ib)
    ib.fill(parent, 10, 10.0)
    ib.fill(stop_loss, 3, 9.8)

    report = client.close_position("SOFI-1", "SOFI", 10)

    assert ib.trades()[-1].order.totalQuantity == 7
    assert report.shares == 10


def test_positions_by_symbol():
    ib = FakeIB()
    ib._positions = [type("P", (), {"contract": Stock("HOOD"), "position": 2.0})()]

    assert IbkrExecutionClient(ib).positions() == {"HOOD": 2}
