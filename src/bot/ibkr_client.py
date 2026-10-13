from __future__ import annotations

import time
from typing import Any

from ib_insync import IB, LimitOrder, MarketOrder, Stock, StopOrder, Trade

from .execution import ExecutionClient
from .logger import get_logger
from .orders import ExitReport, FillReport, OrderRequest, OrderStatus

logger = get_logger("ibkr")

# Child orders carry the entry's order_ref plus a suffix, so exits can be
# found from the fills IBKR reports, including after a restart.
TAKE_PROFIT = ":tp"
STOP_LOSS = ":sl"
FLATTEN = ":flat"

EXIT_REASONS = {TAKE_PROFIT: "target", STOP_LOSS: "stop", FLATTEN: "flatten"}

_STATUS = {
    "Filled": OrderStatus.FILLED.value,
    "Cancelled": OrderStatus.CANCELLED.value,
    "ApiCancelled": OrderStatus.CANCELLED.value,
    # "Inactive" is left PENDING on purpose: the engine times out and sends a
    # cancel, which is the safe outcome whether the order is dead or held.
}


def to_tick(price: float) -> float:
    # US stocks at or above $1 trade in whole cents; IBKR rejects other prices.
    return round(float(price), 2)


class IbkrExecutionClient(ExecutionClient):
    """
    Execution through ib_insync on an already connected `IB` instance.

    NOT YET TESTED AGAINST IBKR: "Read-Only API" is still on in IB Gateway.
    Before trusting it, run it on the paper account and check in particular
    what happens to the stop and target after a partial fill and a cancel.
    """

    def __init__(
        self,
        ib: IB,
        exchange: str = "SMART",
        currency: str = "USD",
        child_tif: str = "GTC",
        flatten_timeout_seconds: float = 30.0,
    ):
        self.ib = ib
        self.exchange = exchange
        self.currency = currency
        # GTC keeps the stop and target alive overnight for swing holds.
        self.child_tif = child_tif
        self.flatten_timeout_seconds = flatten_timeout_seconds
        self._contracts: dict[str, Any] = {}

    @classmethod
    def from_config(cls, ib: IB, cfg: dict) -> "IbkrExecutionClient":
        market = cfg.get("market", {})
        return cls(
            ib,
            exchange=market.get("exchange", "SMART"),
            currency=market.get("currency", "USD"),
            child_tif=cfg.get("ibkr", {}).get("child_tif", "GTC"),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _contract(self, symbol: str):
        if symbol not in self._contracts:
            contract = Stock(symbol, self.exchange, self.currency)
            qualified = self.ib.qualifyContracts(contract)
            if not qualified:
                raise ValueError(f"IBKR could not qualify contract {symbol}")
            self._contracts[symbol] = qualified[0]
        return self._contracts[symbol]

    def _trade(self, order_id: str) -> Trade | None:
        wanted = int(order_id)
        for trade in self.ib.trades():
            if trade.order.orderId == wanted:
                return trade
        return None

    def _children(self, order_id: str) -> list[Trade]:
        wanted = int(order_id)
        return [trade for trade in self.ib.trades() if trade.order.parentId == wanted]

    def _exit_fills(self, order_ref: str) -> list:
        refs = {order_ref + suffix for suffix in EXIT_REASONS}
        return [
            fill
            for fill in self.ib.fills()
            if fill.execution.orderRef in refs and fill.execution.side == "SLD"
        ]

    def _wait(self, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            self.ib.sleep(0.1)
        return True

    # ------------------------------------------------------------------
    # ExecutionClient
    # ------------------------------------------------------------------
    def submit_entry(self, request: OrderRequest) -> str:
        contract = self._contract(request.symbol)

        bracket = self.ib.bracketOrder(
            "BUY",
            request.shares,
            limitPrice=to_tick(request.price),
            takeProfitPrice=to_tick(request.target),
            stopLossPrice=to_tick(request.stop),
        )

        bracket.parent.tif = "DAY"
        bracket.parent.orderRef = request.order_ref
        bracket.takeProfit.tif = self.child_tif
        bracket.takeProfit.orderRef = request.order_ref + TAKE_PROFIT
        bracket.stopLoss.tif = self.child_tif
        bracket.stopLoss.orderRef = request.order_ref + STOP_LOSS

        # The stop is placed last: its transmit=True sends all three together.
        for order in bracket:
            self.ib.placeOrder(contract, order)

        order_id = str(bracket.parent.orderId)
        logger.info(
            "ibkr_bracket_placed order_id=%s ref=%s symbol=%s shares=%s limit=%.2f target=%.2f stop=%.2f",
            order_id,
            request.order_ref,
            request.symbol,
            request.shares,
            to_tick(request.price),
            to_tick(request.target),
            to_tick(request.stop),
        )
        return order_id

    def entry_state(self, order_id: str) -> FillReport:
        trade = self._trade(order_id)

        if trade is None:
            # Not in this session's orders or the open orders synced at connect,
            # so it is finished; today's fills tell whether it bought anything.
            fills = [f for f in self.ib.fills() if f.execution.orderId == int(order_id)]
            shares = int(sum(f.execution.shares for f in fills))
            if shares == 0:
                return FillReport(OrderStatus.CANCELLED.value)
            avg = sum(f.execution.shares * f.execution.price for f in fills) / shares
            return FillReport(OrderStatus.FILLED.value, shares, avg)

        status = _STATUS.get(trade.orderStatus.status, OrderStatus.PENDING.value)
        return FillReport(
            status,
            int(trade.orderStatus.filled),
            float(trade.orderStatus.avgFillPrice),
        )

    def cancel_order(self, order_id: str) -> None:
        trade = self._trade(order_id)
        if trade is not None and not trade.isDone():
            self.ib.cancelOrder(trade.order)
            logger.info("ibkr_cancel_sent order_id=%s", order_id)

    def resize_protection(self, order_id: str, shares: int) -> None:
        children = self._children(order_id)
        active = [child for child in children if not child.isDone()]

        if active:
            for child in active:
                child.order.totalQuantity = shares
                self.ib.placeOrder(child.contract, child.order)
            logger.warning("ibkr_protection_resized order_id=%s shares=%s", order_id, shares)
            return

        # The broker cancelled the children with the parent: the filled shares
        # are unprotected. Place a fresh stop and target as a one-cancels-other pair.
        if not children:
            raise RuntimeError(f"No stop/target found for order {order_id}; position is unprotected")

        ref = children[0].order.orderRef.rsplit(":", 1)[0]
        target = next(c.order.lmtPrice for c in children if c.order.orderRef.endswith(TAKE_PROFIT))
        stop = next(c.order.auxPrice for c in children if c.order.orderRef.endswith(STOP_LOSS))
        contract = children[0].contract

        take_profit = LimitOrder("SELL", shares, target, orderRef=ref + TAKE_PROFIT, tif=self.child_tif)
        stop_loss = StopOrder("SELL", shares, stop, orderRef=ref + STOP_LOSS, tif=self.child_tif)
        for order in (take_profit, stop_loss):
            order.ocaGroup = ref
            order.ocaType = 1  # cancel the other leg when one fills
            self.ib.placeOrder(contract, order)

        logger.warning("ibkr_protection_replaced order_id=%s shares=%s", order_id, shares)

    def exit_fill(self, order_ref: str, shares: int) -> ExitReport | None:
        fills = self._exit_fills(order_ref)
        sold = int(sum(f.execution.shares for f in fills))

        if sold < shares:
            return None

        price = sum(f.execution.shares * f.execution.price for f in fills) / sold
        # Round trip: the entry's commission is charged too, and by now it has been reported.
        trade_refs = {order_ref} | {order_ref + suffix for suffix in EXIT_REASONS}
        commission = sum(
            f.commissionReport.commission or 0.0
            for f in self.ib.fills()
            if f.execution.orderRef in trade_refs
        )
        # The leg that sold last decides the reason (a flatten after a partial stop is a flatten).
        suffix = ":" + fills[-1].execution.orderRef.rsplit(":", 1)[1]
        return ExitReport(price, sold, EXIT_REASONS.get(suffix, "unknown"), commission)

    def close_position(self, order_ref: str, symbol: str, shares: int) -> ExitReport:
        protective = [
            trade
            for trade in self.ib.openTrades()
            if trade.order.orderRef in (order_ref + TAKE_PROFIT, order_ref + STOP_LOSS)
        ]

        for trade in protective:
            self.ib.cancelOrder(trade.order)

        # Wait for the cancels: selling while the stop can still fire could sell twice.
        if not self._wait(lambda: all(t.isDone() for t in protective), self.flatten_timeout_seconds):
            raise RuntimeError(f"{symbol}: stop/target cancel not confirmed; not flattening")

        sold = int(sum(f.execution.shares for f in self._exit_fills(order_ref)))
        remaining = shares - sold

        if remaining > 0:
            order = MarketOrder("SELL", remaining, orderRef=order_ref + FLATTEN, tif="DAY")
            trade = self.ib.placeOrder(self._contract(symbol), order)
            logger.info("ibkr_flatten_sent symbol=%s shares=%s ref=%s", symbol, remaining, order_ref)

            if not self._wait(trade.isDone, self.flatten_timeout_seconds):
                raise RuntimeError(f"{symbol}: flatten order not filled within {self.flatten_timeout_seconds}s")

        report = self.exit_fill(order_ref, shares)
        if report is None:
            raise RuntimeError(f"{symbol}: flatten finished but the fills do not add up to {shares} shares")
        return report

    def positions(self) -> dict[str, int]:
        return {
            position.contract.symbol: int(position.position)
            for position in self.ib.positions()
            if position.position
        }

    def sleep(self, seconds: float) -> None:
        # Keeps ib_insync's event loop running while the engine waits.
        self.ib.sleep(seconds)
