"""Deterministic option-expiration eligibility rules.

This module deliberately does not query market-data definitions.  It produces
the expected expiration date; the subsequent CBBO fetch is the authority on
whether the particular contracts were actually listed and tradeable.
"""

from __future__ import annotations

from datetime import date, timedelta

_SPY_TUESDAY_EXPIRY_START = date(2022, 11, 22)
_SPY_THURSDAY_EXPIRY_START = date(2022, 11, 24)


def scheduled_zero_dte_expiration(ticker: str, day: date) -> tuple[bool, str | None]:
    """Return whether a product has a known schedule and its expected 0-DTE.

    The caller supplies trading sessions, so weekends and exchange holidays
    are deliberately outside this calendar's responsibility.
    """
    if ticker.upper() != "SPY":
        return False, None

    weekday = day.weekday()
    if weekday in {0, 2, 4}:
        return True, day.isoformat()
    if weekday == 1:
        return True, day.isoformat() if day >= _SPY_TUESDAY_EXPIRY_START else None
    if weekday == 3:
        return True, day.isoformat() if day >= _SPY_THURSDAY_EXPIRY_START else None
    return True, None


def scheduled_expiration_on_or_after(ticker: str, day: date) -> str:
    """Return the next calendar-scheduled expiration on or after ``day``.

    SPY has weekday expirations subject to its documented Tuesday/Thursday
    listing starts.  Other equity-option underlyings use the conservative
    standard weekly-Friday schedule.  Exchange holidays and exceptional
    listing changes are intentionally not inferred here: the CBBO leg fetch
    will return no data and the strategy will skip that candidate.
    """
    if ticker.upper() != "SPY":
        return (day + timedelta(days=(4 - day.weekday()) % 7)).isoformat()

    candidate = day
    while True:
        has_schedule, expiration = scheduled_zero_dte_expiration(ticker, candidate)
        if has_schedule and expiration is not None:
            return expiration
        candidate += timedelta(days=1)
