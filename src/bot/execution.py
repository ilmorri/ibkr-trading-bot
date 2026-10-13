from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Callable, Sequence

from .logger import get_logger
from .orders import (
    ClosedTrade,
    ExitReport,
    FillReport,
    OpenTrade,
    OrderAttempt,
    OrderRequest,
    OrderStatus,
)
from .risk import RiskManager

logger = get_logger("execution")

TERMINAL_STATUSES = {
    OrderStatus.FILLED.value,
    OrderStatus.CANCELLED.value,
    OrderStatus.REJECTED.value,
}


class ExecutionClient(ABC):
    """
    Interface for an execution backend.

    Every entry is a bracket: a limit buy with a server-side stop and target
    attached, so the position stays protected if the bot stops running.
    Orders are identified by the entry order id; exits are looked up by the
    entry's order_ref, which survives a restart.
    """

    @abstractmethod
    def submit_entry(self, request: OrderRequest) -> str:
        """Place the bracket. Returns the entry order id."""

    @abstractmethod
    def entry_state(self, order_id: str) -> FillReport:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        """Cancel the entry. The broker cancels the attached stop and target with it."""

    @abstractmethod
    def resize_protection(self, order_id: str, shares: int) -> None:
        """After a partial fill, shrink the stop and target to the shares actually held."""

    @abstractmethod
    def exit_fill(self, order_ref: str, shares: int) -> ExitReport | None:
        """The completed stop or target exit for this trade, or None while it is still open."""

    @abstractmethod
    def close_position(self, order_ref: str, symbol: str, shares: int) -> ExitReport:
        """Cancel the stop and target and sell at market."""

    @abstractmethod
    def positions(self) -> dict[str, int]:
        """Shares held at the broker, by symbol."""

    def sleep(self, seconds: float) -> None:
        # Backends with an event loop (ib_insync) must override this.
        time.sleep(seconds)


class MockExecutionClient(ExecutionClient):
    """
    Mock execution client for offline testing.

    outcomes example:
        {
            "INTC": ["CANCELLED"],       # never fills
            "SOFI": ["FILLED"],          # fills in full
            "HOOD": ["PARTIAL:3"],       # 3 shares fill, the rest times out
            "F":    ["FILLS_ON_CANCEL"], # pending until cancelled, then fills in full
            "T":    ["STUCK"],           # pending, and the cancel is never confirmed
        }

    Exits are scripted with `set_exit(order_ref, price, reason)`.
    """

    def __init__(
        self,
        outcomes: dict[str, list[str]] | None = None,
        default: str = OrderStatus.FILLED.value,
        broker_positions: dict[str, int] | None = None,
    ):
        self.outcomes = outcomes or {}
        self.default = default
        self._counts: dict[str, int] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self._exits: dict[str, ExitReport] = {}
        self.broker_positions: dict[str, int] = dict(broker_positions or {})
        self.submitted: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.resized: dict[str, int] = {}
        self.closed: list[str] = []

    def submit_entry(self, request: OrderRequest) -> str:
        self.submitted.append(request)

        self._counts[request.bot_id] = self._counts.get(request.bot_id, 0) + 1
        attempt = self._counts[request.bot_id]

        outcomes = self.outcomes.get(request.bot_id, [self.default])
        outcome = outcomes[min(attempt - 1, len(outcomes) - 1)].upper()

        order_id = f"MOCK-{request.bot_id}-{attempt}"
        self._orders[order_id] = {"request": request, "outcome": outcome, "cancelled": False}
        logger.info("mock_order_submitted order_id=%s bot=%s outcome=%s", order_id, request.bot_id, outcome)
        return order_id

    def entry_state(self, order_id: str) -> FillReport:
        order = self._orders[order_id]
        request: OrderRequest = order["request"]
        outcome: str = order["outcome"]
        cancelled: bool = order["cancelled"]

        if outcome == OrderStatus.FILLED.value:
            return self._filled(request, request.shares, OrderStatus.FILLED.value)

        if outcome.startswith("PARTIAL:"):
            shares = int(outcome.split(":", 1)[1])
            status = OrderStatus.CANCELLED.value if cancelled else OrderStatus.PENDING.value
            return self._filled(request, shares, status)

        if outcome == "FILLS_ON_CANCEL":
            if cancelled:
                return self._filled(request, request.shares, OrderStatus.FILLED.value)
            return FillReport(OrderStatus.PENDING.value)

        if outcome == "STUCK":
            return FillReport(OrderStatus.PENDING.value)

        if outcome == OrderStatus.CANCELLED.value and not cancelled:
            # A resting limit that never fills: pending until the bot cancels it.
            return FillReport(OrderStatus.PENDING.value)

        return FillReport(outcome)

    def _filled(self, request: OrderRequest, shares: int, status: str) -> FillReport:
        self.broker_positions[request.symbol] = shares
        return FillReport(status, shares, request.price)

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self._orders[order_id]["cancelled"] = True
        logger.info("mock_order_cancelled order_id=%s", order_id)

    def resize_protection(self, order_id: str, shares: int) -> None:
        self.resized[order_id] = shares

    def set_exit(self, order_ref: str, price: float, reason: str, commission: float = 0.0) -> None:
        for order in self._orders.values():
            request: OrderRequest = order["request"]
            if request.order_ref == order_ref:
                shares = self.broker_positions.pop(request.symbol, 0)
                self._exits[order_ref] = ExitReport(price, shares, reason, commission)
                return
        raise KeyError(order_ref)

    def exit_fill(self, order_ref: str, shares: int) -> ExitReport | None:
        return self._exits.get(order_ref)

    def close_position(self, order_ref: str, symbol: str, shares: int) -> ExitReport:
        self.closed.append(order_ref)
        self.broker_positions.pop(symbol, None)
        price = 0.0
        for order in self._orders.values():
            if order["request"].order_ref == order_ref:
                price = order["request"].price
        return ExitReport(price, shares, "flatten")

    def positions(self) -> dict[str, int]:
        return dict(self.broker_positions)

    def sleep(self, seconds: float) -> None:
        pass


