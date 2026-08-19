"""Fair Value Gaps (FVG / imbalances) — traces laissées par la vitesse.

DÉFINITION MATHÉMATIQUE (3 bougies consécutives t-2, t-1, t) :
    FVG haussier  ⟺ low[t] > high[t-2]
                   zone = [high[t-2], low[t]]  (le gap jamais négocié)
                   t-1 = bougie de déplacement (displacement)
    FVG baissier  ⟺ high[t] < low[t-2]
                   zone = [high[t], low[t-2]]

LOGIQUE : le prix s'est déplacé si vite d'un niveau à un autre que peu
d'échanges ont eu lieu dans ce gap. Le marché a une propriété d'efficience
locale : il revient souvent « remplir » l'inefficacité (mean reversion)
avant de poursuivre — d'où l'usage du FVG comme zone d'entrée en retracement.

REMPLISSAGE (fill) mesuré en % de la hauteur du gap :
    FVG haussier, bougie j postérieure : fill% = (zone_top − low[j]) / gap × 100
    - fill < 50 %              -> "active"
    - 50 % ≤ fill < 100 %      -> "mitigated" (partiellement rempli)
    - low[j] ≤ zone_bottom     -> "filled" (entièrement négocié, archivé)

ANTI-REPAINT : le FVG est créé à la clôture de la 3ᵉ bougie (fait figé).
Le fill_pct ne fait que CROÎTRE (monotonie testée) : l'historique des zones
n'est jamais réécri.

FILTRE SCALPING : gap < min_fvg_atr × ATR ignoré (bruit de spread).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def detect_fvgs(
    df: pd.DataFrame,
    atr: np.ndarray,
    min_gap_atr: float = 0.10,
) -> list[dict]:
    """Détection O(n) + passe de remplissage chronologique avec arrêt anticipé."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = df.index
    n = len(df)

    gaps: list[dict] = []
    for i in range(2, n):
        # FVG haussier : low[i] > high[i-2]
        if lows[i] > highs[i - 2]:
            zone_bottom, zone_top = float(highs[i - 2]), float(lows[i])
            gap_size = zone_top - zone_bottom
            if gap_size >= min_gap_atr * atr[i]:
                gaps.append({
                    "id": f"FVG-{len(gaps) + 1:04d}",
                    "direction": "bullish",
                    "zone_top": round(zone_top, 6),
                    "zone_bottom": round(zone_bottom, 6),
                    "gap_size": round(gap_size, 6),
                    "gap_atr": round(float(gap_size / atr[i]), 2) if atr[i] > 0 else None,
                    "displacement_time": times[i - 1],
                    "created_time": times[i],
                    "created_index": int(i),
                    "fill_pct": 0.0,
                    "status": "active",
                    "filled_at": None,
                })
        # FVG baissier : high[i] < low[i-2]
        if highs[i] < lows[i - 2]:
            zone_bottom, zone_top = float(highs[i]), float(lows[i - 2])
            gap_size = zone_top - zone_bottom
            if gap_size >= min_gap_atr * atr[i]:
                gaps.append({
                    "id": f"FVG-{len(gaps) + 1:04d}",
                    "direction": "bearish",
                    "zone_top": round(zone_top, 6),
                    "zone_bottom": round(zone_bottom, 6),
                    "gap_size": round(gap_size, 6),
                    "gap_atr": round(float(gap_size / atr[i]), 2) if atr[i] > 0 else None,
                    "displacement_time": times[i - 1],
                    "created_time": times[i],
                    "created_index": int(i),
                    "fill_pct": 0.0,
                    "status": "active",
                    "filled_at": None,
                })

    # Remplissage chronologique (early break une fois rempli à 100 %)
    for fvg in gaps:
        gap_size = fvg["zone_top"] - fvg["zone_bottom"]
        if gap_size <= 0:
            continue
        for j in range(fvg["created_index"] + 1, n):
            if fvg["direction"] == "bullish":
                if lows[j] <= fvg["zone_bottom"]:
                    fvg["fill_pct"] = 100.0
                    fvg["status"] = "filled"
                    fvg["filled_at"] = times[j]
                    break
                if lows[j] < fvg["zone_top"]:
                    fill = (fvg["zone_top"] - float(lows[j])) / gap_size * 100.0
                    if fill > fvg["fill_pct"]:
                        fvg["fill_pct"] = round(fill, 1)
            else:
                if highs[j] >= fvg["zone_top"]:
                    fvg["fill_pct"] = 100.0
                    fvg["status"] = "filled"
                    fvg["filled_at"] = times[j]
                    break
                if highs[j] > fvg["zone_bottom"]:
                    fill = (float(highs[j]) - fvg["zone_bottom"]) / gap_size * 100.0
                    if fill > fvg["fill_pct"]:
                        fvg["fill_pct"] = round(fill, 1)
        if fvg["status"] != "filled":
            fvg["status"] = "mitigated" if fvg["fill_pct"] >= 50 else "active"

    return gaps
