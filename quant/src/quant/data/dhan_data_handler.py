import io
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dhanhq import dhanhq
from dotenv import load_dotenv

from .cache.stock import StockDataCache

logging.getLogger(__name__).addHandler(logging.NullHandler())
_log = logging.getLogger(__name__)

# Load environment variables from .env file located in the project root
_dotenv_path = Path(__file__).parent / ".." / ".env"
if _dotenv_path.exists():
    load_dotenv(dotenv_path=_dotenv_path)
else:
    _log.warning(
        ".env file not found at %s. Using default or environment-set credentials.",
        _dotenv_path,
    )


class DhanDataHandler:
    """
    Fetches historical data from DhanHQ API.
    Exclusively for Indian Stock Market assets.
    """

    def __init__(self, cache_dir="data_cache"):
        self.access_token = os.getenv("DHAN_ACCESS_TOKEN")
        self.client_id = os.getenv("DHAN_CLIENT_ID")

        if not self.access_token or not self.client_id:
            _log.warning(
                "DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID not found in environment variables."
            )
        else:
            self.dhan = dhanhq(self.client_id, self.access_token)

        self.cache_dir = Path(__file__).parent / cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Initialize StockDataCache
        self._cache = StockDataCache(cache_dir=str(self.cache_dir))

        self.instrument_list = None

    def _load_instrument_list(self):
        """Fetch and parse the Dhan instrument list CSV."""
        if self.instrument_list is not None:
            return

        # Check if we have a cached CSV
        csv_path = self.cache_dir / "dhan_instruments.csv"

        # If cached file exists and is recent (e.g. < 1 day), use it
        if csv_path.exists():
            file_time = datetime.fromtimestamp(csv_path.stat().st_mtime)
            if (datetime.now() - file_time).days < 1:
                try:
                    self.instrument_list = pd.read_csv(csv_path, low_memory=False)
                    return
                except Exception:
                    _log.debug(
                        "Cached instrument CSV unreadable, re-fetching", exc_info=True
                    )

        try:
            url = "https://images.dhan.co/api-data/api-scrip-master.csv"
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            # Save to cache
            csv_path.write_bytes(response.content)

            self.instrument_list = pd.read_csv(
                io.StringIO(response.text), low_memory=False
            )
        except Exception as e:
            _log.error("Failed to load Dhan instrument list: %s", e)
            self.instrument_list = pd.DataFrame()

    def _get_security_info(self, ticker):
        """Map ticker to security ID and exchange segment."""
        self._load_instrument_list()

        if self.instrument_list is None or self.instrument_list.empty:
            return None

        # Try to find exact match on SEM_TRADING_SYMBOL
        # Default to NSE Equity if not specified
        # We assume ticker is just the symbol name e.g. "RELIANCE"

        # Priority 1: NSE Equity
        mask = (
            (self.instrument_list["SEM_TRADING_SYMBOL"] == ticker)
            & (self.instrument_list["SEM_EXM_EXCH_ID"] == "NSE")
            & (self.instrument_list["SEM_SEGMENT"] == "E")
        )

        row = self.instrument_list[mask]
        if not row.empty:
            return row.iloc[0]

        # Priority 2: BSE Equity
        mask = (
            (self.instrument_list["SEM_TRADING_SYMBOL"] == ticker)
            & (self.instrument_list["SEM_EXM_EXCH_ID"] == "BSE")
            & (self.instrument_list["SEM_SEGMENT"] == "E")
        )

        row = self.instrument_list[mask]
        if not row.empty:
            return row.iloc[0]

        # Priority 3: Any match
        mask = self.instrument_list["SEM_TRADING_SYMBOL"] == ticker
        row = self.instrument_list[mask]
        if not row.empty:
            return row.iloc[0]

        return None

    # TODO: fix caching to handle intraday data properly, we don't fetch heads and tails correctly yet
    def get_historical_data(
        self, ticker: str, start: str, end: str, interval: str, use_cache=True
    ) -> pd.DataFrame:
        """
        Get historical data from Dhan.

        Args:
            ticker: Symbol (e.g. 'RELIANCE')
            start: Start date (YYYY-MM-DD)
            end: End date (YYYY-MM-DD)
            interval: '1m', '5m', '15m', '25m', '60m', '1d'
            use_cache: Whether to use cached data
        """
        # Convert start/end to datetime objects for the cache
        # Handle potential time component in string
        try:
            start_dt = pd.to_datetime(start).to_pydatetime()
            end_dt = pd.to_datetime(end).to_pydatetime()
        except Exception:
            # Fallback if parsing fails, though pd.to_datetime is robust
            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")

        if use_cache:
            cached_df = self._cache.get(ticker, start_dt, end_dt, interval)
            if cached_df is not None and not cached_df.empty:
                cached_df = cached_df.rename(
                    columns={
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume",
                    }
                )
                return cached_df

        # Fetch from Dhan
        df = self._fetch_from_dhan(ticker, start, end, interval)

        if use_cache and not df.empty:
            self._cache.put(ticker, start_dt, end_dt, interval, df)

        return df

    def _fetch_from_dhan(
        self, ticker: str, start: str, end: str, interval: str
    ) -> pd.DataFrame:
        if not hasattr(self, "dhan"):
            _log.error("Dhan client not initialized.")
            return pd.DataFrame()

        security_info = self._get_security_info(ticker)
        if security_info is None:
            _log.error(
                "Could not find security info for %s in instrument list.", ticker
            )
            return pd.DataFrame()

        security_id = str(security_info["SEM_SMST_SECURITY_ID"])
        exch_id = security_info["SEM_EXM_EXCH_ID"]
        segment = security_info["SEM_SEGMENT"]

        # Map to exchangeSegment
        exchange_segment = "NSE_EQ"  # Default
        if exch_id == "NSE":
            if segment == "E":
                exchange_segment = "NSE_EQ"
            elif segment == "D":
                exchange_segment = "NSE_FNO"
            elif segment == "C":
                exchange_segment = "NSE_CURR"
        elif exch_id == "BSE":
            if segment == "E":
                exchange_segment = "BSE_EQ"
            elif segment == "D":
                exchange_segment = "BSE_FNO"
            elif segment == "C":
                exchange_segment = "BSE_CURR"
        elif exch_id == "MCX":
            exchange_segment = "MCX_COMM"

        interval_norm = interval.lower().replace("min", "m").replace("minute", "m")

        instrument_type = security_info.get("SEM_INSTRUMENT_NAME", "EQUITY")

        _log.info(
            "Fetching Dhan data for %s: SecurityID=%s, Segment=%s, Instrument=%s, Start=%s, End=%s",
            ticker,
            security_id,
            exchange_segment,
            instrument_type,
            start,
            end,
        )

        try:
            if interval_norm in ["1d", "d", "day"]:
                # Ensure date only for daily
                start_date = start.split(" ")[0]
                end_date = end.split(" ")[0]

                response = self.dhan.historical_daily_data(
                    security_id=security_id,
                    exchange_segment=exchange_segment,
                    instrument_type=instrument_type,
                    from_date=start_date,
                    to_date=end_date,
                )

            else:
                # Intraday Data - Handle 90-day API limit by chunking
                # Convert strings to datetime for calculation
                start_dt = pd.to_datetime(start)
                end_dt = pd.to_datetime(end)

                # Generate all chunks first
                chunks = []
                current_start = start_dt
                # Use 60 days to be safe (limit is ~90 days)
                chunk_size = timedelta(days=60)

                while current_start <= end_dt:
                    current_end = min(current_start + chunk_size, end_dt)
                    chunks.append((current_start, current_end))
                    current_start = current_end + timedelta(days=1)

                # Map interval for Dhan API (removes 'm' suffix)
                dhan_interval = interval_norm.replace("m", "").strip()

                # Validate against Dhan allowed values
                if dhan_interval not in ["1", "5", "15", "25", "60"]:
                    # Handle common cases like '1h' -> '60'
                    if "h" in dhan_interval or dhan_interval == "1":  # 1h
                        dhan_interval = "60"
                    else:
                        _log.warning(
                            "Interval '%s' derived from '%s' is not supported. Defaulting to '60'.",
                            dhan_interval,
                            interval,
                        )
                        dhan_interval = "60"

                import concurrent.futures

                def fetch_chunk(chunk):
                    c_start, c_end = chunk
                    # Format timestamps for API
                    s_str = c_start.strftime("%Y-%m-%d 09:15:00")
                    e_str = c_end.strftime("%Y-%m-%d 15:30:00")

                    try:
                        return self.dhan.intraday_minute_data(
                            security_id=security_id,
                            exchange_segment=exchange_segment,
                            instrument_type=instrument_type,
                            from_date=s_str,
                            to_date=e_str,
                            interval=int(dhan_interval),
                        )
                    except Exception as e:
                        _log.error("Error fetching chunk %s to %s: %s", s_str, e_str, e)
                        return None

                # Parallel execution
                dfs = []
                # TODO: need to fix concurrency issues, so using max_workers=1 for now
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    futures = [executor.submit(fetch_chunk, chunk) for chunk in chunks]

                    for future in concurrent.futures.as_completed(futures):
                        chunk_resp = future.result()

                        if isinstance(chunk_resp, dict) and "data" in chunk_resp:
                            c_data = chunk_resp["data"]
                            if c_data:
                                try:
                                    # Convert to DF immediately to avoid array length mismatch
                                    try:
                                        chunk_df = pd.DataFrame(c_data)
                                    except ValueError:
                                        # Handle scalar values (single row or error dict) by wrapping in list
                                        chunk_df = pd.DataFrame([c_data])

                                    if not chunk_df.empty:
                                        # Basic validation to ensure it contains market data
                                        # Look for at least one expected column to filter out error objects
                                        expected_cols = [
                                            "open",
                                            "high",
                                            "low",
                                            "close",
                                            "start_Time",
                                            "volume",
                                        ]
                                        if any(
                                            col in chunk_df.columns
                                            for col in expected_cols
                                        ):
                                            dfs.append(chunk_df)
                                    # else:
                                    #     print(f"Chunk returned empty DataFrame")
                                except Exception as e:
                                    _log.error(
                                        "Error converting chunk to DataFrame: %s", e
                                    )
                            # else:
                            #     print(f"Chunk data is empty: {chunk_resp.get('status', 'unknown')}")
                        else:
                            # print(f"Invalid chunk response format: {type(chunk_resp)} - {str(chunk_resp)[:200]}...")
                            pass

                    if not dfs:
                        return pd.DataFrame()

                    df = pd.concat(dfs, ignore_index=True)

                    # Handle Dhan API column names (start_Time -> timestamp)
                    if "start_Time" in df.columns:
                        df = df.rename(columns={"start_Time": "timestamp"})

                    # Convert timestamp (Epoch seconds) to datetime
                    if "timestamp" in df.columns:
                        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
                        df = df.set_index("timestamp")
                        df = df.sort_index()
                        # Remove duplicates if any
                        df = df[~df.index.duplicated(keep="first")]
                    else:
                        _log.warning(
                            "'timestamp' or 'start_Time' column not found in Dhan data. Columns: %s",
                            df.columns.tolist(),
                        )

                    # Rename columns
                    df = df.rename(
                        columns={
                            "open": "Open",
                            "high": "High",
                            "low": "Low",
                            "close": "Close",
                            "volume": "Volume",
                        }
                    )

                    return df

            if (
                isinstance(response, dict)
                and "status" in response
                and response["status"] == "failure"
            ):
                _log.error("Dhan API error for %s: %s", ticker, response)
                return pd.DataFrame()

            if "data" in response:
                data = response["data"]
            else:
                data = response

            if not data or "open" not in data:
                _log.warning("No data returned for %s. Response: %s", ticker, response)
                return pd.DataFrame()

            df = pd.DataFrame(data)

            # Handle Dhan API column names (start_Time -> timestamp)
            if "start_Time" in df.columns:
                df = df.rename(columns={"start_Time": "timestamp"})

            # Convert timestamp (Epoch seconds) to datetime
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
                df = df.set_index("timestamp")
                df = df.sort_index()

            # Rename columns
            df = df.rename(
                columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )

            return df

        except Exception as e:
            _log.error("Dhan API error for %s: %s", ticker, e)
            return pd.DataFrame()

    def clear_cache(self, older_than_days=None):
        """Clear the cached data."""
        if older_than_days is not None:
            self._cache.cleanup_old_entries(days_old=older_than_days)
        else:
            self._cache.clear_cache()