@dataclass
class PendingOrder:
    """
    An entry sent to the broker whose final state is not yet known. It is
    saved as soon as it is submitted, so a crash cannot lose it, and its risk
    reservation stays held until it is settled.
    """

    order_id: str
    request: OrderRequest
    reserved_value: float


class ExecutionEngine:
    def __init__(
        self,
        risk: RiskManager,
        client: ExecutionClient,
        order_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.05,
        cancel_timeout_seconds: float = 5.0,
        on_change: Callable[["ExecutionEngine"], None] | None = None,
    ):
        self.risk = risk
        self.client = client
        self.order_timeout_seconds = order_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.cancel_timeout_seconds = cancel_timeout_seconds
        # Called after every state change, e.g. to save state to disk.
        self.on_change = on_change

        self.open_trades: dict[str, OpenTrade] = {}
        self.pending_orders: dict[str, PendingOrder] = {}
        self.closed_trades: list[ClosedTrade] = []

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------
    def _wait_for_terminal(self, order_id: str, timeout: float) -> FillReport:
        deadline = time.monotonic() + timeout

        while True:
            report = self.client.entry_state(order_id)

            if report.status in TERMINAL_STATUSES:
                return report

            if time.monotonic() >= deadline:
                return report

            self.client.sleep(self.poll_interval_seconds)

    def _candidate_fields(self, candidate: Any) -> tuple[str, str, Any]:
        signal = getattr(candidate, "signal", None)

        if signal is None:
            raise ValueError("Candidate must have a .signal attribute")

        bot_id = getattr(candidate, "bot_id", "")
        strategy = getattr(candidate, "strategy", "unknown")

        if not bot_id:
            raise ValueError("Candidate must have a .bot_id attribute")

        return bot_id, strategy, signal

    def execute_candidates(
        self,
        candidates: Sequence[Any],
        equity: float,
    ) -> list[OrderAttempt]:
        attempts: list[OrderAttempt] = []

        for candidate in candidates:
            bot_id, strategy_name, signal = self._candidate_fields(candidate)

            price = float(signal.price)
            stop = float(signal.stop)
            target = float(signal.target)
            score = float(getattr(signal, "score", 0.0))
            net_rr = float(getattr(signal, "net_rr", 0.0))
            score_reason = str(getattr(signal, "score_reason", ""))
            action = signal.action.value if hasattr(signal.action, "value") else str(signal.action)

            def attempt(**kwargs: Any) -> OrderAttempt:
                fields = dict(
                    bot_id=bot_id,
                    symbol=bot_id,
                    strategy=strategy_name,
                    action=action,
                    price=price,
                    stop=stop,
                    target=target,
                    score=score,
                    net_rr=net_rr,
                    score_reason=score_reason,
                )
                fields.update(kwargs)
                return OrderAttempt(**fields)

            decision = self.risk.evaluate_entry(
                bot_id=bot_id,
                equity=float(equity),
                entry_price=price,
                stop_price=stop,
            )

            logger.info(
                "execution_risk bot=%s allowed=%s shares=%s position_value=%.2f reason=%s",
                bot_id,
                decision.allowed,
                decision.shares,
                decision.position_value,
                decision.reason,
            )

            if not decision.allowed:
                attempts.append(
                    attempt(
                        order_id="",
                        status=OrderStatus.SKIPPED.value,
                        shares=0,
                        position_value=0.0,
                        filled=False,
                        trade_opened=False,
                        exit_reason=decision.reason,
                        note="risk_manager_blocked",
                    )
                )
                continue

            request = OrderRequest(
                bot_id=bot_id,
                symbol=bot_id,
                strategy=strategy_name,
                action="BUY",
                shares=decision.shares,
                price=price,
                stop=stop,
                target=target,
                score=score,
                net_rr=net_rr,
                score_reason=score_reason,
                order_ref=f"{bot_id}-{int(time.time() * 1000)}",
                position_value=decision.position_value,
            )

            self.risk.on_order_submitted(bot_id, decision.position_value)

            logger.info(
                "execution_submit bot=%s order_ref=%s shares=%s price=%.4f stop=%.4f target=%.4f",
                bot_id,
                request.order_ref,
                decision.shares,
                price,
                stop,
                target,
            )

            order_id = self.client.submit_entry(request)
            self.pending_orders[order_id] = PendingOrder(order_id, request, decision.position_value)
            self._changed()

            report = self._wait_for_terminal(order_id, self.order_timeout_seconds)

            if report.status not in TERMINAL_STATUSES:
                # Timed out. Cancel, then wait for the final state: the order
                # can still fill, fully or partly, before the cancel lands.
                self.client.cancel_order(order_id)
                report = self._wait_for_terminal(order_id, self.cancel_timeout_seconds)

            if report.status not in TERMINAL_STATUSES:
                # Neither filled nor confirmed cancelled. It stays pending with
                # its reservation held, so risk cannot be double-booked.
                logger.error(
                    "execution_unresolved bot=%s order_id=%s filled=%s",
                    bot_id,
                    order_id,
                    report.filled_shares,
                )
                attempts.append(
                    attempt(
                        order_id=order_id,
                        status=OrderStatus.UNRESOLVED.value,
                        shares=decision.shares,
                        position_value=decision.position_value,
                        filled=False,
                        trade_opened=False,
                        exit_reason="cancel_not_confirmed",
                        note="reservation_held",
                    )
                )
                continue

            trade = self._settle_entry(order_id, request, decision.position_value, report)

            if trade is not None:
                attempts.append(
                    attempt(
                        order_id=order_id,
                        status=OrderStatus.FILLED.value,
                        shares=trade.shares,
                        price=trade.entry_price,
                        position_value=trade.position_value,
                        filled=True,
                        trade_opened=True,
                        exit_reason="order_filled",
                        note=(
                            "position_open"
                            if trade.shares == decision.shares
                            else f"partial_fill_{trade.shares}_of_{decision.shares}"
                        ),
                    )
                )
            else:
                attempts.append(
                    attempt(
                        order_id=order_id,
                        status=report.status,
                        shares=decision.shares,
                        position_value=decision.position_value,
                        filled=False,
                        trade_opened=False,
                        exit_reason=f"order_{report.status.lower()}_not_filled",
                        note="no_position_open",
                    )
                )

        return attempts

    def _settle_entry(
        self,
        order_id: str,
        request: OrderRequest,
        reserved_value: float,
        report: FillReport,
    ) -> OpenTrade | None:
        """Turn a terminal entry report into an open Trade, or release the reservation."""
        self.pending_orders.pop(order_id, None)
        filled = int(report.filled_shares)

        if filled <= 0:
            self.risk.on_order_cancelled(request.bot_id, reserved_value)
            self._changed()
            return None

        if filled < request.shares:
            self.client.resize_protection(order_id, filled)
            logger.warning(
                "execution_partial_fill bot=%s order_id=%s filled=%s of=%s",
                request.bot_id,
                order_id,
                filled,
                request.shares,
            )

        entry_price = float(report.avg_fill_price or request.price)
        filled_value = filled * entry_price

        self.risk.on_order_filled(request.bot_id, reserved_value, filled_value)

        trade = OpenTrade(
            bot_id=request.bot_id,
            symbol=request.symbol,
            strategy=request.strategy,
            entry_order_id=order_id,
            order_ref=request.order_ref,
            shares=filled,
            entry_price=entry_price,
            stop=request.stop,
            target=request.target,
            position_value=filled_value,
        )
        self.open_trades[request.bot_id] = trade
        logger.info(
            "trade_opened bot=%s shares=%s entry=%.4f stop=%.4f target=%.4f",
            trade.bot_id,
            trade.shares,
            trade.entry_price,
            trade.stop,
            trade.target,
        )
        self._changed()
        return trade

    def resolve_pending(self) -> list[OpenTrade]:
        """Settle pending entries the broker now reports as final. Returns trades that turned out filled."""
        opened: list[OpenTrade] = []

        for order_id, item in list(self.pending_orders.items()):
            report = self.client.entry_state(order_id)

            if report.status not in TERMINAL_STATUSES:
                continue

            trade = self._settle_entry(order_id, item.request, item.reserved_value, report)

            if trade is not None:
                opened.append(trade)

        return opened

    # ------------------------------------------------------------------
    # Exits
    # ------------------------------------------------------------------
    def _close(self, trade: OpenTrade, exit_report: ExitReport) -> ClosedTrade:
        pnl = (exit_report.exit_price - trade.entry_price) * trade.shares - exit_report.commission

        self.open_trades.pop(trade.bot_id, None)
        self.risk.on_position_closed(trade.bot_id, trade.position_value, pnl)

        closed = ClosedTrade(
            bot_id=trade.bot_id,
            symbol=trade.symbol,
            strategy=trade.strategy,
            order_ref=trade.order_ref,
            shares=trade.shares,
            entry_price=trade.entry_price,
            exit_price=exit_report.exit_price,
            exit_reason=exit_report.reason,
            pnl=round(pnl, 4),
            opened_at=trade.opened_at,
        )
        self.closed_trades.append(closed)

        logger.info(
            "trade_closed bot=%s reason=%s entry=%.4f exit=%.4f shares=%s pnl=%.2f realized_today=%.2f",
            closed.bot_id,
            closed.exit_reason,
            closed.entry_price,
            closed.exit_price,
            closed.shares,
            closed.pnl,
            self.risk.realized_pnl,
        )
        self._changed()
        return closed

    def check_exits(self) -> list[ClosedTrade]:
        """Record trades whose stop or target has filled at the broker."""
        closed: list[ClosedTrade] = []

        for trade in list(self.open_trades.values()):
            exit_report = self.client.exit_fill(trade.order_ref, trade.shares)

            if exit_report is not None:
                closed.append(self._close(trade, exit_report))

        return closed

    def flatten_all(self) -> list[ClosedTrade]:
        """Sell every open position at market, e.g. at the flatten time."""
        closed = self.check_exits()

        for trade in list(self.open_trades.values()):
            try:
                exit_report = self.client.close_position(trade.order_ref, trade.symbol, trade.shares)
            except Exception:
                # Never book a close that did not happen; the trade stays open for a retry.
                logger.exception("flatten_failed bot=%s order_ref=%s", trade.bot_id, trade.order_ref)
                continue

            closed.append(self._close(trade, exit_report))

        return closed

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------
    def reconcile(self) -> list[str]:
        """
        Compare the bot's open trades with the broker's positions.

        - A trade the broker no longer holds: look for its stop or target
          fill; if none is found it was closed outside the bot and is recorded
          at its entry price (P&L unknown, logged as an error).
        - A broker position the bot does not know, or a share count that
          differs: the bot is blocked from new entries until a human checks.

        Returns a list of human-readable problems; empty means all is in order.
        """
        problems: list[str] = []

        self.resolve_pending()
        broker = {symbol: int(shares) for symbol, shares in self.client.positions().items() if shares}

        for trade in list(self.open_trades.values()):
            held = broker.get(trade.symbol, 0)

            if held == trade.shares:
                continue

            if held == 0:
                exit_report = self.client.exit_fill(trade.order_ref, trade.shares)

                if exit_report is None:
                    exit_report = ExitReport(trade.entry_price, trade.shares, "closed_outside_bot")
                    problems.append(
                        f"{trade.bot_id}: position closed outside the bot; P&L unknown, recorded as 0"
                    )

                self._close(trade, exit_report)
                continue

            problems.append(f"{trade.bot_id}: bot holds {trade.shares} shares, broker holds {held}")
            self.risk.block_bot(trade.bot_id, "share_count_mismatch")

        known = {trade.symbol for trade in self.open_trades.values()}
        pending = {item.request.symbol for item in self.pending_orders.values()}

        for symbol, shares in broker.items():
            if symbol in known or symbol in pending:
                continue

            problems.append(f"{symbol}: broker holds {shares} shares the bot does not know about")
            self.risk.block_bot(symbol, "unknown_broker_position")

        for problem in problems:
            logger.error("reconcile_problem %s", problem)

        if problems:
            self._changed()

        return problems

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self)

    def state_dict(self) -> dict[str, Any]:
        return {
            "open_trades": [asdict(trade) for trade in self.open_trades.values()],
            "pending_orders": [
                {
                    "order_id": item.order_id,
                    "request": asdict(item.request),
                    "reserved_value": item.reserved_value,
                }
                for item in self.pending_orders.values()
            ],
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.open_trades = {
            item["bot_id"]: OpenTrade(**item) for item in state.get("open_trades", [])
        }
        self.pending_orders = {
            item["order_id"]: PendingOrder(
                item["order_id"],
                OrderRequest(**item["request"]),
                float(item["reserved_value"]),
            )
            for item in state.get("pending_orders", [])
        }
