"""Façade haut niveau : données multi-timeframes + contrôle de fraîcheur.

Pourquoi une façade ? Le moteur de signaux a besoin, pour une même paire, de
H1 (structure), H4/D1 (contexte supérieur) en UN appel, avec deux garanties :

1. FRAÎCHEUR    : la dernière bougie reçue est bien récente. Subtilité
                  importante : « < 15 min » naïf est FAUX pour du H4/D1 —
                  une bougie journalière ne se clôture qu'une fois par 24 h.
                  La vérification est donc RELATIVE À LA CADENCE du timeframe :
                  âge_max = durée_bougie + 15 min (tolérance week-end incluse).
2. BOUGIES CLÔTURÉES : la bougie en formation est retirée AVANT l'analyse
                  (ses high/low/close ne sont pas définitifs).

YahooProvider reste la brique bas niveau (cache, retries, throttle) ;
DataFetcher est la seule classe que le moteur de signaux utilisera.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from .provider import Timeframe, timeframe_from_str
from .yahoo_provider import YahooProvider

logger = logging.getLogger(__name__)

#: Multi-timeframe par défaut demandé par la spec : H1, H4, D1.
DEFAULT_TIMEFRAMES = (Timeframe.H1, Timeframe.H4, Timeframe.D1)


@dataclass(frozen=True)
class FreshnessInfo:
    """Résultat du contrôle de fraîcheur d'un DataFrame de bougies."""

    fresh: bool
    age_minutes: float           # âge de la dernière bougie (formante incluse)
    max_expected_minutes: float  # cadence du timeframe + tolérance (+ week-end)
    reason: str = ""

    def __str__(self) -> str:
        state = "frais" if self.fresh else "PERIMÉ"
        return f"{state} (âge {self.age_minutes:.0f} min / max {self.max_expected_minutes:.0f} min)"


@dataclass
class MultiTimeframeData:
    """Bougies prêtes pour l'analyse, par timeframe, + état de fraîcheur."""

    pair: str
    frames: dict[Timeframe, pd.DataFrame] = field(default_factory=dict)
    freshness: dict[Timeframe, FreshnessInfo] = field(default_factory=dict)

    @property
    def all_fresh(self) -> bool:
        return all(info.fresh for info in self.freshness.values()) if self.freshness else False


# --------------------------------------------------------------------------- #
#  Fraîcheur : cadence du timeframe + tolérance + trous de week-end
# --------------------------------------------------------------------------- #
def _weekend_overlap(start: pd.Timestamp, end: pd.Timestamp) -> pd.Timedelta:
    """Temps de week-end Forex (ven 21:00 -> dim 21:00 UTC) entre deux dates.

    Sert à ne PAS déclarer « périmées » des données simplement parce que le
    marché était fermé samedi/dimanche.
    """
    if end <= start:
        return pd.Timedelta(0)
    total = timedelta(0)
    day = (start - timedelta(days=3)).normalize()  # revenir au vendredi précédent
    while day <= end:
        if day.weekday() == 4:  # vendredi
            weekend_start = day + timedelta(hours=21)
            weekend_end = day + timedelta(days=2, hours=21)
            total += max(timedelta(0), min(end, weekend_end) - max(start, weekend_start))
        day += timedelta(days=1)
    return pd.Timedelta(total)


def check_freshness(
    df: pd.DataFrame,
    timeframe: Timeframe,
    max_lag_minutes: float = 15.0,
    now: pd.Timestamp | None = None,
) -> FreshnessInfo:
    """Vérifie que les données sont fraîches, À CADENCE DU TIMEFRAME.

    ``df`` doit contenir la dernière bougie brute (formante incluse) : la
    fraîcheur se mesure sur la présence d'une bougie OUVERTE récemment.
    """
    now = now or pd.Timestamp.now(tz="UTC")
    if df.empty:
        return FreshnessInfo(False, float("inf"), 0.0, "aucune donnée")

    age = now - df.index[-1]                      # depuis l'ouverture de la dernière bougie
    tolerance = pd.Timedelta(minutes=max_lag_minutes)
    allowed = timeframe.delta + tolerance + _weekend_overlap(df.index[-1], now)

    info = FreshnessInfo(
        fresh=bool(age <= allowed),
        age_minutes=round(age.total_seconds() / 60, 1),
        max_expected_minutes=round(allowed.total_seconds() / 60, 1),
    )
    if not info.fresh:
        info = FreshnessInfo(
            info.fresh, info.age_minutes, info.max_expected_minutes,
            reason=f"dernière bougie il y a {info.age_minutes:.0f} min "
                   f"(attendu ≤ {info.max_expected_minutes:.0f} min) — source coupée ?",
        )
    return info


