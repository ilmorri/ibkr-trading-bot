from __future__ import annotations

import pandas as pd

from .base import Action, BaseStrategy, Signal


class VwapReversion(BaseStrategy):
    """
    Intraday VWAP mean reversion.

    Long only.
    Enter when price is several standard deviations below VWAP.
    Target VWAP.
    Stop below recent low.
    """

    name = "vwap_reversion"

    def __init__(self, symbol: str, params: dict | None = None):
        default_params = {
            "z_entry": 2.0,
            "min_bars": 10,
            "stop_buffer_pct": 0.1,
            "min_score": 1.0,
            "estimated_cost_bps": 20.0,
            "min_net_profit_bps": 10.0,
        }
        default_params.update(params or {})
        super().__init__(symbol, default_params)

    def evaluate(self, bars: pd.DataFrame) -> Signal:
        min_bars = int(self.params.get("min_bars", 10))
        z_entry = float(self.params.get("z_entry", 2.0))
        stop_buffer_pct = float(self.params.get("stop_buffer_pct", 0.1))
        min_score = float(self.params.get("min_score", 1.0))
        cost_bps = float(self.params.get("estimated_cost_bps", 20.0))
        min_net_profit_bps = float(self.params.get("min_net_profit_bps", 10.0))

        if bars is None or len(bars) < min_bars:
            price = float(bars["close"].iloc[-1]) if bars is not None and len(bars) else 0.0
            return Signal(
                action=Action.FLAT,
                price=price,
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

        if z_score <= -z_entry and vwap > current_close:
            recent_low = float(bars["low"].iloc[-5:].min())
            stop = recent_low * (1.0 - stop_buffer_pct / 100.0)
            target = vwap

            profit_bps = max(0.0, (target - current_close) / current_close * 10000.0)
            loss_bps = max(1e-9, (current_close - stop) / current_close * 10000.0)

            net_profit_bps = profit_bps - cost_bps
            net_rr = max(0.0, net_profit_bps) / loss_bps

            strength = float(min(3.0, abs(z_score) / 3.0))
            score = net_rr * strength

            score_reason = (
                f"z={z_score:.2f} "
                f"profit_bps={profit_bps:.1f} "
                f"loss_bps={loss_bps:.1f} "
                f"net_bps={net_profit_bps:.1f} "
                f"net_rr={net_rr:.2f} "
                f"strength={strength:.2f}"
            )

            if net_profit_bps < min_net_profit_bps or net_rr < min_score:
                return Signal(
                    action=Action.FLAT,
                    price=current_close,
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
                price=current_close,
                stop=stop,
                target=target,
                reason=f"vwap_reversion_z_{z_score:.2f}",
                timestamp=str(current_time),
                net_rr=net_rr,
                score=score,
                score_reason=score_reason,
            )

        return Signal(
            action=Action.FLAT,
            price=current_close,
            stop=None,
            target=None,
            reason=f"no_signal_z_{z_score:.2f}",
            timestamp=str(current_time),
        )
