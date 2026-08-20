"""Source de données Yahoo Finance via yfinance — 100 % gratuite, sans clé API.

Points clés de l'implémentation (optimisation temps réel) :

1. CACHE INCRÉMENTAL      le 1er appel télécharge l'historique complet ;
                          les suivants ne téléchargent QUE les bougies manquantes
                          (payload minimal -> rapide, discret pour Yahoo).
2. RECONNEXION AUTO       chaque téléchargement est protégé par des retries à
                          backoff exponentiel + jitter (délai aléatoire) pour
                          absorber les coupures réseau et les erreurs 429.
3. DÉGRADATION GRACIEUSE  si le refresh incrémental échoue mais qu'un cache
                          existe, on sert les dernières données connues
                          (marquées WARNING) au lieu de faire planter le moteur.
4. THROTTLE               intervalle minimal entre requêtes HTTP réelles
                          (anti rate-limit Yahoo).
5. FILTRE ANTI-BRUIT      suppression NaN / bougies incohérentes (high < low),
                          dédoublonnage, tri, dtypes float64, mémoire bornée.
6. THREAD-SAFE            un verrou protège cache + throttle.

Note XAUUSD : Yahoo ne fournit pas l'or spot ("XAUUSD=X" inexistant) ;
on utilise le future COMEX "GC=F" qui le suit à quelques dollars près —
largement suffisant pour l'analyse de signaux.
"""
from __future__ import annotations

import logging
import random
import threading
import time

import pandas as pd
import yfinance as yf

from .provider import (
    STANDARD_COLUMNS,
    DataProviderError,
    MarketDataProvider,
    Timeframe,
    resample_candles,
)

logger = logging.getLogger(__name__)

#: Paires supportées -> symboles Yahoo Finance.
#: "GC=F" = future or COMEX (proxy officiel gratuit de XAUUSD sur Yahoo).
YF_SYMBOLS: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "XAUUSD": "GC=F",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "AUDJPY": "AUDJPY=X",
    "EURGBP": "EURGBP=X",
}


