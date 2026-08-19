"""Analyse fondamentale : surprises macro -> sentiment par devise.

Principe quant classique :
    surprise = actual - forecast
    -> une statistique MEILLEURE que le consensus soutient la devise
       (anticipation de taux plus hauts = devise plus attractive),
    -> une statistique DÉCEVANTE l'affaiblit.

EXCEPTIONS (« indicateurs inversés ») : pour le chômage, les demandes
d'allocations et les déficits, une valeur PLUS HAUTE que prévu AFFAIBLIT
la devise (plus de chômage / plus de déficit = mauvais).

NLP léger (regex) : les titres ForexFactory sont des libellés génériques
("CPI m/m") qui ne changent pas selon le résultat — le lexique ne sert que
de DÉPARTAGE pour les news sans chiffres (discours, votes...). VADER (NLTK)
aurait imposé un téléchargement de lexique au premier lancement : lourd et
non déterministe en environnement restreint, donc regex volontaire.

Sorties :
    get_currency_sentiment() -> BULLISH / BEARISH / NEUTRAL par devise
    get_pair_bias()          -> biais d'une paire (base vs quote)
    is_high_impact_soon()    -> True si news rouge imminente (défaut 60 min)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .economic_calendar import CALENDAR_COLUMNS, KEPT_IMPACTS, scrape_forexfactory

logger = logging.getLogger(__name__)

#: Titres contenant ces mots -> indicateur INVERSÉ (haut = mauvais pour la devise).
INVERTED_PATTERNS = (
    "unemployment",
    "jobless",
    "claims",
    "deficit",
)

#: Lexique NLP regex (départage des news sans chiffres).
_BULLISH_RE = re.compile(
    r"\b(beat|beats|better|strong(er)?|surplus|growth|expand|hawkish|rally|"
    r"record|upgrade|improv\w*|hausse|progression|excédent)\b",
    re.IGNORECASE,
)
_BEARISH_RE = re.compile(
    r"\b(miss(es)?|worse|weak(er)?|deficit|contract|dovish|slump|recession|"
    r"plunge|downgrade|falls?|drops?|baisse|recul|faiblesse|récession)\b",
    re.IGNORECASE,
)

_SENTIMENT_LABELS = ("BULLISH", "BEARISH", "NEUTRAL")


@dataclass(frozen=True)
class SentimentResult:
    """Sentiment d'une devise, avec le détail des facteurs."""

    currency: str
    score: float                 # > 0 haussier, < 0 baissier
    label: str                   # BULLISH / BEARISH / NEUTRAL
    events_used: int
    drivers: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        arrows = {"BULLISH": "▲", "BEARISH": "▼", "NEUTRAL": "="}
        return f"{self.currency} {arrows.get(self.label, '?')} {self.label} ({self.score:+.1f})"


@dataclass(frozen=True)
class PairBias:
    """Biais fondamental d'une paire (ex. EURUSD = force EUR - force USD)."""

    pair: str
    label: str
    score: float
    base: SentimentResult | None
    quote: SentimentResult | None


