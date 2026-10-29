"""
Pure-pandas technical indicators used by the TA scorer.
All functions accept a DataFrame with at least columns: open, high, low, close, volume.
All functions return a pd.Series aligned to the input index.
No external TA library dependency — avoids the pandas-ta install complexity.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ── Trend ─────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period).mean()


def dema(series: pd.Series, period: int) -> pd.Series:
    """Double EMA — smoother, less lag than single EMA."""
    e = ema(series, period)
    return 2 * e - ema(e, period)


# ── Momentum ──────────────────────────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder's smoothing).
    Returns values in [0, 100].
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rsi_vals = np.where(avg_loss == 0, 100.0, 100 - (100 / (1 + avg_gain / avg_loss)))
    return pd.Series(rsi_vals, index=series.index)


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD line, signal line, histogram.
    Returns (macd_line, signal_line, histogram).
    """
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def roc(series: pd.Series, period: int = 10) -> pd.Series:
    """Rate of Change: (close / close[n] - 1) × 100."""
    return (series / series.shift(period) - 1) * 100


# ── Volatility ────────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's exponential smoothing."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands: upper, middle (SMA), lower.
    Returns (upper, middle, lower).
    """
    middle = sma(series, period)
    std = series.rolling(window=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def bb_width(series: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.Series:
    """Bollinger Band Width = (upper - lower) / middle."""
    upper, middle, lower = bollinger_bands(series, period, num_std)
    return (upper - lower) / middle.replace(0, np.nan)


def bb_pct_b(series: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.Series:
    """%B = (close - lower) / (upper - lower). 0 = at lower, 1 = at upper."""
    upper, _, lower = bollinger_bands(series, period, num_std)
    band_width = (upper - lower).replace(0, np.nan)
    return (series - lower) / band_width


# ── Volume ────────────────────────────────────────────────────────────────────

def vwap(df: pd.DataFrame) -> pd.Series:
    """
    Cumulative VWAP over the whole frame. For 24/7 crypto prefer
    ``rolling_vwap`` or ``utc_day_vwap`` below.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_tp_vol = (typical * df["volume"]).cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Rolling average volume over ``period`` bars."""
    return df["volume"].rolling(window=period).mean()


# ── Support / Resistance ──────────────────────────────────────────────────────

def pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """
    Classic pivot points from the previous bar's H/L/C.
    Returns DataFrame with columns: P, R1, R2, S1, S2.
    """
    prev_high = df["high"].shift(1)
    prev_low = df["low"].shift(1)
    prev_close = df["close"].shift(1)
    p = (prev_high + prev_low + prev_close) / 3
    return pd.DataFrame({
        "P": p,
        "R1": 2 * p - prev_low,
        "R2": p + (prev_high - prev_low),
        "S1": 2 * p - prev_high,
        "S2": p - (prev_high - prev_low),
    }, index=df.index)


# ── Composite helpers ─────────────────────────────────────────────────────────

def trend_strength(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    """
    EMA separation normalised by ATR.
    Positive → bullish momentum, negative → bearish.
    |value| > 1 means strong trend (separation > 1 ATR).
    """
    fast_ema = ema(df["close"], fast)
    slow_ema = ema(df["close"], slow)
    current_atr = atr(df)
    return (fast_ema - slow_ema) / current_atr.replace(0, np.nan)


def is_above_200sma(df: pd.DataFrame) -> pd.Series:
    """Boolean series: True when close is above the 200-bar SMA (trend regime filter)."""
    return df["close"] > sma(df["close"], 200)


# ── Crypto / intraday additions (24/7 markets: no session open, UTC day anchors) ──

def rolling_vwap(df: pd.DataFrame, window: int = 288) -> pd.Series:
    """Rolling VWAP over ``window`` bars (288 × 5m = 24h). Crypto has no session open,
    so a rolling window avoids the artificial midnight reset of a session VWAP."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = (typical * df["volume"]).rolling(window, min_periods=max(2, window // 4)).sum()
    v = df["volume"].rolling(window, min_periods=max(2, window // 4)).sum()
    return pv / v.replace(0, np.nan)


def utc_day_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP anchored at each UTC midnight (index must be tz-aware UTC)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.tz_convert("UTC").normalize() if df.index.tz is not None else df.index.normalize()
    pv = (typical * df["volume"]).groupby(day).cumsum()
    v = df["volume"].groupby(day).cumsum()
    return pv / v.replace(0, np.nan)


def zscore(series: pd.Series, period: int = 50) -> pd.Series:
    mean = series.rolling(period).mean()
    std = series.rolling(period).std()
    return (series - mean) / std.replace(0, np.nan)


def keltner(df: pd.DataFrame, period: int = 20, mult: float = 1.5
            ) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = ema(df["close"], period)
    a = atr(df, period)
    return mid + mult * a, mid, mid - mult * a


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> tuple[pd.Series, pd.Series]:
    """Returns (supertrend_line, direction) where direction is +1 up / -1 down."""
    hl2 = (df["high"] + df["low"]) / 2
    a = atr(df, period)
    upper = (hl2 + mult * a).to_numpy(dtype=float).copy()
    lower = (hl2 - mult * a).to_numpy(dtype=float).copy()
    close = df["close"].to_numpy(dtype=float)
    n = len(df)
    st = np.full(n, np.nan)
    dirn = np.ones(n)
    for i in range(1, n):
        if close[i - 1] <= upper[i - 1]:
            upper[i] = min(upper[i], upper[i - 1]) if not np.isnan(upper[i - 1]) else upper[i]
        if close[i - 1] >= lower[i - 1]:
            lower[i] = max(lower[i], lower[i - 1]) if not np.isnan(lower[i - 1]) else lower[i]
        if close[i] > upper[i - 1]:
            dirn[i] = 1
        elif close[i] < lower[i - 1]:
            dirn[i] = -1
        else:
            dirn[i] = dirn[i - 1]
        st[i] = lower[i] if dirn[i] == 1 else upper[i]
    return pd.Series(st, index=df.index), pd.Series(dirn, index=df.index)


def realized_vol(series: pd.Series, period: int = 288, bars_per_year: int = 105_120) -> pd.Series:
    """Annualised realised volatility from log returns (default: 5m bars, 24/7 year)."""
    r = np.log(series / series.shift(1))
    return r.rolling(period).std() * np.sqrt(bars_per_year)
