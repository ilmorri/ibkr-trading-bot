from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Iterable

# NYSE regular session, exchange time.
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th given weekday of a month (Monday is 0). n=-1 is the last one."""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))

    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date:
    """A holiday on Saturday is observed on Friday, on Sunday on Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> frozenset[date]:
    days = {
        _nth_weekday(year, 1, 0, 3),   # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),   # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _observed(date(year, 7, 4)),   # Independence Day
        _nth_weekday(year, 9, 0, 1),   # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }

    # NYSE does not close on Friday 31 December for a Saturday New Year's Day.
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:
        days.add(_observed(new_year))

    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth

    return frozenset(days)


@lru_cache(maxsize=None)
def nyse_early_closes(year: int) -> frozenset[date]:
    """Days the NYSE closes at 13:00."""
    holidays = nyse_holidays(year)
    days = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}  # day after Thanksgiving

    for candidate in (date(year, 7, 3), date(year, 12, 24)):
        if candidate.weekday() < 5 and candidate not in holidays:
            days.add(candidate)

    return frozenset(days)


class MarketCalendar:
    """
    NYSE trading days and session close, in exchange time.

    Holidays and half-days are computed from the published NYSE rules.
    Unscheduled closures (national days of mourning, emergencies) cannot be
    predicted; add them through `extra_holidays` in the config.
    """

    def __init__(
        self,
        extra_holidays: Iterable[date | str] = (),
        extra_early_closes: Iterable[date | str] = (),
    ):
        self.extra_holidays = {_as_date(d) for d in extra_holidays}
        self.extra_early_closes = {_as_date(d) for d in extra_early_closes}

    @classmethod
    def from_config(cls, cfg: dict) -> "MarketCalendar":
        market = cfg.get("market", {})
        return cls(
            extra_holidays=market.get("extra_holidays", []) or [],
            extra_early_closes=market.get("extra_early_closes", []) or [],
        )

    def is_trading_day(self, day: date) -> bool:
        return (
            day.weekday() < 5
            and day not in nyse_holidays(day.year)
            and day not in self.extra_holidays
        )

    def session_close(self, day: date) -> time | None:
        if not self.is_trading_day(day):
            return None
        if day in nyse_early_closes(day.year) or day in self.extra_early_closes:
            return EARLY_CLOSE
        return REGULAR_CLOSE

    def is_open(self, moment: datetime) -> bool:
        close = self.session_close(moment.date())
        return close is not None and REGULAR_OPEN <= moment.time() < close

    def flatten_time(self, day: date, configured: time, buffer_minutes: int = 5) -> time | None:
        """
        The configured flatten time, moved earlier on a half-day so it still
        lands `buffer_minutes` before the close. None on a non-trading day.
        """
        close = self.session_close(day)
        if close is None:
            return None

        latest = (datetime.combine(day, close) - timedelta(minutes=buffer_minutes)).time()
        return min(configured, latest)

    def next_trading_day(self, day: date) -> date:
        day += timedelta(days=1)
        while not self.is_trading_day(day):
            day += timedelta(days=1)
        return day


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
