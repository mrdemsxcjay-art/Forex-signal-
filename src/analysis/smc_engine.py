"""SMC Integration Engine — PHASE 6 (module SHADOW : non branché au moteur live).

Rôle (spec §13) : assembler les zones SMC et les évaluer dans le contexte
complet produit par les phases 2-5 :

    ZONES        Order Blocks, Fair Value Gaps, BREAKER BLOCKS (un OB cassé
                 par clôture devient une zone de réaction au sens inverse),
                 statut de mitigation/remplissage, qualité du déplacement
                 qui a créé la zone.
    CONTEXTE     régime (P2), verdict structurel hiérarchique (P3), carte de
                 liquidité (P4), confirmations price action (P5).

RÈGLE ABSOLUTE §13 (le cœur du module) :
    `evaluate_zone()` ne renvoie JAMAIS "prêt" pour une zone seule :
        OB  + mauvais régime            -> NO_TRADE
        FVG + mauvaise localisation     -> NO_TRADE
        FVG + absence de confirmation   -> NO_TRADE
        OB  + contexte + liquidité + structure + confirmation -> setup potentiel
    Une zone n'est qu'un LIEU ; la décision exige le contexte entier.

Toujours shadow et fonction pure : aucune dépendance au moteur live.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .candles import compute_atr
from .liquidity_engine import LiquidityInfo, SweepEvent
from .price_action_engine import PriceActionInfo
from .regime import (
    REGIME_BREAKOUT,
    REGIME_RANGE,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    RegimeInfo,
)
from .smc import SMCEngine
from .structure_engine import StructureVerdict

ZONE_OB = "OB"
ZONE_FVG = "FVG"
ZONE_BREAKER = "BREAKER"

ACTIONABLE_STATUSES = ("active", "tested", "mitigated")


@dataclass(frozen=True)
class SMCZone:
    id: str
    kind: str                  # OB / FVG / BREAKER
    direction: str             # bullish (demande, LONG) / bearish (offre, SHORT)
    zone_top: float
    zone_bottom: float
    created_time: pd.Timestamp | None
    status: str                # active / tested / mitigated / filled / invalidated
    displacement_atr: float | None = None   # qualité du mouvement créateur
    fill_pct: float | None = None           # mitigation (FVG)
    touches: int = 0
    note: str = ""

    @property
    def mid(self) -> float:
        return (self.zone_top + self.zone_bottom) / 2

    @property
    def direction_fr(self) -> str:
        return "demande (LONG)" if self.direction == "bullish" else "offre (SHORT)"


@dataclass
class ZoneEvaluation:
    """Verdict de la gate §13 pour UNE zone dans SON contexte."""

    zone_id: str
    ready: bool
    blockers: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    quality: float = 0.0       # classement des setups DÉJÀ valides (P10)


class SMCIntegrationEngine:
    """Assemble les zones SMC et applique la gate contextuelle §13."""

    def __init__(self, swing_k: int = 4) -> None:
        self.swing_k = int(swing_k)

    # ------------------------------------------------------------------ #
    #  Construction des zones enrichies
    # ------------------------------------------------------------------ #
    def zones(self, df: pd.DataFrame, timeframe: str = "H4") -> list[SMCZone]:
        """OB + FVG + breakers, enrichis (déplacement, mitigation, touches)."""
        if df is None or len(df) < 60:
            return []
        base = SMCEngine("SMC", timeframe).analyze(df)
        atr = compute_atr(df, 14)

        # déplacement du mouvement créateur, par événement de cassure
        disp_by_event: dict[str, float] = {}
        for ev in base["events"]["structure"]:
            i = ev["break_index"]
            body = abs(float(df["close"].iloc[i]) - float(df["open"].iloc[i]))
            a = float(atr[i]) or 1e-9
            disp_by_event[ev["id"]] = round(body / a, 2)

        out: list[SMCZone] = []
        for ob in base["events"]["order_blocks"]:
            touches = 1 if ob.get("touched_at") else 0
            out.append(SMCZone(
                id=ob["id"], kind=ZONE_OB, direction=ob["direction"],
                zone_top=ob["zone_top"], zone_bottom=ob["zone_bottom"],
                created_time=pd.Timestamp(ob["confirmed_time"]),
                status="tested" if ob.get("touched_at") else ob["status"],
                displacement_atr=disp_by_event.get(ob.get("break_event_id", "")),
                touches=touches,
                note="order block",
            ))
            # BREAKER : OB invalidé par clôture -> zone au sens inverse
            if ob["status"] == "invalidated":
                out.append(SMCZone(
                    id=f"BR-{ob['id'][3:]}", kind=ZONE_BREAKER,
                    direction="bearish" if ob["direction"] == "bullish" else "bullish",
                    zone_top=ob["zone_top"], zone_bottom=ob["zone_bottom"],
                    created_time=pd.Timestamp(ob["invalidated_at"]),
                    status=self._breaker_status(df, ob),
                    displacement_atr=disp_by_event.get(ob.get("break_event_id", "")),
                    note="breaker : OB cassé par clôture, réaction au sens inverse",
                ))
        for fvg in base["events"]["fair_value_gaps"]:
            out.append(SMCZone(
                id=fvg["id"], kind=ZONE_FVG, direction=fvg["direction"],
                zone_top=fvg["zone_top"], zone_bottom=fvg["zone_bottom"],
                created_time=pd.Timestamp(fvg["created_time"]),
                status=fvg["status"],
                displacement_atr=None,
                fill_pct=fvg.get("fill_pct", 0.0),
                touches=1 if (fvg.get("fill_pct") or 0) > 0 else 0,
                note=f"fair value gap (rempli à {fvg.get('fill_pct', 0):.0f} %)",
            ))
        # les plus récentes d'abord
        out.sort(key=lambda z: z.created_time or df.index[0], reverse=True)
        return out

    def _breaker_status(self, df: pd.DataFrame, ob: dict) -> str:
        """Statut du breaker après sa naissance : testé si le prix y revient."""
        try:
            born = df.index.get_indexer([pd.Timestamp(ob["invalidated_at"])])[0]
        except Exception:  # noqa: BLE001
            return "active"
        if born < 0:
            return "active"
        flipped = "bearish" if ob["direction"] == "bullish" else "bullish"
        for i in range(born + 1, len(df)):
            hi, lo = float(df["high"].iloc[i]), float(df["low"].iloc[i])
            inside = lo <= ob["zone_top"] and hi >= ob["zone_bottom"]
            if inside:
                return "tested"
            # invalidation du breaker : clôture au-delà du côté opposé
            close = float(df["close"].iloc[i])
            if flipped == "bearish" and close > ob["zone_top"]:
                return "invalidated"
            if flipped == "bullish" and close < ob["zone_bottom"]:
                return "invalidated"
        return "active"

    # ------------------------------------------------------------------ #
    #  LA GATE §13 : une zone seule ne suffit JAMAIS
    # ------------------------------------------------------------------ #
    def evaluate_zone(
        self,
        zone: SMCZone,
        price: float,
        regime: RegimeInfo,
        verdict: StructureVerdict,
        liq: LiquidityInfo,
        pa: PriceActionInfo,
    ) -> ZoneEvaluation:
        ev = ZoneEvaluation(zone_id=zone.id, ready=False)
        blockers, reasons = ev.blockers, ev.reasons
        want_long = zone.direction == "bullish"

        # --- Gate 1 : la zone est-elle actionnable ? ------------------------
        if zone.kind == ZONE_FVG:
            if (zone.fill_pct or 0) >= 100 or zone.status == "filled":
                blockers.append("zone consommée (FVG rempli à 100 %)")
            elif zone.status not in ACTIONABLE_STATUSES:
                blockers.append(f"FVG non actionnable ({zone.status})")
        else:
            if zone.status not in ACTIONABLE_STATUSES:
                blockers.append(f"zone non actionnable ({zone.status})")
        # position : la zone doit chevaucher ou précéder le prix — un prix
        # À L'INTÉRIEUR de la zone est une localisation valide (achat dans la
        # zone). Seule une zone ENTIÈREMENT au-delà du prix est inutilisable.
        if want_long and zone.zone_bottom > price + 1e-9:
            blockers.append("zone entièrement au-dessus du prix : rien à acheter en retrait")
        if not want_long and zone.zone_top < price - 1e-9:
            blockers.append("zone entièrement sous le prix : rien à vendre en retrait")

        # --- Gate 2 : régime + localisation (hiérarchie §4-§5) ---------------
        if regime.is_no_trade_regime:
            blockers.append(f"régime {regime.regime} (NO_TRADE par conception)")
        elif regime.regime == REGIME_RANGE:
            pos = regime.position_pct
            if pos is None:
                blockers.append("range détecté mais position du prix inconnue")
            elif 45 <= pos <= 55:
                # spec §5 : MID_RANGE = NO_TRADE (interdiction de trader au milieu)
                blockers.append(f"localisation : MILIEU de range ({pos:.0f}%) — NO_TRADE")
            elif want_long and pos > 55:
                blockers.append(f"localisation : LONG à {pos:.0f}% du range (partie haute) — "
                                "on achète uniquement la partie basse")
            elif not want_long and pos < 45:
                blockers.append(f"localisation : SHORT à {pos:.0f}% du range (partie basse) — "
                                "on vend uniquement la partie haute")
            else:
                reasons.append(f"localisation range valide ({pos:.0f}%, "
                               f"{'discount' if want_long else 'premium'})")
        elif regime.regime == REGIME_TREND_UP and not want_long:
            blockers.append("SHORT contre TREND_UP")
        elif regime.regime == REGIME_TREND_DOWN and want_long:
            blockers.append("LONG contre TREND_DOWN")
        elif regime.regime == REGIME_BREAKOUT:
            if regime.breakout_direction == "up" and not want_long:
                blockers.append("SHORT contre un breakout haussier accepté")
            if regime.breakout_direction == "down" and want_long:
                blockers.append("LONG contre un breakout baissier accepté")

        # --- Gate 3 : structure HTF compatible -------------------------------
        if verdict.allowed_direction is None:
            blockers.append("structure HTF sans direction autorisée")
        elif (verdict.allowed_direction == "LONG") != want_long:
            blockers.append(f"structure HTF incompatible (autorise "
                            f"{verdict.allowed_direction}, zone {zone.direction_fr})")
        else:
            reasons.append(f"structure HTF alignée ({verdict.alignment})")

        # --- Gate 4 : liquidité (carburant OU objectif) ------------------------
        fuel = (liq.just_swept_lows and want_long) or (liq.just_swept_highs and not want_long)
        target = (want_long and liq.nearest_above is not None
                  and liq.nearest_above.price > zone.zone_top) or \
                 (not want_long and liq.nearest_below is not None
                  and liq.nearest_below.price < zone.zone_bottom)
        if fuel:
            reasons.append("carburant : liquidité opposée récemment balayée")
        if target:
            reasons.append("objectif : pool intact au-delà de la zone")
        if not fuel and not target:
            blockers.append("aucune liquidité pertinente (ni carburant ni objectif)")

        # --- Gate 5 : confirmation price action (§14) ---------------------------
        pa_dir = "bullish" if want_long else "bearish"
        conf = pa.confirmation(pa_dir)
        if conf is None:
            blockers.append(f"aucune confirmation price action {pa_dir}")
        else:
            reasons.append(f"confirmation : {conf.name} (force {conf.strength:.2f})")

        # --- Qualité (classement des setups DÉJÀ valides, P10) ------------------
        q = 0.5
        if zone.displacement_atr:
            q += min(0.25, zone.displacement_atr / 8)
        if zone.touches == 0:
            q += 0.15            # zone vierge
        if fuel and target:
            q += 0.10
        ev.quality = round(min(1.0, q), 2)
        ev.ready = not blockers
        return ev
