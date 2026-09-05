"""SignalTracker — résolution automatique des issues (chantier 6.2).

Engagement du comité de pilotage : AUCUN signal ne reste `EN_COURS`
plus de 48 bougies M15 (12 h).

Méthode (identique aux conventions du backtester, conservatisme inclus) :
    - on marche les bougies M15 CLÔTURÉES postérieures au signal ;
    - SL testé AVANT TP dans la même bougie (prudence : on suppose le pire) ;
    - sans toucher après `expiry_bars` (48) -> EXPIRE, sortie à la clôture,
      R partiel calculé ;
    - chaque clôture met à jour SQLite ET notifie Telegram (R réalisé,
      solde cumulé).

Limite assumée (documentée en réunion) : l'entrée limite au bord de zone
n'est pas « remplie » explicitement ; le suivi démarre à l'heure du signal
(convention marché, conservatrice car le SL est prioritaire).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import pandas as pd

from ..data.data_fetcher import DataFetcher
from ..notifications.telegram import format_closure_message
from ..storage.database import SignalDatabase, _parse_created

logger = logging.getLogger(__name__)


class SignalTracker:
    """Clôture les signaux ouverts à partir des bougies réelles."""

    def __init__(
        self,
        db: SignalDatabase,
        fetcher: DataFetcher,
        telegram=None,
        expiry_bars: int = 48,
    ) -> None:
        self.db = db
        self.fetcher = fetcher
        self.telegram = telegram
        self.expiry_bars = int(expiry_bars)

    # ------------------------------------------------------------------ #
    def update_all(self, now: datetime | None = None) -> list[dict]:
        """Examine tous les signaux EN_COURS ; renvoie les clôtures effectuées."""
        now = now or datetime.now(timezone.utc)
        open_rows = self.db.open_signals()
        resolutions: list[dict] = []
        for _, row in open_rows.iterrows():
            try:
                outcome = self._resolve(row, now)
            except Exception as exc:  # noqa: BLE001 — un signal fragile n'arrête rien
                logger.error("[tracker] signal id=%s : %s", row["id"], exc)
                continue
            if outcome is not None:
                resolutions.append(outcome)
        if resolutions:
            logger.info("[tracker] %d signal(s) clôturé(s) ce cycle", len(resolutions))
        return resolutions

    # ------------------------------------------------------------------ #
    def _resolve(self, row, now: datetime) -> dict | None:
        signal_id = int(row["id"])
        pair = str(row["paire"])
        long_side = str(row["type"]) == "LONG"
        entry, sl, tp = float(row["entree"]), float(row["sl"]), float(row["tp"])
        entry_time = _parse_created(row["date"])

        bars = self._bars_since(pair, entry_time, now)
        if bars is None or bars.empty:
            return None  # pas encore de données postérieures

        risk = abs(entry - sl) or 1e-9
        rr_target = float(row["rr"] or 2.0)

        resultat = exit_price = exit_time = None
        exit_r = 0.0

        for i, (ts, bar) in enumerate(bars.iterrows()):
            if i >= self.expiry_bars:
                break
            if long_side:
                if bar["low"] <= sl:      # SL prioritaire (conservateur)
                    resultat, exit_price, exit_time, exit_r = "SL_ATTEINT", sl, ts, -1.0
                    break
                if bar["high"] >= tp:
                    resultat, exit_price, exit_time, exit_r = "TP_ATTEINT", tp, ts, rr_target
                    break
            else:
                if bar["high"] >= sl:
                    resultat, exit_price, exit_time, exit_r = "SL_ATTEINT", sl, ts, -1.0
                    break
                if bar["low"] <= tp:
                    resultat, exit_price, exit_time, exit_r = "TP_ATTEINT", tp, ts, rr_target
                    break

        if resultat is None:
            if len(bars) >= self.expiry_bars:  # expiration à la clôture
                last = bars.iloc[self.expiry_bars - 1]
                ts = bars.index[self.expiry_bars - 1]
                exit_price = float(last["close"])
                exit_r = ((exit_price - entry) / risk) if long_side else ((entry - exit_price) / risk)
                resultat, exit_time = "EXPIRE", ts
            else:
                return None  # toujours en cours, rien à faire

        self.db.update_result(
            signal_id, resultat,
            exit_price=round(float(exit_price), 6),
            exit_r=round(float(exit_r), 3),
            exit_time=str(exit_time),
        )
        resolution = {
            "id": signal_id, "paire": pair, "type": row["type"], "resultat": resultat,
            "exit_price": round(float(exit_price), 6), "exit_r": round(float(exit_r), 3),
        }
        logger.info(
            "[tracker] %s %s #%d -> %s (%+.1fR)",
            pair, row["type"], signal_id, resultat, exit_r,
        )
        if self.telegram is not None:
            try:
                from ..fundamental.dxy import get_dxy

                dxy = get_dxy()
                self.telegram.send_text(
                    format_closure_message(resolution, row, self.db.stats(),
                                           dxy_txt=str(dxy) if dxy else "")
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[tracker] notification de clôture impossible : %s", exc)
        return resolution

    # ------------------------------------------------------------------ #
    def _bars_since(self, pair: str, since: datetime, now: datetime):
        """Bougies M15 clôturées strictement postérieures au signal."""
        days = math.ceil((now - since).total_seconds() / 86_400) + 1
        lookback = max(1, min(days, 30))
        try:
            df = self.fetcher.get_candles(pair, "15m", lookback_days=lookback)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tracker] %s : bougies indisponibles (%s)", pair, exc)
            return None
        since_ts = pd.Timestamp(since)
        return df[df.index > since_ts]
