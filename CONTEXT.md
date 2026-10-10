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