class FundamentalAnalyzer:
    """Compare actual vs forecast et en déduit un sentiment par devise."""

    def __init__(
        self,
        calendar: pd.DataFrame | None = None,
        cache_ttl_minutes: int = 15,
        sentiment_threshold: float = 1.2,
        soon_default_minutes: int = 60,
        fetch_on_init: bool = True,
    ) -> None:
        """
        Args:
            calendar:             calendrier injecté (tests/synthetic).
                                  Sinon : scrape automatique de la semaine.
            cache_ttl_minutes:    durée de validité du calendrier auto-scrapé.
            sentiment_threshold:  |score| minimal pour BULLISH/BEARISH.
            soon_default_minutes: fenêtre par défaut de is_high_impact_soon.
        """
        self._calendar = calendar
        self._fetched_at: pd.Timestamp | None = None
        self._ttl = pd.Timedelta(minutes=cache_ttl_minutes)
        self._threshold = float(sentiment_threshold)
        self.soon_default_minutes = int(soon_default_minutes)
        if calendar is None and fetch_on_init:
            self.ensure_calendar(force=True)

    # ------------------------------------------------------------------ #
    #  Calendrier
    # ------------------------------------------------------------------ #
    def ensure_calendar(self, force: bool = False) -> pd.DataFrame:
        """Retourne le calendrier de la semaine, re-scrapé si périmé.

        Le format « toute la semaine » est volontaire : le sentiment regarde
        les news déjà publiées d'aujourd'hui, is_high_impact_soon regarde
        celles à venir — même source pour les deux.
        """
        stale = (
            self._fetched_at is None
            or (pd.Timestamp.now(tz="UTC") - self._fetched_at) > self._ttl
        )
        if self._calendar is None or None in self._calendar.columns or force or (
            self._fetched_at is not None and stale
        ):
            try:
                self._calendar = scrape_forexfactory(day="all", include_medium=True)
                self._fetched_at = pd.Timestamp.now(tz="UTC")
            except Exception as exc:  # noqa: BLE001 — le fondamental ne doit jamais tuer le moteur
                logger.error("Calendrier indisponible : %s — analyse fondamental désactivée ce cycle", exc)
                if self._calendar is None:
                    self._calendar = pd.DataFrame(columns=CALENDAR_COLUMNS)
        return self._calendar

    @property
    def calendar(self) -> pd.DataFrame:
        return self._calendar if self._calendar is not None else pd.DataFrame(columns=CALENDAR_COLUMNS)

    # ------------------------------------------------------------------ #
    #  Sentiment par devise
    # ------------------------------------------------------------------ #
    def get_currency_sentiment(
        self,
        currencies: list[str] | None = None,
        now: pd.Timestamp | None = None,
    ) -> dict[str, SentimentResult]:
        """Score le sentiment de chaque devise sur les news DU JOUR publiées.

        Score = Σ (direction × poids_impact × force_surprise × fraîcheur)
            poids_impact : High=2.0, Medium=1.0
            force_surprise : |actual-forecast|/|forecast|, saturée à 10 % de beat
            fraîcheur     : ≤4 h → 1.0, ≤12 h → 0.6, sinon 0.3
        """
        now = now or pd.Timestamp.now(tz="UTC")
        cal = self.ensure_calendar()
        if currencies is None:
            currencies = sorted(cal["devise"].unique()) if not cal.empty else []

        results: dict[str, SentimentResult] = {}
        for cur in currencies:
            cur = str(cur).upper()
            events = cal[
                (cal["devise"] == cur)
                & cal["impact"].isin(KEPT_IMPACTS)
                & (cal["heure"] <= now)
                & (cal["heure"].dt.date == now.date())
            ]

            score = 0.0
            drivers: list[str] = []
            headline_only: list[pd.Series] = []

            for _, row in events.iterrows():
                contrib, reason = self._contribution(row, now)
                if contrib != 0.0:
                    score += contrib
                    drivers.append(reason)
                elif row["impact"] == "High" and not row["actual"]:
                    headline_only.append(row)  # news rouge sans chiffres -> départage NLP

            # Départage NLP : uniquement s'il n'y a AUCUN signal chiffré
            if score == 0.0 and not drivers:
                for row in headline_only:
                    hl = self.score_headline(str(row["evenement"]))
                    if hl != 0.0:
                        weight = 2.0 if row["impact"] == "High" else 1.0
                        contrib = hl * weight
                        score += contrib
                        drivers.append(
                            f"{row['evenement']}: titre {'haussier' if hl > 0 else 'baissier'} (NLP)"
                        )

            label = (
                "BULLISH" if score >= self._threshold
                else "BEARISH" if score <= -self._threshold
                else "NEUTRAL"
            )
            results[cur] = SentimentResult(
                currency=cur, score=round(score, 2), label=label,
                events_used=int(len(events)), drivers=drivers[:5],
            )
        return results

    def get_pair_bias(
        self, pair: str, now: pd.Timestamp | None = None
    ) -> PairBias:
        """Biais d'une paire : force de la devise de base - celle de la cote.

        Cas XAUUSD (or coté en USD) : l'or monte quand le USD baisse ->
        score = -score(USD).
        """
        pair = pair.upper()
        base, quote = pair[:3], pair[3:]
        sentiments = self.get_currency_sentiment(
            [c for c in (base, quote) if c != "XAU"], now=now
        )
        s_base = sentiments.get(base)
        s_quote = sentiments.get(quote)

        if base == "XAU":
            score = -(s_quote.score if s_quote else 0.0)
            detail = "or ≈ opposé du USD"
        else:
            score = (s_base.score if s_base else 0.0) - (s_quote.score if s_quote else 0.0)
            detail = f"{base} − {quote}"

        label = (
            "BULLISH" if score >= self._threshold
            else "BEARISH" if score <= -self._threshold
            else "NEUTRAL"
        )
        return PairBias(pair=pair, label=label, score=round(score, 2), base=s_base, quote=s_quote)

    # ------------------------------------------------------------------ #
    #  News rouge imminente
    # ------------------------------------------------------------------ #
    def is_high_impact_soon(
        self,
        currency: str | None = None,
        within_minutes: int | None = None,
        now: pd.Timestamp | None = None,
    ) -> bool:
        """True si une news à fort impact sort dans les N prochaines minutes.

        Args:
            currency:       code devise à surveiller ("USD"), ou None = toutes.
            within_minutes: fenêtre (défaut : 60, configurable __init__).
        """
        window = int(within_minutes if within_minutes is not None else self.soon_default_minutes)
        now = now or pd.Timestamp.now(tz="UTC")
        cal = self.ensure_calendar()
        if cal.empty:
            return False

        mask = (
            (cal["impact"] == "High")
            & (cal["heure"] > now)
            & (cal["heure"] <= now + pd.Timedelta(minutes=window))
        )
        if currency is not None:
            mask &= cal["devise"] == str(currency).upper()
        return bool(mask.any())

    def next_high_impact_events(
        self,
        currency: str | None = None,
        limit: int = 8,
        now: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Prochaines news rouges (pour affichage dashboard / message Telegram)."""
        now = now or pd.Timestamp.now(tz="UTC")
        cal = self.ensure_calendar()
        if cal.empty:
            return pd.DataFrame(columns=CALENDAR_COLUMNS + ["dans_min"])

        upcoming = cal[(cal["impact"] == "High") & (cal["heure"] > now)].copy()
        if currency is not None:
            upcoming = upcoming[upcoming["devise"] == str(currency).upper()]
        if upcoming.empty:
            return pd.DataFrame(columns=CALENDAR_COLUMNS + ["dans_min"])

        upcoming["dans_min"] = (
            (upcoming["heure"] - now).dt.total_seconds() / 60
        ).round(0).astype(int)
        return upcoming.sort_values("heure").head(limit).reset_index(drop=True)

    # ------------------------------------------------------------------ #
    #  Brique : contribution d'une news au score
    # ------------------------------------------------------------------ #
    def _contribution(self, row: pd.Series, now: pd.Timestamp) -> tuple[float, str]:
        """Score d'une news publiée : (contribution, raison lisible)."""
        actual = self.parse_number(row["actual"])
        forecast = self.parse_number(row["forecast"])
        if actual is None or forecast is None:
            return 0.0, ""  # pas encore publié OU pas de consensus -> départage NLP

        diff = actual - forecast
        if forecast == 0.0:
            ratio = 0.0 if diff == 0 else 1.0
        else:
            ratio = abs(diff) / abs(forecast)
        if ratio < 0.01:
            return 0.0, f"{row['evenement']}: conforme au consensus"

        strength = min(1.0, ratio / 0.10)  # 10 % de surprise = force maximale
        inverted = any(p in str(row["evenement"]).lower() for p in INVERTED_PATTERNS)
        direction = 1.0
        if diff < 0:
            direction = -1.0
        if inverted:
            direction *= -1.0

        weight = 2.0 if row["impact"] == "High" else 1.0
        hours = (now - row["heure"]).total_seconds() / 3600
        recency = 1.0 if hours <= 4 else (0.6 if hours <= 12 else 0.3)

        score = direction * weight * strength * recency
        sens = "haussier" if score > 0 else "baissier"
        kind = "inversé" if inverted else "standard"
        reason = (
            f"{row['evenement']}: {row['actual']} vs prév {row['forecast']} "
            f"({kind}) → {sens}"
        )
        return score, reason

    # ------------------------------------------------------------------ #
    #  Utilitaires (publics : réutilisés par les tests)
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_number(value) -> float | None:
        """Convertit '312K', '0.51%', '$-12.3B', '1,234' en float (ou None)."""
        if value is None:
            return None
        s = str(value).strip()
        s = (
            s.replace("\u00a0", "").replace(",", "").replace("$", "")
            .replace("€", "").replace("£", "").replace("%", "")
            .replace("<", "").replace(">", "")
        )
        if s in ("", "-", "—", "N/A", "TBA"):
            return None
        multipliers = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
        mult = 1.0
        if s and s[-1].upper() in multipliers:
            mult = multipliers[s[-1].upper()]
            s = s[:-1]
        try:
            return float(s) * mult
        except ValueError:
            return None

    @staticmethod
    def score_headline(title: str) -> float:
        """NLP regex sur un titre : +1 haussier, -1 baissier, 0 neutre.

        Un seul mot-clé non ambigu suffit à saturer (les titres macro sont
        courts : « hawkish », « récession »...) -> division par 1.
        """
        if not title:
            return 0.0
        bull = len(_BULLISH_RE.findall(title))
        bear = len(_BEARISH_RE.findall(title))
        if bull == bear:
            return 0.0
        sign = 1.0 if bull > bear else -1.0
        return sign * min(1.0, abs(bull - bear) / 1)
