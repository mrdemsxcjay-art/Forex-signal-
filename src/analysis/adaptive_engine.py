"""Adaptive Signal Engine — PHASE 10 (SHADOW : assemblage final, non branché au live).

Le nouveau cerveau décisionnel : les 8 phases assemblées en un pipeline de
HARD GATES hiérarchiques (spec §4, §19), avec un score qui NE SERT QU'A
CLASSER les setups déjà valides (§18 : score 95 + MID_RANGE = NO_TRADE).

    GATE 1  DATA            fraîcheur et suffissance de chaque timeframe
    GATE 2  REGIME          (P2) régime tradeable
    GATE 3  LOCATION        (P7) pas de milieu de range, zone à portée
    GATE 4  STRUCTURE       (P3) direction autorisée par le HTF
    GATE 5  SCENARIO        (P7) scénario déclenché (A/B/T/K)
    GATE 6  EXTENDED_MOVE   (P7) amplitude du jour pas déjà consommée
    GATE 7  SMC CONTEXT     (P6) zone + liquidité + confirmation (gate §13)
    GATE 8  PLAN            (P8) SL structurel valide + RR disponible >= min
    GATE 9  NEWS            news HIGH < N heures -> blocage (injecté, testé)
    GATE 10 ANTI-OVERTRADING(P9) dedup / cooldown / plafonds
    ---- à ce point le setup est VALIDE ----
    SCORE   classement 0-100 (§19) + grade A+/A/B, seuil de qualité min.

Réparations d'audit intégrées :
    F-02 (bonus mort)  : plus aucun bonus caché — la gate news est une
                         fonction INJECTÉE et testée (jamais une constante
                         faussement branchée) ;
    F-03 (config ignorée) : EngineConfig = source unique ; chaque sous-moteur
                         est CONSTRUIT depuis la config (test anti-dérive :
                         changer un paramètre change le comportement).

Sortie : EngineDecision — payload complet §32 (ASSET, DIRECTION, REGIME,
ENTRY, SL, TP1-3, RR, SETUP, STRUCTURE, LIQUIDITY, CONFIRMATION, WHY NOW,
INVALIDATION).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .anti_overtrading_engine import (
    AntiOvertradingEngine,
    Candidate,
    ContextEvents,
    SignalRecord,
)
from .candles import compute_atr
from .liquidity_engine import MarketLiquidityEngine
from .price_action_engine import PriceActionEngine
from .regime import MarketRegimeEngine, RegimeInfo
from .risk_target_engine import RiskTargetEngine, TradePlan
from .scenario_engine import ScenarioEngine
from .smc_engine import SMCIntegrationEngine, SMCZone
from .structure_engine import MarketStructureEngine, StructureVerdict

# ---- exigences de données par timeframe ------------------------------------- #
DATA_MIN_BARS = {"D1": 210, "H4": 210, "M15": 60, "M30": 40, "M5": 40, "H1": 50}
DATA_FRESH_BARS = 4          # age max = 4 périodes de la bougie fermée

STAGE_DATA = "DATA"
STAGE_REGIME = "REGIME"
STAGE_LOCATION = "LOCATION"
STAGE_STRUCTURE = "STRUCTURE"
STAGE_SCENARIO = "SCENARIO"
STAGE_PLAN = "PLAN"
STAGE_NEWS = "NEWS"
STAGE_ANTI_OVERTRADING = "ANTI_OVERTRADING"
STAGE_QUALITY = "QUALITY"


@dataclass
class EngineConfig:
    """Source unique de vérité : chaque sous-moteur est construit depuis elle."""

    # P2 régime
    regime: dict = field(default_factory=lambda: dict(
        swing_k=4, trend_majority=0.6, range_boundary_drift_atr=0.5))
    # P3 structure
    structure: dict = field(default_factory=lambda: dict(swing_k=4))
    # P4 liquidité
    liquidity: dict = field(default_factory=lambda: dict(swing_k=4))
    # P5 price action
    price_action: dict = field(default_factory=lambda: dict(swing_k=4))
    # P6 SMC
    smc: dict = field(default_factory=lambda: dict(swing_k=4))
    # P7 scénarios / localisation
    scenario: dict = field(default_factory=lambda: dict(
        zone_reach_atr=1.5, extended_move_ratio=1.5,
        require_confirmation=True))
    # P8 plan
    risk: dict = field(default_factory=lambda: dict(
        sl_buffer_atr=0.15, min_sl_atr=0.3, max_sl_atr=4.0, min_rr1=1.5))
    # P9 anti-overtrading
    anti_overtrading: dict = field(default_factory=lambda: dict(
        cooldown_after_sl_min=240, cooldown_after_tp_min=120,
        cooldown_neutral_min=180, max_per_day=3, max_same_zone_per_day=2,
        max_post_sl_per_day=2, max_consecutive_same_direction=3))
    # gates transverses
    news_block_hours: float = 2.0     # 0 = gate désactivée
    min_score: int = 65               # seuil de QUALITÉ (classement, pas validité)
    grades: tuple = ((85, "A+"), (75, "A"), (65, "B"))


@dataclass
class EngineDecision:
    """Décision complète du pipeline (SIGNAL ou NO_TRADE motivé)."""

    decision: str                      # "SIGNAL" / "NO_TRADE"
    stage: str                         # gate émettrice
    code: str                          # code §33 / OK_SETUP / ""
    detail: str
    # ---- payload §32 (rempli si SIGNAL) ----
    direction: str | None = None
    regime: str | None = None
    entry: float | None = None
    sl: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    tp3: float | None = None
    rr1: float | None = None
    rr2: float | None = None
    rr3: float | None = None
    setup: str = ""                    # thèse unique (§17)
    market_structure: str = ""
    liquidity_narrative: str = ""
    confirmation: str = ""
    why_now: str = ""
    invalidation: str = ""
    score: int | None = None
    grade: str | None = None
    zone_id: str | None = None
    scenario_name: str | None = None


class AdaptiveSignalEngine:
    """Le pipeline décisionnel complet — fonction pure de ses entrées."""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        c = self.config
        # F-03 : chaque sous-moteur est CONSTRUIT depuis la config — jamais
        # de valeur cachée dans un constructeur interne.
        self.regime_engine = MarketRegimeEngine(**c.regime)
        self.structure_engine = MarketStructureEngine(**c.structure)
        self.liquidity_engine = MarketLiquidityEngine(**c.liquidity)
        self.pa_engine = PriceActionEngine(**c.price_action)
        self.smc_engine = SMCIntegrationEngine(**c.smc)
        self.scenario_engine = ScenarioEngine(**c.scenario)
        self.risk_engine = RiskTargetEngine(**c.risk)
        self.anti_ot = AntiOvertradingEngine(**c.anti_overtrading)

    # ------------------------------------------------------------------ #
    def analyze(
        self,
        frames: dict[str, pd.DataFrame],
        now: pd.Timestamp,
        history: list[SignalRecord] | None = None,
        context_events: ContextEvents | None = None,
        news_hours_fn: Callable[[], float | None] | None = None,
    ) -> EngineDecision:
        """Exécute les gates dans l'ordre §19. `frames` : {"D1": df, "H4": df,
        "M15": df, ...} bougies clôturées. `news_hours_fn` : heures avant la
        prochaine news HIGH (None = pas de donnée → on ne bloque pas)."""

        def no(stage: str, code: str, detail: str) -> EngineDecision:
            return EngineDecision("NO_TRADE", stage, code, detail)

        # ---------- GATE 1 : DATA ------------------------------------------- #
        for tf in ("D1", "H4", "M15"):
            df = frames.get(tf)
            if df is None or len(df) < DATA_MIN_BARS.get(tf, 60):
                return no(STAGE_DATA, "DATA_INSUFFICIENT",
                          f"{tf} : {0 if df is None else len(df)} bougies "
                          f"< {DATA_MIN_BARS.get(tf, 60)} requises")
        for tf, df in frames.items():
            if df is None or df.empty:
                continue
            period = df.index[-1] - df.index[-2] if len(df) > 1 else pd.Timedelta(0)
            age = now - df.index[-1]
            if age > DATA_FRESH_BARS * period:
                return no(STAGE_DATA, "DATA_STALE",
                          f"{tf} : dernière bougie il y a "
                          f"{age.total_seconds() / 60:.0f} min "
                          f"(max {DATA_FRESH_BARS} périodes)")

        h4, d1, m15 = frames["H4"], frames["D1"], frames["M15"]
        price = float(m15["close"].iloc[-1])

        # ---------- GATE 2 : RÉGIME (P2) ------------------------------------ #
        regime = self.regime_engine.detect(h4)
        if regime.regime in ("INSUFFICIENT_DATA", "CHAOTIC", "TRANSITION",
                             "BREAKOUT_RETEST"):
            return no(STAGE_REGIME, "REGIME_UNCERTAIN",
                      f"régime {regime.regime} : {regime.detail}")

        # ---------- contexte complet (P3/P4/P5/P6) --------------------------- #
        summaries = {"D1": self.structure_engine.summarize(d1, "D1"),
                     "H4": self.structure_engine.summarize(h4, "H4"),
                     "M15": self.structure_engine.summarize(m15, "M15")}
        for tf in ("M30", "M5", "H1"):
            if frames.get(tf) is not None and len(frames[tf]) >= DATA_MIN_BARS.get(tf, 40):
                summaries[tf] = self.structure_engine.summarize(frames[tf], tf)
        verdict = MarketStructureEngine.combine(summaries)

        liq = self.liquidity_engine.analyze(h4, d1)
        untouched = [l.price for l in liq.levels if l.status == "untouched"][:6]
        pa = self.pa_engine.analyze(m15, levels=untouched)
        zones = self.smc_engine.zones(h4, "H4")

        # ---------- GATES 3-7 : localisation -> scénario -> SMC (P7) --------- #
        pre = self.scenario_engine.decide(price, regime, verdict, liq, pa,
                                          zones, d1, h4, self.smc_engine)
        if pre.decision == "NO_TRADE":
            stage = (STAGE_LOCATION if pre.reason_code in ("MID_RANGE", "NO_ZONE")
                     else STAGE_STRUCTURE if pre.reason_code == "HTF_CONFLICT"
                     else STAGE_SCENARIO)
            return no(stage, pre.reason_code, pre.detail)
        zone = pre.zone
        scenario = pre.scenario

        # ---------- GATE 8 : PLAN structurel (P8) ----------------------------- #
        plan: TradePlan = self.risk_engine.build_plan(
            pre.direction, zone, price, regime, liq, m15, h4)
        if not plan.valid:
            return no(STAGE_PLAN, plan.invalid_code, plan.invalid_reason)

        # ---------- GATE 9 : NEWS (injectée — réparation F-02) ----------------- #
        if news_hours_fn is not None and self.config.news_block_hours > 0:
            hours = news_hours_fn()
            if hours is not None and hours < self.config.news_block_hours:
                return no(STAGE_NEWS, "NEWS_BLOCK",
                          f"news HIGH dans {hours:.1f} h "
                          f"(< {self.config.news_block_hours:.0f} h) : signal bloqué")

        # ---------- GATE 10 : ANTI-OVERTRADING (P9) ---------------------------- #
        candidate = Candidate(zone.id, pre.direction, scenario.name)
        ot = self.anti_ot.check(candidate, history or [], now, context_events)
        if not ot.allowed:
            return no(STAGE_ANTI_OVERTRADING, ot.code, ot.detail)

        # ---------- SCORE : classement des setups DÉJÀ valides (§19) ----------- #
        ev = self.smc_engine.evaluate_zone(zone, price, regime, verdict, liq, pa)
        conf = pa.confirmation("bullish" if pre.direction == "LONG" else "bearish")
        score = self._score(ev.quality, conf.strength if conf else 0.0,
                            regime, plan)
        grade = next((g for minimum, g in self.config.grades if score >= minimum), None)
        if score < self.config.min_score:
            return no(STAGE_QUALITY, "SCORE_TOO_LOW",
                      f"setup valide mais qualité {score}/100 < seuil "
                      f"{self.config.min_score} (classement, non négociable par "
                      "les autres gates)")

        # ---------- SIGNAL : payload §32 ---------------------------------------- #
        return EngineDecision(
            decision="SIGNAL", stage="OK", code="SETUP_VALID",
            detail=f"scénario {scenario.name} validé par toutes les gates",
            direction=pre.direction,
            regime=regime.regime,
            entry=plan.entry, sl=plan.sl,
            tp1=plan.tp1, tp2=plan.tp2, tp3=plan.tp3,
            rr1=plan.rr1, rr2=plan.rr2, rr3=plan.rr3,
            setup=scenario.thesis,
            market_structure=verdict.description,
            liquidity_narrative=liq.narrative,
            confirmation=(f"{conf.name} (force {conf.strength:.2f})" if conf
                          else "aucune (gates déjà saturées)"),
            why_now=" ; ".join(scenario.met) if scenario.met else "conditions réunies",
            invalidation=plan.sl_reason,
            score=score, grade=grade, zone_id=zone.id, scenario_name=scenario.name,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _score(zone_quality: float, conf_strength: float, regime: RegimeInfo,
               plan: TradePlan) -> int:
        """Classement 0-100 — n'a AUCUN pouvoir de validation (§19)."""
        s = 40.0
        s += zone_quality * 20          # qualité de zone (P6)
        s += conf_strength * 10         # force de confirmation (P5)
        s += regime.confidence * 10     # netteté du régime (P2)
        if plan.rr1 is not None and plan.rr1 >= 1.5:
            s += 5
        if plan.rr2 is not None and plan.rr2 >= 3.0:
            s += 5
        if plan.tp3 is not None:
            s += 5
        return int(round(min(100.0, s)))
