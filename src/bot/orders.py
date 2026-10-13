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
    # A cancel was sent but the broker never confirmed the final state.
    UNRESOLVED = "UNRESOLVED"


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


@dataclass
class FillReport:
    """State of an entry order as the broker reports it."""

    status: str
    filled_shares: int = 0
    avg_fill_price: float = 0.0


@dataclass
class ExitReport:
    """
    A completed exit: the stop or target child filled, or the position was
    flattened. `commission` is the whole round trip, entry included.
    """

    exit_price: float
    shares: int
    reason: str
    commission: float = 0.0


@dataclass
class OpenTrade:
    bot_id: str
    symbol: str
    strategy: str
    entry_order_id: str
    order_ref: str
    shares: int
    entry_price: float
    stop: float
    target: float
    position_value: float
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ClosedTrade:
    bot_id: str
    symbol: str
    strategy: str
    order_ref: str
    shares: int
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl: float
    opened_at: str
    closed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
