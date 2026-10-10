from __future__ import annotations

import pandas as pd
from .base import Action, BaseStrategy, Signal


class VwapReversion(BaseStrategy):
    """
    Intraday VWAP mean reversion.

    Logic:
    1. Compute intraday VWAP from the start of the day.
    2. Compute standard deviation of closes.
    3. If price is at least `z_entry` standard deviations below VWAP, go long.
    4. Target VWAP.
    5. Stop below the recent low with a small buffer.

    Conservative version:
    - long only
    - no short in v1
    - no entry if not enough bars or zero volatility
    """

    name = "vwap_reversion"

    def __init__(self, symbol: str, params: dict | None = None):
        default_params = {
            "z_entry": 2.0,
            "min_bars": 10,
            "stop_buffer_pct": 0.1,
        }
        default_params.update(params or {})
        super().__init__(symbol, default_params)

    def evaluate(self, bars: pd.DataFrame) -> Signal:
        min_bars = int(self.params.get("min_bars", 10))
        z_entry = float(self.params.get("z_entry", 2.0))
        stop_buffer_pct = float(self.params.get("stop_buffer_pct", 0.1))

        if bars is None or len(bars) < min_bars:
            return Signal(
                action=Action.FLAT,
                price=float(bars["close"].iloc[-1]) if bars is not None and len(bars) else 0.0,
                stop=None,
                target=None,
                reason="not_enough_bars",
            )

        current = bars.iloc[-1]
        current_close = float(current["close"])
        current_time = pd.Timestamp(current["timestamp"])

        volume = bars["volume"].clip(lower=1)
        typical_price = bars["close"]

        vwap = float((typical_price * volume).sum() / volume.sum())

        std = float(bars["close"].std(ddof=0))

        if std <= 0:
            return Signal(
                action=Action.FLAT,
                price=current_close,
                stop=None,
                target=None,
                reason="zero_volatility",
                timestamp=str(current_time),
            )

        z_score = (current_close - vwap) / std

        if z_score <= -z_entry:
            recent_low = float(bars["low"].iloc[-5:].min())
            stop = recent_low * (1.0 - stop_buffer_pct / 100.0)
            target = vwap

            return Signal(
                action=Action.BUY,
                price=current_close,
                stop=stop,
                target=target,
                reason=f"vwap_reversion_z_{z_score:.2f}",
                timestamp=str(current_time),
            )

        return Signal(
            action=Action.FLAT,
            price=current_close,
            stop=None,
            target=None,
            reason=f"no_signal_z_{z_score:.2f}",
            timestamp=str(current_time),
        )
