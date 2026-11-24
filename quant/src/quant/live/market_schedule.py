import datetime
import logging
import time

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Calendar
from alpaca.trading.requests import GetCalendarRequest


class MarketSchedule:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)

        # Dynamic Market Hours (Default to generous US hours if fetch fails, but updated dynamically)
        self.market_open_time = datetime.time(13, 30)
        self.market_close_time = datetime.time(20, 00)
        self.force_close_time = datetime.time(19, 45)  # Close intraday positions

        self.current_trading_date = None
        self.is_market_open_today = False

    def update_market_hours(self, client: TradingClient | None) -> None:
        """
        Updates market open/close times for the current day using Alpaca Calendar API.
        Handles DST, holidays, and early closes.
        """
        if not client:
            return

        try:
            # Determine today's date in NY
            today_ny = pd.Timestamp.now(tz="America/New_York").date()

            # Only update if date changed
            if self.current_trading_date == today_ny:
                return

            req = GetCalendarRequest(start=today_ny, end=today_ny)
            raw = client.get_calendar(req)
            # get_calendar returns list[Calendar] | dict[str, Any]; we only use the list path.
            calendar_list: list[Calendar] = raw if isinstance(raw, list) else []
            if not calendar_list:
                self.logger.info(f"Market closed for {today_ny} (No calendar entry)")
                self.is_market_open_today = False
            else:
                cal: Calendar = calendar_list[0]
                # Calendar.open and .close are always tz-naive datetime objects
                # (assembled from date+time strings by the Alpaca SDK).
                # Declare NYC timezone before converting to UTC.
                ny_open = pd.Timestamp(cal.open, tz="America/New_York")
                ny_close = pd.Timestamp(cal.close, tz="America/New_York")

                self.market_open_time = ny_open.tz_convert("UTC").time()
                self.market_close_time = ny_close.tz_convert("UTC").time()
                self.force_close_time = (
                    (ny_close - datetime.timedelta(minutes=15)).tz_convert("UTC").time()
                )

                self.is_market_open_today = True
                self.logger.info(
                    f"Market Hours Updated for {today_ny}: Open {self.market_open_time}, Close {self.market_close_time} (UTC)"
                )

            self.current_trading_date = today_ny

        except Exception as e:
            self.logger.error(f"Error updating market hours: {e}")

    def is_market_open(
        self, client: TradingClient | None, now: datetime.datetime | None = None
    ) -> bool:
        # Check for updates (e.g. day rollover)
        self.update_market_hours(client)

        if not self.is_market_open_today:
            return False

        if now is None:
            now = datetime.datetime.now(datetime.UTC)

        now_time = now.time()
        return self.market_open_time <= now_time <= self.market_close_time

    def wait_for_market_open(self, client: TradingClient | None) -> None:
        """
        Blocks until market is open.
        """
        while not self.is_market_open(client):
            self.logger.info("Market is closed. Waiting...")
            time.sleep(60)
