"""Moteur SMC agrégé — BOS, CHoCH, Order Blocks, FVG, Liquidité -> JSON.

Usage temps réel (boucle moteur) :
    engine = SMCEngine("EURUSD", Timeframe.M15)
    result = engine.analyze(df_candles_clôturées)   # dict JSON-ready
    json_str = SMCEngine.to_json(result)            # str
    SMCEngine.save_json(result, "data/smc.json")    # fichier

GARANTIES TEMPS RÉEL :
    - stateless : analyze(df) = fonction pure de df (même entrée -> même sortie,
      donc rejouable et testable — cœur de la garantie anti-repaint) ;
    - O(n) : ~3 000 bougies analysées en quelques dizaines de ms ;
    - consomme UNIQUEMENT des bougies clôturées (la couche données garantit
      déjà only_closed=True) ;
    - les détecteurs n'émettent que des faits figés à la clôture qui les crée.

Sortie (dict prêt pour json.dumps) : voir la docstring de SMCEngine.analyze.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

import pandas as pd

from ..data.provider import Timeframe, timeframe_from_str
from .candles import compute_atr, find_swing_points
from .fvg import detect_fvgs
from .liquidity import detect_liquidity
from .order_blocks import detect_order_blocks
from .structure import detect_structure


@dataclass
class SMCParams:
    """Paramètres du moteur (toutes les tolérances en multiples d'ATR).

    Réglage scalping/intraday (défauts) : k=2 (fractales 5 bougies, réactif),
    FVG ≥ 0.10 ATR (filtre le bruit de spread), tolérance equal highs 0.25 ATR.
    """

    swing_k: int = 2
    atr_period: int = 14
    eq_tol_atr: float = 0.25
    min_fvg_atr: float = 0.10
    ob_max_lookback: int = 20
    ob_use_body: bool = False
    min_eq_count: int = 2

    def to_dict(self) -> dict:
        return asdict(self)


def _iso(value) -> str | None:
    """Timestamp -> ISO 8601 UTC (None si absent)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return pd.Timestamp(value).isoformat()


def _iso_all(records: list[dict], time_keys: tuple[str, ...] = ()) -> list[dict]:
    """Convertit les timestamps d'une liste d'enregistrements en ISO."""
    scalar_keys = ("time", "filled_at", "touched_at", "invalidated_at",
                   "origin_time", "confirmed_time", "break_time", "swing_time",
                   "created_time", "displacement_time", "swept_at") + time_keys
    out = []
    for rec in records:
        rec = dict(rec)
        for key in scalar_keys:
            if key in rec:
                rec[key] = _iso(rec[key])
        if "times" in rec:  # liste des swings d'un pool de liquidité
            rec["times"] = [_iso(t) for t in rec["times"]]
        out.append(rec)
    return out


class SMCEngine:
    """Analyse SMC complète d'une paire / timeframe -> dict JSON-ready."""

    def __init__(self, pair: str, timeframe: Timeframe | str, params: SMCParams | None = None) -> None:
        self.pair = pair.upper()
        self.timeframe = timeframe_from_str(timeframe) if isinstance(timeframe, str) else timeframe
        self.params = params or SMCParams()

    # ------------------------------------------------------------------ #
    def analyze(self, df: pd.DataFrame) -> dict:
        """Analyse complète. df = bougies CLÔTURÉES au format standard projet.

        Returns (structure du dict) :
            pair, timeframe, generated_at, candles_analyzed,
            first_candle, last_candle, last_close, atr,
            trend {state, since},
            events {
                structure      : [BOS/CHoCH]  (faits immuables)
                order_blocks   : [zones OB]   (état évolue par ajout)
                fair_value_gaps: [zones FVG]  (fill_pct croissant)
                liquidity      : {equal_highs, equal_lows, sweeps}
            },
            context {nearest zones actives, pools intacts, dernier événement}
        """
        p = self.params
        swings = find_swing_points(df, k=p.swing_k)
        atr = compute_atr(df, period=p.atr_period)
        structure_events, state = detect_structure(df, swings)
        order_blocks = detect_order_blocks(
            df, structure_events, max_lookback=p.ob_max_lookback, use_body=p.ob_use_body
        )
        fvgs = detect_fvgs(df, atr, min_gap_atr=p.min_fvg_atr)
        liquidity = detect_liquidity(
            df, swings, atr, eq_tol_atr=p.eq_tol_atr, min_eq_count=p.min_eq_count
        )

        last_close = float(df["close"].iloc[-1])
        atr_last = float(atr[-1]) if len(atr) else 0.0

        result = {
            "pair": self.pair,
            "timeframe": self.timeframe.value,
            "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "candles_analyzed": int(len(df)),
            "first_candle": _iso(df.index[0]),
            "last_candle": _iso(df.index[-1]),
            "last_close": round(last_close, 6),
            "atr": round(atr_last, 6),
            "trend": {"state": state.trend or "range", "since": _iso(state.trend_since)},
            "params": p.to_dict(),
            "events": {
                "structure": _iso_all(structure_events),
                "order_blocks": _iso_all(order_blocks),
                "fair_value_gaps": _iso_all(fvgs),
                "liquidity": {
                    "equal_highs": _iso_all(liquidity["equal_highs"]),
                    "equal_lows": _iso_all(liquidity["equal_lows"]),
                    "sweeps": _iso_all(liquidity["sweeps"]),
                },
            },
            "context": self._build_context(last_close, atr_last, state, order_blocks, fvgs, liquidity),
        }
        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_context(last_close, atr, state, order_blocks, fvgs, liquidity) -> dict:
        """Synthèse 'prête à scorer' : zones actives les plus proches du prix."""
        active_obs = [ob for ob in order_blocks if ob["status"] == "active"]
        active_fvgs = [f for f in fvgs if f["status"] in ("active", "mitigated")]

        def nearest_below(zones):
            below = [z for z in zones if z["zone_top"] < last_close]
            return max(below, key=lambda z: z["zone_top"], default=None)

        def nearest_above(zones):
            above = [z for z in zones if z["zone_bottom"] > last_close]
            return min(above, key=lambda z: z["zone_bottom"], default=None)

        def slim(z):
            if z is None:
                return None
            return {
                "id": z["id"], "direction": z["direction"],
                "zone_top": z["zone_top"], "zone_bottom": z["zone_bottom"],
                "distance_atr": round(abs(last_close - (z["zone_top"] + z["zone_bottom"]) / 2) / atr, 2)
                if atr > 0 else None,
                "touched": z.get("touched_at") is not None,
                "fill_pct": z.get("fill_pct"),
            }

        pools_highs = [p for p in liquidity["equal_highs"] if p["status"] == "untouched"]
        pools_lows = [p for p in liquidity["equal_lows"] if p["status"] == "untouched"]

        return {
            "trend": state.trend or "range",
            "nearest_ob_below": slim(nearest_below(active_obs)),
            "nearest_ob_above": slim(nearest_above(active_obs)),
            "nearest_fvg_below": slim(nearest_below(active_fvgs)),
            "nearest_fvg_above": slim(nearest_above(active_fvgs)),
            "untouched_pools_above": len(pools_highs),
            "untouched_pools_below": len(pools_lows),
        }

    # ------------------------------------------------------------------ #
    @staticmethod
    def to_json(result: dict) -> str:
        """Dict -> chaîne JSON indentée (readable)."""
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    @staticmethod
    def save_json(result: dict, path: str | Path) -> Path:
        """Écrit le JSON sur disque et renvoie le chemin."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SMCEngine.to_json(result), encoding="utf-8")
        return path
