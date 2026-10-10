from __future__ import annotations

import pandas as pd

from .base import Action, BaseStrategy, Signal


class OpeningRange(BaseStrategy):
    """
    Intraday opening range breakout.

    Long only.
    Entry is modeled near the opening range high.
    Stop is below the opening range low.
    Target is above entry by a multiple of the opening range size.
    """

    name = "opening_range"

    def __init__(self, symbol: str, params: dict | None = None):
        default_params = {
            "range_minutes": 15,
            "target_range_multiple": 1.5,
            "min_score": 1.0,
            "estimated_cost_bps": 20.0,
            "min_net_profit_bps": 10.0,
        }
        default_params.update(params or {})
        super().__init__(symbol, default_params)

    def evaluate(self, bars: pd.DataFrame) -> Signal:
        if bars is None or len(bars) < 2:
            price = float(bars["close"].iloc[-1]) if bars is not None and len(bars) else 0.0
            return Signal(
                action=Action.FLAT,
                price=price,
                stop=None,
                target=None,
                reason="not_enough_bars",
            )

        range_minutes = int(self.params.get("range_minutes", 15))
        target_multiple = float(self.params.get("target_range_multiple", 1.5))
        min_score = float(self.params.get("min_score", 1.0))
        cost_bps = float(self.params.get("estimated_cost_bps", 20.0))
        min_net_profit_bps = float(self.params.get("min_net_profit_bps", 10.0))

        first_time = pd.Timestamp(bars["timestamp"].iloc[0])
        range_end = first_time + pd.Timedelta(minutes=range_minutes)

        current = bars.iloc[-1]
        current_time = pd.Timestamp(current["timestamp"])
        current_close = float(current["close"])

        if current_time < range_end:
            return Signal(
                action=Action.FLAT,
                price=current_close,
                stop=None,
                target=None,
                reason="waiting_for_opening_range",
                timestamp=str(current_time),
            )

        range_bars = bars[bars["timestamp"] < range_end]

        if len(range_bars) < 2:
            return Signal(
                action=Action.FLAT,
                price=current_close,
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
                price=current_close,
                stop=None,
                target=None,
                reason="zero_range",
                timestamp=str(current_time),
            )

        if current_close <= range_high:
            return Signal(
                action=Action.FLAT,
                price=current_close,
                stop=None,
                target=None,
                reason="no_breakout",
                timestamp=str(current_time),
            )

        entry_price = round(range_high * 1.0005, 6)
        stop = range_low
        target = entry_price + range_size * target_multiple

        profit_bps = max(0.0, (target - entry_price) / entry_price * 10000.0)
        loss_bps = max(1e-9, (entry_price - stop) / entry_price * 10000.0)

        net_profit_bps = profit_bps - cost_bps
        net_rr = max(0.0, net_profit_bps) / loss_bps

        avg_volume = float(range_bars["volume"].mean()) if "volume" in range_bars.columns else 1.0
        breakout_volume = float(current.get("volume", 1.0))

        volume_ratio = breakout_volume / max(1.0, avg_volume)
        strength = float(min(2.0, max(0.5, volume_ratio)))

        score = net_rr * strength
        score_reason = (
            f"profit_bps={profit_bps:.1f} "
            f"loss_bps={loss_bps:.1f} "
            f"net_bps={net_profit_bps:.1f} "
            f"net_rr={net_rr:.2f} "
            f"strength={strength:.2f}"
        )

        if net_profit_bps < min_net_profit_bps or net_rr < min_score:
            return Signal(
                action=Action.FLAT,
                price=entry_price,
                stop=stop,
                target=target,
                reason="no_edge_after_costs",
                timestamp=str(current_time),
                net_rr=net_rr,
                score=0.0,
                score_reason=score_reason,
            )

        return Signal(
            action=Action.BUY,
            price=entry_price,
            stop=stop,
            target=target,
            reason=f"opening_range_breakout_high_{range_high:.4f}",
            timestamp=str(current_time),
            net_rr=net_rr,
            score=score,
            score_reason=score_reason,
        )
