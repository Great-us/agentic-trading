"""Composite technical score in [-1, 1] built from trend, crossover, momentum, and RSI.

This is deliberately simple and transparent (no ML, no curve-fit parameters) so
its output is easy to sanity-check and easy for the LLM layer to reason about
alongside qualitative context.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class QuantSignal:
    symbol: str
    score: float  # composite, [-1, 1], positive = bullish
    last_price: float
    sma20: float
    sma50: float
    rsi14: float
    momentum_20d_pct: float
    atr14: float
    volatility_annualized_pct: float
    extended: bool = False  # trend is healthy but the entry is chasing


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _macd_histogram(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """MACD histogram: adds a second, faster momentum read alongside the 20-day
    rate of change, which on its own misses turns that are already underway."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line


DEFAULT_WEIGHTS: dict[str, float] = {
    "trend": 0.30, "cross": 0.20, "momentum": 0.20, "macd": 0.20, "rsi": 0.10,
}


def weights_without(*dropped: str) -> dict[str, float]:
    """Renormalise DEFAULT_WEIGHTS after dropping components (ablation helper)."""
    kept = {k: v for k, v in DEFAULT_WEIGHTS.items() if k not in dropped}
    total = sum(kept.values()) or 1.0
    out = {k: v / total for k, v in kept.items()}
    for key in dropped:
        out[key] = 0.0
    return out


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, x)))


def compute_signal(
    symbol: str,
    df: pd.DataFrame,
    min_bars: int = 55,
    weights: dict[str, float] | None = None,
) -> QuantSignal | None:
    """Returns None if there isn't enough history to compute stable indicators."""
    if df.empty or len(df) < min_bars:
        return None

    close = df["Close"]
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    rsi = _rsi(close, 14)
    atr = _atr(df, 14)

    last_price = float(close.iloc[-1])
    last_sma20 = float(sma20.iloc[-1])
    last_sma50 = float(sma50.iloc[-1])
    last_rsi = float(rsi.iloc[-1])
    last_atr = float(atr.iloc[-1])

    momentum_20d_pct = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) > 21 else 0.0
    daily_returns = close.pct_change().dropna()
    volatility_annualized_pct = float(daily_returns.tail(20).std() * np.sqrt(252) * 100) if len(daily_returns) >= 20 else 0.0

    macd_hist = _macd_histogram(close)
    last_macd_hist = float(macd_hist.iloc[-1])

    trend_component = _clip((last_price - last_sma50) / last_sma50 * 5) if last_sma50 else 0.0
    cross_component = _clip((last_sma20 - last_sma50) / last_sma50 * 10) if last_sma50 else 0.0
    momentum_component = _clip(momentum_20d_pct / 10)
    rsi_component = _clip((50 - last_rsi) / 50)
    # Normalised by price so the histogram is comparable across share prices.
    macd_component = _clip((last_macd_hist / last_price) * 100) if last_price else 0.0

    weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
    components = {
        "trend": trend_component,
        "cross": cross_component,
        "momentum": momentum_component,
        "macd": macd_component,
        "rsi": rsi_component,
    }
    score = _clip(sum(weights[k] * components[k] for k in weights))

    # "Good stock, bad entry": the trend is intact but price is stretched far
    # above its own short-term mean with an overbought oscillator. Worth holding,
    # not worth chasing — see Decision.WAIT.
    stretch = (last_price / last_sma20 - 1) if last_sma20 else 0.0
    extended = bool(score > 0 and last_rsi >= 70 and stretch >= 0.08)

    return QuantSignal(
        symbol=symbol,
        score=score,
        last_price=last_price,
        sma20=last_sma20,
        sma50=last_sma50,
        rsi14=last_rsi,
        momentum_20d_pct=momentum_20d_pct,
        atr14=last_atr,
        volatility_annualized_pct=volatility_annualized_pct,
        extended=extended,
    )
