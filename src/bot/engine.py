from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from .logger import get_logger
from .risk import RiskManager
from .strategies.base import Action, Signal
from .strategies.opening_range import OpeningRange
from .strategies.vwap_reversion import VwapReversion

logger = get_logger("engine")

STRATEGIES = {
    "opening_range": OpeningRange,
    "vwap_reversion": VwapReversion,
}


@dataclass
class Candidate:
    score: float
    symbol: str
    strategy_name: str
    priority: int
    signal: Signal


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
    score: float = 0.0
    net_rr: float = 0.0
    score_reason: str = ""


class Engine:
    """
    Offline / paper engine.

    It evaluates enabled bots against supplied bars, scores valid BUY signals,
    then asks the RiskManager whether the best available signal is allowed.
    """

    def __init__(self, cfg: Mapping[str, Any]):
        self.cfg = dict(cfg)
        self.risk = RiskManager.from_config(self.cfg)

        self.bots: list[tuple[str, str, Any, int]] = []

        for item in self.cfg.get("universe", []):
            if not item.get("enabled", True):
                continue

            symbol = item["symbol"]
            strategy_name = item.get("strategy")
            priority = int(item.get("priority", 0))

            if strategy_name not in STRATEGIES:
                raise ValueError(f"Unknown strategy for {symbol}: {strategy_name}")

            strategy_cls = STRATEGIES[strategy_name]

            params = {
                k: v
                for k, v in item.items()
                if k not in {"symbol", "name", "strategy", "enabled", "priority"}
            }

            self.bots.append(
                (
                    symbol,
                    strategy_name,
                    strategy_cls(symbol, params),
                    priority,
                )
            )

        # Static priority is now only a tie-breaker.
        self.bots.sort(key=lambda x: (-x[3], x[0]))

        logger.info(
            "Engine initialized with %d enabled bots: %s",
            len(self.bots),
            [b[0] for b in self.bots],
        )

    def run_once(
        self,
        bars_by_symbol: Mapping[str, pd.DataFrame],
        equity: float,
    ) -> list[BotEvaluation]:
        results: list[BotEvaluation] = []
        candidates: list[Candidate] = []

        for symbol, strategy_name, strategy, priority in self.bots:
            bars = bars_by_symbol.get(symbol)

            if bars is None or len(bars) == 0:
                result = BotEvaluation(
                    bot_id=symbol,
                    strategy=strategy_name,
                    action="NO_DATA",
                    price=0.0,
                    stop=None,
                    target=None,
                    reason="no_bars_supplied",
                    risk_allowed=None,
                )

                logger.warning(
                    "bot=%s strategy=%s priority=%d action=%s reason=%s",
                    symbol,
                    strategy_name,
                    priority,
                    result.action,
                    result.reason,
                )

                results.append(result)
                continue

            signal: Signal = strategy.evaluate(bars)

            logger.info(
                "bot=%s strategy=%s priority=%d action=%s price=%s stop=%s target=%s "
                "reason=%s net_rr=%.2f score=%.2f score_reason=%s",
                symbol,
                strategy_name,
                priority,
                signal.action.value,
                signal.price,
                signal.stop,
                signal.target,
                signal.reason,
                signal.net_rr,
                signal.score,
                signal.score_reason,
            )

            if signal.action == Action.BUY and signal.stop is not None and signal.target is not None:
                if signal.score <= 0.0:
                    result = BotEvaluation(
                        bot_id=symbol,
                        strategy=strategy_name,
                        action=signal.action.value,
                        price=float(signal.price),
                        stop=float(signal.stop),
                        target=float(signal.target),
                        reason=signal.reason,
                        risk_allowed=None,
                        risk_reason="zero_score",
                        score=float(signal.score),
                        net_rr=float(signal.net_rr),
                        score_reason=signal.score_reason,
                    )
                    results.append(result)
                else:
                    candidates.append(
                        Candidate(
                            score=float(signal.score),
                            symbol=symbol,
                            strategy_name=strategy_name,
                            priority=priority,
                            signal=signal,
                        )
                    )
            else:
                result = BotEvaluation(
                    bot_id=symbol,
                    strategy=strategy_name,
                    action=signal.action.value,
                    price=float(signal.price),
                    stop=signal.stop,
                    target=signal.target,
                    reason=signal.reason,
                    risk_allowed=None,
                    risk_reason="no_trade_signal",
                    score=float(signal.score),
                    net_rr=float(signal.net_rr),
                    score_reason=signal.score_reason,
                )
                results.append(result)

        # Best opportunity first.
        # If score is equal, use static priority as tie-breaker.
        candidates.sort(key=lambda c: (-c.score, -c.priority, c.symbol))

        for candidate in candidates:
            signal = candidate.signal

            decision = self.risk.evaluate_entry(
                bot_id=candidate.symbol,
                equity=float(equity),
                entry_price=float(signal.price),
                stop_price=float(signal.stop),
            )

            logger.info(
                "bot=%s risk_allowed=%s shares=%s position_value_eur=%.2f risk_reason=%s",
                candidate.symbol,
                decision.allowed,
                decision.shares,
                decision.position_value_eur,
                decision.reason,
            )

            if decision.allowed:
                self.risk.on_entry_opened(candidate.symbol, decision.position_value_eur)

            result = BotEvaluation(
                bot_id=candidate.symbol,
                strategy=candidate.strategy_name,
                action=signal.action.value,
                price=float(signal.price),
                stop=float(signal.stop),
                target=float(signal.target),
                reason=signal.reason,
                risk_allowed=bool(decision.allowed),
                shares=int(decision.shares),
                position_value_eur=float(decision.position_value_eur),
                risk_reason=decision.reason,
                score=float(signal.score),
                net_rr=float(signal.net_rr),
                score_reason=signal.score_reason,
            )

            results.append(result)

        return results
