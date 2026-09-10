"""ShadowTracker — clôture papier des signaux simulés + MAE/MFE (§28).

Méthode (alignée sur le tracker live, plus mesures §28) :
    - bougies M15 réelles postérieures à l'entrée ;
    - SL testé AVANT TP1 dans la même bougie (conservateur, cohérent live) ;
    - MAE = pire excursion ADVERSE en R (<= 0) ;
    - MFE = meilleure excursion FAVORABLE en R (>= 0) ;
    - time_to_exit en minutes ;
    - expiration après `expiry_hours` (défaut 24 h) -> EXPIRE au dernier close.
Papier uniquement : aucune notification, aucune action.
"""
from __future__ import annotations

import logging
import math

import pandas as pd

from ..data.data_fetcher import DataFetcher
from .shadow_recorder import ShadowDatabase

logger = logging.getLogger(__name__)


class ShadowTracker:
    """Clôture et mesure des signaux papier sur bougies réelles."""

    def __init__(self, db: ShadowDatabase, fetcher: DataFetcher,
                 expiry_hours: float = 24.0) -> None:
        self.db = db
        self.fetcher = fetcher
        self.expiry_hours = float(expiry_hours)

    def update_all(self, now: pd.Timestamp) -> list[dict]:
        resolutions = []
        for row in self.db.open_outcomes():
            try:
                res = self._resolve(row, now)
            except Exception as exc:  # noqa: BLE001 — jamais casser le cycle
                logger.warning("[shadow] outcome %s : %s", row["decision_id"], exc)
                continue
            if res:
                resolutions.append(res)
        if resolutions:
            logger.info("[shadow] %d clôture(s) papier(s) ce cycle",
                        len(resolutions))
        return resolutions

    # ------------------------------------------------------------------ #
    def _resolve(self, row, now: pd.Timestamp) -> dict | None:
        entry = float(row["entry"])
        sl, tp1 = float(row["sl"]), float(row["tp1"])
        long_side = row["direction"] == "LONG"
        entry_time = pd.Timestamp(row["ts_entry"])
        risk = abs(entry - sl) or 1e-9

        bars = self._bars_since(entry_time, now)
        if bars is None or bars.empty:
            return None

        mae = 0.0    # pire adverse (négatif)
        mfe = 0.0    # meilleure favorable (positif)
        status = exit_price = exit_time = None

        expiry_ts = entry_time + pd.Timedelta(hours=self.expiry_hours)
        for ts, bar in bars.iterrows():
            adverse = (bar["low"] - entry) if long_side else (entry - bar["high"])
            favorable = (bar["high"] - entry) if long_side else (entry - bar["low"])
            mae = min(mae, adverse / risk)
            mfe = max(mfe, favorable / risk)
            if status is None:
                hit_sl = bar["low"] <= sl if long_side else bar["high"] >= sl
                hit_tp = bar["high"] >= tp1 if long_side else bar["low"] <= tp1
                if hit_sl:                     # SL prioritaire (conservateur)
                    status, exit_price, exit_time = "SL", sl, ts
                elif hit_tp:
                    status, exit_price, exit_time = "TP", tp1, ts
                elif ts > expiry_ts:
                    status, exit_price, exit_time = "EXPIRE", float(bar["close"]), ts

        if status is None:
            # toujours ouvert : MAE/MFE partiels non persistés (on attend la fin)
            return None

        minutes = (pd.Timestamp(exit_time) - entry_time).total_seconds() / 60
        self.db.close_outcome(int(row["decision_id"]), status, exit_price,
                              exit_time, mae, mfe, minutes)
        return {"id": row["decision_id"], "status": status,
                "mae_r": round(mae, 2), "mfe_r": round(mfe, 2)}

    # ------------------------------------------------------------------ #
    def _bars_since(self, since: pd.Timestamp, now: pd.Timestamp):
        days = math.ceil((now - since).total_seconds() / 86_400) + 1
        lookback = max(1, min(days, 30))
        try:
            df = self.fetcher.get_candles("EURUSD", "15m", lookback_days=lookback)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[shadow] bougies indisponibles : %s", exc)
            return None
        return df[df.index > since]
