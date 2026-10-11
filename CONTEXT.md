# Trading Bot

An automated intraday trading system for a small personal IBKR account, trading Borsa Italiana (MIL) equities in EUR.

## Language

**Bot**:
One Strategy bound to one Instrument; the unit that per-day trade limits apply to.
_Avoid_: using "bot" for the whole process or for a strategy across many symbols

**Strategy**:
A rule set that turns a series of Bars for one Instrument into a Signal (e.g. opening range breakout, VWAP reversion).
_Avoid_: algo, model

**Instrument**:
A tradable security on the configured exchange, identified by its symbol (e.g. STLA, BPER).
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
The IBKR account's NetLiquidation in EUR; the whole account belongs to the bot, so every position in it is a bot position.
_Avoid_: liquidity, balance, cash, capital

**Net R:R**:
A Signal's expected reward after Estimated Cost, divided by its risk (entry to stop), both in basis points of the entry price.
_Avoid_: RR, reward/risk (unqualified)

**Score**:
Net R:R weighted by a Strategy-specific strength factor; ranks competing BUY Signals so the best one is offered to risk first, with config priority only breaking ties.
_Avoid_: rank, confidence

**Estimated Cost**:
The assumed round-trip trading cost of a position, in basis points of entry price, subtracted from expected reward before a Signal is allowed.
_Avoid_: fees, slippage (each is only part of it)
