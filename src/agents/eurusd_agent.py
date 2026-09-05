"""AGENT STRATÉGIE EUR/USD — spécialiste exclusif (règle n°1 absolue).

Pipeline OBLIGATOIRE (5 portes, tout doit s'aligner) :

    PORTE 1  D1   tendance de fond   : EMA200 D1 + structure SMC D1
    PORTE 2  H4   tendance principale: EMA50/EMA200 H4 + zone H4 (OB/FVG/structure)
    PORTE 3  H1   direction autorisée: EMA50 H1 + structure H1 — DOIT = H4 sinon STOP
    PORTE 4  M15  zone de décision   : cassure récente (BOS/CHoCH) + RETEST en zone
    PORTE 5  M5/M30 timing d'entrée  : bougie de confirmation engulfing / pin bar

Tout échec de porte = AUCUN SIGNAL (bloqueur explicite, journalisé).

Confiance /100 (seuil configurable, défaut 65) :
    50 base (les 5 portes passées)
    +10 retest DANS une zone (OB/FVG H4 ou M15)
    +10 confirmation sur M5 ET M30
    +10 DXY aligné (dollar faible pour un BUY, fort pour un SELL)
    +10 fondamental aligné (surprises EUR/USD du jour)
    +5  session Londres/NY ; +5 RSI avec de la marge

Risque affiché : ÉLEVE si news HIGH < 6 h ou |DXY| > 0,6 % ; FAIBLE si
calme ; MOYEN sinon. Les news HIGH à moins de 2 h BLOQUENT (agent risque).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..analysis.indicators import confirmation_candle, ema, rsi
from ..analysis.smc import SMCEngine
from ..data.provider import Timeframe

logger = logging.getLogger(__name__)

GATE_TIMEFRAMES = (Timeframe.D1, Timeframe.H4, Timeframe.H1, Timeframe.M30, Timeframe.M15, Timeframe.M5)


@dataclass
class StrategyView:
    """Verdict complet de la stratégie (utilisé par le moteur et le message)."""

    direction: str | None          # "LONG" / "SHORT" / None
    blockers: list[str] = field(default_factory=list)
    gates: dict[str, str] = field(default_factory=dict)     # libellés par porte
    score: int = 0
    breakdown: dict[str, int] = field(default_factory=dict)
    confluences: list[str] = field(default_factory=list)
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    rr: float = 3.0
    risk_label: str = "MOYEN"
    dxy_txt: str = ""
    news_txt: str = ""
    macro_bias: str = ""


class EURUSDAgent:
    """Le robot EUR/USD unique : analyse, décide, chiffre."""

    def __init__(self, trigger_max_age_m15: int = 12, retest_atr: float = 0.8,
                 min_risk_atr: float = 0.5, rr: float = 3.0) -> None:
        self.trigger_max_age = int(trigger_max_age_m15)
        self.retest_atr = float(retest_atr)
        self.min_risk_atr = float(min_risk_atr)
        self.rr = float(rr)

    # ------------------------------------------------------------------ #
    def assess(
        self,
        frames: dict[Timeframe, pd.DataFrame],
        dxy,                     # DxyInfo | None
        fundamental,             # FundamentalView (Agent 1)
        high_news_hours_ahead: float | None,   # heures avant la prochaine news HIGH EUR/USD
        now: pd.Timestamp,
    ) -> StrategyView:
        view = StrategyView(direction=None)
        d1, h4, h1 = frames.get(Timeframe.D1), frames.get(Timeframe.H4), frames.get(Timeframe.H1)
        m30, m15, m5 = frames.get(Timeframe.M30), frames.get(Timeframe.M15), frames.get(Timeframe.M5)
        minimums = {"D1": 210, "H4": 210, "H1": 60, "M30": 60, "M15": 60, "M5": 60}
        for name, df in (("D1", d1), ("H4", h4), ("H1", h1), ("M30", m30), ("M15", m15), ("M5", m5)):
            if df is None or df.empty or len(df) < minimums[name]:
                view.blockers.append(
                    f"données {name} insuffisantes ({0 if df is None else len(df)}/{minimums[name]})"
                )
                return view

        price = float(m15["close"].iloc[-1])
        atr = float(SMCEngine("EURUSD", "15m").analyze(m15)["atr"] or 1e-4)

        # ---------------- PORTE 1 : D1 (EMA200 + structure) ----------------
        ema200_d1 = float(ema(d1["close"], 200).iloc[-1])
        smc_d1 = SMCEngine("EURUSD", "1d").analyze(d1)
        struct_d1 = smc_d1["trend"]["state"]
        above = price > ema200_d1
        if above and struct_d1 != "bearish":
            d1_dir, d1_txt = "bullish", f"Haussier > EMA200 ({ema200_d1:.5f})"
        elif not above and struct_d1 != "bullish":
            d1_dir, d1_txt = "bearish", f"Bearish < EMA200 ({ema200_d1:.5f})"
        else:
            d1_dir, d1_txt = None, f"mixte (EMA200 {ema200_d1:.5f}, structure {struct_d1})"
        view.gates["D1"] = d1_txt
        if d1_dir is None:
            view.blockers.append(f"D1 non directionnel ({d1_txt})")
            return view

        # ---------------- PORTE 2 : H4 (EMA50/200 + zone) -------------------
        e50 = ema(h4["close"], 50); e200 = ema(h4["close"], 200)
        c4 = float(h4["close"].iloc[-1])
        if c4 > float(e50.iloc[-1]) and float(e50.iloc[-1]) > float(e200.iloc[-1]):
            h4_dir = "bullish"
        elif c4 < float(e50.iloc[-1]) and float(e50.iloc[-1]) < float(e200.iloc[-1]):
            h4_dir = "bearish"
        else:
            h4_dir = None
        smc_h4 = SMCEngine("EURUSD", "4h").analyze(h4)
        zone = self._nearest_zone(smc_h4, price, d1_dir)
        zone_txt = (f"zone {zone['id']} [{zone['zone_bottom']:.5f}, {zone['zone_top']:.5f}]"
                    if zone else "aucune zone proche")
        ema_txt = "EMA50>EMA200" if h4_dir == "bullish" else "EMA50<EMA200" if h4_dir == "bearish" else "EMA mixtes"
        view.gates["H4"] = f"{ema_txt} + {zone_txt}"
        if h4_dir is None:
            view.blockers.append(f"H4 non directionnel ({ema_txt})")
            return view
        if h4_dir != d1_dir:
            view.blockers.append(f"H4 ({h4_dir}) contre D1 ({d1_dir})")
            return view

        # ---------------- PORTE 3 : H1 (direction autorisée) ---------------
        ema50_h1 = float(ema(h1["close"], 50).iloc[-1])
        smc_h1 = SMCEngine("EURUSD", "1h").analyze(h1)
        last_h1 = smc_h1["events"]["structure"][-1] if smc_h1["events"]["structure"] else None
        h1_ema_dir = "bullish" if float(h1["close"].iloc[-1]) > ema50_h1 else "bearish"
        h1_struct_dir = last_h1["direction"] if last_h1 else None
        if h1_ema_dir == h4_dir and (h1_struct_dir in (None, h4_dir)):
            h1_dir = h4_dir
            view.gates["H1"] = f"alignée ({h1_dir}, EMA50 {ema50_h1:.5f})"
        else:
            view.blockers.append(
                f"H1 ({h1_ema_dir}, struct {h1_struct_dir}) non alignée avec H4 ({h4_dir})"
            )
            return view

        # ---------------- PORTE 4 : M15 (cassure + retest) ------------------
        smc_m15 = SMCEngine("EURUSD", "15m").analyze(m15)
        events = smc_m15["events"]["structure"]
        trigger, trig_age = None, None
        for ev in reversed(events):
            age = len(m15) - 1 - ev["break_index"]
            if age > self.trigger_max_age:
                break
            if ev["direction"] == h4_dir:
                trigger, trig_age = ev, age
                break
        if trigger is None:
            view.blockers.append(f"aucune cassure M15 alignée dans les {self.trigger_max_age} dernières bougies")
            return view
        # retest : proximité du niveau cassé ou présence dans une zone alignée
        m15_zone = self._nearest_zone(smc_m15, price, h4_dir) or zone
        dist_level = abs(price - trigger["swing_level"])
        in_zone = m15_zone is not None and m15_zone["zone_bottom"] <= price <= m15_zone["zone_top"]
        near = dist_level <= self.retest_atr * atr or in_zone
        if not near:
            view.blockers.append(
                f"pas de retest : prix à {dist_level / atr:.1f} ATR de la cassure, hors zone"
            )
            return view
        rsi_m15 = float(rsi(m15["close"]).iloc[-1])
        view.gates["M15"] = (f"{trigger['type']} {h4_dir} + retest "
                             f"{'en zone' if in_zone else 'du niveau'}, RSI {rsi_m15:.0f}")

        # ---------------- PORTE 5 : M5 / M30 (confirmation) -----------------
        conf5 = confirmation_candle(m5, h4_dir, lookback=6)
        conf30 = confirmation_candle(m30, h4_dir, lookback=3)
        if conf5 is None and conf30 is None:
            view.blockers.append("aucune bougie de confirmation M5/M30 (engulfing / pin bar)")
            return view
        parts = []
        if conf30:
            parts.append(f"M30 : {conf30['kind']}")
        if conf5:
            parts.append(f"M5 : {conf5['kind']}")
        view.gates["M5/M30"] = " + ".join(parts)

        # ---------------- Direction + plan ----------------------------------
        direction = "LONG" if h4_dir == "bullish" else "SHORT"
        view.direction = direction
        long_side = direction == "LONG"

        z = m15_zone
        if z is not None:
            entry = float(price) if z["zone_bottom"] <= price <= z["zone_top"] else (
                float(z["zone_top"]) if long_side else float(z["zone_bottom"]))
            base = float(z["zone_bottom"]) if long_side else float(z["zone_top"])
        else:
            entry, base = price, price
        swing = float(m15["low"].tail(10).min()) if long_side else float(m15["high"].tail(10).max())
        base = min(base, swing) if long_side else max(base, swing)   # stop structure large
        sl = base - 0.1 * atr if long_side else base + 0.1 * atr
        risk = (entry - sl) if long_side else (sl - entry)
        if risk < self.min_risk_atr * atr:                            # plancher anti-bruit
            sl = entry - self.min_risk_atr * atr if long_side else entry + self.min_risk_atr * atr
            risk = self.min_risk_atr * atr
        tp = entry + self.rr * risk if long_side else entry - self.rr * risk
        view.entry, view.sl, view.tp = entry, sl, tp

        # ---------------- Confiance /100 -------------------------------------
        bd = {"alignement_d1_h4_h1": 50}
        confs = [f"Alignement D1/H4/H1 {h4_dir}", f"{trigger['type']} M15 + retest"]
        if in_zone and z is not None:
            bd["retest_en_zone"] = 10
            confs.append(f"Retest dans la zone {z['id']}")
        if conf5 and conf30:
            bd["confirmation_m5_m30"] = 10
        elif conf5 or conf30:
            bd["confirmation_partielle"] = 5
        confs.append(" / ".join(parts))
        if dxy is not None:
            dollar_weak = dxy.change_pct <= -0.10
            dollar_strong = dxy.change_pct >= 0.10
            if (long_side and dollar_weak) or (not long_side and dollar_strong):
                bd["dxy_aligne"] = 10
                confs.append(f"DXY aligné ({dxy})")
            view.dxy_txt = str(dxy)
        if fundamental.supports_direction:
            bd["fondamental_aligne"] = 10
            confs.append("Fondamental aligné (surprises macro du jour)")
        view.macro_bias = f"biais {fundamental.bias} ({fundamental.score:+.1f})"
        hour = now.hour
        if 7 <= hour < 17:
            bd["session"] = 5
        rsi_room = (long_side and rsi_m15 < 68) or (not long_side and rsi_m15 > 32)
        if rsi_room:
            bd["marge_rsi"] = 5
        view.score = sum(bd.values())
        view.breakdown = bd
        view.confluences = confs

        # ---------------- Risque affiché + contexte news ----------------------
        if (high_news_hours_ahead is not None and high_news_hours_ahead < 6) or \
                (dxy is not None and abs(dxy.change_pct) > 0.6):
            view.risk_label = "ELEVE"
        elif high_news_hours_ahead is None and (dxy is None or abs(dxy.change_pct) < 0.3):
            view.risk_label = "FAIBLE"
        else:
            view.risk_label = "MOYEN"
        view.news_txt = (f"news HIGH EUR/USD dans ~{high_news_hours_ahead:.0f} h"
                         if high_news_hours_ahead is not None
                         else "aucune news HIGH EUR/USD < 24 h")
        return view

    # ------------------------------------------------------------------ #
    @staticmethod
    def _nearest_zone(smc_result: dict, price: float, direction: str) -> dict | None:
        """Zone OB/FVG alignée la plus proche (sous le prix en LONG, above en SHORT)."""
        long_side = direction == "bullish"
        best, best_d = None, None
        zones = ([dict(z, src="OB") for z in smc_result["events"]["order_blocks"]
                  if z["status"] == "active"] +
                 [dict(z, src="FVG") for z in smc_result["events"]["fair_value_gaps"]
                  if z["status"] in ("active", "mitigated")])
        for z in zones:
            if z["direction"] != direction:
                continue
            if long_side and z["zone_bottom"] > price + 1e-9:
                continue
            if not long_side and z["zone_top"] < price - 1e-9:
                continue
            mid = (z["zone_top"] + z["zone_bottom"]) / 2
            d = abs(price - mid)
            if best_d is None or d < best_d:
                best, best_d = dict(z), d
        return best
