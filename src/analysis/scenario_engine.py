"""Location + Scenario Engine — PHASE 7 (module SHADOW : non branché au moteur live).

Trois responsabilités :

    1. LOCATION (§15) : répondre à « où sommes-nous ? » — position dans le
       range (haut/bas/milieu), premium/discount, proximité des liquidités
       et des zones actionnables. Localisation mauvaise -> NO_TRADE.
    2. SCÉNARIOS CONDITIONNELS (§16) : ne pas prédire BUY/SELL mais décrire
       ce qui se passerait si... et statuer :
           WAITING    le prix n'est pas encore au bon endroit
           ARMED      prix au bon endroit, on attend les événements déclencheurs
           TRIGGERED  événements présents (sweep + retournement + confirmation)
           INVALIDATED le scénario opposé s'est confirmé
    3. DAILY MARKET MAP (§23) : carte quotidienne (PDH/PDL/PWH/PWL, range,
       premium/discount, amplitude RÉALISÉE vs attendue, distance aux
       objectifs) + détection EXTENDED_MOVE (mouvement déjà consommé).

Codes NO_TRADE (§33) : MID_RANGE, BAD_LOCATION, NO_ZONE, NO_LIQUIDITY,
NO_CONFIRMATION, HTF_CONFLICT, REGIME_UNCERTAIN, EXTENDED_MOVE,
DATA_INSUFFICIENT, DUPLICATE_SETUP (P9), BAD_RR (P8).

La décision finale `decide()` assemble tout et passe la zone candidate à la
gate SMC de la PHASE 6 — jamais l'inverse : la hiérarchie §4 est respectée.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .candles import compute_atr
from .liquidity_engine import LiquidityInfo
from .price_action_engine import PriceActionInfo
from .regime import (
    REGIME_BREAKOUT,
    REGIME_RANGE,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    RegimeInfo,
)
from .smc_engine import SMCIntegrationEngine, SMCZone
from .structure_engine import StructureVerdict

# ---- codes NO_TRADE (§33) -------------------------------------------------- #
NT_MID_RANGE = "MID_RANGE"
NT_BAD_LOCATION = "BAD_LOCATION"
NT_NO_ZONE = "NO_ZONE"
NT_NO_LIQUIDITY = "NO_LIQUIDITY"
NT_NO_CONFIRMATION = "NO_CONFIRMATION"
NT_HTF_CONFLICT = "HTF_CONFLICT"
NT_REGIME_UNCERTAIN = "REGIME_UNCERTAIN"
NT_EXTENDED_MOVE = "EXTENDED_MOVE"
NT_DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
OK_SETUP = "SETUP_VALID"

SC_WAITING = "WAITING"
SC_ARMED = "ARMED"
SC_TRIGGERED = "TRIGGERED"
SC_INVALIDATED = "INVALIDATED"


@dataclass
class MarketLocation:
    """Réponse structurée à « où sommes-nous ? » (§15)."""

    position_pct: float | None
    range_zone: str | None          # upper / lower / mid / None (hors range)
    premium_discount: str | None
    near_liquidity: list[tuple[str, float]] = field(default_factory=list)  # (kind, ATR)
    nearest_zone: SMCZone | None = None
    zone_distance_atr: float | None = None
    verdict: str = ""               # AT_LOCATION / MID_RANGE / NO_ZONE_NEAR / OK
    detail: str = ""


@dataclass
class Scenario:
    name: str                       # "A" / "B" / "C" ...
    thesis: str                     # la thèse en une phrase (§17)
    direction: str | None           # LONG / SHORT / None (NO_TRADE)
    status: str                     # WAITING / ARMED / TRIGGERED / INVALIDATED
    met: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    zone_id: str | None = None


@dataclass
class ScenarioSet:
    scenarios: list[Scenario] = field(default_factory=list)

    @property
    def triggered(self) -> Scenario | None:
        for s in self.scenarios:
            if s.status == SC_TRIGGERED:
                return s
        return None


@dataclass
class DailyMarketMap:
    """Carte de marché quotidienne (§23)."""

    d1_bias: str | None = None
    h4_structure: str | None = None
    regime: str | None = None
    current_price: float | None = None
    pdh: float | None = None
    pdl: float | None = None
    pwh: float | None = None
    pwl: float | None = None
    range_high: float | None = None
    range_low: float | None = None
    premium_discount: str | None = None
    major_ob: str | None = None
    major_fvg: str | None = None
    nearest_liquidity_above: str | None = None
    nearest_liquidity_below: str | None = None
    daily_realized: float | None = None          # amplitude du jour (prix)
    daily_expected: float | None = None          # amplitude attendue (médiane 20 j)
    realized_ratio: float | None = None          # réalisé / attendu
    extended_move: bool = False
    distance_to_objectives: str | None = None    # en ATR

    def lines(self) -> list[str]:
        out = [
            f"D1 bias : {self.d1_bias} | H4 : {self.h4_structure} | régime : {self.regime}",
            f"Prix : {self.current_price}",
            f"PDH {self.pdh} / PDL {self.pdl} | PWH {self.pwh} / PWL {self.pwl}",
        ]
        if self.range_high is not None:
            out.append(f"Range : [{self.range_low}, {self.range_high}] "
                       f"({self.premium_discount})")
        out.append(f"OB majeur : {self.major_ob} | FVG majeur : {self.major_fvg}")
        out.append(f"Liquidité au-dessus : {self.nearest_liquidity_above} | "
                   f"en dessous : {self.nearest_liquidity_below}")
        if self.realized_ratio is not None:
            out.append(f"Amplitude du jour : {self.daily_realized} "
                       f"(attendue {self.daily_expected} — ratio {self.realized_ratio:.0%}"
                       + (" ⚠ MOUVEMENT DÉJÀ CONSOMMÉ" if self.extended_move else "") + ")")
        if self.distance_to_objectives:
            out.append(f"Distance aux objectifs : {self.distance_to_objectives}")
        return out


@dataclass
class TradeDecision:
    """Décision finale du pipeline (SIGNAL ou NO_TRADE, TOUJOURS motivée)."""

    decision: str                    # "SIGNAL" / "NO_TRADE"
    reason_code: str                 # code §33 ou OK_SETUP
    detail: str
    direction: str | None = None
    scenario: Scenario | None = None
    location: MarketLocation | None = None
    zone: SMCZone | None = None


class ScenarioEngine:
    """Localisation + scénarios + carte quotidienne + décision assemblée."""

    def __init__(
        self,
        upper_band: float = 55.0,        # % du range : au-dessus = partie haute
        lower_band: float = 45.0,
        zone_reach_atr: float = 1.5,     # distance max à une zone actionnable
        level_proximity_atr: float = 1.0,
        extended_move_ratio: float = 1.5,  # réalisé/attendu >= x -> EXTENDED
        expected_window: int = 20,         # jours pour l'amplitude attendue
        h4_atr: float | None = None,       # ATR H4 injectable (tests)
        require_confirmation: bool = True, # False : confirmation = bonus, plus gate
    ) -> None:
        self.upper_band = float(upper_band)
        self.lower_band = float(lower_band)
        self.zone_reach_atr = float(zone_reach_atr)
        self.level_proximity_atr = float(level_proximity_atr)
        self.extended_move_ratio = float(extended_move_ratio)
        self.expected_window = int(expected_window)
        self.require_confirmation = bool(require_confirmation)
        self._h4_atr = h4_atr

    # ------------------------------------------------------------------ #
    #  1. LOCATION (§15)
    # ------------------------------------------------------------------ #
    def locate(self, price: float, regime: RegimeInfo, liq: LiquidityInfo,
               zones: list[SMCZone], atr: float) -> MarketLocation:
        loc = MarketLocation(position_pct=regime.position_pct,
                             range_zone=None, premium_discount=regime.premium_discount)

        # zone du range
        if regime.regime == REGIME_RANGE and regime.position_pct is not None:
            pos = regime.position_pct
            if 45 <= pos <= 55:
                loc.range_zone = "mid"
            elif pos > self.upper_band:
                loc.range_zone = "upper"
            elif pos < self.lower_band:
                loc.range_zone = "lower"
            else:
                loc.range_zone = "mid" if abs(pos - 50) <= 5 else (
                    "upper" if pos > 50 else "lower")

        # proximité des liquidités
        for lv in liq.levels:
            if lv.status == "untouched":
                d = abs(lv.price - price) / (atr or 1e-9)
                if d <= self.level_proximity_atr:
                    loc.near_liquidity.append((lv.kind, round(d, 2)))

        # zone actionnable la plus proche
        actionable = [z for z in zones if z.status in ("active", "tested", "mitigated")]
        if actionable:
            best = min(actionable, key=lambda z: abs(z.mid - price))
            d = abs(best.mid - price) / (atr or 1e-9)
            loc.nearest_zone = best
            loc.zone_distance_atr = round(d, 2)

        # verdict
        if loc.range_zone == "mid" and regime.regime == REGIME_RANGE:
            loc.verdict = "MID_RANGE"
            loc.detail = f"milieu du range ({regime.position_pct:.0f} %)"
        elif loc.nearest_zone is not None and loc.zone_distance_atr <= self.zone_reach_atr:
            loc.verdict = "AT_LOCATION"
            loc.detail = (f"à {loc.zone_distance_atr} ATR de la zone "
                          f"{loc.nearest_zone.id} ({loc.nearest_zone.kind} "
                          f"{loc.nearest_zone.direction_fr})")
        elif loc.near_liquidity:
            loc.verdict = "NEAR_LIQUIDITY"
            loc.detail = (f"proche de {len(loc.near_liquidity)} liquidité(s) : "
                          + ", ".join(f"{k} ({d} ATR)" for k, d in loc.near_liquidity[:3]))
        else:
            loc.verdict = "NO_ZONE_NEAR"
            loc.detail = (f"aucune zone à moins de {self.zone_reach_atr} ATR "
                          "(prix loin de tout lieu pertinent)")
        return loc

    # ------------------------------------------------------------------ #
    #  2. SCÉNARIOS CONDITIONNELS (§16)
    # ------------------------------------------------------------------ #
    def build_scenarios(self, price: float, regime: RegimeInfo, loc: MarketLocation,
                        verdict: StructureVerdict, liq: LiquidityInfo,
                        pa: PriceActionInfo, zones: list[SMCZone]) -> ScenarioSet:
        ss = ScenarioSet()

        def zone_for(direction: str) -> SMCZone | None:
            cands = [z for z in zones
                     if z.direction == direction
                     and z.status in ("active", "tested", "mitigated")
                     and abs(z.mid - price) <= self.zone_reach_atr * 2.5]
            return min(cands, key=lambda z: abs(z.mid - price)) if cands else None

        def bear_trigger() -> tuple[list[str], list[str]]:
            met, pend = [], []
            if liq.just_swept_highs:
                met.append("liquidité haute balayée")
            else:
                pend.append("prise de la liquidité haute")
            conf = pa.confirmation("bearish")
            if conf:
                met.append(f"confirmation {conf.name}")
            elif self.require_confirmation:
                pend.append("confirmation price action baissière")
            return met, pend

        def bull_trigger() -> tuple[list[str], list[str]]:
            met, pend = [], []
            if liq.just_swept_lows:
                met.append("liquidité basse balayée")
            else:
                pend.append("prise de la liquidité basse")
            conf = pa.confirmation("bullish")
            if conf:
                met.append(f"confirmation {conf.name}")
            elif self.require_confirmation:
                pend.append("confirmation price action haussière")
            return met, pend

        # ---------------- RANGE : A (SELL haut) / B (BUY bas) / C (milieu) --
        if regime.regime == REGIME_RANGE:
            z_sell = zone_for("bearish")
            met_a, pend_a = bear_trigger()
            status_a = SC_WAITING
            if regime.breakout_direction == "up" or (
                    regime.regime == REGIME_BREAKOUT):
                status_a = SC_INVALIDATED
            elif loc.range_zone == "upper":
                status_a = SC_TRIGGERED if not pend_a else SC_ARMED
            ss.scenarios.append(Scenario(
                "A", "Prix au haut du range : sweep de la liquidité haute puis "
                "retournement confirmé -> SELL vers le milieu/bas du range",
                "SHORT", status_a, met_a, pend_a,
                z_sell.id if z_sell else None))

            z_buy = zone_for("bullish")
            met_b, pend_b = bull_trigger()
            status_b = SC_WAITING
            if regime.breakout_direction == "down":
                status_b = SC_INVALIDATED
            elif loc.range_zone == "lower":
                status_b = SC_TRIGGERED if not pend_b else SC_ARMED
            ss.scenarios.append(Scenario(
                "B", "Prix au bas du range : sweep de la liquidité basse puis "
                "retournement confirmé -> BUY vers le milieu/haut du range",
                "LONG", status_b, met_b, pend_b,
                z_buy.id if z_buy else None))

            ss.scenarios.append(Scenario(
                "C", "Prix au milieu du range : aucun avantage -> NO_TRADE",
                None, SC_TRIGGERED if loc.range_zone == "mid" else SC_WAITING,
                ["milieu de range"] if loc.range_zone == "mid" else [],
                [] if loc.range_zone == "mid" else ["prix au milieu"]))

        # ---------------- TREND : continuation sur retrait -------------------
        elif regime.regime in (REGIME_TREND_UP, REGIME_TREND_DOWN):
            if regime.regime == REGIME_TREND_UP:
                z = zone_for("bullish")
                met, pend = bull_trigger()
                thesis = ("Retracement haussier dans une zone de demande : "
                          "sweep du creux + confirmation -> LONG de continuation")
                direction = "LONG"
            else:
                z = zone_for("bearish")
                met, pend = bear_trigger()
                thesis = ("Repli baissier dans une zone d'offre : sweep du sommet "
                          "+ confirmation -> SHORT de continuation")
                direction = "SHORT"
            in_zone = loc.nearest_zone is not None and loc.zone_distance_atr <= self.zone_reach_atr
            status = (SC_TRIGGERED if not pend and in_zone
                      else SC_ARMED if in_zone else SC_WAITING)
            ss.scenarios.append(Scenario(
                "T", thesis, direction, status, met, pend,
                z.id if z else None))

        # ---------------- BREAKOUT : retest + confirmation --------------------
        elif regime.regime == REGIME_BREAKOUT:
            up = regime.breakout_direction == "up"
            direction = "LONG" if up else "SHORT"
            met, pend = ([], ["retest du niveau cassé"])
            conf = pa.confirmation("bullish" if up else "bearish")
            if conf:
                met.append(f"confirmation {conf.name}")
            elif self.require_confirmation:
                pend.append(f"confirmation price action {'haussière' if up else 'baissière'}")
            ss.scenarios.append(Scenario(
                "K", f"Breakout {'haussier' if up else 'baissier'} accepté : attendre "
                "le retest du niveau cassé puis la confirmation avant l'entrée",
                direction, SC_ARMED if not met else SC_TRIGGERED, met, pend))
        return ss

    # ------------------------------------------------------------------ #
    #  3. DAILY MARKET MAP (§23)
    # ------------------------------------------------------------------ #
    def daily_map(self, price: float, d1: pd.DataFrame, h4: pd.DataFrame,
                  regime: RegimeInfo, liq: LiquidityInfo,
                  verdict: StructureVerdict, zones: list[SMCZone]) -> DailyMarketMap:
        m = DailyMarketMap(d1_bias=verdict.external_trend,
                           h4_structure=verdict.alignment,
                           regime=regime.regime, current_price=price)
        by_kind = {lv.kind: lv.price for lv in liq.levels}
        for kind in ("PDH", "PDL", "PWH", "PWL"):
            if kind in liq.key_levels:
                by_kind[kind] = liq.key_levels[kind].price
        m.pdh, m.pdl = by_kind.get("PDH"), by_kind.get("PDL")
        m.pwh, m.pwl = by_kind.get("PWH"), by_kind.get("PWL")
        m.range_high, m.range_low = regime.range_high, regime.range_low
        m.premium_discount = regime.premium_discount
        obs = [z for z in zones if z.kind == "OB" and z.status in ("active", "tested")]
        fvgs = [z for z in zones if z.kind == "FVG" and z.status in ("active", "mitigated")]
        if obs:
            z = min(obs, key=lambda z: abs(z.mid - price))
            m.major_ob = f"{z.id} [{z.zone_bottom:.5f}, {z.zone_top:.5f}] ({z.status})"
        if fvgs:
            z = min(fvgs, key=lambda z: abs(z.mid - price))
            m.major_fvg = f"{z.id} [{z.zone_bottom:.5f}, {z.zone_top:.5f}]"
        if liq.nearest_above:
            m.nearest_liquidity_above = (f"{liq.nearest_above.kind} "
                                         f"{liq.nearest_above.price:.5f}")
        if liq.nearest_below:
            m.nearest_liquidity_below = (f"{liq.nearest_below.kind} "
                                         f"{liq.nearest_below.price:.5f}")

        # amplitude du jour vs attendue (§23 : mouvement déjà consommé ?)
        try:
            today = h4.index[-1].normalize()
            todays = h4[h4.index.normalize() == today]
            realized = float(todays["high"].max() - todays["low"].min()) if len(todays) else 0.0
            hist = d1["high"] - d1["low"]
            expected = float(hist.tail(self.expected_window).median())
            m.daily_realized, m.daily_expected = realized, expected
            if expected > 0:
                m.realized_ratio = realized / expected
                m.extended_move = m.realized_ratio >= self.extended_move_ratio
        except Exception:  # noqa: BLE001 — la carte ne doit jamais échouer
            pass
        return m

    # ------------------------------------------------------------------ #
    #  4. DÉCISION ASSEMBLÉE (hiérarchie §4 respectée)
    # ------------------------------------------------------------------ #
    def decide(self, price: float, regime: RegimeInfo, verdict: StructureVerdict,
               liq: LiquidityInfo, pa: PriceActionInfo, zones: list[SMCZone],
               d1: pd.DataFrame, h4: pd.DataFrame,
               smc_gate: SMCIntegrationEngine) -> TradeDecision:
        def no_trade(code: str, detail: str) -> TradeDecision:
            return TradeDecision("NO_TRADE", code, detail)

        # 1. Données / régime (portes les plus hautes, §4)
        if regime.regime == "INSUFFICIENT_DATA":
            return no_trade(NT_DATA_INSUFFICIENT, regime.detail)
        if regime.regime in ("CHAOTIC", "TRANSITION", "BREAKOUT_RETEST"):
            return no_trade(NT_REGIME_UNCERTAIN,
                            f"régime {regime.regime} : {regime.detail}")

        atr = self._h4_atr or float(compute_atr(h4, 14)[-1]) or 1e-9
        loc = self.locate(price, regime, liq, zones, atr)
        ss = self.build_scenarios(price, regime, loc, verdict, liq, pa, zones)
        mmap = self.daily_map(price, d1, h4, regime, liq, verdict, zones)

        # 2. Localisation (§15)
        if loc.verdict == "MID_RANGE":
            return TradeDecision("NO_TRADE", NT_MID_RANGE, loc.detail,
                                 location=loc)
        if loc.verdict == "NO_ZONE_NEAR" and not loc.near_liquidity:
            return TradeDecision("NO_TRADE", NT_NO_ZONE, loc.detail, location=loc)

        # 3. Structure HTF (§4)
        if verdict.allowed_direction is None:
            return TradeDecision("NO_TRADE", NT_HTF_CONFLICT,
                                 f"structure externe : {verdict.description}",
                                 location=loc)

        # 4. Scénario déclenché ?
        scenario = ss.triggered
        if scenario is None or scenario.direction is None:
            armed = [s for s in ss.scenarios if s.status == SC_ARMED]
            detail = ("; ".join(f"scénario {s.name} armé : manque {s.pending[0]}"
                                for s in armed) or
                      "aucun scénario mûr (prix attendu en zone)")
            return TradeDecision("NO_TRADE", NT_NO_CONFIRMATION, detail,
                                 location=loc)
        if verdict.allowed_direction != scenario.direction:
            return TradeDecision("NO_TRADE", NT_HTF_CONFLICT,
                                 f"scénario {scenario.direction} mais structure "
                                 f"autorise {verdict.allowed_direction}",
                                 location=loc, scenario=scenario)

        # 5. Mouvement déjà consommé ? (§23 — uniquement en continuation de tendance)
        if regime.regime in (REGIME_TREND_UP, REGIME_TREND_DOWN) and mmap.extended_move:
            return TradeDecision(
                "NO_TRADE", NT_EXTENDED_MOVE,
                f"amplitude du jour déjà {mmap.realized_ratio:.0%} de l'attendu "
                f"({mmap.daily_realized:.5f} vs {mmap.daily_expected:.5f}) : "
                "probabilité de continuation réduite",
                location=loc, scenario=scenario)

        # 6. Zone candidate -> gate SMC complète (P6, §13)
        zone = next((z for z in zones if z.id == scenario.zone_id), loc.nearest_zone)
        if zone is None:
            return TradeDecision("NO_TRADE", NT_NO_ZONE,
                                 "scénario déclenché sans zone actionnable",
                                 location=loc, scenario=scenario)
        ev = smc_gate.evaluate_zone(zone, price, regime, verdict, liq, pa)
        if not ev.ready:
            code = self._map_blocker(ev.blockers[0] if ev.blockers else "")
            return TradeDecision("NO_TRADE", code,
                                 "; ".join(ev.blockers[:3]),
                                 location=loc, scenario=scenario, zone=zone)

        # 7. SIGNAL — thèse unique, zone validée, contexte complet
        return TradeDecision(
            "SIGNAL", OK_SETUP,
            f"scénario {scenario.name} : {scenario.thesis}",
            direction=scenario.direction, scenario=scenario,
            location=loc, zone=zone)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _map_blocker(blocker: str) -> str:
        b = blocker.lower()
        if "milieu" in b or "localisation" in b:
            return NT_MID_RANGE if "milieu" in b else NT_BAD_LOCATION
        if "régime" in b or "regime" in b:
            return NT_REGIME_UNCERTAIN
        if "structure htf" in b:
            return NT_HTF_CONFLICT
        if "confirmation" in b:
            return NT_NO_CONFIRMATION
        if "liquidité" in b or "liquidite" in b:
            return NT_NO_LIQUIDITY
        if "consommée" in b or "actionnable" in b or "au-dessus" in b or "sous le prix" in b:
            return NT_NO_ZONE
        return NT_BAD_LOCATION
