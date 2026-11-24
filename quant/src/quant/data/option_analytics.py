import contextlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from massive import RESTClient
from scipy.stats import norm

from quant.data.options_data_handler import OptionDataHandler

logging.getLogger(__name__).addHandler(logging.NullHandler())
_log = logging.getLogger(__name__)


class PolygonMarketAnalysis:
    """
    Analyzes option chain data using Polygon.io API.

    Two modes:
    ──────────
    1. **Live snapshots** (``get_live_market_state``)
       Uses ``/v3/snapshot/options/{ticker}`` — returns real-time OI,
       Greeks (delta/gamma/theta/vega), IV, and bid/ask for every
       contract in a single paginated call.  GEX is computed from
       the snapshot's own gamma & OI fields.

    2. **Historical reconstruction** (``get_daily_market_state``)
       Uses ``get_daily_open_close_agg`` per contract — gives OHLCV
       but **NOT** open interest (Polygon's daily O/C model does not
       include OI).  IV is Newton-Raphson from close prices.
       GEX and total_oi will be zero because OI is unavailable.

    For backtesting with OI/GEX you need Polygon flat-file downloads
    or a provider like Databento that has historical OI.
    """

    def __init__(self):
        """
        Initialize with an OptionDataHandler instance.
        """
        self.option_handler = OptionDataHandler()
        self.client = RESTClient(
            api_key=os.getenv("MASSIVE_API_KEY", ""),
            num_pools=50,
            retries=3,
        )

    def _black_scholes_price(self, S, K, T, r, sigma, type_="call"):
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        if type_ == "call":
            return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
        else:
            return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    def _calculate_iv(self, price, S, K, T, r=0.05, type_="call"):
        """
        Calculates Implied Volatility using Newton-Raphson.
        """
        if T <= 0 or price <= 0:
            return 0.0

        sigma = 0.5  # Initial guess
        for _i in range(20):
            try:
                P = self._black_scholes_price(S, K, T, r, sigma, type_)
                diff = price - P
                if abs(diff) < 1e-4:
                    return sigma

                d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
                vega = S * np.sqrt(T) * norm.pdf(d1)

                if abs(vega) < 1e-8:
                    break

                sigma = sigma + diff / vega
                if sigma <= 0:
                    sigma = 0.01  # Keep positive
            except Exception:
                return 0.0

        return sigma

    def _calculate_greeks(self, S, K, T, r, sigma, type_="call"):
        """
        Calculates Delta and Gamma.
        """
        if T <= 0 or sigma <= 0:
            return 0.0, 0.0

        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))

        if type_ == "call":
            delta = norm.cdf(d1)
        else:
            delta = norm.cdf(d1) - 1

        gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))

        return delta, gamma

    # ─────────────────────────────────────────────────────────
    # Live Snapshot (real-time OI + Greeks + IV)
    # ─────────────────────────────────────────────────────────

    def get_live_market_state(
        self,
        ticker: str,
        underlying_price: float | None = None,
        strike_range: float = 10.0,
        max_expirations: int = 2,
    ) -> dict[str, Any]:
        """Return option market state from **live snapshots**.

        Uses ``list_snapshot_options_chain`` — a single paginated API
        call that returns OI, Greeks (delta/gamma/theta/vega), IV,
        and bid/ask for every listed contract.

        Parameters
        ----------
        ticker : str
            Underlying ticker (e.g. 'SPY', 'QQQ').
        underlying_price : float, optional
            Current spot price.  If *None*, inferred from the first
            snapshot's ``underlying_asset.price``.
        strike_range : float
            Only include contracts within ±strike_range of spot.
        max_expirations : int
            Limit analysis to nearest N expirations.

        Returns
        -------
        dict with keys:
            vol_pcr, oi_pcr, atm_iv, expected_move, net_gex,
            total_oi, underlying_price, iv_skew, term_structure
        """
        try:
            snaps = list(self.client.list_snapshot_options_chain(ticker))
        except Exception as e:
            print(f"Error fetching snapshot chain for {ticker}: {e}")
            return {}

        if not snaps:
            return {}

        # Infer spot if not provided
        if underlying_price is None:
            for s in snaps:
                ua = getattr(s, "underlying_asset", None)
                if ua and getattr(ua, "price", None):
                    underlying_price = ua.price
                    break
        if underlying_price is None or underlying_price <= 0:
            return {}

        # Parse contract details from each snapshot
        today = datetime.now().date()
        records = []
        for s in snaps:
            det = getattr(s, "details", None)
            if det is None:
                continue
            strike = getattr(det, "strike_price", None)
            exp_str = getattr(det, "expiration_date", None)
            ctype = getattr(det, "contract_type", None)
            if strike is None or exp_str is None or ctype is None:
                continue
            # Filter by strike range
            if abs(strike - underlying_price) > strike_range:
                continue
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except Exception:
                _log.debug(
                    "Could not parse expiration date: %s", exp_str, exc_info=True
                )
                continue
            dte = (exp_date - today).days
            if dte < 0:
                continue

            oi = getattr(s, "open_interest", 0) or 0
            iv = getattr(s, "implied_volatility", 0) or 0
            greeks = getattr(s, "greeks", None)
            gamma = getattr(greeks, "gamma", 0) if greeks else 0
            delta = getattr(greeks, "delta", 0) if greeks else 0
            theta = getattr(greeks, "theta", 0) if greeks else 0
            vega = getattr(greeks, "vega", 0) if greeks else 0

            day_data = getattr(s, "day", None)
            volume = getattr(day_data, "volume", 0) if day_data else 0

            records.append(
                {
                    "strike": strike,
                    "exp": exp_str,
                    "dte": dte,
                    "type": ctype,
                    "oi": oi,
                    "iv": iv,
                    "gamma": gamma,
                    "delta": delta,
                    "theta": theta,
                    "vega": vega,
                    "volume": volume or 0,
                }
            )

        if not records:
            return {}

        df = pd.DataFrame(records)

        # Limit to nearest N expirations
        unique_exps = sorted(df["exp"].unique())[:max_expirations]
        df = df[df["exp"].isin(unique_exps)]

        # ── Aggregates ──
        calls = df[df["type"] == "call"]
        puts = df[df["type"] == "put"]

        total_vol_call = calls["volume"].sum()
        total_vol_put = puts["volume"].sum()
        total_oi_call = calls["oi"].sum()
        total_oi_put = puts["oi"].sum()

        vol_pcr = total_vol_put / max(total_vol_call, 1)
        oi_pcr = total_oi_put / max(total_oi_call, 1)
        total_oi = total_oi_call + total_oi_put

        # ── ATM IV (within 1% of spot) ──
        atm_mask = (abs(df["strike"] - underlying_price) / underlying_price) < 0.01
        atm_df = df[atm_mask & (df["iv"] > 0) & (df["iv"] < 5.0)]
        atm_iv = atm_df["iv"].mean() if len(atm_df) > 0 else 0.0

        # ── IV skew (25-delta put IV - 25-delta call IV) ──
        iv_skew = 0.0
        put_25d = puts[(puts["delta"].abs() - 0.25).abs() < 0.10]
        call_25d = calls[(calls["delta"].abs() - 0.25).abs() < 0.10]
        if len(put_25d) > 0 and len(call_25d) > 0:
            iv_skew = put_25d["iv"].mean() - call_25d["iv"].mean()

        # ── Expected move (use nearest exp DTE) ──
        nearest_dte = df["dte"].min()
        if nearest_dte > 0 and atm_iv > 0:
            expected_move = underlying_price * atm_iv * np.sqrt(nearest_dte / 365.0)
        else:
            expected_move = 0.0

        # ── Net GEX (real gamma x real OI from snapshot) ──
        # GEX = Gamma x OI x 100 x Spot x (1 for calls, -1 for puts)
        net_gex = 0.0
        for _, row in df.iterrows():
            g = row["gamma"] or 0
            oi_r = row["oi"] or 0
            if g > 0 and oi_r > 0:
                gex = g * oi_r * 100 * underlying_price
                if row["type"] == "put":
                    gex = -gex
                net_gex += gex

        # ── Term structure (IV by expiration) ──
        term_structure = {}
        for exp in unique_exps:
            exp_df = df[(df["exp"] == exp) & atm_mask & (df["iv"] > 0)]
            if len(exp_df) > 0:
                term_structure[exp] = float(exp_df["iv"].mean())

        return {
            "vol_pcr": vol_pcr,
            "oi_pcr": oi_pcr,
            "atm_iv": atm_iv,
            "iv_skew": iv_skew,
            "expected_move": expected_move,
            "net_gex": net_gex,
            "total_oi": total_oi,
            "underlying_price": underlying_price,
            "term_structure": term_structure,
            "n_contracts": len(df),
            "nearest_dte": int(nearest_dte) if not pd.isna(nearest_dte) else 0,
        }

    # ─────────────────────────────────────────────────────────
    # Historical Reconstruction (daily O/C — no OI/GEX)
    # ─────────────────────────────────────────────────────────

    def get_market_state_history(
        self,
        ticker: str,
        dates: list[datetime],
        underlying_prices: list[float],
        max_workers: int = 20,
    ) -> pd.DataFrame:
        """Fetch historical market state for multiple dates in parallel.

        Uses ``get_daily_market_state`` (daily O/C aggs) which provides
        volume-based PCR and Newton-Raphson IV but **not** OI or GEX
        (Polygon's ``DailyOpenCloseAgg`` model lacks ``open_interest``).

        Returns a DataFrame indexed by date.
        """
        results = []

        # Use ThreadPoolExecutor to fetch data in parallel
        # This significantly speeds up backtesting by overlapping network I/O
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_date = {
                executor.submit(self.get_daily_market_state, ticker, date, price): date
                for date, price in zip(dates, underlying_prices, strict=False)
            }

            for future in as_completed(future_to_date):
                date = future_to_date[future]
                try:
                    res = future.result()
                    if res:
                        res["date"] = date
                        results.append(res)
                except Exception as e:
                    print(f"Error fetching state for {date}: {e}")

        if not results:
            return pd.DataFrame()

        df = pd.DataFrame(results)
        df = df.set_index("date")
        df = df.sort_index()
        return df

    def get_daily_market_state(
        self, ticker: str, date: datetime, underlying_price: float
    ) -> dict[str, Any]:
        """Fetch daily aggregates for a single historical date.

        Uses ``get_daily_open_close_agg`` per contract.

        **Limitation**: Polygon's ``DailyOpenCloseAgg`` model does NOT
        include ``open_interest``; ``getattr(stat, 'open_interest', 0)``
        always returns 0.  Consequently ``total_oi``, ``oi_pcr``, and
        ``net_gex`` will be zero for historical dates.  Use
        ``get_live_market_state()`` for real-time data with OI & Greeks.

        Returns dict with: vol_pcr, oi_pcr (always 1.0), atm_iv,
        expected_move, net_gex (always 0), total_oi (always 0).
        """
        date_str = date.strftime("%Y-%m-%d")

        if underlying_price is None or underlying_price <= 0:
            return {}

        # 1. List Contracts (Next 14 days, +/- 15% strikes)
        # We widen the strike range slightly to capture more OI for GEX/PCR
        min_exp = date_str
        max_exp = (date + timedelta(days=14)).strftime("%Y-%m-%d")

        try:
            resp = self.client.list_options_contracts(
                underlying_ticker=ticker,
                expiration_date_gte=min_exp,
                expiration_date_lte=max_exp,
                strike_price_gte=underlying_price - 10,
                strike_price_lte=underlying_price + 10,
                limit=500,
                expired=True,
            )
            all_contracts = list(resp)
        except Exception as e:
            print(f"Error listing contracts for {date_str}: {e}")
            return {}

        if not all_contracts:
            return {}

        # 2. Filter for Near Term (Next 2 Expirations)
        all_contracts.sort(key=lambda x: x.expiration_date)
        unique_exps = sorted({c.expiration_date for c in all_contracts})[:2]

        target_contracts = [
            c for c in all_contracts if c.expiration_date in unique_exps
        ]

        # 3. Fetch Daily Stats (Volume & OI)
        total_vol_put: int | float = 0
        total_vol_call: int | float = 0
        total_oi_put: int | float = 0
        total_oi_call: int | float = 0

        atm_ivs = []
        net_gamma = 0.0

        # NOTE: get_daily_open_close_agg does NOT return open_interest.
        # The DailyOpenCloseAgg model only has: open/high/low/close/volume.
        # OI is therefore always 0 for historical dates (see class docstring).

        for c in target_contracts:
            try:
                # Get daily O/C — gives OHLCV but NOT OI.
                # 1 API call per contract.
                daily_stat = None
                with contextlib.suppress(Exception):
                    daily_stat = self.client.get_daily_open_close_agg(
                        c.ticker, date_str
                    )

                if not daily_stat:
                    continue

                volume: int | float = daily_stat.volume or 0  # type: ignore[union-attr]  # Polygon DailyOpenCloseAgg
                oi = getattr(daily_stat, "open_interest", 0) or 0
                close_price: float | None = daily_stat.close  # type: ignore[union-attr]  # Polygon DailyOpenCloseAgg

                if c.contract_type == "put":
                    total_vol_put += volume
                    total_oi_put += oi
                else:
                    total_vol_call += volume
                    total_oi_call += oi

                # Calculate IV & Greeks
                dte = (datetime.strptime(c.expiration_date, "%Y-%m-%d") - date).days
                if dte > 0:
                    t_years = dte / 365.0

                    # IV Calculation (Only if price > 0.05 to avoid noise)
                    iv = 0.0
                    if close_price is not None and close_price > 0.05:
                        iv = self._calculate_iv(
                            close_price,
                            underlying_price,
                            c.strike_price,
                            t_years,
                            type_=c.contract_type,
                        )

                    # ATM IV Collection (within 1% of spot)
                    if (
                        abs(c.strike_price - underlying_price) / underlying_price < 0.01
                        and iv > 0
                        and iv < 5.0
                    ):
                        atm_ivs.append(iv)

                    # Gamma Exposure Calculation
                    # GEX = Gamma * OI * 100 * Spot * (1 if Call, -1 if Put)
                    if iv > 0:
                        _delta, gamma = self._calculate_greeks(
                            underlying_price,
                            c.strike_price,
                            t_years,
                            0.05,
                            iv,
                            c.contract_type,
                        )
                        contract_gex = gamma * oi * 100 * underlying_price
                        if c.contract_type == "put":
                            contract_gex = -contract_gex
                        net_gamma += contract_gex

            except Exception:
                _log.debug("Skipping contract in GEX calculation", exc_info=True)
                continue
        vol_pcr = total_vol_put / total_vol_call if total_vol_call > 0 else 1.0
        oi_pcr = total_oi_put / total_oi_call if total_oi_call > 0 else 1.0
        avg_iv = np.mean(atm_ivs) if atm_ivs else 0.0

        # Expected Move — use actual nearest-expiration DTE (not hardcoded 7 days)
        nearest_dte = min(
            ((datetime.strptime(e, "%Y-%m-%d") - date).days for e in unique_exps),
            default=7,
        )
        if nearest_dte <= 0:
            nearest_dte = 1
        expected_move = underlying_price * avg_iv * np.sqrt(nearest_dte / 365.0)

        return {
            "vol_pcr": vol_pcr,
            "oi_pcr": oi_pcr,
            "atm_iv": avg_iv,
            "expected_move": expected_move,
            "net_gex": net_gamma,
            "total_oi": total_oi_call + total_oi_put,
            "underlying_price": underlying_price,
        }
