"""AGENT 3 — Gestionnaire du risque.

Mandat : « Valide le signal des agents 1 et 2 et calcule le R/R. »
Il est le SEUL habilité à rejeter un signal déjà scoré. Blocages possibles :

    - désalignement des timeframes (géré en amont par le moteur) ;
    - news rouge imminente (fenêtre paramétrable, défaut 60 min) ;
    - R/R insuffisant (défaut >= 1.5) ;
    - zone incohérente (stop derrière l'entrée, risque <= 0).

Plan de trade (méthode ICT) :
    entrée = bord de la zone actionnable (ordre LIMITE) si OB/FVG trouvée,
             sinon prix de marché (dernière clôture M15) ;
    stop   = bord opposé de la zone − sl_buffer_atr x ATR(M15)
             (ou extremum des 5 dernières M15 si pas de zone) ;
    objectif = pool de liquidité intact le plus proche (H4) si atteignable
             avec >= min_rr, sinon multiple fixe default_rr (défaut 2.0) ;
    lots   = (compte x risk_pct) / (risque en pips x valeur du pip).

Le calcul est une fonction pure des entrées : déterministe, testable.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..signals.models import MultiTFView, RiskPlan, pip_spec

logger = logging.getLogger(__name__)


class RiskAgent:
    """Valide le candidat des agents 1+2 et produit le plan chiffré."""

    def __init__(
        self,
        min_rr: float = 1.5,
        default_rr: float = 2.0,
        sl_buffer_atr: float = 0.10,
        risk_pct: float = 1.0,
        account_size: float = 10_000.0,
        news_block_minutes: int = 60,
    ) -> None:
        self.min_rr = float(min_rr)
        self.default_rr = float(default_rr)
        self.sl_buffer_atr = float(sl_buffer_atr)
        self.risk_pct = float(risk_pct)
        self.account_size = float(account_size)
        self.news_block_minutes = int(news_block_minutes)

    # ------------------------------------------------------------------ #
    def validate(
        self,
        pair: str,
        direction: str,
        smc: MultiTFView,
        m15: pd.DataFrame,
        h4_liquidity: dict | None = None,
        high_impact_soon: bool = False,
    ) -> RiskPlan:
        """Construit et valide le plan. `h4_liquidity` = bloc liquidité de
        l'analyse SMC H4 (pools intacts, pour l'objectif)."""
        blockers: list[str] = []
        long_side = direction == "LONG"
        pip_size, pip_value = pip_spec(pair)
        price = smc.current_price
        atr = smc.atr_m15

        # --- News rouge imminente : blocage (le fondamental n'annule pas
        #     un setup technique, mais une news rouge à 30 min le fait) ----
        if high_impact_soon:
            blockers.append(f"news à fort impact dans les {self.news_block_minutes} min")

        # --- Entrée --------------------------------------------------------
        # Prix DANS la zone -> entrée au marché ; zone sous le prix -> ordre
        # LIMITE au bord de la zone (on laisse le prix venir, on ne le poursuit pas).
        zone = smc.ob_near or smc.fvg_near
        if zone is not None:
            if long_side:
                entry = float(price) if price <= zone["zone_top"] else float(zone["zone_top"])
            else:
                entry = float(price) if price >= zone["zone_bottom"] else float(zone["zone_bottom"])
            entry_kind = f"bord de la zone {zone['id']} ({zone.get('timeframe', '')})"
        else:
            entry = float(price)
            entry_kind = "marché (dernière clôture M15)"

        # --- Stop ----------------------------------------------------------
        if zone is not None:
            base = float(zone["zone_bottom"]) if long_side else float(zone["zone_top"])
        else:
            window = m15.tail(5)
            base = float(window["low"].min()) if long_side else float(window["high"].max())
        sl = base - self.sl_buffer_atr * atr if long_side else base + self.sl_buffer_atr * atr

        risk = (entry - sl) if long_side else (sl - entry)
        if risk <= 0:
            blockers.append("zone incohérente : stop du mauvais côté de l'entrée")
            risk = max(risk, 1e-9)

        # --- Objectif : liquidité intacte sinon multiple fixe ---------------
        tp = None
        if h4_liquidity:
            pools = h4_liquidity.get("equal_highs", []) if long_side \
                else h4_liquidity.get("equal_lows", [])
            candidates = [p for p in pools if p.get("status") == "untouched"]
            if long_side:
                candidates = [p for p in candidates if p["level"] > entry]
                candidates.sort(key=lambda p: p["level"])
            else:
                candidates = [p for p in candidates if p["level"] < entry]
                candidates.sort(key=lambda p: -p["level"])
            for pool in candidates:
                reward = (pool["level"] - entry) if long_side else (entry - pool["level"])
                if reward / risk >= self.min_rr:
                    tp = float(pool["level"])
                    break
        if tp is None:
            tp = entry + self.default_rr * risk if long_side else entry - self.default_rr * risk

        reward = (tp - entry) if long_side else (entry - tp)
        rr = reward / risk
        if rr < self.min_rr:
            blockers.append(f"R/R {rr:.2f} < minimum {self.min_rr:.2f}")

        # --- Position sizing -------------------------------------------------
        risk_pips = abs(entry - sl) / pip_size
        tp_pips = abs(tp - entry) / pip_size
        lots = (self.account_size * self.risk_pct / 100.0) / (risk_pips * pip_value)
        lots = round(lots, 2)

        reasons = [
            f"entrée : {entry_kind}",
            f"stop : {'zone' if zone is not None else 'extremum M15'} "
            f"− {self.sl_buffer_atr}×ATR",
            f"objectif : {'liquidité H4 intacte' if h4_liquidity and tp else f'{self.default_rr:.1f}R'}",
            f"risque : {self.risk_pct:.1f} % du compte ({self.account_size:.0f}) "
            f"≈ {lots} lots",
        ]

        valid = not blockers
        if not valid:
            logger.info("[%s] signal REJETÉ par l'agent risque : %s", pair, ", ".join(blockers))
        return RiskPlan(
            valid=valid, entry=round(entry, 6), sl=round(sl, 6), tp=round(tp, 6),
            rr=round(float(rr), 2), risk_pips=round(risk_pips, 1), tp_pips=round(tp_pips, 1),
            lots=lots, reasons=reasons, blockers=blockers,
        )