def keep_closed_only(df: pd.DataFrame, timeframe: Timeframe, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Retire la bougie encore en formation (même règle que YahooProvider)."""
    now = now or pd.Timestamp.now(tz="UTC")
    return df[df.index <= now - timeframe.delta]


# --------------------------------------------------------------------------- #
#  Façade
# --------------------------------------------------------------------------- #
class DataFetcher:
    """Point d'entrée unique du moteur pour les données de marché."""

    def __init__(self, provider: YahooProvider | None = None, config=None) -> None:
        """
        Args:
            provider: source bas niveau (sinon : YahooProvider construit
                      depuis config/settings.yaml si disponible).
            config:   objet Config (src.config) — optionnel.
        """
        if provider is None:
            provider = self._default_provider(config)
        self.provider = provider

    @staticmethod
    def _default_provider(config) -> YahooProvider:
        if config is not None:
            d = config.data
            return YahooProvider(
                min_request_interval=d.min_request_interval_seconds,
                max_retries=d.max_retries,
                retry_base_delay=d.retry_base_delay_seconds,
                max_candles=d.max_candles,
            )
        try:  # config auto-chargée si exécution depuis la racine du projet
            from src.config import load_config

            return DataFetcher._default_provider(load_config())
        except Exception:  # noqa: BLE001 — valeurs par défaut saines
            return YahooProvider()

    # ------------------------------------------------------------------ #
    def get_candles(
        self,
        pair: str,
        timeframe: Timeframe | str,
        lookback_days: int = 30,
        only_closed: bool = True,
    ) -> pd.DataFrame:
        """Accès direct à une paire / un timeframe (passe-plat du provider)."""
        tf = timeframe_from_str(timeframe) if isinstance(timeframe, str) else timeframe
        return self.provider.get_candles(pair, tf, lookback_days=lookback_days, only_closed=only_closed)

    def get_multi_timeframe_data(
        self,
        pair: str,
        timeframes: tuple[Timeframe | str, ...] = DEFAULT_TIMEFRAMES,
        lookback_days: int = 30,
        max_lag_minutes: float = 15.0,
    ) -> MultiTimeframeData:
        """Récupère H1/H4/D1 (défaut) pour une paire, fraîcheur vérifiée.

        Pour chaque timeframe :
          1. télécharge les bougies BRUTES (bougie formante incluse) -> fraîcheur
          2. retire la bougie formante -> DataFrame prêt pour l'analyse

        Returns:
            MultiTimeframeData (frames, freshness, all_fresh).
        """
        tfs = [timeframe_from_str(t) if isinstance(t, str) else t for t in timeframes]
        result = MultiTimeframeData(pair=pair.upper())

        for tf in tfs:
            try:
                raw = self.provider.get_candles(pair, tf, lookback_days=lookback_days, only_closed=False)
                if raw.empty:
                    logger.error("[%s %s] aucune donnée reçue", pair, tf.value)
                    result.frames[tf] = pd.DataFrame()
                    result.freshness[tf] = FreshnessInfo(False, float("inf"), 0.0, "aucune donnée")
                    continue
                result.freshness[tf] = check_freshness(raw, tf, max_lag_minutes)
                result.frames[tf] = keep_closed_only(raw, tf)
                if not result.freshness[tf].fresh:
                    logger.warning("[%s %s] données %s", pair, tf.value, result.freshness[tf])
            except Exception as exc:  # noqa: BLE001 — on continue avec les autres TF
                logger.error("[%s %s] échec : %s", pair, tf.value, exc)
                result.frames[tf] = pd.DataFrame()
                result.freshness[tf] = FreshnessInfo(False, float("inf"), 0.0, str(exc))

        logger.info(
            "[%s] multi-TF %s — fraîcheur : %s",
            pair,
            "/".join(tf.value for tf in tfs),
            "OK" if result.all_fresh else "ATTENTION (voir logs)",
        )
        return result
