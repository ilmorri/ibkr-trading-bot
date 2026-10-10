from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class RiskDecision:
    allowed: bool
    shares: int
    position_value_eur: float
    reason: str


@dataclass
class RiskManager:
    max_concurrent_positions: int = 1
    max_gross_exposure_pct: float = 80.0
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    max_trades_per_bot_per_day: int = 1
    min_position_value_eur: float = 150.0
    max_position_value_eur: float = 240.0

    current_date: date = field(default_factory=date.today)
    open_positions: int = 0
    gross_exposure_eur: float = 0.0
    start_equity: float | None = None
    trades_by_bot: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "RiskManager":
        risk = cfg.get("risk", {})

        return cls(
            max_concurrent_positions=int(risk.get("max_concurrent_positions", 1)),
            max_gross_exposure_pct=float(risk.get("max_gross_exposure_pct", 80.0)),
            risk_per_trade_pct=float(risk.get("risk_per_trade_pct", 1.0)),
            max_daily_loss_pct=float(risk.get("max_daily_loss_pct", 2.0)),
            max_trades_per_bot_per_day=int(risk.get("max_trades_per_bot_per_day", 1)),
            min_position_value_eur=float(risk.get("min_position_value_eur", 150.0)),
            max_position_value_eur=float(risk.get("max_position_value_eur", 240.0)),
        )

    def _reset_day(self, equity: float) -> None:
        self.current_date = date.today()
        self.start_equity = equity
        self.trades_by_bot = {}

    def _roll_day_if_needed(self, equity: float) -> None:
        today = date.today()

        if today != self.current_date or self.start_equity is None:
            self._reset_day(equity)

    def evaluate_entry(
        self,
        *,
        bot_id: str,
        equity: float,
        entry_price: float,
        stop_price: float,
    ) -> RiskDecision:
        self._roll_day_if_needed(equity)

        if equity <= 0:
            return RiskDecision(False, 0, 0.0, "invalid_equity")

        if entry_price <= 0 or stop_price <= 0:
            return RiskDecision(False, 0, 0.0, "invalid_price")

        if stop_price >= entry_price:
            return RiskDecision(False, 0, 0.0, "stop_must_be_below_entry_for_long")

        stop_distance_pct = (entry_price - stop_price) / entry_price

        if stop_distance_pct <= 0 or stop_distance_pct > 0.50:
            return RiskDecision(False, 0, 0.0, "unreasonable_stop_distance")

        if self.open_positions >= self.max_concurrent_positions:
            return RiskDecision(
                False,
                0,
                0.0,
                f"max_concurrent_positions_reached_{self.max_concurrent_positions}",
            )

        bot_trades_today = self.trades_by_bot.get(bot_id, 0)

        if bot_trades_today >= self.max_trades_per_bot_per_day:
            return RiskDecision(
                False,
                0,
                0.0,
                f"max_trades_per_bot_per_day_reached_{bot_id}",
            )

        if self.start_equity is not None:
            daily_loss_limit = self.start_equity * (1.0 - self.max_daily_loss_pct / 100.0)

            if equity <= daily_loss_limit:
                return RiskDecision(
                    False,
                    0,
                    0.0,
                    f"daily_loss_limit_reached_equity_{equity:.2f}_limit_{daily_loss_limit:.2f}",
                )

        max_gross = equity * (self.max_gross_exposure_pct / 100.0)
        remaining_gross = max(0.0, max_gross - self.gross_exposure_eur)

        if remaining_gross < self.min_position_value_eur:
            return RiskDecision(
                False,
                0,
                0.0,
                f"not_enough_remaining_gross_{remaining_gross:.2f}",
            )

        risk_amount = equity * (self.risk_per_trade_pct / 100.0)
        notional_by_risk = risk_amount / stop_distance_pct

        notional = min(
            notional_by_risk,
            remaining_gross,
            self.max_position_value_eur,
        )

        shares = int(notional // entry_price)

        max_shares = int(self.max_position_value_eur // entry_price)

        if shares > max_shares:
            shares = max_shares

        position_value_eur = shares * entry_price

        if position_value_eur < self.min_position_value_eur:
            return RiskDecision(
                False,
                0,
                0.0,
                f"position_below_minimum_{position_value_eur:.2f}_{self.min_position_value_eur:.2f}",
            )

        return RiskDecision(
            True,
            shares,
            position_value_eur,
            "ok",
        )

    def on_entry_opened(self, bot_id: str, position_value_eur: float) -> None:
        self.open_positions += 1
        self.gross_exposure_eur += position_value_eur
        self.trades_by_bot[bot_id] = self.trades_by_bot.get(bot_id, 0) + 1

    def on_position_closed(self, position_value_eur: float) -> None:
        self.open_positions = max(0, self.open_positions - 1)
        self.gross_exposure_eur = max(0.0, self.gross_exposure_eur - position_value_eur)
