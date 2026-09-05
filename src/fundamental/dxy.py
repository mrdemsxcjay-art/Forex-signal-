"""DXY (Dollar Index) — source Yahoo DX-Y.NYB, SANS clé API.

Le prompt d'origine suggérait TwelveData (clé API obligatoire, inscription).
DX-Y.NYB sur Yahoo fournit le même indice gratuitement et sans compte :
le robot reste 100 % sans inscription.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DxyInfo:
    close: float
    change_pct: float          # variation du jour en %
    fresh: bool = True

    @property
    def tone(self) -> str:
        """Lecture dollar : fort = pression baissière sur EUR/USD."""
        if self.change_pct <= -0.25:
            return "dollar faible"
        if self.change_pct >= 0.25:
            return "dollar fort"
        return "dollar neutre"

    def __str__(self) -> str:
        return f"{self.close:.2f} {self.change_pct:+.2f}% ({self.tone})"


def get_dxy() -> DxyInfo | None:
    """Dernier DXY + variation du jour (None si source indisponible)."""
    try:
        df = yf.download("DX-Y.NYB", period="10d", interval="1d",
                         progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].dropna()
        if len(close) < 2:
            return None
        last, prev = float(close.iloc[-1]), float(close.iloc[-2])
        return DxyInfo(close=last, change_pct=round((last / prev - 1) * 100, 2))
    except Exception as exc:  # noqa: BLE001 — le DXY ne doit jamais bloquer un cycle
        logger.warning("DXY indisponible : %s", exc)
        return None
