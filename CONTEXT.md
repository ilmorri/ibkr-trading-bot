# Trading Bot

An automated intraday trading system for a small personal IBKR account, trading US equities in USD through IBKR smart routing. It began as a Borsa Italiana bot and moved to the US market on 2026-10-11.

## Language

**Bot**:
One Strategy bound to one Instrument; the unit that per-day trade limits apply to.
_Avoid_: using "bot" for the whole process or for a strategy across many symbols

**Strategy**:
A rule set that turns a series of Bars for one Instrument into a Signal (e.g. opening range breakout, VWAP reversion).
_Avoid_: algo, model

**Instrument**:
A tradable security on the configured exchange, identified by its symbol (e.g. INTC, SOFI).
_Avoid_: ticker, stock, asset

**Universe**:
The configured set of Bots, each entry naming an Instrument, its Strategy, and whether it is enabled.
_Avoid_: watchlist, portfolio

**Signal**:
A Strategy's verdict on the latest Bar: BUY with entry, stop and target prices, or FLAT with a reason.
_Avoid_: alert, trigger

**Trade**:
One risk-approved position in one Instrument, from entry fill until exit; the unit counted against a Bot's daily trade limit. Its size and exposure are what actually filled, not what was approved. An approved entry that never fills is not a Trade.
_Avoid_: order, fill, round trip

**Equity**:
The fixed budget the bot sizes positions from, set as `account.equity` in the config, in the trading currency (USD). It is not the balance IBKR reports: the paper account holds 1,000,000, and sizing from it would be meaningless.
_Avoid_: liquidity, balance, cash, capital

**Net R:R**:
A Signal's expected reward minus Estimated Cost, divided by its risk (entry to stop) plus Estimated Cost, all in basis points of the entry price. The cost counts on both sides because a losing trade pays it too.
_Avoid_: RR, reward/risk (unqualified)

**Score**:
Net R:R weighted by a Strategy-specific strength factor; ranks competing BUY Signals so the best one is offered to risk first, with config priority only breaking ties.
_Avoid_: rank, confidence

**Estimated Cost**:
The assumed round-trip trading cost of a position, in basis points of entry price, subtracted from expected reward and added to expected loss before a Signal is allowed.
_Avoid_: fees, slippage (each is only part of it)
