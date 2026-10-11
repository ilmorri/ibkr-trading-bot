from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    TIMED_OUT = "TIMED_OUT"
    SKIPPED = "SKIPPED"


@dataclass
class OrderRequest:
    bot_id: str
    symbol: str
    strategy: str
    action: str
    shares: int
    price: float
    stop: float
    target: float
    score: float
    net_rr: float
    score_reason: str
    order_ref: str
    position_value: float


@dataclass
class OrderAttempt:
    bot_id: str
    symbol: str
    strategy: str
    action: str
    order_id: str
    status: str
    shares: int
    price: float
    stop: float
    target: float
    score: float
    net_rr: float
    score_reason: str
    position_value: float
    filled: bool
    trade_opened: bool
    exit_reason: str
    note: str = ""
    submitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
