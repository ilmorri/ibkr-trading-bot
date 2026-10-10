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
    net_rr: float = 0.0
    score: float = 0.0
    score_reason: str = ""


class BaseStrategy:
    name = "base"

    def __init__(self, symbol: str, params: dict | None = None):
        self.symbol = symbol
        self.params = params or {}

    def evaluate(self, bars: pd.DataFrame) -> Signal:
        raise NotImplementedError
