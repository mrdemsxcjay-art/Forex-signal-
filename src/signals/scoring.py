"""Moteur de scoring des signaux — pondérations de la spécification (total 100).

    Fondamental   sentiment aligné .....................  +25
    SMC           order block actionnable ..............  +20
                  fair value gap actionnable ...........  +15
                  CHoCH confirmé (M15/H4, récent) ......  +15
    Timing        session Londres ou New York ..........  +10
    Structure     zone premium/discount alignée ........  +10
                  liquidité balayée (equal H/L swept) ..   +5

    Émission : score >= seuil (défaut 70) ET alignement D1/H4/M15 (moteur).

Le scoring est une FONCTION PURE : mêmes entrées -> même score, testable
au point près, rejouable en backtest.
"""
from __future__ import annotations

import pandas as pd

WEIGHTS = {
    "fondamental": 25,
    "order_block": 20,
    "fvg": 15,
    "choch": 15,
    "session": 10,
    "premium_discount": 10,
    "sweep": 5,
}

#: Sessions de trading en UTC (approximations professionnelles usuelles).
SESSIONS = [
    (0, 7, "Asie / hors session", False),
    (7, 10, "London Kill Zone", True),
    (10, 12, "London", True),
    (12, 15, "New York Kill Zone (chevauchement)", True),
    (15, 17, "New York", True),
    (17, 21, "New York PM", True),
    (21, 24, "Sydney / hors session", False),
]


def get_session(now: pd.Timestamp) -> tuple[str, bool]:
    """(libellé de session, compte-t-elle pour le score London/NY ?)."""
    hour = now.hour + now.minute / 60.0
    for start, end, label, scores in SESSIONS:
        if start <= hour < end:
            return label, scores
    return "hors session", False


def grade_of(score: int, grades: list[tuple[int, str]] | None = None) -> str | None:
    """Note littérale associée au score (défaut : 85 A+, 75 A, 70 B)."""
    for minimum, label in grades or [(85, "A+"), (75, "A"), (70, "B")]:
        if score >= minimum:
            return label
    return None


def compute_score(
    direction: str,
    smc,                       # MultiTFView (Agent 2)
    fundamental,               # FundamentalView (Agent 1)
    now: pd.Timestamp,
) -> tuple[int, dict, list[str]]:
    """Retourne (score /100, détail par composante, libellés de confluence).

    Args:
        direction: "LONG" ou "SHORT" candidat.
        smc: verdict de l'Agent 2 (zones, déclencheur, premium/discount...).
        fundamental: verdict de l'Agent 1.
        now: horodatage UTC injecté (déterminisme des tests).
    """
    long_side = direction == "LONG"
    breakdown: dict[str, int] = {}
    confluences: list[str] = []

    # --- Fondamental (+25) --------------------------------------------------
    breakdown["fondamental"] = WEIGHTS["fondamental"] if fundamental.supports_direction else 0
    if fundamental.supports_direction:
        driver = fundamental.drivers[0] if fundamental.drivers else "sentiment aligné"
        confluences.append(f"News : {driver}")

    # --- Order block (+20) --------------------------------------------------
    ob = smc.ob_near
    breakdown["order_block"] = WEIGHTS["order_block"] if ob else 0
    if ob:
        confluences.append(f"Order Block {ob.get('timeframe', '')} "
                           f"[{ob.get('zone_bottom')}, {ob.get('zone_top')}]")

    # --- Fair value gap (+15) -----------------------------------------------
    fvg = smc.fvg_near
    breakdown["fvg"] = WEIGHTS["fvg"] if fvg else 0
    if fvg:
        fill = fvg.get("fill_pct")
        fill_txt = f" rempli à {fill} %" if fill else ""
        confluences.append(f"FVG {fvg.get('timeframe', '')}{fill_txt}")

    # --- CHoCH confirmé (+15) -----------------------------------------------
    choch_ok = (smc.m15_trigger is not None
                and smc.m15_trigger == ("bullish" if long_side else "bearish")
                and smc.m15_trigger_kind == "CHoCH")
    breakdown["choch"] = WEIGHTS["choch"] if choch_ok else 0
    if choch_ok:
        confluences.append(f"CHoCH M15 confirmé (il y a {smc.m15_trigger_age} bougies)")

    # --- Session (+10) ------------------------------------------------------
    session_label, session_scores = get_session(now)
    breakdown["session"] = WEIGHTS["session"] if session_scores else 0
    if session_scores:
        confluences.append(f"Session : {session_label}")

    # --- Premium / discount (+10) -------------------------------------------
    # LONG favorisé si le prix est en DISCOUNT (sous l'équilibre du range H4),
    # SHORT favorisé s'il est en PREMIUM : on achète bas, on vend haut.
    pd_aligned = (
        (long_side and smc.premium_discount == "discount")
        or (not long_side and smc.premium_discount == "premium")
    )
    breakdown["premium_discount"] = WEIGHTS["premium_discount"] if pd_aligned else 0
    if pd_aligned:
        pct = f" ({smc.pd_position_pct:.0f} % du range H4)" if smc.pd_position_pct is not None else ""
        confluences.append(f"Zone {smc.premium_discount}{pct}")

    # --- Liquidité balayée (+5) ---------------------------------------------
    sweep = smc.sweep_recent
    breakdown["sweep"] = WEIGHTS["sweep"] if sweep else 0
    if sweep:
        confluences.append(f"Liquidité balayée : {sweep.get('label', 'sweep')}")

    score = sum(breakdown.values())
    return score, breakdown, confluences
