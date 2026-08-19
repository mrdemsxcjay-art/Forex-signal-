"""Structure de marché : BOS / CHoCH — machine à états chronologique.

DÉFINITIONS :
    BOS (Break of Structure)    : clôture AU-DELÀ du dernier swing DANS le sens
                                  de la tendance dominante -> continuation.
    CHoCH (Change of Character) : première clôture au-delà du dernier swing
                                  CONTRE la tendance dominante -> premier
                                  signal de retournement.

MATHÉMATIQUEMENT (SH = dernier swing high confirmé, SL = dernier swing low) :
    close[i] > SH  et trend == "bullish"  ->  BOS  haussier  (continuation)
    close[i] > SH  et trend == "bearish"  ->  CHoCH haussier (retournement)
    close[i] < SL  et trend == "bearish"  ->  BOS  baissier  (continuation)
    close[i] < SL  et trend == "bullish"  ->  CHoCH baissier (retournement)
    (première cassure sans tendance établie -> BOS)

CHOIX VOLONTAIRES (anti-repaint + temps réel) :
    1. SEULE LA CLÔTURE CASSE. Une mèche au-delà du swing n'est pas une cassure :
       c'est un balayage de liquidité -> module liquidity.py. La séparation
       clôture/mèche est LE mécanisme anti-faux-signaux du modèle SMC/ICT.
    2. Un swing n'est cassable qu'une fois CONFIRMÉ (k bougies, voir candles.py).
    3. Chaque événement est un fait historique immuable : émis à la bougie i,
       il n'est jamais modifié, déplacé ni supprimé ensuite (vérifié par test).
    4. Le swing de référence = le dernier swing confirmé non cassé (un sommet
       plus bas le remplace : suite de sommets baissiers = structure baissière).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .candles import SwingPoint


@dataclass
class StructureState:
    """État structurel en fin d'analyse (contexte temps réel)."""

    trend: str | None  # "bullish" | "bearish" | None (indéterminé)
    trend_since: pd.Timestamp | None
    last_swing_high: dict | None  # {"level", "index", "time"} non cassé
    last_swing_low: dict | None


def detect_structure(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    prefix: str = "SE",
) -> tuple[list[dict], StructureState]:
    """Balayage chronologique candle-par-candle, O(n).

    Returns:
        (événements BOS/CHoCH chronologiques, état final)
    """
    closes = df["close"].to_numpy()
    times = df.index
    n = len(df)

    events: list[dict] = []
    trend: str | None = None
    trend_since: pd.Timestamp | None = None
    ref_high: dict | None = None  # {"level", "index", "time"}
    ref_low: dict | None = None

    def emit(ev_type: str, direction: str, swing: dict, i: int) -> None:
        events.append({
            "id": f"{prefix}-{len(events) + 1:04d}",
            "type": ev_type,
            "direction": direction,
            "swing_time": swing["time"],
            "swing_level": round(float(swing["level"]), 6),
            "swing_index": int(swing["index"]),
            "break_time": times[i],
            "break_close": round(float(closes[i]), 6),
            "break_index": int(i),
        })

    p = 0
    for i in range(n):
        # 1) Les swings confirmés à cette bougie entrent en scène
        while p < len(swings) and swings[p].confirm_index <= i:
            s = swings[p]
            p += 1
            if s.kind == "high":
                if closes[i] > s.price:  # déjà dépassé à sa confirmation
                    emit(
                        "CHoCH" if trend == "bearish" else "BOS",
                        "bullish",
                        {"level": s.price, "index": s.index, "time": s.time},
                        i,
                    )
                    trend, trend_since = "bullish", times[i]
                else:
                    ref_high = {"level": s.price, "index": s.index, "time": s.time}
            else:  # swing low
                if closes[i] < s.price:
                    emit(
                        "CHoCH" if trend == "bullish" else "BOS",
                        "bearish",
                        {"level": s.price, "index": s.index, "time": s.time},
                        i,
                    )
                    trend, trend_since = "bearish", times[i]
                else:
                    ref_low = {"level": s.price, "index": s.index, "time": s.time}

        # 2) Cassures par clôture des swings de référence
        if ref_high is not None and closes[i] > ref_high["level"]:
            emit("CHoCH" if trend == "bearish" else "BOS", "bullish", ref_high, i)
            trend, trend_since = "bullish", times[i]
            ref_high = None  # consommé (les anciens sommets sont sous la clôture)
        if ref_low is not None and closes[i] < ref_low["level"]:
            emit("CHoCH" if trend == "bullish" else "BOS", "bearish", ref_low, i)
            trend, trend_since = "bearish", times[i]
            ref_low = None

    return events, StructureState(trend, trend_since, ref_high, ref_low)
