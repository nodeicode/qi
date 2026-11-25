"""Sizing strategies — one per asset class (Strategy pattern).

Before this module, ``TradingEngine`` owned four hand-written sizing methods
(``size_equity / size_option / size_spread / size_ic``) with asset-specific
math inline, so adding an asset class meant editing the engine.  Now each
asset class implements :class:`SizingStrategy` with a uniform ``size(**kw)``
signature and registers itself in the global :data:`SIZING_STRATEGIES`
registry, keyed by :class:`~quant.core._utils.AssetClass`.

``TradingEngine`` keeps its four public methods as thin, byte-identical
delegates (executors and live code call them today), and adds
``size_for_signal`` for asset-class-agnostic dispatch — the path new
executors (e.g. futures) use.

Behavior of the four existing size paths is **verbatim** from the historical
engine methods — the golden backtest harness pins this end-to-end.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from quant.core._utils import SIGNAL_REGISTRY, AssetClass
from quant.instruments.base import Instrument
from quant.utils.strategy_config import BaseStrategyConfig


class SizingStrategy(ABC):
    """Uniform sizing contract for one asset class.

    Subclasses accept *different* keyword inputs (equity shares, IC
    collateral, futures margin…) — the contract is the **return shape**
    (a sizing dict with ``n_contracts``/``shares``/``capital`` keys), not a
    shared parameter list.  The base therefore declares a fully-open
    signature; each implementation re-declares its own typed kwargs.
    """

    #: Asset class this strategy sizes for — the registry key.
    asset_class: ClassVar[AssetClass]

    @abstractmethod
    def size(self, *args: Any, **kw: Any) -> dict:
        """Return the sizing dict for a trade.

        Keyword arguments are supplied by the caller (``TradingEngine``
        dispatch or executor); each strategy pulls the ones it needs.
        Returning ``n_contracts``/``shares`` of 0 signals "do not trade".
        """
        ...


class EquitySizing(SizingStrategy):
    """Fraction-of-equity share sizing (verbatim port of ``size_equity``)."""

    asset_class = AssetClass.EQUITY

    def size(
        self,
        *,
        config: BaseStrategyConfig,
        equity: float,
        entry_price: float,
        pos_size: float = 1.0,
        **kw: Any,
    ) -> dict:
        capital_frac = float(config.CAPITAL_PER_TRADE)
        effective_capital = equity * capital_frac * pos_size
        shares = int(effective_capital / entry_price) if entry_price > 0 else 0
        return {
            "effective_capital": effective_capital,
            "shares": shares,
            "capital_fraction": capital_frac,
        }


class OptionSizing(SizingStrategy):
    """Single-leg option sizing (verbatim port of ``size_option``)."""

    asset_class = AssetClass.OPTION

    def size(
        self,
        *,
        config: BaseStrategyConfig,
        equity: float,
        premium_per_contract: float,
        **kw: Any,
    ) -> dict:
        capital_frac = float(config.CAPITAL_PER_TRADE)
        capital = equity * capital_frac
        n_contracts = (
            max(1, int(capital / premium_per_contract))
            if premium_per_contract > 0
            else 0
        )
        return {"capital": capital, "n_contracts": n_contracts}


class SpreadSizing(SizingStrategy):
    """Spread sizing — ``CAPITAL_PER_TRADE`` as direct dollar amount (verbatim port of ``size_spread``)."""

    asset_class = AssetClass.SPREAD

    def size(
        self, *, config: BaseStrategyConfig, entry_price_per_share: float, **kw: Any
    ) -> dict:
        cpt = float(config.CAPITAL_PER_TRADE)
        premium_per_contract = abs(entry_price_per_share) * 100
        n_contracts = (
            max(1, int(cpt / premium_per_contract)) if premium_per_contract > 0 else 0
        )
        return {"capital": cpt, "n_contracts": n_contracts}


class IronCondorSizing(SizingStrategy):
    """Iron condor sizing with optional confidence tiers (verbatim port of ``size_ic``)."""

    asset_class = AssetClass.IC

    def size(
        self,
        *,
        config: BaseStrategyConfig,
        equity: float,
        confidence: float,
        collateral_per_contract: float,
        **kw: Any,
    ) -> dict:
        use_conf = getattr(config, "USE_CONFIDENCE_SIZING", False)
        if use_conf:
            tiers = getattr(config, "CONFIDENCE_SIZING_TIERS", [])
            cpt = float(config.CAPITAL_PER_TRADE)
            for min_conf, frac in sorted(tiers, reverse=True):
                if confidence >= min_conf:
                    cpt = frac
                    break
        else:
            cpt = float(config.CAPITAL_PER_TRADE)

        capital = equity * cpt if cpt < 1.0 else cpt
        max_cap = float(getattr(config, "MAX_CAPITAL_PER_TRADE", 0.0))
        capital = min(capital, max_cap)

        # Guard: invalid collateral or non-finite capital → 0 contracts so the
        # caller skips the trade (never divide by zero / size nonsense).
        if collateral_per_contract <= 0 or not math.isfinite(capital):
            return {"capital": capital, "n_contracts": 0, "collateral_per_contract": 0}

        n = max(1, int(capital / collateral_per_contract))
        return {
            "capital": capital,
            "n_contracts": n,
            "collateral_per_contract": collateral_per_contract,
        }


class FuturesSizing(SizingStrategy):
    """Outright futures sizing via per-contract margin.

    Futures are margined instruments — you do not pay the full notional.  The
    per-contract margin is resolved as::

        margin = config.FUTURES_MARGIN_PER_CONTRACT        (explicit $, if set)
        else config.FUTURES_MARGIN_FRACTION * entry_price * instrument.multiplier

    Contract facts come from the passed typed ``instrument`` — there is no
    separate contract-facts registry.  ``FUTURES_MARGIN_FRACTION`` defaults to
    ``0.10`` — a documented approximation; strategies that need exchange-exact
    margin (initial + maintenance, per-tier, product-specific) should set
    ``FUTURES_MARGIN_PER_CONTRACT`` explicitly.
    """

    asset_class = AssetClass.FUTURES

    def size(
        self,
        *,
        config: BaseStrategyConfig,
        equity: float,
        entry_price: float,
        instrument: Instrument | None = None,
        **kw: Any,
    ) -> dict:
        capital = equity * float(config.CAPITAL_PER_TRADE)
        multiplier = instrument.multiplier if instrument is not None else None
        explicit_margin = getattr(config, "FUTURES_MARGIN_PER_CONTRACT", None)
        if explicit_margin:
            margin = float(explicit_margin)
        elif multiplier is not None:
            frac = float(getattr(config, "FUTURES_MARGIN_FRACTION", 0.10))
            margin = frac * entry_price * multiplier
        else:
            raise ValueError(
                "FuturesSizing requires either FUTURES_MARGIN_PER_CONTRACT "
                "or a resolvable product root (for multiplier * fraction margin)."
            )

        if margin <= 0 or not math.isfinite(capital):
            return {"capital": capital, "n_contracts": 0, "margin_per_contract": 0.0}

        n = max(1, int(capital // margin)) if capital >= margin else 0
        return {
            "capital": capital,
            "n_contracts": n,
            "margin_per_contract": float(margin),
        }


class SizingRegistry:
    """Registry of :class:`SizingStrategy` keyed by :class:`AssetClass`.

    Additive by construction — registering a sizing strategy for a new asset
    class never edits the engine (mirrors ``SignalRegistry``).
    """

    def __init__(self) -> None:
        self._strategies: dict[AssetClass, SizingStrategy] = {}

    def register(self, strategy: SizingStrategy) -> None:
        key = AssetClass(strategy.asset_class)
        if key in self._strategies:
            raise ValueError(
                f"SizingStrategy already registered for asset class {key!r} — "
                f"register under a distinct asset class."
            )
        self._strategies[key] = strategy

    def get(
        self, asset_class: AssetClass | str, *, strict: bool = True
    ) -> SizingStrategy:
        key = AssetClass(str(asset_class).upper())
        try:
            return self._strategies[key]
        except KeyError:
            if strict:
                registered = (
                    ", ".join(sorted(a.value for a in self._strategies)) or "(none)"
                )
                raise KeyError(
                    f"No SizingStrategy registered for asset class {key!r}. "
                    f"Registered: {registered}"
                ) from None
            raise

    def asset_classes(self) -> list[str]:
        return sorted(a.value for a in self._strategies)


# Global registry — seed with the asset classes the engine ships today.
SIZING_STRATEGIES: SizingRegistry = SizingRegistry()
SIZING_STRATEGIES.register(EquitySizing())
SIZING_STRATEGIES.register(OptionSizing())
SIZING_STRATEGIES.register(SpreadSizing())
SIZING_STRATEGIES.register(IronCondorSizing())
SIZING_STRATEGIES.register(FuturesSizing())


def size_for_asset(
    asset_class: AssetClass | str, *, config: BaseStrategyConfig, **kw: Any
) -> dict:
    """Registry-backed sizing dispatch used by the engine dispatch path."""
    return SIZING_STRATEGIES.get(asset_class, strict=True).size(config=config, **kw)


def size_for_signal(signal: str, *, config: BaseStrategyConfig, **kw: Any) -> dict:
    """Asset-class-agnostic sizing dispatch (unknown → EQUITY, live-safe).

    The strategy-owned entry point — ``Strategy.size`` redirects here.
    ``signal`` may be a signal value (e.g. ``"BUY"``) or any registered alias;
    keyword arguments forward to the asset class's :class:`SizingStrategy`
    (equity/entry_price/pos_size for EQUITY, confidence/collateral_per_contract
    for IC, root for FUTURES, …). Unknown signals fall back to EQUITY sizing
    via ``SIGNAL_REGISTRY``'s forgiving routing semantics.
    """
    return size_for_asset(SIGNAL_REGISTRY.asset_class(signal), config=config, **kw)
