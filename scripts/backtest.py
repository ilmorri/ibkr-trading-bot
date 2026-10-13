#!/usr/bin/env python3
"""Replay saved 1-minute bars through the bot's strategies and risk rules.

Each (symbol, strategy) pair is tested on its own, one simulated day at a time.
Reads data/<SYMBOL>_1min.csv as saved by scripts/download_bars.py.

Assumptions (all conservative where a choice had to be made):
  - A signal on bar N places a limit buy at the signal price. It fills only if a
    bar in the next --fill-bars bars trades at or below that price; otherwise it
    counts as a cancelled order attempt.
  - Whole shares, sized by the real RiskManager from the fixed account equity.
  - After a fill, the stop is checked before the target on every bar. If both
    are touched in the same bar, the stop is assumed to have hit first.
  - Any position still open at the flatten time is sold at that bar's close.
  - Costs: IBKR tiered commission per order, max(minimum, per-share rate), plus
    slippage per share on market exits (stop and flatten). Limit fills have none.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import logging

import pandas as pd

from bot.config import load_config
from bot.risk import RiskManager
from bot.strategies.base import Action
from bot.strategies.opening_range import OpeningRange
from bot.strategies.vwap_reversion import VwapReversion

STRATEGIES = {"opening_range": OpeningRange, "vwap_reversion": VwapReversion}
DEFAULT_PARAMS = {
    "opening_range": {"range_minutes": 15, "target_range_multiple": 2.0},
    "vwap_reversion": {"z_entry": 2.0, "min_bars": 10},
}


@dataclass
class Trade:
    symbol: str
    strategy: str
    day: str
    entry_time: str
    exit_time: str
    shares: int
    entry: float
    exit: float
    exit_reason: str
    gross: float
    cost: float

    @property
    def net(self) -> float:
        return self.gross - self.cost


def parse_time(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def load_bars(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    ts = pd.to_datetime(df["date"], utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None)
    out = pd.DataFrame({
        "timestamp": ts,
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        "volume": df["volume"].astype(float),
    })
    return out.sort_values("timestamp").reset_index(drop=True)


def in_windows(t: time, windows: list[tuple[time, time]]) -> bool:
    return any(start <= t < end for start, end in windows)


def order_cost(shares: int, per_share: float, minimum: float) -> float:
    return max(minimum, shares * per_share)


def simulate_day(symbol, strategy_name, strategy, day_bars, cfg, args, windows, flatten_at):
    """Returns (trade or None, attempts, blocked_reason or None)."""
    risk = RiskManager.from_config(cfg)
    equity = float(cfg["account"]["equity"])
    times = [ts.time() for ts in day_bars["timestamp"]]
    n = len(day_bars)
    attempts = 0
    blocked = None
    i = 0

    while i < n - 1:
        t = times[i]
        if t >= flatten_at:
            break
        if not in_windows(t, windows):
            i += 1
            continue

        signal = strategy.evaluate(day_bars.iloc[: i + 1])
        if signal.action != Action.BUY or signal.stop is None or signal.target is None or signal.score <= 0:
            i += 1
            continue

        decision = risk.evaluate_entry(
            bot_id=symbol, equity=equity, entry_price=float(signal.price), stop_price=float(signal.stop)
        )
        if not decision.allowed:
            blocked = decision.reason.split("_position_")[0].split("_remaining_")[0]
            if "max_order_attempts" in decision.reason:
                break
            i += 1
            continue

        # Limit order placed after bar i closes.
        risk.on_order_submitted(symbol, decision.position_value)
        attempts += 1
        limit = float(signal.price)
        fill_index, fill_price = None, None
        last = min(n - 1, i + args.fill_bars)
        for j in range(i + 1, last + 1):
            if times[j] >= flatten_at:
                break
            bar = day_bars.iloc[j]
            if bar["low"] <= limit:
                fill_index, fill_price = j, min(limit, float(bar["open"]))
                break

        if fill_index is None:
            risk.on_order_cancelled(symbol, decision.position_value)
            i = last + 1
            continue

        risk.on_order_filled(symbol, decision.position_value)
        shares = decision.shares
        stop, target = float(signal.stop), float(signal.target)
        exit_price, exit_reason, exit_index = None, None, None

        for j in range(fill_index, n):
            bar = day_bars.iloc[j]
            if times[j] >= flatten_at:
                exit_price, exit_reason, exit_index = float(bar["close"]), "flatten", j
                break
            if bar["low"] <= stop:
                # A gap through the stop sells at the open, not at the stop.
                price = min(stop, float(bar["open"])) if j > fill_index else stop
                exit_price, exit_reason, exit_index = price, "stop", j
                break
            if j > fill_index and bar["high"] >= target:
                exit_price, exit_reason, exit_index = max(target, float(bar["open"])), "target", j
                break

        if exit_price is None:
            exit_index = n - 1
            exit_price, exit_reason = float(day_bars.iloc[-1]["close"]), "flatten"

        cost = 2 * order_cost(shares, args.commission_per_share, args.commission_min)
        if exit_reason in ("stop", "flatten"):
            cost += shares * args.slippage_per_share

        trade = Trade(
            symbol=symbol,
            strategy=strategy_name,
            day=str(day_bars["timestamp"].iloc[0].date()),
            entry_time=str(day_bars["timestamp"].iloc[fill_index].time()),
            exit_time=str(day_bars["timestamp"].iloc[exit_index].time()),
            shares=shares,
            entry=round(fill_price, 4),
            exit=round(exit_price, 4),
            exit_reason=exit_reason,
            gross=round((exit_price - fill_price) * shares, 4),
            cost=round(cost, 4),
        )
        return trade, attempts, blocked

    return None, attempts, blocked


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbols", nargs="*", help="default: every data/*_1min.csv")
    p.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    p.add_argument("--fill-bars", type=int, default=2, help="bars a limit order may wait for a fill")
    p.add_argument("--commission-per-share", type=float, default=0.0035)
    p.add_argument("--commission-min", type=float, default=0.35)
    p.add_argument("--slippage-per-share", type=float, default=0.01)
    args = p.parse_args()

    logging.disable(logging.CRITICAL)
    cfg = load_config()
    trading = cfg.get("trading", {})
    windows = [(parse_time(w["start"]), parse_time(w["end"])) for w in trading.get("windows", [])]
    if not windows:
        windows = [(time(9, 30), time(15, 50))]
    flatten_at = parse_time(trading.get("flatten_time", "15:55"))
    cost_bps = cfg.get("costs", {}).get("estimated_cost_bps")
    per_symbol = {item["symbol"]: item for item in cfg.get("universe", [])}

    data_dir = ROOT / "data"
    symbols = args.symbols or sorted(f.name.replace("_1min.csv", "") for f in data_dir.glob("*_1min.csv"))
    if not symbols:
        print("No data files found in data/. Run scripts/download_bars.py first.")
        return 1

    print(f"Equity {cfg['account']['equity']} {cfg['account']['currency']} | windows "
          f"{[(str(a)[:5], str(b)[:5]) for a, b in windows]} | flatten {str(flatten_at)[:5]} | "
          f"strategy cost filter {cost_bps} bps | fill window {args.fill_bars} bars")
    print(f"Commission max(${args.commission_min}, ${args.commission_per_share}/share) per order | "
          f"slippage ${args.slippage_per_share}/share on stop and flatten exits\n")

    trades: list[Trade] = []
    rows = []
    for symbol in symbols:
        path = data_dir / f"{symbol}_1min.csv"
        if not path.exists():
            print(f"{symbol}: no data file, skipped")
            continue
        bars = load_bars(path)
        days = [g.reset_index(drop=True) for _, g in bars.groupby(bars["timestamp"].dt.date)]

        for strategy_name in args.strategies:
            params = dict(DEFAULT_PARAMS[strategy_name])
            item = per_symbol.get(symbol)
            if item and item.get("strategy") == strategy_name:
                params.update({k: v for k, v in item.items()
                               if k not in {"symbol", "name", "strategy", "enabled", "priority"}})
            if cost_bps is not None:
                params.setdefault("estimated_cost_bps", cost_bps)
            strategy = STRATEGIES[strategy_name](symbol, params)

            bot_trades, attempts, blocked_days, reasons = [], 0, 0, {}
            for day_bars in days:
                trade, day_attempts, blocked = simulate_day(
                    symbol, strategy_name, strategy, day_bars, cfg, args, windows, flatten_at)
                attempts += day_attempts
                if trade is not None:
                    bot_trades.append(trade)
                elif blocked:
                    blocked_days += 1
                    reasons[blocked] = reasons.get(blocked, 0) + 1

            trades.extend(bot_trades)
            wins = [t for t in bot_trades if t.net > 0]
            losses = [t for t in bot_trades if t.net <= 0]
            gross = sum(t.gross for t in bot_trades)
            cost = sum(t.cost for t in bot_trades)
            rows.append({
                "symbol": symbol,
                "strategy": strategy_name,
                "days": len(days),
                "orders": attempts,
                "trades": len(bot_trades),
                "blocked_days": blocked_days,
                "win_pct": round(100 * len(wins) / len(bot_trades), 1) if bot_trades else 0.0,
                "avg_win": round(sum(t.net for t in wins) / len(wins), 2) if wins else 0.0,
                "avg_loss": round(sum(t.net for t in losses) / len(losses), 2) if losses else 0.0,
                "gross": round(gross, 2),
                "costs": round(cost, 2),
                "net": round(gross - cost, 2),
                "net_2x_cost": round(gross - 2 * cost, 2),
                "top_block": max(reasons, key=reasons.get) if reasons else "",
            })
            print(f"{symbol} {strategy_name}: {len(bot_trades)} trades", flush=True)

    summary = pd.DataFrame(rows)
    print()
    print(summary.to_string(index=False))
    if trades:
        out = data_dir / "backtest_trades.csv"
        pd.DataFrame([{**t.__dict__, "net": round(t.net, 4)} for t in trades]).to_csv(out, index=False)
        summary.to_csv(data_dir / "backtest_summary.csv", index=False)
        reasons = pd.Series([t.exit_reason for t in trades]).value_counts().to_dict()
        print(f"\nExits: {reasons}")
        print(f"All trades saved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