class YahooProvider(MarketDataProvider):
    """Implémentation ``MarketDataProvider`` sur Yahoo Finance."""

    def __init__(
        self,
        min_request_interval: float = 0.5,
        max_retries: int = 4,
        retry_base_delay: float = 1.0,
        max_candles: int = 3000,
    ) -> None:
        self._min_interval = float(min_request_interval)
        self._max_retries = max(1, int(max_retries))
        self._base_delay = float(retry_base_delay)
        self._max_candles = int(max_candles)
        # cache[(pair, timeframe)] -> DataFrame standardisé
        self._cache: dict[tuple[str, Timeframe], pd.DataFrame] = {}
        self._last_request_at = 0.0          # pour le throttle (monotonic)
        self._lock = threading.Lock()        # moteur multi-fils + dashboard

    # ------------------------------------------------------------------ #
    #  API publique
    # ------------------------------------------------------------------ #
    @property
    def supported_pairs(self) -> list[str]:
        return sorted(YF_SYMBOLS)

    def get_candles(
        self,
        pair: str,
        timeframe: Timeframe,
        lookback_days: int | None = 30,
        only_closed: bool = True,
    ) -> pd.DataFrame:
        symbol = YF_SYMBOLS.get(pair.upper())
        if symbol is None:
            raise ValueError(
                f"Paire '{pair}' non supportée par YahooProvider. "
                f"Disponibles : {self.supported_pairs}"
            )
        if not isinstance(timeframe, Timeframe):
            raise TypeError(f"timeframe doit être un Timeframe, reçu : {timeframe!r}")

        with self._lock:
            key = (pair.upper(), timeframe)
            cached = self._cache.get(key)

            if cached is None or len(cached) == 0:
                # 1er appel : historique complet
                df = self._download(symbol, timeframe, lookback_days=int(lookback_days or 30))
                logger.info(
                    "[%s %s] historique initial chargé : %d bougies (%s)",
                    pair, timeframe.value, len(df), symbol,
                )
            else:
                # Appels suivants : rafraîchissement incrémental
                df = self._refresh_incremental(pair, timeframe, symbol, cached)

            df = self._post_process(df, timeframe, only_closed=only_closed)
            self._cache[key] = df
            # copie défensive : l'appelant ne peut pas corrompre le cache
            return df.copy()

    def clear_cache(self, pair: str | None = None) -> None:
        with self._lock:
            if pair is None:
                self._cache.clear()
            else:
                for key in [k for k in self._cache if k[0] == pair.upper()]:
                    self._cache.pop(key)

    # ------------------------------------------------------------------ #
    #  Téléchargement + reconnexion automatique
    # ------------------------------------------------------------------ #
    def _download(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        lookback_days: int | None = None,
        start: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Télécharge des bougies AVEC reconnexion automatique.

        Boucle de retries à backoff exponentiel + jitter autour de
        ``_fetch_once`` (l'appel réseau réel). Si toutes les tentatives
        échouent -> ``DataProviderError`` (et le caller décide : cache ou crash).

        Args:
            symbol:        symbole Yahoo (ex. "EURUSD=X").
            timeframe:     unité de temps.
            lookback_days: utilisé si start est None (mode historique).
            start:         date de début du téléchargement (mode incrémental).
        """
        end = pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=1)
        if start is None:
            start = end - pd.Timedelta(days=int(lookback_days or 30))

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            self._throttle()
            try:
                return self._fetch_once(symbol, timeframe, start, end)
            except Exception as exc:  # noqa: BLE001 — réseau : tout capturer
                last_exc = exc
                if attempt < self._max_retries:
                    delay = self._base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    logger.warning(
                        "[%s %s] tentative %d/%d échouée (%s) — reconnexion dans %.1fs",
                        symbol, timeframe.value, attempt, self._max_retries,
                        type(exc).__name__, delay,
                    )
                    time.sleep(delay)

        raise DataProviderError(
            f"Échec du téléchargement {symbol} {timeframe.value} "
            f"après {self._max_retries} tentatives : {last_exc}"
        )

    def _fetch_once(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        """UN appel réseau réel vers Yahoo (sans retry — voir _download).

        H4 : Yahoo/yfinance l'accepte mais ancre les bacs sur SA timezone
        d'échange (minuit Londres pour le FX -> bacs 23:00/03:00 UTC).
        On télécharge donc du H1 et on ré-échantillonne nous-mêmes sur la
        grille déterministe 00/04/08... UTC (resample_candles, origin=epoch).
        """
        fetch_tf = timeframe.base_timeframe or timeframe
        raw = yf.download(
            symbol,
            interval=fetch_tf.value,
            start=start,
            end=end,
            progress=False,
            auto_adjust=False,   # pas d'ajustement de dividendes en FX
            actions=False,
            threads=False,
        )
        if raw is None or raw.empty:
            # Réponse vide = symbole invalide OU aucune nouvelle bougie
            # (marché fermé) : laissé à l'appelant pour trancher.
            return pd.DataFrame()
        df = self._normalize(raw, symbol)
        if timeframe.base_timeframe is not None:
            df = resample_candles(df, timeframe.resample_rule)
        return df

    def _refresh_incremental(
        self,
        pair: str,
        timeframe: Timeframe,
        symbol: str,
        cached: pd.DataFrame,
    ) -> pd.DataFrame:
        """Ne télécharge que les bougies postérieures au cache (+ la dernière,
        volontairement re-téléchargée car elle a pu évoluer depuis)."""
        start = cached.index[-1] - timeframe.delta  # marge d'une bougie
        try:
            fresh = self._download(symbol, timeframe, start=start)
        except DataProviderError:
            # DÉGRADATION GRACIEUSE : on continue avec les dernières données
            # connues plutôt que de faire planter la boucle temps réel.
            logger.warning(
                "[%s %s] refresh impossible — données potentiellement périmées servies depuis le cache",
                pair, timeframe.value,
            )
            return cached

        if fresh is None or fresh.empty:
            # Marché fermé (week-end) ou aucune nouvelle bougie : normal.
            logger.debug("[%s %s] aucune nouvelle bougie", pair, timeframe.value)
            return cached

        merged = pd.concat([cached, fresh])
        # keep="last" : la dernière version d'une bougie (mise à jour partielle) gagne
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        logger.debug(
            "[%s %s] +%d bougie(s) nouvelles (total %d)",
            pair, timeframe.value, len(fresh), len(merged),
        )
        return merged

    # ------------------------------------------------------------------ #
    #  Normalisation + contrôle qualité
    # ------------------------------------------------------------------ #
    def _normalize(self, raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Ramène n'importe quelle réponse Yahoo au format standard du projet."""
        df = raw.copy()

        # yfinance peut renvoyer des colonnes MultiIndex (selon versions)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.rename(columns=str.lower)
        if "adj close" in df.columns:
            df = df.drop(columns=["adj close"])

        # Garantit les 5 colonnes (volume absent chez certaines sources)
        for col in STANDARD_COLUMNS:
            if col not in df.columns:
                df[col] = 0.0
        df = df[STANDARD_COLUMNS].astype("float64")

        # Index -> DatetimeIndex UTC nommé "datetime"
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        idx.name = "datetime"
        df.index = idx

        return df[~df.index.duplicated(keep="last")].sort_index()

    def _post_process(
        self,
        df: pd.DataFrame,
        timeframe: Timeframe,
        only_closed: bool,
    ) -> pd.DataFrame:
        """Nettoyage final : NaN, bougies absurdes, bougie en formation, mémoire."""
        if df.empty:
            return df

        df = df.dropna(subset=["open", "high", "low", "close"])

        # Cohérence OHLC : high doit dominer, low doit être dominé
        body_max = df[["open", "close", "low"]].max(axis=1)
        body_min = df[["open", "close", "high"]].min(axis=1)
        sane = (df["high"] >= body_max - 1e-9) & (df["low"] <= body_min + 1e-9)
        if int((~sane).sum()) > 0:
            logger.warning(
                "%d bougie(s) incohérente(s) supprimée(s) (%s)",
                int((~sane).sum()), timeframe.value,
            )
            df = df[sane]

        # Mémoire bornée : on ne garde que les max_candles dernières bougies
        df = df.iloc[-self._max_candles:]

        # Bougie ENCORE EN FORMATION : une bougie ouverte à t se clôture à t+delta.
        # On ne l'envoie pas à l'analyse (ses high/low/close ne sont pas définitifs).
        if only_closed:
            now = pd.Timestamp.now(tz="UTC")
            df = df[df.index <= now - timeframe.delta]

        return df

    # ------------------------------------------------------------------ #
    #  Throttle anti rate-limit
    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        """Espace les requêtes HTTP réelles d'au moins _min_interval secondes."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()
