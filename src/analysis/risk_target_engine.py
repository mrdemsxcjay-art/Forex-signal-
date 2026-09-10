"""Dynamic Risk/Target Engine — PHASE 8 (module SHADOW : non branché au moteur live).

Responsabilités (spec §20-§22) :

    STOP LOSS STRUCTUREL (§20) — le SL a une RAISON :
        ancre = le point LE PLUS PROTECTEUR parmi
            - le bord opposé de la zone (invalidation de la zone)
            - l'extrême du sweep récent (la mèche qui a pris la liquidité)
            - le swing structurel M15 récent
        SL = ancre ± buffer (0.15 ATR par défaut)
        Bornes de sanité : trop serré (< 0.3 ATR = bruit) ou trop large
        (> 4 ATR = risque démesuré) -> plan invalide.

    TAKE PROFITS DYNAMIQUES (§21-§22) — le marché décide :
        TP1 = première liquidité / structure opposée atteignable
        TP2 = objectif structurel principal :
                RANGE  -> bornage opposé du range
                TREND  -> prochain swing / deuxième liquidité
                BREAKOUT -> liquidité suivante
        TP3 = extension / liquidité HTF (PWH/PWL ou troisième candidat)
        Aucun TP n'est inventé : si le candidat n'existe pas, le champ
        reste None.

    RR DISPONIBLE AVANT L'ENTRÉE (§21) :
        rr1 = (TP1 - entrée) / risque   -> si rr1 < min_rr1, le plan est
        INVALIDE (BAD_RR) : on ne fabrique pas un TP plus loin pour
        satisfaire un ratio. "Si le marché offre 1.5R, on prend 1.5R."

Fonction pure : entry/sl/tp dérivés uniquement des bougies clôturées et du
contexte des phases 2-7.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .candles import compute_atr
from .liquidity_engine import LiquidityInfo
from .regime import (
    REGIME_RANGE,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    RegimeInfo,
)
from .smc_engine import SMCZone

INVALID_TIGHT = "SL_TROP_SERRE"
INVALID_WIDE = "SL_TROP_LARGE"
INVALID_RR = "BAD_RR"
INVALID_NO_TARGET = "NO_TARGET"


@dataclass
class TradePlan:
    direction: str                      # LONG / SHORT
    entry: float
    sl: float
    tp1: float | None = None
    tp2: float | None = None
    tp3: float | None = None
    rr1: float | None = None            # RR disponible vers TP1 (décide la validité)
    rr2: float | None = None
    rr3: float | None = None
    sl_reason: str = ""                 # raison structurelle §20
    tp_reasons: list[str] = field(default_factory=list)   # une raison par TP
    valid: bool = True
    invalid_reason: str = ""
    invalid_code: str = ""              # BAD_RR / SL_TROP_SERRE / ...

    def describe(self) -> str:
        if not self.valid:
            return f"PLAN INVALIDE [{self.invalid_code}] : {self.invalid_reason}"
        tps = " / ".join(
            f"TP{i} {tp:.5f} (rr{rr:.1f})"
            for i, (tp, rr) in enumerate(
                [(self.tp1, self.rr1), (self.tp2, self.rr2), (self.tp3, self.rr3)], 1)
            if tp is not None)
        return (f"{self.direction} entrée {self.entry:.5f} · SL {self.sl:.5f} · "
                f"{tps} — SL : {self.sl_reason}")


class RiskTargetEngine:
    """Construit le plan de trade structurel (SL motivé, TP contextuels)."""

    def __init__(
        self,
        sl_buffer_atr: float = 0.15,
        min_sl_atr: float = 0.3,
        max_sl_atr: float = 4.0,
        min_rr1: float = 1.5,           # RR minimal vers TP1 (tendances)
        min_rr1_range: float = 1.2,     # RR minimal en RANGE (aller-retour
                                        #  extrême -> milieu plus court : TP
                                        #  différenciés selon l'opportunité §21)
        tp_min_gap_atr: float = 0.2,    # écart minimal entre TP successifs
        atr: float | None = None,       # ATR injectable (tests déterministes)
    ) -> None:
        self.sl_buffer_atr = float(sl_buffer_atr)
        self.min_sl_atr = float(min_sl_atr)
        self.max_sl_atr = float(max_sl_atr)
        self.min_rr1 = float(min_rr1)
        self.min_rr1_range = float(min_rr1_range)
        self.tp_min_gap_atr = float(tp_min_gap_atr)
        self._atr = atr

    # ------------------------------------------------------------------ #
    def build_plan(
        self,
        direction: str,
        zone: SMCZone,
        price: float,
        regime: RegimeInfo,
        liq: LiquidityInfo,
        m15: pd.DataFrame,
        h4: pd.DataFrame,
    ) -> TradePlan:
        long_side = direction == "LONG"
        atr = self._atr or float(compute_atr(h4, 14)[-1]) or 1e-9

        # --- Entrée : retrait au bord de zone, ou prix si déjà dedans ------
        if long_side:
            entry = min(price, zone.zone_top)
        else:
            entry = max(price, zone.zone_bottom)

        # --- SL structurel : l'ancre la plus protectrice (§20) -------------
        anchors: list[tuple[float, str]] = [(zone.zone_bottom if long_side
                                             else zone.zone_top,
                                             f"bord opposé de la zone {zone.id}")]
        for s in liq.recent_sweeps:
            if long_side and s.direction == "swept_lows":
                extreme = s.level_price - s.wick_atr * atr
                anchors.append((extreme,
                                f"extrême du sweep {s.kind} {s.level_price:.5f} "
                                f"(mèche {s.wick_atr:.1f} ATR)"))
            if not long_side and s.direction == "swept_highs":
                extreme = s.level_price + s.wick_atr * atr
                anchors.append((extreme,
                                f"extrême du sweep {s.kind} {s.level_price:.5f} "
                                f"(mèche {s.wick_atr:.1f} ATR)"))
        if m15 is not None and len(m15) >= 10:
            if long_side:
                swing = float(m15["low"].tail(10).min())
                anchors.append((swing, "swing low M15 récent"))
            else:
                swing = float(m15["high"].tail(10).max())
                anchors.append((swing, "swing high M15 récent"))

        if long_side:
            anchor, reason = min(anchors, key=lambda a: a[0])
            sl = anchor - self.sl_buffer_atr * atr
            risk = entry - sl
        else:
            anchor, reason = max(anchors, key=lambda a: a[0])
            sl = anchor + self.sl_buffer_atr * atr
            risk = sl - entry

        plan = TradePlan(direction=direction, entry=round(entry, 6),
                         sl=round(sl, 6),
                         sl_reason=f"{reason} ± {self.sl_buffer_atr} ATR")

        # --- Sanité du stop --------------------------------------------------
        if risk < self.min_sl_atr * atr:
            plan.valid = False
            plan.invalid_code = INVALID_TIGHT
            plan.invalid_reason = (f"stop à {risk / atr:.2f} ATR seulement "
                                   "(bruit : zone trop proche du prix)")
            return plan
        if risk > self.max_sl_atr * atr:
            plan.valid = False
            plan.invalid_code = INVALID_WIDE
            plan.invalid_reason = (f"stop structurel à {risk / atr:.1f} ATR "
                                   "(risque démesuré pour un seul trade)")
            return plan

        # --- Candidats objectifs (uniquement des niveaux RÉELS) --------------
        gap = self.tp_min_gap_atr * atr
        candidates: list[tuple[float, str]] = []
        for lv in liq.levels:
            if lv.status != "untouched":
                continue
            if long_side and lv.price > entry + gap:
                candidates.append((lv.price, f"liquidité {lv.kind} {lv.price:.5f}"))
            if not long_side and lv.price < entry - gap:
                candidates.append((lv.price, f"liquidité {lv.kind} {lv.price:.5f}"))
        for kind in ("PWH", "PWL"):
            lv = liq.key_levels.get(kind)
            if lv is None:
                continue
            if long_side and kind == "PWH" and lv.price > entry + gap:
                candidates.append((lv.price, f"{kind} {lv.price:.5f} (liquidité HTF)"))
            if not long_side and kind == "PWL" and lv.price < entry - gap:
                candidates.append((lv.price, f"{kind} {lv.price:.5f} (liquidité HTF)"))
        if regime.regime == REGIME_RANGE and regime.range_high is not None:
            if long_side and regime.range_high > entry + gap:
                candidates.append((regime.range_high,
                                   "bornage opposé du range (objectif range)"))
            if not long_side and regime.range_low < entry - gap:
                candidates.append((regime.range_low,
                                   "bornage opposé du range (objectif range)"))
        if m15 is not None and len(m15) >= 10:
            if long_side:
                hi = float(m15["high"].tail(10).max())
                if hi > entry + gap:
                    candidates.append((hi, "swing high M15 récent"))
            else:
                lo = float(m15["low"].tail(10).min())
                if lo < entry - gap:
                    candidates.append((lo, "swing low M15 récent"))

        # --- Majeurs vs mineurs (§22) ------------------------------------------
        # Mineur  = swing M15 (simple premier obstacle -> TP1 seulement)
        # Majeur  = liquidité, bornage de range, PWH/PWL (objectifs principaux)
        def dedup(items):
            seen: list[float] = []
            out = []
            for p, r in sorted(items, key=lambda c: c[0], reverse=not long_side):
                if any(abs(p - s) < gap for s in seen):
                    continue
                seen.append(p)
                out.append((p, r))
            return out

        majors = dedup([c for c in candidates if not c[1].startswith("swing")])
        minors = dedup([c for c in candidates if c[1].startswith("swing")])
        all_c = dedup(candidates)
        if not all_c:
            plan.valid = False
            plan.invalid_code = INVALID_NO_TARGET
            plan.invalid_reason = "aucune structure opposée atteignable identifiée"
            return plan

        # --- TP1 : première structure opposée qui PAIE le risque (§21) --------
        # Un pro ne vise pas l'obstacle trivial le plus proche : TP1 = le
        # premier niveau dont le RR disponible >= minimum du régime. Les
        # candidats trop proches sont sautés (aucun TP inventé pour autant).
        min_rr = self.min_rr1_range if regime.regime == REGIME_RANGE else self.min_rr1

        def rr_to(p: float) -> float:
            return (p - entry) / risk if long_side else (entry - p) / risk

        eligible = [c for c in all_c if rr_to(c[0]) >= min_rr]
        if not eligible:
            plan.valid = False
            plan.invalid_code = INVALID_RR
            best = max((rr_to(c[0]) for c in all_c), default=0.0)
            plan.invalid_reason = (
                f"meilleure structure opposée à {best:.1f}R < {min_rr:.1f}R "
                "minimum — le marché n'offre pas assez d'espace, on n'invente "
                "pas un TP plus loin")
            return plan
        tp1, r1 = eligible[0]
        plan.tp1, plan.tp_reasons = round(tp1, 6), [r1]

        # --- TP2 : objectif structurel PRINCIPAL (majeurs seulement) ------------
        tp2_candidate = None
        if regime.regime == REGIME_RANGE:
            bound = regime.range_high if long_side else regime.range_low
            if bound is not None and (long_side and bound > tp1 + gap
                                      or not long_side and bound < tp1 - gap):
                tp2_candidate = (bound, "bornage opposé du range (objectif principal)")
        if tp2_candidate is None:
            beyond = [c for c in majors
                      if (long_side and c[0] > tp1 + gap)
                      or (not long_side and c[0] < tp1 - gap)]
            if beyond:
                tp2_candidate = beyond[0]
        if tp2_candidate is not None:
            plan.tp2 = round(tp2_candidate[0], 6)
            plan.tp_reasons.append(tp2_candidate[1])

        # --- TP3 : extension / liquidité HTF (majeur suivant) --------------------
        if plan.tp2 is not None:
            beyond2 = [c for c in majors
                       if (long_side and c[0] > plan.tp2 + gap)
                       or (not long_side and c[0] < plan.tp2 - gap)]
            if beyond2:
                plan.tp3 = round(beyond2[0][0], 6)
                plan.tp_reasons.append(beyond2[0][1])

        # --- RR disponible AVANT l'entrée (§21) ---------------------------------
        def rr(tp: float | None) -> float | None:
            if tp is None:
                return None
            return round((tp - entry) / risk if long_side else (entry - tp) / risk, 2)

        plan.rr1, plan.rr2, plan.rr3 = rr(plan.tp1), rr(plan.tp2), rr(plan.tp3)

        if plan.rr1 is None or plan.rr1 < min_rr - 1e-9:
            plan.valid = False
            plan.invalid_code = INVALID_RR
            plan.invalid_reason = (f"RR disponible vers la première structure : "
                                   f"{plan.rr1 if plan.rr1 is not None else 0:.1f}R "
                                   f"< {min_rr:.1f}R minimum")
        return plan
