"""Causal vectorized signal/regime caches for repeated audit runs."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..signals.macro import MacroRegime, RATIOS, VIX_CALM, VIX_STRESSED, VIX_TICKER
from ..signals.technical import (
    DEFAULT_WEIGHTS,
    QuantSignal,
    _atr,
    _macd_histogram,
    _rsi,
)


def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    idx = pd.DatetimeIndex(work.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    work.index = idx.normalize()
    return work[~work.index.duplicated(keep="last")].sort_index()


class CachedSignalModel:
    """Drop-in ``compute_signal`` replacement whose tables are built once."""

    def __init__(self, bars: dict[str, pd.DataFrame], *, mode: str = "composite"):
        if mode not in {"composite", "simple_trend"}:
            raise ValueError(f"Unknown signal mode: {mode}")
        self.mode = mode
        self.tables = {
            symbol: self._table(frame)
            for symbol, frame in bars.items()
            if frame is not None and not frame.empty and "Close" in frame
        }

    def _table(self, frame: pd.DataFrame) -> pd.DataFrame:
        work = _normalise_frame(frame)
        close = work["Close"].astype(float)
        sma20 = close.rolling(20).mean()
        sma50 = close.rolling(50).mean()
        sma200 = close.rolling(200).mean()
        rsi = _rsi(close, 14)
        atr = _atr(work, 14)
        momentum = (close / close.shift(20) - 1.0) * 100.0
        volatility = close.pct_change().rolling(20).std() * np.sqrt(252.0) * 100.0
        macd = _macd_histogram(close)

        trend = ((close - sma50) / sma50 * 5.0).clip(-1.0, 1.0)
        cross = ((sma20 - sma50) / sma50 * 10.0).clip(-1.0, 1.0)
        momentum_component = (momentum / 10.0).clip(-1.0, 1.0)
        rsi_component = ((50.0 - rsi) / 50.0).clip(-1.0, 1.0)
        macd_component = ((macd / close) * 100.0).clip(-1.0, 1.0)
        stretch = close / sma20 - 1.0
        simple = pd.Series(
            np.where((close > sma200) & (sma50 > sma200), 1.0, -1.0),
            index=close.index,
        )
        return pd.DataFrame({
            "last_price": close,
            "sma20": sma20,
            "sma50": sma50,
            "sma200": sma200,
            "rsi": rsi,
            "atr": atr,
            "momentum": momentum,
            "volatility": volatility,
            "macd": macd,
            "trend_component": trend,
            "cross_component": cross,
            "momentum_component": momentum_component,
            "macd_component": macd_component,
            "rsi_component": rsi_component,
            "extended": (rsi >= 70.0) & (stretch >= 0.08),
            "simple": simple,
            "average_dollar_volume": (
                (close * work["Volume"].astype(float)).rolling(20, min_periods=1).mean()
                if "Volume" in work.columns
                else pd.Series(0.0, index=close.index)
            ),
        })

    def average_dollar_volume(self, symbol: str, frame: pd.DataFrame) -> float:
        if frame is None or frame.empty:
            return 0.0
        table = self.tables.get(symbol)
        if table is None:
            return 0.0
        day = pd.Timestamp(frame.index[-1])
        if day.tzinfo is not None:
            day = day.tz_localize(None)
        try:
            return float(table.at[day.normalize(), "average_dollar_volume"])
        except (KeyError, TypeError, ValueError):
            return 0.0

    def __call__(
        self,
        symbol: str,
        frame: pd.DataFrame,
        min_bars: int = 55,
        weights: dict[str, float] | None = None,
    ) -> QuantSignal | None:
        required = 200 if self.mode == "simple_trend" else min_bars
        if frame is None or frame.empty or len(frame) < required:
            return None
        table = self.tables.get(symbol)
        if table is None:
            return None
        day = pd.Timestamp(frame.index[-1])
        if day.tzinfo is not None:
            day = day.tz_localize(None)
        day = day.normalize()
        if day not in table.index:
            return None
        row = table.loc[day]
        if pd.isna(row["sma50"]) or pd.isna(row["atr"]):
            return None
        components = {
            "trend": float(row["trend_component"]),
            "cross": float(row["cross_component"]),
            "momentum": float(row["momentum_component"]),
            "macd": float(row["macd_component"]),
            "rsi": float(row["rsi_component"]),
        }
        if self.mode == "simple_trend":
            score = float(row["simple"])
            components = {"simple_trend": score}
        else:
            selected = DEFAULT_WEIGHTS if weights is None else weights
            score = float(np.clip(sum(selected[key] * components[key] for key in selected), -1.0, 1.0))
        return QuantSignal(
            symbol=symbol,
            score=score,
            last_price=float(row["last_price"]),
            sma20=float(row["sma20"]),
            sma50=float(row["sma50"]),
            rsi14=float(row["rsi"]),
            momentum_20d_pct=float(row["momentum"]),
            atr14=float(row["atr"]),
            volatility_annualized_pct=float(row["volatility"]),
            extended=bool(score > 0 and row["extended"]),
        )


class CachedRegimeModel:
    """Vectorized causal equivalent of the historical five-ratio regime."""

    def __init__(self, bars: dict[str, pd.DataFrame]):
        component_frames: dict[str, pd.Series] = {}
        for label, numerator, denominator, _ in RATIOS:
            num = bars.get(numerator, pd.DataFrame())
            den = bars.get(denominator, pd.DataFrame())
            if num is None or den is None or num.empty or den.empty:
                continue
            n = _normalise_frame(num)["Close"].astype(float)
            d = _normalise_frame(den)["Close"].astype(float)
            ratio = (n / d).dropna()
            level = (ratio / ratio.rolling(50).mean() - 1.0) * 8.0
            change = (ratio / ratio.shift(20) - 1.0) * 5.0
            score = 0.6 * level.clip(-1.0, 1.0) + 0.4 * change.clip(-1.0, 1.0)
            score.iloc[:59] = np.nan  # _ratio_score requires at least 60 observations.
            component_frames[label] = score
        self.components = pd.DataFrame(component_frames).sort_index()

        vix = bars.get(VIX_TICKER, pd.DataFrame())
        if vix is None or vix.empty:
            self.vix = pd.Series(dtype=float)
        else:
            self.vix = _normalise_frame(vix)["Close"].astype(float)

    def first_full_date(self) -> pd.Timestamp | None:
        if self.components.empty or len(self.components.columns) < len(RATIOS):
            return None
        valid = self.components.dropna()
        return None if valid.empty else pd.Timestamp(valid.index[0])

    def __call__(self, asof) -> MacroRegime:
        day = pd.Timestamp(asof)
        if day.tzinfo is not None:
            day = day.tz_localize(None)
        day = day.normalize()
        prior = self.components.loc[:day]
        values = {} if prior.empty else prior.iloc[-1].dropna().to_dict()
        if len(values) < 3:
            return MacroRegime(
                score=0.0,
                label="unknown",
                components={str(k): float(v) for k, v in values.items()},
                notes=["insufficient macro data; fail-closed, no new entries"],
            )
        base = float(np.clip(np.mean(list(values.values())), -1.0, 1.0))
        vix_value = None
        vix_prior = self.vix.loc[:day]
        if not vix_prior.empty:
            vix_value = float(vix_prior.iloc[-1])
            if vix_value >= VIX_STRESSED:
                base -= 0.5
            elif vix_value > VIX_CALM:
                base -= 0.5 * (vix_value - VIX_CALM) / (VIX_STRESSED - VIX_CALM)
            base = float(np.clip(base, -1.0, 1.0))
        label = "risk_on" if base >= 0.20 else ("risk_off" if base <= -0.20 else "neutral")
        return MacroRegime(
            score=base,
            label=label,
            components={str(k): float(v) for k, v in values.items()},
            vix=vix_value,
            notes=["precomputed causal regime"],
        )


class CachedCorrelationModel:
    """On-demand cache of causal rolling return correlations."""

    def __init__(self, bars: dict[str, pd.DataFrame]):
        self.returns = {
            symbol: _normalise_frame(frame)["Close"].astype(float).pct_change(fill_method=None).dropna()
            for symbol, frame in bars.items()
            if frame is not None and not frame.empty and "Close" in frame
        }
        self._pairs: dict[tuple[str, str, int], pd.Series] = {}

    def _pair(self, first: str, second: str, window: int) -> pd.Series:
        left, right = sorted((first, second))
        key = (left, right, window)
        cached = self._pairs.get(key)
        if cached is not None:
            return cached
        if left not in self.returns or right not in self.returns:
            result = pd.Series(dtype=float)
        else:
            joined = pd.concat(
                [self.returns[left].rename("left"), self.returns[right].rename("right")],
                axis=1,
                join="inner",
            ).dropna()
            result = joined["left"].rolling(window, min_periods=20).corr(joined["right"])
        # A duplicate calculation from another audit thread is harmless; all
        # inputs and outputs are deterministic and the assignment is atomic.
        self._pairs[key] = result
        return result

    def __call__(self, symbol: str, names: list[str], asof, window: int = 60) -> float | None:
        day = pd.Timestamp(asof)
        if day.tzinfo is not None:
            day = day.tz_localize(None)
        day = day.normalize()
        values: list[float] = []
        for other in dict.fromkeys(names):
            if other == symbol:
                continue
            prior = self._pair(symbol, other, int(window)).loc[:day].dropna()
            if not prior.empty:
                values.append(float(prior.iloc[-1]))
        return None if not values else float(sum(values) / len(values))
