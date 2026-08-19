"""Liquidité : equal highs/lows (pools) et balayages (sweeps).

EQUAL HIGHS / EQUAL LOWS — définition :
    un pool de liquidité = cluster de ≥ 2 swings du même type dont les prix
    sont à tolérance près identiques :
        |price_i − moyenne_cluster| ≤ eq_tol_atr × ATR (au swing i)
    Deux creux égaux = sell-side liquidity (stops des acheteurs sous les
    creux) ; deux sommets égaux = buy-side liquidity (stops des vendeurs
    au-dessus des sommets).

LIQUIDITY SWEEP (balayage / stop hunt) — définition stricte :
    sweep d'un pool de creux  ⟺ low[j]  < niveau  ET  close[j] > niveau
    sweep d'un pool de sommets⟺ high[j] > niveau  ET  close[j] < niveau
    (la mèche PREND la liquidité, la clôture REVIENT : échec de cassure)

INTERPRÉTATION : un sweep est la signature d'une manipulation institutionnelle —
    sweep des creux  -> implication HAUSSIÈRE (les stops alimentent les achats)
    sweep des sommets-> implication BAISSIÈRE
C'est typiquement le déclencheur qui précède un CHoCH (séquence ICT :
accumulation -> sweep -> displacement -> CHoCH -> retest OB).

ANTI-REPAINT : un pool n'exige que des swings CONFIRMÉS ; le sweep est un
fait de bougie clôturée (mèche + clôture figées) — émis une fois, jamais modifié.
Un pool peut ensuite être "swept" (état, par ajout) ou "broken" (cassé par
clôture -> c'est alors un BOS/CHoCH, pas un sweep).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .candles import SwingPoint


def _cluster_swings(points: list[SwingPoint], atr: np.ndarray, eq_tol_atr: float,
                    min_count: int, kind: str, times: pd.DatetimeIndex) -> list[dict]:
    """Regroupe les swings consécutifs d'un même type en clusters de prix proches."""
    clusters: list[dict] = []
    current: list[SwingPoint] = []

    def flush() -> None:
        if len(current) >= min_count:
            level = float(np.mean([s.price for s in current]))
            clusters.append({
                "kind": kind,  # "high" | "low"
                "level": round(level, 6),
                "count": len(current),
                "times": [s.time for s in current],
                "last_confirm_index": max(s.confirm_index for s in current),
                "status": "untouched",
                "swept_at": None,
            })

    for s in points:
        if not current:
            current = [s]
            continue
        tol = eq_tol_atr * atr[s.index]
        running_mean = float(np.mean([x.price for x in current]))
        if abs(s.price - running_mean) <= tol:
            current.append(s)
        else:
            flush()
            current = [s]
    flush()
    return clusters


def detect_liquidity(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    atr: np.ndarray,
    eq_tol_atr: float = 0.25,
    min_eq_count: int = 2,
) -> dict:
    """Retourne les pools (equal highs/lows) et les événements de sweep."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = df.index
    n = len(df)

    swing_highs = [s for s in swings if s.kind == "high"]
    swing_lows = [s for s in swings if s.kind == "low"]

    eq_highs = _cluster_swings(swing_highs, atr, eq_tol_atr, min_eq_count, "high", times)
    eq_lows = _cluster_swings(swing_lows, atr, eq_tol_atr, min_eq_count, "low", times)

    sweeps: list[dict] = []
    for pool in eq_highs + eq_lows:
        level = pool["level"]
        for j in range(pool["last_confirm_index"] + 1, n):
            if pool["kind"] == "high":
                if highs[j] > level:
                    if closes[j] < level:  # mèche au-dessus, clôture en dessous
                        sweeps.append({
                            "direction": "swept_highs",
                            "level": round(float(level), 6),
                            "time": times[j],
                            "index": int(j),
                            "wick_size": round(float(highs[j] - level), 6),
                            "wick_atr": round(float((highs[j] - level) / atr[j]), 2) if atr[j] > 0 else None,
                            "implication": "bearish",
                            "pool_count": pool["count"],
                        })
                        pool["status"], pool["swept_at"] = "swept", times[j]
                    else:
                        pool["status"] = "broken"  # cassé par clôture = BOS/CHoCH
                    break  # un seul événement par pool
            else:
                if lows[j] < level:
                    if closes[j] > level:
                        sweeps.append({
                            "direction": "swept_lows",
                            "level": round(float(level), 6),
                            "time": times[j],
                            "index": int(j),
                            "wick_size": round(float(level - lows[j]), 6),
                            "wick_atr": round(float((level - lows[j]) / atr[j]), 2) if atr[j] > 0 else None,
                            "implication": "bullish",
                            "pool_count": pool["count"],
                        })
                        pool["status"], pool["swept_at"] = "swept", times[j]
                    else:
                        pool["status"] = "broken"
                    break

    # IDs CHRONOLOGIQUES : le même événement garde le même identifiant quelle
    # que soit la fenêtre d'analyse (exigence de stabilité anti-repaint).
    sweeps.sort(key=lambda s: (s["index"], s["direction"]))
    for seq, sweep in enumerate(sweeps, start=1):
        sweep["id"] = f"SW-{seq:04d}"

    return {"equal_highs": eq_highs, "equal_lows": eq_lows, "sweeps": sweeps}
