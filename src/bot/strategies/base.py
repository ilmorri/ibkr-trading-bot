from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    FLAT = "FLAT"


@dataclass
class Signal:
    action: Action
    price: float
    stop: Optional[float]
    target: Optional[float]
    reason: str
    timestamp: Optional[str] = None


class BaseStrategy:
    name = "base"

    def __init__(self, symbol: str, params: dict | None = None):
        self.symbol = symbol
        self.params = params or {}

    def evaluate(self, bars: pd.DataFrame) -> Signal:
        """
        bars must contain:
            timestamp, open, high, low, close, volume
        Use only data available up to the last bar.
        """
        raise NotImplementedError
