from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from .logger import get_logger
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
    bot_id: str
    strategy: str
    priority: int
    signal: Signal


@dataclass
class Evaluation:
    bot_id: str
    strategy: str
    action: str
    price: float
    stop: float | None
    target: float | None
    reason: str
    score: float
    net_rr: float
    score_reason: str
    note: str = ""


@dataclass
class EngineResult:
    evaluations: list[Evaluation]
    candidates: list[Candidate]


class Engine:
    """
    Signal engine.

    Responsibilities:
        - load enabled bots
        - evaluate strategies
        - collect valid BUY candidates
        - sort candidates by score

    It does NOT submit orders.
    It does NOT open positions.
    """

    def __init__(self, cfg: Mapping[str, Any]):
        self.cfg = dict(cfg)

        self.bots: list[tuple[str, str, Any, int]] = []

        cost_bps = self.cfg.get("costs", {}).get("estimated_cost_bps")

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
                key: value
                for key, value in item.items()
                if key not in {"symbol", "name", "strategy", "enabled", "priority"}
            }
            if cost_bps is not None:
                params.setdefault("estimated_cost_bps", cost_bps)

            self.bots.append(
                (
                    symbol,
                    strategy_name,
                    strategy_cls(symbol, params),
                    priority,
                )
            )

        logger.info(
            "Engine initialized with %d enabled bots: %s",
            len(self.bots),
            [bot[0] for bot in self.bots],
        )

    def evaluate(self, bars_by_symbol: Mapping[str, pd.DataFrame]) -> EngineResult:
        evaluations: list[Evaluation] = []
        candidates: list[Candidate] = []

        for symbol, strategy_name, strategy, priority in self.bots:
            bars = bars_by_symbol.get(symbol)

            if bars is None or len(bars) == 0:
                evaluations.append(
                    Evaluation(
                        bot_id=symbol,
                        strategy=strategy_name,
                        action="NO_DATA",
                        price=0.0,
                        stop=None,
                        target=None,
                        reason="no_bars_supplied",
                        score=0.0,
                        net_rr=0.0,
                        score_reason="",
                        note="no_data",
                    )
                )

                logger.warning(
                    "engine_no_data bot=%s strategy=%s",
                    symbol,
                    strategy_name,
                )

                continue

            signal: Signal = strategy.evaluate(bars)

            action = signal.action.value
            score = float(signal.score)
            net_rr = float(signal.net_rr)
            score_reason = str(signal.score_reason)

            logger.info(
                "engine_signal bot=%s strategy=%s priority=%d action=%s price=%s stop=%s "
                "target=%s reason=%s score=%.3f net_rr=%.3f score_reason=%s",
                symbol,
                strategy_name,
                priority,
                action,
                signal.price,
                signal.stop,
                signal.target,
                signal.reason,
                score,
                net_rr,
                score_reason,
            )

            is_candidate = (
                signal.action == Action.BUY
                and signal.stop is not None
                and signal.target is not None
                and score > 0.0
            )

            if is_candidate:
                candidates.append(
                    Candidate(
                        bot_id=symbol,
                        strategy=strategy_name,
                        priority=priority,
                        signal=signal,
                    )
                )

            evaluations.append(
                Evaluation(
                    bot_id=symbol,
                    strategy=strategy_name,
                    action=action,
                    price=float(signal.price),
                    stop=signal.stop,
                    target=signal.target,
                    reason=signal.reason,
                    score=score,
                    net_rr=net_rr,
                    score_reason=score_reason,
                    note="candidate" if is_candidate else "no_candidate",
                )
            )

        # Best opportunity first.
        # If score is identical, use static priority as tie-breaker.
        candidates.sort(key=lambda c: (-float(c.signal.score), -c.priority, c.bot_id))

        return EngineResult(
            evaluations=evaluations,
            candidates=candidates,
        )
