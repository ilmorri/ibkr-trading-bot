from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from .risk import RiskManager
from .strategies.base import Action, Signal
from .strategies.opening_range import OpeningRange
from .strategies.vwap_reversion import VwapReversion


STRATEGIES = {
    "opening_range": OpeningRange,
    "vwap_reversion": VwapReversion,
}


@dataclass
class BotEvaluation:
    bot_id: str
    strategy: str
    action: str
    price: float
    stop: float | None
    target: float | None
    reason: str
    risk_allowed: bool | None
    shares: int = 0
    position_value_eur: float = 0.0
    risk_reason: str = ""


class Engine:
    """
    Offline engine skeleton.

    It evaluates enabled bots against supplied bars and asks the RiskManager
    whether each signal is allowed. It does not connect to IBKR yet.
    """

    def __init__(self, cfg: Mapping[str, Any]):
        self.cfg = dict(cfg)
        self.risk = RiskManager.from_config(self.cfg)

        self.bots: list[tuple[str, str, Any]] = []

        for item in self.cfg.get("universe", []):
            if not item.get("enabled", True):
                continue

            symbol = item["symbol"]
            strategy_name = item.get("strategy")

            if strategy_name not in STRATEGIES:
                raise ValueError(f"Unknown strategy for {symbol}: {strategy_name}")

            strategy_cls = STRATEGIES[strategy_name]
            params = {
                k: v
                for k, v in item.items()
                if k not in {"symbol", "name", "strategy", "enabled"}
            }

            self.bots.append((symbol, strategy_name, strategy_cls(symbol, params)))

    def run_once(
        self,
        bars_by_symbol: Mapping[str, pd.DataFrame],
        equity: float,
    ) -> list[BotEvaluation]:
        results: list[BotEvaluation] = []

        for symbol, strategy_name, strategy in self.bots:
            bars = bars_by_symbol.get(symbol)

            if bars is None or len(bars) == 0:
                results.append(
                    BotEvaluation(
                        bot_id=symbol,
                        strategy=strategy_name,
                        action="NO_DATA",
                        price=0.0,
                        stop=None,
                        target=None,
                        reason="no_bars_supplied",
                        risk_allowed=None,
                    )
                )
                continue

            signal: Signal = strategy.evaluate(bars)

            if signal.action == Action.BUY:
                if signal.stop is None:
                    results.append(
                        BotEvaluation(
                            bot_id=symbol,
                            strategy=strategy_name,
                            action=signal.action.value,
                            price=float(signal.price),
                            stop=None,
                            target=signal.target,
                            reason=signal.reason,
                            risk_allowed=None,
                            risk_reason="buy_signal_without_stop",
                        )
                    )
                    continue

                decision = self.risk.evaluate_entry(
                    bot_id=symbol,
                    equity=float(equity),
                    entry_price=float(signal.price),
                    stop_price=float(signal.stop),
                )

                if decision.allowed:
                    self.risk.on_entry_opened(symbol, decision.position_value_eur)

                    results.append(
                        BotEvaluation(
                            bot_id=symbol,
                            strategy=strategy_name,
                            action=signal.action.value,
                            price=float(signal.price),
                            stop=float(signal.stop),
                            target=signal.target,
                            reason=signal.reason,
                            risk_allowed=True,
                            shares=decision.shares,
                            position_value_eur=decision.position_value_eur,
                            risk_reason="ok",
                        )
                    )
                else:
                    results.append(
                        BotEvaluation(
                            bot_id=symbol,
                            strategy=strategy_name,
                            action=signal.action.value,
                            price=float(signal.price),
                            stop=float(signal.stop),
                            target=signal.target,
                            reason=signal.reason,
                            risk_allowed=False,
                            shares=0,
                            position_value_eur=0.0,
                            risk_reason=decision.reason,
                        )
                    )
            else:
                results.append(
                    BotEvaluation(
                        bot_id=symbol,
                        strategy=strategy_name,
                        action=signal.action.value,
                        price=float(signal.price),
                        stop=signal.stop,
                        target=signal.target,
                        reason=signal.reason,
                        risk_allowed=None,
                        risk_reason="no_trade_signal",
                    )
                )

        return results
