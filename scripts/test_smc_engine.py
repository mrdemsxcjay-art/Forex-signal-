"""Tests du SMC Integration Engine — PHASE 6 (synthétique, hors-ligne).

Deux blocs :
  A. Construction des zones (OB enrichi, breaker né d'une invalidation)
  B. LA GATE §13 : les 4 exemples de la spec, plus les cas limites
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.liquidity_engine import LiquidityInfo, LiquidityLevel, SweepEvent
from src.analysis.price_action_engine import Pattern, PriceActionInfo
from src.analysis.regime import (
    REGIME_RANGE,
    REGIME_TRANSITION,
    REGIME_TREND_UP,
    RegimeInfo,
)
from src.analysis.smc_engine import SMCIntegrationEngine, SMCZone
from src.analysis.structure_engine import StructureVerdict

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


def to_df(closes, spread=0.0004, freq="4h"):
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c
    idx = pd.date_range("2026-06-01", periods=len(rows), freq=freq, tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def build_series():
    """Déclin -> bougie baissière (OB) -> rallye cassant (CHoCH+displacement)
    -> consolidation -> chute invalidante (breaker) -> retour dans la zone."""
    closes = []
    p = 1.1050
    for i in range(40):                       # déclin zigzag (swings réels)
        p -= 0.00020
        closes.append(p + 0.0008 * (1 if i % 4 < 2 else -1))
    closes.append(closes[-1] - 0.0010)        # la bougie baissière = futur OB
    for _ in range(4):                        # rallye avec déplacement
        closes.append(closes[-1] + 0.0020)
    for i in range(12):                       # consolidation
        closes.append(closes[-1] + 0.0006 * (1 if i % 4 < 2 else -1))
    for _ in range(5):                        # chute invalidante
        closes.append(closes[-1] - 0.0025)
    for _ in range(4):                        # retour dans la zone
        closes.append(closes[-1] + 0.0022)
    return closes


# --------------------------------------------------------------------------- #
#  Contextes fabriqués pour la gate §13
# --------------------------------------------------------------------------- #
def zone_ob_bull(bottom=1.0950, top=1.0960):
    return SMCZone("OB-0001", "OB", "bullish", top, bottom,
                   pd.Timestamp("2026-06-10", tz="UTC"), "active",
                   displacement_atr=1.4, touches=0, note="ob de test")


def zone_fvg_bull(fill=20.0):
    return SMCZone("FVG-0001", "FVG", "bullish", 1.0960, 1.0950,
                   pd.Timestamp("2026-06-10", tz="UTC"), "active",
                   fill_pct=fill, touches=0, note="fvg de test")


def regime_range(pos=30.0):
    return RegimeInfo(REGIME_RANGE, 0.9, "range de test",
                      1.1050, 1.0900, 1.0975, pos,
                      "discount" if pos < 45 else "premium")


def regime_transition():
    return RegimeInfo(REGIME_TRANSITION, 0.6, "transition de test")


def verdict_long():
    return StructureVerdict("bullish", "bullish", "ALIGNED_UP", "LONG", "aligné")


def verdict_none():
    return StructureVerdict(None, None, "EXTERNAL_UNCLEAR", None, "aucune")


def liq_fuel_and_target():
    return LiquidityInfo(
        recent_sweeps=[SweepEvent(1.0940, "EQL", "swept_lows",
                                  pd.Timestamp("2026-06-11", tz="UTC"),
                                  0.7, "bullish", "test")],
        nearest_above=LiquidityLevel(1.1010, "EQH", "internal", "untouched"),
    )


def liq_empty():
    return LiquidityInfo()


def pa_with_bull():
    info = PriceActionInfo()
    info.patterns = [Pattern("engulfing", "bullish", 99,
                             pd.Timestamp("2026-06-11", tz="UTC"), 0.8, "test")]
    return info


def pa_empty():
    return PriceActionInfo()


PRICE = 1.1000   # zone sous le prix -> retrait possible pour un LONG


def main() -> int:
    eng = SMCIntegrationEngine()
    print("=" * 70)
    print(" Tests SMC Integration Engine — PHASE 6")
    print("=" * 70)

    # ---------------- A. Construction des zones ---------------- ----------
    zones = eng.zones(to_df(build_series()), "H4")
    obs = [z for z in zones if z.kind == "OB" and z.direction == "bullish"]
    check("OB haussier détecté et enrichi (déplacement enregistré)",
          len(obs) >= 1 and obs[0].displacement_atr is not None
          and obs[0].displacement_atr > 0.5,
          f"disp {obs[0].displacement_atr} ATR" if obs else "aucun OB")

    brk = [z for z in zones if z.kind == "BREAKER"]
    check("breaker né de l'invalidation (sens inverse, testé au retour)",
          len(brk) >= 1 and brk[0].direction == "bearish"
          and brk[0].status == "tested",
          f"{brk[0].direction} / {brk[0].status}" if brk else "aucun breaker")

    # ---------------- B. LA GATE §13 -------------------------------- -------
    print("\n--- Les 4 exemples de la spec §13 ---")

    # 1. OB + mauvais régime -> NO_TRADE
    ev = eng.evaluate_zone(zone_ob_bull(), PRICE, regime_transition(),
                           verdict_long(), liq_fuel_and_target(), pa_with_bull())
    check("OB + régime TRANSITION -> NO_TRADE",
          not ev.ready and any("régime" in b for b in ev.blockers),
          str(ev.blockers[:1]))

    # 2. FVG + mauvaise localisation -> NO_TRADE
    ev = eng.evaluate_zone(zone_fvg_bull(), PRICE, regime_range(pos=80.0),
                           verdict_long(), liq_fuel_and_target(), pa_with_bull())
    check("FVG + prix à 80% du range (LONG) -> NO_TRADE (localisation)",
          not ev.ready and any("localisation" in b for b in ev.blockers),
          str(ev.blockers[:1]))

    # 3. FVG + absence de confirmation -> NO_TRADE
    ev = eng.evaluate_zone(zone_fvg_bull(), PRICE, regime_range(pos=30.0),
                           verdict_long(), liq_fuel_and_target(), pa_empty())
    check("FVG + aucune confirmation PA -> NO_TRADE",
          not ev.ready and any("confirmation" in b for b in ev.blockers),
          str(ev.blockers[:1]))

    # 4. OB + contexte + liquidité + structure + confirmation -> setup potentiel
    ev = eng.evaluate_zone(zone_ob_bull(), PRICE, regime_range(pos=30.0),
                           verdict_long(), liq_fuel_and_target(), pa_with_bull())
    check("OB + régime + localisation + structure + liquidité + confirmation "
          "-> SETUP POTENTIEL",
          ev.ready and not ev.blockers and len(ev.reasons) >= 4
          and ev.quality > 0.5,
          f"qualité {ev.quality} ; raisons : {len(ev.reasons)}")

    print("\n--- Cas limites de la gate ---")

    # 5. SHORT contre TREND_UP
    z = SMCZone("OB-0002", "OB", "bearish", 1.1060, 1.1050,
                pd.Timestamp("2026-06-10", tz="UTC"), "active")
    ev = eng.evaluate_zone(z, 1.1000,
                           RegimeInfo(REGIME_TREND_UP, 0.9, "trend up"),
                           verdict_long(), liq_fuel_and_target(), pa_with_bull())
    check("zone SHORT en TREND_UP -> NO_TRADE",
          not ev.ready and any("TREND_UP" in b for b in ev.blockers))

    # 6. Structure HTF incompatible
    verdict_short = StructureVerdict("bearish", "bearish", "ALIGNED_DOWN",
                                      "SHORT", "aligné baissier")
    ev = eng.evaluate_zone(zone_ob_bull(), PRICE, regime_range(pos=30.0),
                           verdict_short, liq_fuel_and_target(), pa_with_bull())
    check("structure autorise SHORT mais zone LONG -> NO_TRADE",
          not ev.ready and any("structure HTF incompatible" in b for b in ev.blockers))

    # 7. Aucune liquidité (ni carburant ni objectif)
    ev = eng.evaluate_zone(zone_ob_bull(), PRICE, regime_range(pos=30.0),
                           verdict_long(), liq_empty(), pa_with_bull())
    check("aucune liquidité pertinente -> NO_TRADE",
          not ev.ready and any("liquidité" in b for b in ev.blockers))

    # 8. Zone au-dessus du prix pour un LONG
    z = SMCZone("OB-0003", "OB", "bullish", 1.1080, 1.1060,
                pd.Timestamp("2026-06-10", tz="UTC"), "active")
    ev = eng.evaluate_zone(z, PRICE, regime_range(pos=30.0),
                           verdict_long(), liq_fuel_and_target(), pa_with_bull())
    check("zone au-DESSUS du prix pour un LONG -> NO_TRADE",
          not ev.ready and any("au-dessus du prix" in b for b in ev.blockers))

    # 9. FVG rempli à 100 %
    ev = eng.evaluate_zone(zone_fvg_bull(fill=100.0), PRICE, regime_range(pos=30.0),
                           verdict_long(), liq_fuel_and_target(), pa_with_bull())
    check("FVG consommé (100 %) -> NO_TRADE",
          not ev.ready and any("consommée" in b for b in ev.blockers))

    # 10. LONG en range à 50% (milieu) -> NO_TRADE (règle §5)
    ev = eng.evaluate_zone(zone_ob_bull(), PRICE, regime_range(pos=50.0),
                           verdict_long(), liq_fuel_and_target(), pa_with_bull())
    check("LONG à 50% du range (milieu) -> NO_TRADE",
          not ev.ready and any("localisation" in b for b in ev.blockers))

    # 11. Causalité : les zones ne dépendent pas des bougies futures
    full = to_df(build_series())
    z_full = eng.zones(full, "H4")
    z_trunc = eng.zones(full.iloc[:-3], "H4")
    ids_full = {z.id for z in z_full}
    actionable = [zt for zt in z_trunc if zt.status in ("active", "mitigated", "filled")]
    ok = (len(actionable) > 0 and all(
        any(zf.id == zt.id and zf.zone_top == zt.zone_top
            and zf.zone_bottom == zt.zone_bottom for zf in z_full)
        for zt in actionable
    ))
    check("causalité : zones stables fenêtre tronquée vs complète (non vide)", ok,
          f"{len(actionable)} zones actionnables tronquées ⊂ {len(ids_full)} complètes")

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — SMC Integration Engine validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
