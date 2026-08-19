"""AGENT 1 — Analyste fondamental.

Mandat : « Analyse uniquement les news et donne un sentiment. »
Il ne voit NI les prix, NI les zones : uniquement le calendrier économique
(surprises actual vs forecast, déjà industrialisées en phase 3).

Sortie : FundamentalView (biais de paire, soutient-il la direction ?,
drivers lisibles, news rouge imminente).
"""
from __future__ import annotations

import logging

import pandas as pd

from ..fundamental.fundamental_analyzer import FundamentalAnalyzer
from ..signals.models import FundamentalView

logger = logging.getLogger(__name__)


class FundamentalAgent:
    """Adapte FundamentalAnalyzer en agent à verdict binaire pour le scoring."""

    def __init__(self, analyzer: FundamentalAnalyzer | None = None) -> None:
        self.analyzer = analyzer or FundamentalAnalyzer()

    def assess(
        self,
        pair: str,
        direction: str | None,
        now: pd.Timestamp | None = None,
    ) -> FundamentalView:
        """Évalue le sentiment de la paire et son alignement à la direction.

        Args:
            pair:      ex. "EURUSD" (biais = force base − force quote).
            direction: "LONG"/"SHORT" candidate (None = simple consultation).
            now:       horodatage injecté (déterminisme des tests).
        """
        now = now or pd.Timestamp.now(tz="UTC")
        try:
            bias = self.analyzer.get_pair_bias(pair, now=now)
            soon = self.analyzer.is_high_impact_soon(now=now)
            drivers: list[str] = []
            for side in (bias.base, bias.quote):
                if side is not None:
                    drivers.extend(f"{side.currency} {d}" for d in side.drivers[:2])
        except Exception as exc:  # noqa: BLE001 — le fondamental ne bloque jamais le moteur
            logger.warning("Agent 1 (fondamental) indisponible : %s", exc)
            return FundamentalView("NEUTRAL", 0.0, False, [], False)

        wanted = "BULLISH" if direction == "LONG" else "BEARISH" if direction else None
        supports = bool(wanted) and bias.label == wanted and abs(bias.score) >= 1.2
        if not drivers and bias.label != "NEUTRAL":
            drivers = [f"biais {pair} {bias.label} ({bias.score:+.1f})"]

        return FundamentalView(
            bias=bias.label,
            score=bias.score,
            supports_direction=supports,
            drivers=drivers,
            high_impact_soon=soon,
        )
