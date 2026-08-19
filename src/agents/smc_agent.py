"""AGENT 2 — Trader SMC expert, analyse multi-timeframe D1 / H4 / M15.

Mandat : « Analyse uniquement le graphique et identifie les zones. »
Méthode top-down (la suite logique des phases 3-4) :

    D1  -> BIAIS. On ne prend jamais un signal contre la tendance journalière.
           Biais = tendance SMC du D1 (machine à états BOS/CHoCH).
    H4  -> ZONES. Order blocks / FVG actifs proches du prix, événements de
           structure récents, pools de liquidité -> le contexte.
    M15 -> DÉCLENCHEUR. Dernier CHoCH/BOS confirmé, âge <= trigger_max_age
           (défaut 6 bougies = 90 min), sweep récent, position dans le range.

Sortie : MultiTFView + les analyses SMC complètes par timeframe (pour le
dashboard et les explications). Les zones « actionnables » sont à distance
<= zone_proximity_atr x ATR(M15) du prix courant, dans le sens du trade.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..analysis.smc import SMCEngine
from ..signals.models import MultiTFView
from ..data.provider import Timeframe

logger = logging.getLogger(__name__)


class SMCAgent:
    """Analyse les 3 timeframes et rend un verdict de confluence."""

    def __init__(
        self,
        trigger_max_age: int = 6,
        zone_proximity_atr: float = 1.5,
        h4_event_max_age: int = 6,
        sweep_max_age: int = 8,
        range_lookback: int = 20,
    ) -> None:
        self.trigger_max_age = int(trigger_max_age)
        self.zone_proximity_atr = float(zone_proximity_atr)
        self.h4_event_max_age = int(h4_event_max_age)
        self.sweep_max_age = int(sweep_max_age)
        self.range_lookback = int(range_lookback)

    # ------------------------------------------------------------------ #
    def analyze(
        self,
        pair: str,
        d1: pd.DataFrame,
        h4: pd.DataFrame,
        m15: pd.DataFrame,
    ) -> tuple[MultiTFView, dict]:
        """Args: DataFrames de bougies CLÔTURÉES (D1, H4, M15).

        Returns: (verdict MultiTFView, analyses SMC complètes par timeframe)
        """
        r_d1 = SMCEngine(pair, Timeframe.D1).analyze(d1)
        r_h4 = SMCEngine(pair, Timeframe.H4).analyze(h4)
        r_m15 = SMCEngine(pair, Timeframe.M15).analyze(m15)

        price = r_m15["last_close"]
        atr = r_m15["atr"] or 1e-9

        # ---- D1 : biais directionnel ------------------------------------
        d1_bias = r_d1["trend"]["state"]  # bullish / bearish / range
        d1_bias = "neutral" if d1_bias == "range" else d1_bias
        d1_events = r_d1["events"]["structure"]
        d1_event = f"{d1_events[-1]['type']} {d1_events[-1]['direction']}" if d1_events else None

        # ---- M15 : déclencheur (dernier événement assez récent) ----------
        m15_events = r_m15["events"]["structure"]
        trigger = trigger_kind = None
        trigger_age = None
        for ev in reversed(m15_events):
            age = len(m15) - 1 - ev["break_index"]
            if age > self.trigger_max_age:
                break
            trigger = ev["direction"]
            trigger_kind = ev["type"]
            trigger_age = age
            break  # le plus récent gagne ; CHoCH privilegie par ordre inverse

        direction = None
        if trigger is not None:
            direction = "LONG" if trigger == "bullish" else "SHORT"
        long_side = direction == "LONG"

        # ---- H4 : soutien + zones actionnables ---------------------------
        h4_events = r_h4["events"]["structure"]
        h4_recent = [
            e for e in reversed(h4_events)
            if len(h4) - 1 - e["break_index"] <= self.h4_event_max_age
        ]
        ob_near = self._nearest_zone(
            [dict(z, timeframe="H4") for z in r_h4["events"]["order_blocks"]]
            + [dict(z, timeframe="M15") for z in r_m15["events"]["order_blocks"]],
            price, atr, direction, active_only=True,
        )
        fvg_near = self._nearest_zone(
            [dict(z, timeframe="H4") for z in r_h4["events"]["fair_value_gaps"]]
            + [dict(z, timeframe="M15") for z in r_m15["events"]["fair_value_gaps"]],
            price, atr, direction, active_only=False,
        )

        if direction is None:
            h4_supports, h4_reason = False, "aucun déclencheur M15 récent"
        else:
            aligned_events = [e for e in h4_recent if e["direction"] == trigger]
            zone_supports = (ob_near is not None and ob_near["direction"] == trigger) or \
                            (fvg_near is not None and fvg_near["direction"] == trigger)
            if aligned_events:
                e = aligned_events[0]
                h4_supports = True
                h4_reason = f"{e['type']} {e['direction']} H4 récent"
            elif zone_supports:
                h4_supports = True
                h4_reason = f"zone {'OB' if ob_near and ob_near['direction']==trigger else 'FVG'} alignée sur H4"
            else:
                h4_supports, h4_reason = False, "aucun soutien H4 récent ni zone alignée"

        # ---- Sweep récent aligné ------------------------------------------
        sweep_recent = None
        for s in reversed(r_m15["events"]["liquidity"]["sweeps"]):
            if len(m15) - 1 - s["index"] > self.sweep_max_age:
                break
            implication = s["implication"]
            if direction is None or (long_side and implication == "bullish") or \
               (not long_side and implication == "bearish"):
                sweep_recent = {
                    "label": f"{s['direction'].replace('_', ' ')} @{s['level']} (M15)",
                    "time": s["time"],
                }
                break

        # ---- Premium / discount : position dans le range H4 ---------------
        window = h4.tail(self.range_lookback)
        range_high = float(window["high"].max())
        range_low = float(window["low"].min())
        span = range_high - range_low
        if span > 0:
            position_pct = (price - range_low) / span * 100.0
            premium_discount = (
                "discount" if position_pct < 45
                else "premium" if position_pct > 55
                else "equilibrium"
            )
        else:
            position_pct, premium_discount = None, None

        view = MultiTFView(
            d1_bias=d1_bias,
            d1_event=d1_event,
            h4_supports=h4_supports,
            h4_reason=h4_reason,
            m15_trigger=trigger,
            m15_trigger_kind=trigger_kind,
            m15_trigger_age=trigger_age,
            current_price=price,
            atr_m15=atr,
            ob_near=ob_near,
            fvg_near=fvg_near,
            sweep_recent=sweep_recent,
            premium_discount=premium_discount,
            pd_position_pct=position_pct,
            direction=direction,
        )
        analyses = {"D1": r_d1, "H4": r_h4, "M15": r_m15}
        return view, analyses

    # ------------------------------------------------------------------ #
    def _nearest_zone(
        self,
        zones: list[dict],
        price: float,
        atr: float,
        direction: str | None,
        active_only: bool,
    ) -> dict | None:
        """Zone la plus proche du prix, du bon côté et du bon sens.

        Pour un LONG : zone SOUS le prix (/support* : OB haussier, FVG haussier).
        Pour un SHORT : zone AU-DESSUS. Distance <= zone_proximity_atr x ATR.
        """
        if direction is None:
            return None
        long_side = direction == "LONG"
        best = None
        best_dist = None
        for z in zones:
            if active_only and z.get("status") == "invalidated":
                continue
            if not active_only and z.get("status") == "filled":
                continue
            if z["direction"] != ("bullish" if long_side else "bearish"):
                continue
            if long_side:
                if z["zone_bottom"] > price:  # zone entièrement au-dessus du prix
                    continue
                dist = max(0.0, price - z["zone_top"])  # 0 si le prix est DANS la zone
            else:
                if z["zone_top"] < price:
                    continue
                dist = max(0.0, z["zone_bottom"] - price)
            if dist > self.zone_proximity_atr * atr:
                continue
            if best_dist is None or dist < best_dist:
                best, best_dist = dict(z), dist
        if best is not None:
            best["distance_atr"] = round(best_dist / atr, 2)
        return best
