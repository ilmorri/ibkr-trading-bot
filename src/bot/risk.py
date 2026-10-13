from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

EXCHANGE_TIMEZONE = "America/New_York"


def exchange_today(tz: str = EXCHANGE_TIMEZONE) -> date:
    # The trading day is the exchange's date, not the local (Italian) one.
    return datetime.now(ZoneInfo(tz)).date()


@dataclass
class RiskDecision:
    allowed: bool
    shares: int
    position_value: float
    reason: str


@dataclass
class RiskManager:
    max_concurrent_positions: int = 1
    max_gross_exposure_pct: float = 80.0
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    max_trades_per_bot_per_day: int = 1
    max_order_attempts_per_bot_per_day: int = 3
    min_position_value: float = 150.0
    max_position_value: float = 240.0

    timezone: str = EXCHANGE_TIMEZONE
    current_date: date = field(default_factory=exchange_today)

    # Filled positions only
    open_positions: int = 0
    gross_exposure: float = 0.0
    # Exposure of each open position, by bot. Survives the day roll: swing trades hold overnight.
    open_by_bot: dict[str, float] = field(default_factory=dict)

    # Today's closed-trade P&L, net of commissions. Drives the daily loss limit.
    realized_pnl: float = 0.0

    # Bots refused new entries until cleared, e.g. a broker position the bot does not know about.
    blocked_bots: dict[str, str] = field(default_factory=dict)

    # Orders submitted but not yet filled/cancelled
    reserved_positions: int = 0
    reserved_gross: float = 0.0

    start_equity: float | None = None

    # Filled trades only
    trades_by_bot: dict[str, int] = field(default_factory=dict)

    # All order attempts, filled or cancelled
    order_attempts_by_bot: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "RiskManager":
        risk = cfg.get("risk", {})

        return cls(
            max_concurrent_positions=int(risk.get("max_concurrent_positions", 1)),
            max_gross_exposure_pct=float(risk.get("max_gross_exposure_pct", 80.0)),
            risk_per_trade_pct=float(risk.get("risk_per_trade_pct", 1.0)),
            max_daily_loss_pct=float(risk.get("max_daily_loss_pct", 2.0)),
            max_trades_per_bot_per_day=int(risk.get("max_trades_per_bot_per_day", 1)),
            max_order_attempts_per_bot_per_day=int(
                risk.get("max_order_attempts_per_bot_per_day", 3)
            ),
            min_position_value=float(
                risk.get("min_position_value", risk.get("min_position_value_eur", 150.0))
            ),
            max_position_value=float(
                risk.get("max_position_value", risk.get("max_position_value_eur", 240.0))
            ),
            timezone=str(cfg.get("market", {}).get("timezone", EXCHANGE_TIMEZONE)),
        )

    def _reset_day(self, equity: float) -> None:
        # Open positions and blocks carry over; only the daily counters reset.
        self.current_date = exchange_today(self.timezone)
        self.start_equity = equity
        self.trades_by_bot = {}
        self.order_attempts_by_bot = {}
        self.realized_pnl = 0.0

    def _roll_day_if_needed(self, equity: float) -> None:
        today = exchange_today(self.timezone)

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

        if bot_id in self.blocked_bots:
            return RiskDecision(False, 0, 0.0, f"bot_blocked_{self.blocked_bots[bot_id]}")

        if self.open_positions + self.reserved_positions >= self.max_concurrent_positions:
            return RiskDecision(
                False,
                0,
                0.0,
                (
                    "max_concurrent_positions_reached_"
                    f"open_{self.open_positions}_"
                    f"reserved_{self.reserved_positions}"
                ),
            )

        bot_trades_today = self.trades_by_bot.get(bot_id, 0)

        if bot_trades_today >= self.max_trades_per_bot_per_day:
            return RiskDecision(
                False,
                0,
                0.0,
                f"max_trades_per_bot_per_day_reached_{bot_id}",
            )

        bot_attempts_today = self.order_attempts_by_bot.get(bot_id, 0)

        if bot_attempts_today >= self.max_order_attempts_per_bot_per_day:
            return RiskDecision(
                False,
                0,
                0.0,
                f"max_order_attempts_per_bot_per_day_reached_{bot_id}",
            )

        if bot_id in self.open_by_bot:
            return RiskDecision(False, 0, 0.0, f"position_already_open_{bot_id}")

        if self.start_equity is not None:
            daily_loss_limit = self.start_equity * (1.0 - self.max_daily_loss_pct / 100.0)
            # `equity` is the fixed sizing budget, so the day's realized P&L is added to it.
            equity_after_pnl = equity + self.realized_pnl

            if equity_after_pnl <= daily_loss_limit:
                return RiskDecision(
                    False,
                    0,
                    0.0,
                    (
                        "daily_loss_limit_reached_"
                        f"equity_{equity_after_pnl:.2f}_"
                        f"limit_{daily_loss_limit:.2f}"
                    ),
                )

        max_gross = equity * (self.max_gross_exposure_pct / 100.0)
        committed = self.gross_exposure + self.reserved_gross
        remaining_gross = max(0.0, max_gross - committed)

        if remaining_gross < self.min_position_value:
            return RiskDecision(
                False,
                0,
                0.0,
                (
                    "not_enough_remaining_gross_"
                    f"remaining_{remaining_gross:.2f}_"
                    f"min_{self.min_position_value:.2f}"
                ),
            )

        risk_amount = equity * (self.risk_per_trade_pct / 100.0)
        notional_by_risk = risk_amount / stop_distance_pct

        notional = min(
            notional_by_risk,
            remaining_gross,
            self.max_position_value,
        )

        shares = int(notional // entry_price)
        max_shares = int(self.max_position_value // entry_price)

        if shares > max_shares:
            shares = max_shares

        position_value = shares * entry_price

        if position_value < self.min_position_value:
            return RiskDecision(
                False,
                0,
                0.0,
                (
                    "position_below_minimum_"
                    f"position_{position_value:.2f}_"
                    f"min_{self.min_position_value:.2f}"
                ),
            )

        return RiskDecision(
            True,
            shares,
            position_value,
            "ok",
        )

    def on_order_submitted(self, bot_id: str, position_value: float) -> None:
        self.reserved_positions += 1
        self.reserved_gross += position_value
        self.order_attempts_by_bot[bot_id] = self.order_attempts_by_bot.get(bot_id, 0) + 1

    def on_order_filled(
        self,
        bot_id: str,
        position_value: float,
        filled_value: float | None = None,
    ) -> None:
        """
        `position_value` is what was reserved at submission and is released.
        `filled_value` is what actually filled (less on a partial fill) and
        becomes the exposure; it defaults to `position_value`.
        """
        if self.reserved_positions > 0:
            self.reserved_positions -= 1

        self.reserved_gross = max(0.0, self.reserved_gross - position_value)

        exposure = position_value if filled_value is None else filled_value

        self.open_positions += 1
        self.gross_exposure += exposure
        self.open_by_bot[bot_id] = exposure

        self.trades_by_bot[bot_id] = self.trades_by_bot.get(bot_id, 0) + 1

    def on_order_cancelled(self, bot_id: str, position_value: float) -> None:
        if self.reserved_positions > 0:
            self.reserved_positions -= 1

        self.reserved_gross = max(0.0, self.reserved_gross - position_value)

    def on_position_closed(
        self,
        bot_id: str,
        position_value: float | None = None,
        pnl: float = 0.0,
    ) -> None:
        exposure = self.open_by_bot.pop(bot_id, None)

        if position_value is None:
            position_value = exposure or 0.0

        if self.open_positions > 0:
            self.open_positions -= 1

        self.gross_exposure = max(0.0, self.gross_exposure - position_value)
        self.realized_pnl += pnl

    def block_bot(self, bot_id: str, reason: str) -> None:
        self.blocked_bots[bot_id] = reason

    def unblock_bot(self, bot_id: str) -> None:
        self.blocked_bots.pop(bot_id, None)

    # Persistence: state only, limits always come from the config.
    _STATE_FIELDS = (
        "current_date",
        "open_positions",
        "gross_exposure",
        "open_by_bot",
        "realized_pnl",
        "blocked_bots",
        "reserved_positions",
        "reserved_gross",
        "start_equity",
        "trades_by_bot",
        "order_attempts_by_bot",
    )

    def state_dict(self) -> dict[str, Any]:
        data = asdict(self)
        state = {key: data[key] for key in self._STATE_FIELDS}
        state["current_date"] = self.current_date.isoformat()
        return state

    def load_state(self, state: dict[str, Any]) -> None:
        for key in self._STATE_FIELDS:
            if key in state:
                setattr(self, key, state[key])

        self.current_date = date.fromisoformat(str(self.current_date))
