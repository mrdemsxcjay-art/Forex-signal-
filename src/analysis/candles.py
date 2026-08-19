"""Utilitaires bougies : swings fractals (anti-repaint) + ATR.

SWINGS FRACTALS — définition mathématique :
    high[t] est un swing high ⟺ high[t] = max(high[t-k .. t+k])
    low[t]  est un swing low  ⟺ low[t]  = min(low[t-k .. t+k])
    k = nombre de bougies de chaque côté (défaut 2 -> fractale à 5 bougies,
    adapté au scalping/intraday).

    FONDAMENTAL ANTI-REPAINT : le côté droit (t+1..t+k) est indispensable ->
    un swing n'est CONFIRMÉ qu'à la clôture de la bougie t+k. Le moteur
    n'annonce donc jamais un sommet qui pourrait « disparaître » : il l'annonce
    avec k bougies de retard, retard nécessaire et CONSTANT (ce n'est pas du
    repaint, c'est le prix de la certitude).

ATR (Average True Range) :
    TR_t  = max(high−low, |high−close[t−1]|, |low−close[t−1]|)
    ATR_t = moyenne mobile simple de TR sur `period` bougies.
    Toutes les tolérances du moteur SMC (equal highs, taille mini de FVG,
    buffers de stop) sont exprimées en MULTIPLES D'ATR : elles restent
    valides quelle que soit la paire (EURUSD ~1e-3, XAUUSD ~10) et la
    volatilité du moment.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SwingPoint:
    """Sommet/creux confirmé. confirm_index = bougie où il devient utilisable."""

    kind: str  # "high" | "low"
    index: int  # position de la bougie extrême
    confirm_index: int  # position où le swing est confirmé (index + k)
    price: float
    time: pd.Timestamp


def find_swing_points(df: pd.DataFrame, k: int = 2) -> list[SwingPoint]:
    """Détecte tous les swings fractals, triés par index de confirmation.

    Implémentation vectorisée (rolling center) : O(n) — 3 000 bougies en ~ms.
    Les ex-aequo (double sommet dans la fenêtre) sont tous retenus : ils
    deviennent des zones d'intérêt pour le module de liquidité.
    """
    highs, lows = df["high"], df["low"]
    win = 2 * k + 1
    roll_high = highs.rolling(win, center=True).max()
    roll_low = lows.rolling(win, center=True).min()
    is_high = (highs == roll_high) & roll_high.notna()
    is_low = (lows == roll_low) & roll_low.notna()

    times = df.index
    points: list[SwingPoint] = []
    for t in range(len(df)):
        if is_high.iloc[t]:
            points.append(SwingPoint("high", t, t + k, float(highs.iloc[t]), times[t]))
        if is_low.iloc[t]:
            points.append(SwingPoint("low", t, t + k, float(lows.iloc[t]), times[t]))
    points.sort(key=lambda s: (s.confirm_index, s.index))
    return points


def compute_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """ATR en tableau numpy, sans NaN (moyenne expansive au début)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period, min_periods=1).mean()
    return atr.to_numpy(dtype=float)
