from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Sequence

from .logger import get_logger
from .orders import OrderAttempt, OrderRequest, OrderStatus
from .risk import RiskManager

logger = get_logger("execution")


class ExecutionClient(ABC):
    """
    Interface for an execution backend.

    Later we will implement:
        IbkrExecutionClient

    For now we use:
        MockExecutionClient
    """

    @abstractmethod
    def submit_limit_buy(self, request: OrderRequest) -> str:
        raise NotImplementedError

    @abstractmethod
    def order_status(self, order_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError


class MockExecutionClient(ExecutionClient):
    """
    Mock execution client for offline testing.

    outcomes example:
        {
            "STLA": ["CANCELLED"],
            "BPER": ["FILLED"],
        }
    """

    def __init__(
        self,
        outcomes: dict[str, list[str]] | None = None,
        default: str = OrderStatus.FILLED.value,
    ):
        self.outcomes = outcomes or {}
        self.default = default
        self._counts: dict[str, int] = {}
        self.submitted: list[OrderRequest] = []
        self.cancelled: list[str] = []

    def submit_limit_buy(self, request: OrderRequest) -> str:
        self.submitted.append(request)

        self._counts[request.bot_id] = self._counts.get(request.bot_id, 0) + 1

        order_id = f"MOCK-{request.bot_id}-{self._counts[request.bot_id]}"
        logger.info("mock_order_submitted order_id=%s bot=%s", order_id, request.bot_id)
        return order_id

    def order_status(self, order_id: str) -> str:
        parts = order_id.split("-")

        if len(parts) < 3:
            return self.default

        bot_id = "-".join(parts[1:-1])
        attempt_number = int(parts[-1])

        outcomes = self.outcomes.get(bot_id, [self.default])
        index = min(attempt_number - 1, len(outcomes) - 1)

        status = outcomes[index].upper()
        logger.info("mock_order_status order_id=%s status=%s", order_id, status)

        return status

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        logger.info("mock_order_cancelled order_id=%s", order_id)


class ExecutionEngine:
    def __init__(
        self,
        risk: RiskManager,
        client: ExecutionClient,
        order_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.05,
    ):
        self.risk = risk
        self.client = client
        self.order_timeout_seconds = order_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def _terminal_statuses(self) -> set[str]:
        return {
            OrderStatus.FILLED.value,
            OrderStatus.CANCELLED.value,
            OrderStatus.REJECTED.value,
        }

    def _poll_status(self, order_id: str) -> str:
        deadline = time.monotonic() + self.order_timeout_seconds

        while True:
            status = str(self.client.order_status(order_id)).upper()

            if status in self._terminal_statuses():
                return status

            if time.monotonic() >= deadline:
                return OrderStatus.TIMED_OUT.value

            time.sleep(self.poll_interval_seconds)

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

            decision = self.risk.evaluate_entry(
                bot_id=bot_id,
                equity=float(equity),
                entry_price=price,
                stop_price=stop,
            )

            logger.info(
                "execution_risk bot=%s allowed=%s shares=%s position_value_eur=%.2f reason=%s",
                bot_id,
                decision.allowed,
                decision.shares,
                decision.position_value_eur,
                decision.reason,
            )

            if not decision.allowed:
                attempts.append(
                    OrderAttempt(
                        bot_id=bot_id,
                        symbol=bot_id,
                        strategy=strategy_name,
                        action=action,
                        order_id="",
                        status=OrderStatus.SKIPPED.value,
                        shares=0,
                        price=price,
                        stop=stop,
                        target=target,
                        score=score,
                        net_rr=net_rr,
                        score_reason=score_reason,
                        position_value_eur=0.0,
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
                position_value_eur=decision.position_value_eur,
            )

            self.risk.on_order_submitted(bot_id, decision.position_value_eur)

            logger.info(
                "execution_submit bot=%s order_ref=%s shares=%s price=%.4f stop=%.4f target=%.4f",
                bot_id,
                request.order_ref,
                decision.shares,
                price,
                stop,
                target,
            )

            order_id = self.client.submit_limit_buy(request)
            status = self._poll_status(order_id)

            if status == OrderStatus.FILLED.value:
                self.risk.on_order_filled(bot_id, decision.position_value_eur)

                attempts.append(
                    OrderAttempt(
                        bot_id=bot_id,
                        symbol=bot_id,
                        strategy=strategy_name,
                        action=action,
                        order_id=order_id,
                        status=OrderStatus.FILLED.value,
                        shares=decision.shares,
                        price=price,
                        stop=stop,
                        target=target,
                        score=score,
                        net_rr=net_rr,
                        score_reason=score_reason,
                        position_value_eur=decision.position_value_eur,
                        filled=True,
                        trade_opened=True,
                        exit_reason="order_filled",
                        note="position_open",
                    )
                )

            else:
                self.client.cancel_order(order_id)
                self.risk.on_order_cancelled(bot_id, decision.position_value_eur)

                attempts.append(
                    OrderAttempt(
                        bot_id=bot_id,
                        symbol=bot_id,
                        strategy=strategy_name,
                        action=action,
                        order_id=order_id,
                        status=status,
                        shares=decision.shares,
                        price=price,
                        stop=stop,
                        target=target,
                        score=score,
                        net_rr=net_rr,
                        score_reason=score_reason,
                        position_value_eur=decision.position_value_eur,
                        filled=False,
                        trade_opened=False,
                        exit_reason=f"order_{status.lower()}_not_filled",
                        note="no_position_open",
                    )
                )

        return attempts
