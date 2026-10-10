from __future__ import annotations

import pandas as pd
from .base import Action, BaseStrategy, Signal


class OpeningRange(BaseStrategy):
    """
    Intraday opening range breakout.

    Logic:
    1. Wait for the first `range_minutes` bars after market open.
    2. Compute range high and range low.
    3. If current close breaks above range high, go long.
    4. Stop below range low.
    5. Target one range size above entry.

    Conservative version:
    - long only
    - no short in v1
    - one entry decision per bar evaluation
    """

    name = "opening_range"

    def __init__(self, symbol: str, params: dict | None = None):
        default_params = {
            "range_minutes": 15,
            "target_range_multiple": 1.0,
        }
        default_params.update(params or {})
        super().__init__(symbol, default_params)

    def evaluate(self, bars: pd.DataFrame) -> Signal:
        if bars is None or len(bars) < 2:
            return Signal(
                action=Action.FLAT,
                price=float(bars["close"].iloc[-1]) if bars is not None and len(bars) else 0.0,
                stop=None,
                target=None,
                reason="not_enough_bars",
            )

        range_minutes = int(self.params.get("range_minutes", 15))
        target_multiple = float(self.params.get("target_range_multiple", 1.0))

        first_time = pd.Timestamp(bars["timestamp"].iloc[0])
        range_end = first_time + pd.Timedelta(minutes=range_minutes)

        current = bars.iloc[-1]
        current_time = pd.Timestamp(current["timestamp"])

        if current_time < range_end:
            return Signal(
                action=Action.FLAT,
                price=float(current["close"]),
                stop=None,
                target=None,
                reason="waiting_for_opening_range",
                timestamp=str(current_time),
            )

        range_bars = bars[bars["timestamp"] < range_end]

        if len(range_bars) < 2:
            return Signal(
                action=Action.FLAT,
                price=float(current["close"]),
                stop=None,
                target=None,
                reason="opening_range_incomplete",
                timestamp=str(current_time),
            )

        range_high = float(range_bars["high"].max())
        range_low = float(range_bars["low"].min())
        range_size = range_high - range_low

        if range_size <= 0:
            return Signal(
                action=Action.FLAT,
                price=float(current["close"]),
                stop=None,
                target=None,
                reason="zero_range",
                timestamp=str(current_time),
            )

        current_close = float(current["close"])

        if current_close > range_high:
            stop = range_low
            target = current_close + range_size * target_multiple
            return Signal(
                action=Action.BUY,
                price=current_close,
                stop=stop,
                target=target,
                reason=f"opening_range_breakout_high_{range_high:.4f}",
                timestamp=str(current_time),
            )

        return Signal(
            action=Action.FLAT,
            price=current_close,
            stop=None,
            target=None,
            reason="no_breakout",
            timestamp=str(current_time),
        )
