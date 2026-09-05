"""Indicateurs techniques : EMA, RSI, bougies de confirmation (M5/M30).

Utilisés par la stratégie EUR/USD :
    D1  : EMA200 (tendance de fond)
    H4  : EMA50 / EMA200 (tendance principale)
    H1  : EMA50 (direction autorisée)
    M15 : RSI14 (momentum, affiché dans le signal)
    M5/M30 : engulfing / pin bar = bougie de CONFIRMATION d'entrée
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Moyenne mobile exponentielle (période classique, min_periods=period)."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder."""
    delta = series.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _body(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()


def bullish_engulfing(df: pd.DataFrame, i: int) -> bool:
    """Bougie i avale baissière précédente et clôture au-dessus."""
    if i < 1:
        return False
    o0, c0 = df["open"].iloc[i - 1], df["close"].iloc[i - 1]
    o1, h1, l1, c1 = (df["close"].iloc[i], df["high"].iloc[i],
                      df["low"].iloc[i], df["close"].iloc[i])
    o1 = df["open"].iloc[i]
    return bool(c0 < o0 and c1 > o1 and c1 >= o0 and o1 <= c0 and c1 > (h1 + l1) / 2)


def bearish_engulfing(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    o0, c0 = df["open"].iloc[i - 1], df["close"].iloc[i - 1]
    o1, l1, h1, c1 = df["open"].iloc[i], df["low"].iloc[i], df["high"].iloc[i], df["close"].iloc[i]
    return bool(c0 > o0 and c1 < o1 and c1 <= o0 and o1 >= c0 and c1 < (h1 + l1) / 2)


def bull_pin_bar(df: pd.DataFrame, i: int, wick_ratio: float = 2.0) -> bool:
    """Marteau : mèche basse >= wick_ratio x corps, mèche haute faible, clôture haute."""
    if i < 0:
        return False
    o, h, l, c = df["open"].iloc[i], df["high"].iloc[i], df["low"].iloc[i], df["close"].iloc[i]
    body = max(abs(c - o), 1e-12)
    lower = min(o, c) - l
    upper = h - max(o, c)
    return bool(lower >= wick_ratio * body and upper < body and c >= o)


def bear_pin_bar(df: pd.DataFrame, i: int, wick_ratio: float = 2.0) -> bool:
    """Étoile filante : mèche haute dominante, clôture basse."""
    if i < 0:
        return False
    o, h, l, c = df["open"].iloc[i], df["high"].iloc[i], df["low"].iloc[i], df["close"].iloc[i]
    body = max(abs(c - o), 1e-12)
    lower = min(o, c) - l
    upper = h - max(o, c)
    return bool(upper >= wick_ratio * body and lower < body and c <= o)


def confirmation_candle(df: pd.DataFrame, direction: str, lookback: int = 3) -> dict | None:
    """Dernière bougie de confirmation (engulfing/pin bar) dans les `lookback` dernières.

    Returns: {"kind": "engulfing"|"pin bar", "tf": ..., "i": ...} ou None.
    """
    if df is None or df.empty:
        return None
    start = max(0, len(df) - lookback)
    for i in range(len(df) - 1, start - 1, -1):
        if direction == "bullish":
            if bullish_engulfing(df, i):
                return {"kind": "engulfing haussière", "i": i}
            if bull_pin_bar(df, i):
                return {"kind": "pin bar haussier (marteau)", "i": i}
        else:
            if bearish_engulfing(df, i):
                return {"kind": "engulfing baissière", "i": i}
            if bear_pin_bar(df, i):
                return {"kind": "pin bar baissier", "i": i}
    return None
