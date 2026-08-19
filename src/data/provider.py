"""Interface commune des sources de données de marché (couche abstraction).

Le moteur de signaux ne connaît QUE ``MarketDataProvider`` : il ne sait pas
d'où viennent les bougies. Changer de source gratuite (Yahoo -> OANDA démo,
TwelveData...) = écrire une nouvelle classe fille, zéro modification ailleurs.

Contrat de données (format DataFrame pandas standardisé) :
    - index      : DatetimeIndex **UTC**, nommé "datetime" = heure d'OUVERTURE
                   de la bougie, trié croissant, sans doublons
    - colonnes   : "open" | "high" | "low" | "close" | "volume"  (float64)
    - volume     : 0.0 pour le Forex (pas de volume centralisé, c'est normal)

Timeframes :
    M5, M15, H1 natifs sur Yahoo ; D1 natif ; **H4 n'existe pas sur Yahoo** ->
    il est construit par ré-échantillonnage de H1 (voir Timeframe.base_timeframe
    et la fonction resample_candles).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

import pandas as pd

#: Colonnes garanties dans cet ordre exact par toutes les sources.
STANDARD_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataProviderError(RuntimeError):
    """Erreur de source de données : réseau épuisé après retries, symbole invalide..."""


class Timeframe(Enum):
    """Timeframes supportés (valeur = intervalle natif Yahoo Finance)."""

    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"   # non natif Yahoo : téléchargé en H1 puis ré-échantillonné
    D1 = "1d"

    @property
    def delta(self) -> pd.Timedelta:
        """Durée de la bougie (ex. M15 -> 15 minutes)."""
        return _TIMEFRAME_DELTAS[self]

    @property
    def base_timeframe(self) -> "Timeframe | None":
        """Timeframe réellement téléchargé si ré-échantillonnage nécessaire.

        None pour les timeframes natifs ; Timeframe.H1 pour H4.
        """
        return _RESAMPLE_BASE.get(self)

    @property
    def resample_rule(self) -> str | None:
        """Règle pandas de ré-échantillonnage (None si natif)."""
        return _RESAMPLE_RULE.get(self)

    def __str__(self) -> str:  # affichage lisible dans les logs
        return self.value


_TIMEFRAME_DELTAS = {
    Timeframe.M5: pd.Timedelta(minutes=5),
    Timeframe.M15: pd.Timedelta(minutes=15),
    Timeframe.H1: pd.Timedelta(hours=1),
    Timeframe.H4: pd.Timedelta(hours=4),
    Timeframe.D1: pd.Timedelta(hours=24),
}

#: Timeframe H4 -> téléchargé en H1 puis ré-échantillonné en barres de 4 h.
_RESAMPLE_BASE: dict["Timeframe", "Timeframe"] = {Timeframe.H4: Timeframe.H1}
_RESAMPLE_RULE: dict["Timeframe", str] = {Timeframe.H4: "4h"}

#: Alias acceptés par Timeframe.from_str (tolérance M15/m15/15min/240m...)
_TIMEFRAME_ALIASES = {
    "5m": Timeframe.M5, "m5": Timeframe.M5, "5min": Timeframe.M5,
    "15m": Timeframe.M15, "m15": Timeframe.M15, "15min": Timeframe.M15,
    "1h": Timeframe.H1, "h1": Timeframe.H1, "60m": Timeframe.H1,
    "4h": Timeframe.H4, "h4": Timeframe.H4, "240m": Timeframe.H4,
    "1d": Timeframe.D1, "d1": Timeframe.D1, "daily": Timeframe.D1, "day": Timeframe.D1,
}


def timeframe_from_str(value: str) -> Timeframe:
    """Convertit une chaîne ("15m", "H4", "1d"...) en Timeframe.

    Raises:
        ValueError: chaîne inconnue (la liste des valides est listée).
    """
    tf = _TIMEFRAME_ALIASES.get(str(value).strip().lower())
    if tf is None:
        raise ValueError(
            f"Timeframe inconnu '{value}'. Valides : "
            f"{sorted(set(t.value for t in Timeframe))}"
        )
    return tf


def resample_candles(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Ré-échantillonne des bougies OHLCV vers une unité supérieure.

    Règles d'agrégation OHLC (les seules correctes) :
        open = première, high = max, low = min, close = dernière, volume = somme
    Alignement : label="left", closed="left" -> l'index reste l'heure
    d'OUVERTURE du bac, conforme au contrat du module.

    Les bacs incomplets en fin de série (ex. 2 h sur 4 pour H4) sont conservés
    tels quels : c'est la bougie « en formation », filtrée plus haut par
    only_closed si nécessaire.

    ANCRAGE DÉTERMINISTE : origin="epoch" -> bacs toujours alignés sur la
    grille 00:00/04:00/08:00... UTC, quel que soit le fuseau natif de la
    source (Yahoo renvoie du H1 en heure de Londres !) ou le premier élément.
    Sans cela, deux ré-échantillonnages de la même série peuvent produire
    des grilles décalées — inacceptable pour comparer des zones SMC.
    """
    if df.empty:
        return df.copy()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(rule, label="left", closed="left", origin="epoch").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])


class MarketDataProvider(ABC):
    """Contrat que toute source de données doit implémenter."""

    @abstractmethod
    def get_candles(
        self,
        pair: str,
        timeframe: Timeframe,
        lookback_days: int | None = None,
        only_closed: bool = True,
    ) -> pd.DataFrame:
        """Renvoie les bougies au format standard (voir docstring module).

        Args:
            pair:          paire en écriture simple, ex. "EURUSD", "XAUUSD".
            timeframe:     Timeframe.M5 / M15 / H1 / H4 / D1.
            lookback_days: historique souhaité en jours (au 1er appel seulement).
            only_closed:   si True (défaut), retire la bougie en cours de formation
                           — indispensable pour une analyse fiable.
        """

    @abstractmethod
    def clear_cache(self, pair: str | None = None) -> None:
        """Vide le cache (tout, ou une seule paire). Utile en reconnexion forcée."""

    @property
    @abstractmethod
    def supported_pairs(self) -> list[str]:
        """Paires réellement disponibles auprès de cette source."""
