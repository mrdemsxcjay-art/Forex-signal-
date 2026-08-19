"""Calendrier économique ForexFactory — récupération + filtrage High/Medium.

DEUX chemins, du plus fiable au moins fiable :

1. FLUX JSON PUBLIC (chemin principal)
   https://nfs.faireconomy.media/ff_calendar_thisweek.json
   - c'est le flux officiel utilisé par le site ForexFactory lui-même
   - gratuit, sans clé API, format structuré, dates ISO 8601 avec offset
     (précision absolue sur les fuseaux horaires)

2. SCRAPING HTML BeautifulSoup (chemin de secours)
   https://www.forexfactory.com/calendar
   - déclenché automatiquement si le flux JSON tombe
   - plus fragile : la page est renderée en JavaScript et protégée par
     Cloudflare ; les heures affichées dépendent du fuseau du visiteur
     -> elles sont interprétées en UTC (approximation assumée, loggée)

Sortie standardisée (identique quel que soit le chemin) :
    heure     : Timestamp pandas UTC (heure de publication)
    devise    : "USD", "EUR", ...
    evenement : titre ("CPI m/m", ...)
    impact    : "High" | "Medium"
    actual    : valeur publiée ("" si pas encore sortie)
    forecast  : consensus
    previous  : valeur précédente
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

FF_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_HTML_URL = "https://www.forexfactory.com/calendar"

#: Colonnes garanties, dans cet ordre.
CALENDAR_COLUMNS = ["heure", "devise", "evenement", "impact", "actual", "forecast", "previous"]

#: Impacts conservés par scrape_forexfactory (spec : High + Medium).
KEPT_IMPACTS = ("High", "Medium")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec"


class EconomicCalendarError(RuntimeError):
    """Échec de récupération du calendrier (JSON ET scraping HTML)."""


# --------------------------------------------------------------------------- #
#  API publique
# --------------------------------------------------------------------------- #
def scrape_forexfactory(
    day: str = "today",
    include_medium: bool = True,
    timeout: int = 20,
    session: requests.Session | None = None,
    html_fallback: bool = True,
) -> pd.DataFrame:
    """Récupère les news ForexFactory, filtrées par impact et par jour.

    Args:
        day:            "today" (défaut, jour courant en UTC), "all" (toute la
                        semaine) ou une date "YYYY-MM-DD".
        include_medium: si False, ne garde que l'impact High.
        timeout:        timeout HTTP en secondes.
        session:        session requests injectable (tests / réutilisation).
        html_fallback:  si True, tente le scraping BeautifulSoup si le JSON échoue.

    Returns:
        DataFrame [heure, devise, evenement, impact, actual, forecast, previous],
        trié par heure croissante. Vide (mais correctement typé) si aucun event.
    """
    session = session or requests.Session()
    session.headers.update(_HEADERS)

    try:
        df = _calendar_from_json(session, timeout)
        source = "flux JSON officiel"
    except Exception as exc:  # noqa: BLE001 — on tente le repli avant d'échouer
        logger.warning(
            "Flux JSON ForexFactory indisponible (%s: %s) — repli scraping HTML",
            type(exc).__name__, exc,
        )
        if not html_fallback:
            raise
        df = _calendar_from_html(session, timeout)
        source = "scraping HTML BeautifulSoup (heures ≈ UTC)"

    df = _filter_day_and_impact(df, day=day, include_medium=include_medium)
    logger.info(
        "Calendrier ForexFactory [%s] : %d événement(s) %s pour %s",
        source, len(df), "/".join(KEPT_IMPACTS[: 2 if include_medium else 1]), day,
    )
    return df


# --------------------------------------------------------------------------- #
#  Chemin 1 : flux JSON officiel
# --------------------------------------------------------------------------- #
def _calendar_from_json(session: requests.Session, timeout: int) -> pd.DataFrame:
    resp = session.get(FF_JSON_URL, timeout=timeout)
    resp.raise_for_status()
    events = resp.json()
    if not isinstance(events, list):
        raise ValueError("réponse JSON inattendue (pas une liste)")

    records = []
    for ev in events:
        try:
            ts = pd.Timestamp(ev["date"])  # ISO 8601 avec offset timezone
        except (KeyError, TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        records.append({
            "heure": ts.tz_convert("UTC"),
            "devise": str(ev.get("country") or "").strip(),
            "evenement": str(ev.get("title") or "").strip(),
            "impact": str(ev.get("impact") or "").strip().title(),
            "actual": str(ev.get("actual") or "").strip(),
            "forecast": str(ev.get("forecast") or "").strip(),
            "previous": str(ev.get("previous") or "").strip(),
        })

    df = pd.DataFrame(records, columns=CALENDAR_COLUMNS)
    df["heure"] = pd.to_datetime(df["heure"], utc=True)
    return df.sort_values("heure", ignore_index=True)


# --------------------------------------------------------------------------- #
#  Chemin 2 : scraping HTML BeautifulSoup (repli)
# --------------------------------------------------------------------------- #
def _calendar_from_html(session: requests.Session, timeout: int) -> pd.DataFrame:
    resp = session.get(FF_HTML_URL, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    logger.warning(
        "Scraping HTML ForexFactory : les heures de la page dépendent du "
        "fuseau visiteur ; elles sont interprétées comme UTC (approximation)."
    )

    records: list[dict] = []
    current_day: datetime | None = None

    for tr in soup.select("tr.calendar__row"):
        # Ligne de changement de jour -> met à jour la date courante
        date_td = tr.select_one("td.calendar__date")
        if date_td is not None and date_td.get_text(strip=True):
            parsed = _parse_ff_date(date_td.get_text(" ", strip=True))
            if parsed is not None:
                current_day = parsed
            continue

        currency = tr.select_one("td.calendar__currency")
        if currency is None:  # lignes de séparation / en-têtes
            continue
        devise = currency.get_text(strip=True)
        if not devise:
            continue

        time_txt = ""
        time_td = tr.select_one("td.calendar__time")
        if time_td is not None:
            time_txt = time_td.get_text(strip=True)

        title_el = tr.select_one(".calendar__event-title") or tr.select_one("td.calendar__event")
        evenement = title_el.get_text(" ", strip=True) if title_el else ""

        records.append({
            "heure": _combine_day_time(current_day, time_txt),
            "devise": devise,
            "evenement": evenement,
            "impact": _impact_from_row(tr),
            "actual": _cell_text(tr, "actual"),
            "forecast": _cell_text(tr, "forecast"),
            "previous": _cell_text(tr, "previous"),
        })

    if not records:
        raise EconomicCalendarError(
            "scraping HTML ForexFactory : aucune ligne d'événement trouvée "
            "(structure de la page probablement modifiée)"
        )

    df = pd.DataFrame(records, columns=CALENDAR_COLUMNS)
    df = df.dropna(subset=["heure"])
    df["heure"] = pd.to_datetime(df["heure"], utc=True)
    return df.sort_values("heure", ignore_index=True)


def _cell_text(tr, name: str) -> str:
    el = tr.select_one(f"td.calendar__{name}") or tr.select_one(f"span.calendar__{name}")
    return el.get_text(strip=True) if el else ""


def _impact_from_row(tr) -> str:
    """Mappe l'icône colorée FF (red/ora/yel) vers le niveau d'impact."""
    span = tr.select_one("td.calendar__impact span")
    classes = " ".join(span.get("class", [])) if span else ""
    if "red" in classes:
        return "High"
    if "ora" in classes:
        return "Medium"
    if "yel" in classes:
        return "Low"
    return ""


def _parse_ff_date(text: str) -> datetime | None:
    """Parsé un libellé FF type 'Wed Aug 20' -> date (année courante ajustée)."""
    match = re.search(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(" + _MONTHS.replace(" ", "|") + r")\s+(\d{1,2})",
        text,
    )
    if not match:
        return None
    now = datetime.now(timezone.utc)
    day_name, month_abbr, day_num = match.groups()
    try:
        parsed = datetime.strptime(f"{day_name} {month_abbr} {day_num} {now.year}", "%a %b %d %Y")
    except ValueError:
        return None
    # Semaine à cheval sur le 1er janvier : le calendrier affiche l'année ~courante
    if (parsed - now.replace(tzinfo=None)) > timedelta(days=8):
        parsed = parsed.replace(year=parsed.year - 1)
    return parsed


def _combine_day_time(day: datetime | None, time_txt: str):
    """Fusionne date + '5:30pm' en Timestamp UTC (fuseau page ≈ UTC, voir note)."""
    if day is None:
        return None
    day = day.replace(tzinfo=None)
    if time_txt:
        try:
            t = datetime.strptime(time_txt.upper(), "%I:%M%p").time()
        except ValueError:
            t = datetime.min.time()
        return pd.Timestamp(datetime.combine(day, t), tz="UTC")
    return pd.Timestamp(day, tz="UTC")  # événement « toute la journée »


# --------------------------------------------------------------------------- #
#  Filtrage
# --------------------------------------------------------------------------- #
def _filter_day_and_impact(df: pd.DataFrame, day: str, include_medium: bool) -> pd.DataFrame:
    if df.empty:
        return df

    impacts = ["High", "Medium"] if include_medium else ["High"]
    df = df[df["impact"].isin(impacts)]

    if day != "all":
        if day == "today":
            target = pd.Timestamp.now(tz="UTC").date()
        else:
            target = pd.Timestamp(day).date()  # accepte "YYYY-MM-DD"
        df = df[df["heure"].dt.date == target]

    return df.sort_values("heure").reset_index(drop=True)
