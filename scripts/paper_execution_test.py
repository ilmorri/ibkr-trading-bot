#!/usr/bin/env python3
"""Exercise IbkrExecutionClient against the IBKR PAPER account.

Refuses to run unless connected on the paper port and every managed account
is a paper account (IDs start with "DU"). Trades 1 share at a time.

Scenarios:
  A  resting bracket: entry far below the market, check the three orders,
     cancel the entry, check the stop and target are cancelled with it
  B  full lifecycle through ExecutionEngine: marketable entry fills,
     reconcile, check_exits, flatten_all, P&L recorded, state saved
  C  restart: place a resting bracket, disconnect, reconnect with the same
     clientId, check the orders and today's fills (with orderRef) come back

Usage:
    python scripts/paper_execution_test.py [SYMBOL]     # default SOFI
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ib_insync import IB

from bot.config import load_config
from bot.engine import Candidate
from bot.execution import ExecutionEngine
from bot.ibkr_client import IbkrExecutionClient
from bot.orders import OrderRequest
from bot.risk import RiskManager
from bot.state import StateStore
from bot.strategies.base import Action, Signal

PAPER_PORT = 4002
CLIENT_ID = 17  # dedicated, so the test's orders never mix with the bot's
REF_PREFIX = "PTEST"

results: list[tuple[str, bool, str]] = []
b_order_ref = ""  # set by scenario B, checked by C


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(ok)


def connect(cfg: dict) -> IB:
    ib = IB()
    ib.connect(cfg["ibkr"]["host"], PAPER_PORT, clientId=CLIENT_ID, timeout=15)
    accounts = ib.managedAccounts()
    if not accounts or not all(a.startswith("DU") for a in accounts):
        ib.disconnect()
        raise SystemExit(f"Refusing to run: accounts {accounts} are not all paper (DU...)")
    return ib


def last_price(ib: IB, contract) -> float:
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="1800 S", barSizeSetting="1 min",
        whatToShow="TRADES", useRTH=True,
    )
    if not bars:
        raise SystemExit(f"No recent bars for {contract.symbol}")
    return float(bars[-1].close)


def wait_until(ib: IB, predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        ib.sleep(0.25)
    return True


def statuses(trades) -> str:
    return ", ".join(f"{t.order.orderRef}={t.orderStatus.status}" for t in trades)


def request(symbol: str, price: float, stop: float, target: float, ref: str) -> OrderRequest:
    return OrderRequest(
        bot_id=symbol, symbol=symbol, strategy="paper_test", action="BUY", shares=1,
        price=price, stop=stop, target=target, score=1.0, net_rr=1.0, score_reason="",
        order_ref=ref, position_value=price,
    )


def scenario_a(ib: IB, client: IbkrExecutionClient, symbol: str, last: float) -> None:
    print("\nA  resting bracket, then cancel")
    ref = f"{REF_PREFIX}-A-{int(time.time())}"
    order_id = client.submit_entry(request(symbol, last * 0.90, last * 0.85, last * 0.95, ref))
    ib.sleep(3)

    family = [t for t in ib.trades() if t.order.orderRef.startswith(ref)]
    check("three orders placed", len(family) == 3, statuses(family))
    check("entry is working", client.entry_state(order_id).status == "PENDING",
          client._trade(order_id).orderStatus.status)

    client.cancel_order(order_id)
    done = wait_until(ib, lambda: all(t.isDone() for t in family))
    check("entry, stop and target all cancelled", done, statuses(family))


def scenario_b(ib: IB, client: IbkrExecutionClient, symbol: str, last: float, cfg: dict) -> None:
    print("\nB  fill, reconcile, flatten through ExecutionEngine")
    risk = RiskManager.from_config(cfg)
    # One share is far below the bot's $150 minimum; the limits are relaxed for this test only.
    risk.min_position_value = 0.0
    risk.max_position_value = last * 1.5
    risk.risk_per_trade_pct = 100.0

    engine = ExecutionEngine(risk, client, order_timeout_seconds=20, poll_interval_seconds=0.25,
                             cancel_timeout_seconds=10)
    state_path = Path(tempfile.mkdtemp()) / "state.json"
    store = StateStore(state_path)
    store.attach(engine)

    # Marketable limit 1% above the last trade; stop and target 5% away so they do not fire.
    signal = Signal(Action.BUY, round(last * 1.01, 2), round(last * 0.95, 2), round(last * 1.05, 2),
                    "paper_test", net_rr=1.0, score=1.0)
    attempts = engine.execute_candidates([Candidate(symbol, "paper_test", 0, signal)], equity=300.0)
    attempt = attempts[0]
    if not check("entry filled", attempt.trade_opened, f"status={attempt.status} {attempt.exit_reason}"):
        return

    global b_order_ref
    trade = engine.open_trades[symbol]
    b_order_ref = trade.order_ref
    check("filled 1 share", trade.shares == 1, f"entry={trade.entry_price}")
    check("state file written", state_path.exists())

    children = client._children(trade.entry_order_id)
    check("stop and target working", len(children) == 2 and all(not c.isDone() for c in children),
          statuses(children))

    problems = engine.reconcile()
    others = [p for p in problems if not p.startswith(symbol)]
    mine = [p for p in problems if p.startswith(symbol)]
    check("reconcile agrees on the test position", not mine, "; ".join(mine))
    if others:
        print(f"  note: other positions in the paper account: {others}")

    check("no exit yet", engine.check_exits() == [])

    closed = engine.flatten_all()
    ok = check("flattened", len(closed) == 1 and closed[0].exit_reason == "flatten",
               f"{closed[0].exit_price if closed else '-'}")
    if ok:
        c = closed[0]
        gross = (c.exit_price - c.entry_price) * c.shares
        print(f"  entry {c.entry_price:.4f} exit {c.exit_price:.4f} gross {gross:.4f} "
              f"commission {gross - c.pnl:.4f} pnl {c.pnl:.4f}")
        check("round-trip commission counted", gross - c.pnl > 0, f"{gross - c.pnl:.4f}")
    check("children cancelled", all(c.isDone() for c in children), statuses(children))
    check("risk released", risk.open_positions == 0 and risk.gross_exposure == 0.0)

    fills = [f for f in ib.fills() if f.execution.orderRef.startswith(trade.order_ref)]
    refs = sorted({f.execution.orderRef for f in fills})
    check("fills carry orderRef", len(refs) == 2, str(refs))
    check("broker flat in symbol", client.positions().get(symbol, 0) == 0,
          str(client.positions().get(symbol, 0)))


def scenario_c(cfg: dict, symbol: str, last: float) -> IB:
    print("\nC  restart: do orders and fills come back?")
    ib = connect(cfg)
    client = IbkrExecutionClient.from_config(ib, cfg)
    ref = f"{REF_PREFIX}-C-{int(time.time())}"
    order_id = client.submit_entry(request(symbol, last * 0.90, last * 0.85, last * 0.95, ref))
    ib.sleep(3)
    fills_before = len([f for f in ib.fills() if b_order_ref and f.execution.orderRef.startswith(b_order_ref)])
    ib.disconnect()
    time.sleep(2)

    ib = connect(cfg)
    client = IbkrExecutionClient.from_config(ib, cfg)
    ib.sleep(2)
    family = [t for t in ib.trades() if t.order.orderRef.startswith(ref)]
    check("open bracket resynced after reconnect", len(family) == 3, statuses(family))
    check("entry found by id", client._trade(order_id) is not None)

    fills_after = [f for f in ib.fills() if b_order_ref and f.execution.orderRef.startswith(b_order_ref)]
    check("today's fills resynced with orderRef", len(fills_after) >= fills_before and fills_before > 0,
          f"before={fills_before} after={len(fills_after)}")

    client.cancel_order(order_id)
    wait_until(ib, lambda: all(t.isDone() for t in family))
    return ib


def cleanup(ib: IB) -> None:
    leftover = [t for t in ib.openTrades() if t.order.orderRef.startswith(REF_PREFIX)]
    for t in leftover:
        ib.cancelOrder(t.order)
    if leftover:
        ib.sleep(3)
    print(f"\ncleanup: cancelled {len(leftover)} leftover test orders")


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "SOFI"
    cfg = load_config()

    ib = connect(cfg)
    print(f"connected: accounts {ib.managedAccounts()} port {PAPER_PORT} clientId {CLIENT_ID}")
    try:
        client = IbkrExecutionClient.from_config(ib, cfg)
        contract = client._contract(symbol)
        last = last_price(ib, contract)
        print(f"{symbol} last {last:.2f}")

        scenario_a(ib, client, symbol, last)
        scenario_b(ib, client, symbol, last, cfg)
        cleanup(ib)
        ib.disconnect()

        ib = scenario_c(cfg, symbol, last)
    finally:
        cleanup(ib)
        ib.disconnect()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
