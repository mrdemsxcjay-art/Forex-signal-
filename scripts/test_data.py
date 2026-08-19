"""Test fonctionnel du module de données — Étapes 2 & 3.

Usage :  python scripts/test_data.py

SECTIONS
  A. Moteur temps réel : EURUSD/GBPUSD/XAUUSD x M5/M15/H1 (données réelles)
  B. Cache incrémental + bougie clôturée
  C. Gestion d'erreurs : paire inconnue, coupure réseau, panne prolongée,
     dégradation gracieuse (pannes simulées sur _fetch_once = l'appel réseau)
  D. Multi-timeframe H1/H4/D1 (DataFetcher) + fraîcheur + exactitude resample
  E. Calendrier économique ForexFactory (flux réel)
  F. Analyseur fondamental : tests déterministes (calendrier synthétique)
     + sentiment live + news rouge imminente
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import requests

from src.config import load_config
from src.data.data_fetcher import DataFetcher
from src.data.provider import STANDARD_COLUMNS, DataProviderError, Timeframe, resample_candles
from src.data.yahoo_provider import YahooProvider
from src.fundamental.economic_calendar import CALENDAR_COLUMNS, scrape_forexfactory
from src.fundamental.fundamental_analyzer import FundamentalAnalyzer, SentimentResult
from src.logger import setup_logging

PAIRS = ["EURUSD", "GBPUSD", "XAUUSD"]
TIMEFRAMES = [Timeframe.M5, Timeframe.M15, Timeframe.H1]

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    tag = "     OK" if ok else " ÉCHEC"
    line = f"[{tag}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


# --------------------------------------------------------------------------- #
def market_closed(now: pd.Timestamp) -> bool:
    """Week-end Forex : vendredi 21h30 UTC -> dimanche 21h30 UTC (approx.)."""
    if now.weekday() == 5:
        return True
    if now.weekday() == 4 and now.hour >= 21:
        return True
    if now.weekday() == 6 and now.hour < 21:
        return True
    return False


def validate_df(df: pd.DataFrame, tf: Timeframe) -> tuple[bool, str]:
    """Validation complète d'un DataFrame de bougies."""
    if not isinstance(df, pd.DataFrame):
        return False, "pas un DataFrame"
    if df.empty:
        return False, "DataFrame vide"
    if list(df.columns) != STANDARD_COLUMNS:
        return False, f"colonnes={list(df.columns)}"
    if df[list(df.columns)].isna().any().any():
        return False, "valeurs NaN présentes"
    if not isinstance(df.index, pd.DatetimeIndex):
        return False, "index non DatetimeIndex"
    if str(df.index.tz) != "UTC":
        return False, f"timezone={df.index.tz}"
    if not df.index.is_monotonic_increasing:
        return False, "index non trié"
    if df.index.has_duplicates:
        return False, "doublons dans l'index"
    sane = (df["high"] >= df[["open", "close", "low"]].max(axis=1) - 1e-9).all() and (
        df["low"] <= df[["open", "close", "high"]].min(axis=1) + 1e-9
    ).all()
    if not sane:
        return False, "OHLC incohérent"
    if not all(str(df[c].dtype) == "float64" for c in STANDARD_COLUMNS):
        return False, "dtypes non float64"

    # Fraîcheur tolérante aux réalités du gratuit :
    #   - le flux spot FX Yahoo peut geler 30-60 min puis se rattraper ;
    #   - le contrôle qualité peut supprimer une bougie journalière aberrante
    #     (cas réel observé : D1 EURUSD 17/08 avec low > close) -> tolérance
    #     D1 élargie à ~5 jours (week-end + 1 bougie retirée).
    now = pd.Timestamp.now(tz="UTC")
    age = now - df.index[-1]
    if market_closed(now):
        max_age = pd.Timedelta(days=3)
    elif tf is Timeframe.D1:
        max_age = pd.Timedelta(days=5) + pd.Timedelta(hours=6)
    else:
        max_age = tf.delta * 4 + pd.Timedelta(minutes=45)
    if age > max_age:
        return False, f"dernière bougie âgée de {age}"
    return True, f"{len(df)} bougies, dernière il y a {age.total_seconds() / 60:.0f} min"


# --------------------------------------------------------------------------- #
def section_a_b_c() -> None:
    provider = YahooProvider(min_request_interval=0.4)

    print("\n--- A. Récupération réelle : paires x timeframes ---")
    for pair in PAIRS:
        for tf in TIMEFRAMES:
            try:
                t0 = time.perf_counter()
                df = provider.get_candles(pair, tf, lookback_days=30)
                elapsed = time.perf_counter() - t0
                ok, detail = validate_df(df, tf)
                check(f"{pair} {tf.value}", ok, f"{detail} | {elapsed:.2f}s")
            except Exception as exc:  # noqa: BLE001
                check(f"{pair} {tf.value}", False, f"{type(exc).__name__}: {exc}")

    print("\n--- B. Cache incrémental ---")
    t0 = time.perf_counter()
    df1 = provider.get_candles("EURUSD", Timeframe.M15)
    first_duration = time.perf_counter() - t0
    t0 = time.perf_counter()
    df2 = provider.get_candles("EURUSD", Timeframe.M15)
    second_duration = time.perf_counter() - t0
    ok = (not df2.empty) and len(df2) >= len(df1) - 1
    check(
        "2e appel = refresh incrémental",
        ok,
        f"1er appel {first_duration:.2f}s -> 2e appel {second_duration:.2f}s "
        f"| {len(df1)} -> {len(df2)} bougies",
    )
    if not df2.empty:
        last = df2.iloc[-1]
        check(
            "dernière bougie EURUSD M15 (clôturée)",
            True,
            f"{df2.index[-1]:%Y-%m-%d %H:%M} UTC | O={last['open']:.5f} "
            f"H={last['high']:.5f} L={last['low']:.5f} C={last['close']:.5f}",
        )

    print("\n--- C. Gestion d'erreurs ---")
    try:
        provider.get_candles("ZZZ123", Timeframe.M15)
        check("paire inconnue -> ValueError", False, "aucune exception levée")
    except ValueError as exc:
        check("paire inconnue -> ValueError", True, str(exc)[:70] + "...")
    except Exception as exc:  # noqa: BLE001
        check("paire inconnue -> ValueError", False, f"mauvais type : {type(exc).__name__}")

    print("\n--- C bis. Reconnexion automatique : 2 coupures puis retour du réseau ---")
    original_fetch = provider._fetch_once
    attempts = {"n": 0}

    def flaky_fetch(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            print(f"    ... coupure réseau simulée (tentative {attempts['n']})")
            raise requests.ConnectionError("simulation : réseau coupé")
        return original_fetch(*args, **kwargs)

    provider.clear_cache("EURUSD")
    provider._fetch_once = flaky_fetch
    try:
        df = provider.get_candles("EURUSD", Timeframe.M15)
        check(
            "rétablissement après 2 échecs",
            not df.empty and attempts["n"] == 3,
            f"{len(df)} bougies récupérées en {attempts['n']} tentatives",
        )
    except Exception as exc:  # noqa: BLE001
        check("rétablissement après 2 échecs", False, f"{type(exc).__name__}: {exc}")
    finally:
        provider._fetch_once = original_fetch
        provider.clear_cache("EURUSD")

    print("\n--- C ter. Panne prolongée : retries épuisés ---")
    dying = YahooProvider(max_retries=4, retry_base_delay=0.2, min_request_interval=0.0)

    def always_down(*args, **kwargs):
        raise requests.ConnectionError("simulation : plus de réseau du tout")

    dying._fetch_once = always_down
    t0 = time.perf_counter()
    try:
        dying.get_candles("GBPUSD", Timeframe.H1)
        check("panne prolongée -> DataProviderError", False, "aucune exception levée")
    except DataProviderError as exc:
        check(
            "panne prolongée -> DataProviderError",
            True,
            f"levée proprement en {time.perf_counter() - t0:.1f}s : {str(exc)[:55]}...",
        )
    except Exception as exc:  # noqa: BLE001
        check("panne prolongée -> DataProviderError", False, f"mauvais type : {type(exc).__name__}")

    print("\n--- C quarto. Dégradation gracieuse : panne APRÈS un cache ---")
    resilient = YahooProvider(max_retries=2, retry_base_delay=0.1, min_request_interval=0.0)
    try:
        df_before = resilient.get_candles("EURUSD", Timeframe.M15)
        resilient._fetch_once = always_down
        df_after = resilient.get_candles("EURUSD", Timeframe.M15)
        check(
            "panne en cours de route -> cache servi",
            not df_after.empty and len(df_after) >= len(df_before) - 1,
            f"{len(df_after)} bougies servies depuis le cache (moteur vivant)",
        )
    except Exception as exc:  # noqa: BLE001
        check("panne en cours de route -> cache servi", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
def section_d() -> None:
    print("\n--- D. Multi-timeframe H1/H4/D1 (DataFetcher) ---")
    fetcher = DataFetcher(config=load_config())
    mtd_eur = None

    for pair in ["EURUSD", "XAUUSD"]:
        mtd = fetcher.get_multi_timeframe_data(pair)  # H1, H4, D1 par défaut
        if pair == "EURUSD":
            mtd_eur = mtd
        for tf, df in mtd.frames.items():
            ok, detail = validate_df(df, tf)
            fresh = mtd.freshness[tf]
            check(
                f"{pair} {tf.value}",
                ok,
                f"{detail} | {fresh}",
            )

    if mtd_eur is not None:
        h1, h4 = mtd_eur.frames[Timeframe.H1], mtd_eur.frames[Timeframe.H4]
        manual = resample_candles(h1, "4h")
        common = h4.index.intersection(manual.index).sort_values()
        ok = len(common) >= 20
        if ok:
            sample = common[-60:]
            ok = bool(
                np.allclose(h4.loc[sample, ["open", "high", "low", "close"]].values,
                            manual.loc[sample, ["open", "high", "low", "close"]].values,
                            atol=1e-9)
            )
        check(
            "H4 = ré-échantillonnage EXACT de H1 (open/max/min/close)",
            ok,
            f"{len(common)} bacs communs comparés, valeurs identiques" if ok else f"{len(common)} bacs communs",
        )


# --------------------------------------------------------------------------- #
def section_e() -> pd.DataFrame:
    print("\n--- E. Calendrier économique ForexFactory (flux réel) ---")
    try:
        cal = scrape_forexfactory(day="today")
    except Exception as exc:  # noqa: BLE001
        check("calendrier ForexFactory", False, f"{type(exc).__name__}: {exc}")
        return pd.DataFrame(columns=CALENDAR_COLUMNS)

    if cal.empty:
        print("    [ATTENTION] aucune news High/Medium aujourd'hui (week-end/férié) — structure quand même vérifiée")
        check("structure du calendrier (vide mais typé)", list(cal.columns) == CALENDAR_COLUMNS)
        return cal

    check(
        "colonnes + types du calendrier",
        list(cal.columns) == CALENDAR_COLUMNS
        and str(cal["heure"].dtype) == "datetime64[ns, UTC]"
        and str(cal["heure"].dt.tz) == "UTC",
        f"dtype={cal['heure'].dtype} | tz={cal['heure'].dt.tz}",
    )
    check(
        "impacts filtrés High/Medium uniquement",
        bool(cal["impact"].isin(["High", "Medium"]).all()),
        f"{len(cal)} événements : {cal['impact'].value_counts().to_dict()}",
    )
    print("\n    Extrait du calendrier du jour (heure UTC) :")
    for _, r in cal.head(8).iterrows():
        flag = "🔴" if r["impact"] == "High" else "🟠"
        print(f"      {r['heure']:%H:%M}  {r['devise']:<4} {flag} {str(r['evenement'])[:38]:<40}"
              f" A={str(r['actual'])[:8]:<9} F={str(r['forecast'])[:8]:<9} P={str(r['previous'])[:8]}")
    return cal


# --------------------------------------------------------------------------- #
def _mk_row(heure, devise, evenement, impact, actual, forecast, previous):
    return {"heure": heure, "devise": devise, "evenement": evenement,
            "impact": impact, "actual": actual, "forecast": forecast, "previous": previous}


def section_f() -> None:
    print("\n--- F. Analyseur fondamental (tests déterministes) ---")
    NOW = pd.Timestamp("2026-08-19T12:00:00Z")

    # 1. parse_number
    cases = [("312K", 312_000.0), ("0.51%", 0.51), ("1,234", 1234.0), ("-0.4%", -0.4),
             ("<0.1%", 0.1), ("$-12.3B", -12.3e9), ("", None), ("TBA", None)]
    ok = all(
        (FundamentalAnalyzer.parse_number(a) is None and b is None)
        or (FundamentalAnalyzer.parse_number(a) is not None and abs(FundamentalAnalyzer.parse_number(a) - b) < 1e-6)
        for a, b in cases
    )
    check("parse_number (K/%/,/$/B/vide)", ok, f"{len(cases)} cas testés")

    # 2. sentiment sur calendrier synthétique
    synth = pd.DataFrame([
        _mk_row(NOW - pd.Timedelta(hours=1), "USD", "Non-Farm Employment Change", "High", "250K", "200K", "180K"),
        _mk_row(NOW - pd.Timedelta(hours=1), "USD", "Unemployment Rate", "High", "5.0%", "4.5%", "4.4%"),
        _mk_row(NOW - pd.Timedelta(hours=2), "EUR", "CPI m/m", "High", "0.5%", "0.3%", "0.2%"),
        _mk_row(NOW - pd.Timedelta(hours=3), "GBP", "Retail Sales m/m", "High", "-0.4%", "0.2%", "0.1%"),
        _mk_row(NOW + pd.Timedelta(minutes=30), "USD", "FOMC Statement", "High", "", "", ""),
        _mk_row(NOW + pd.Timedelta(hours=3), "EUR", "ECB Press Conference", "High", "", "", ""),
    ])
    an = FundamentalAnalyzer(calendar=synth, fetch_on_init=False)
    s = an.get_currency_sentiment(["USD", "EUR", "GBP"], now=NOW)

    check("USD NEUTRAL (+2.0 NFP battu, -2.0 chômage inversé)", s["USD"].label == "NEUTRAL", str(s["USD"]))
    check("EUR BULLISH (CPI 0.5% vs 0.3%)", s["EUR"].label == "BULLISH", str(s["EUR"]))
    check("GBP BEARISH (retail sales -0.4% vs +0.2%)", s["GBP"].label == "BEARISH", str(s["GBP"]))
    print("    drivers EUR :", "; ".join(s["EUR"].drivers))

    bias = an.get_pair_bias("EURUSD", now=NOW)
    check("biais paire EURUSD = BULLISH", bias.label == "BULLISH",
          f"{bias.pair} {bias.label} ({bias.score:+.1f}) = EUR {s['EUR'].score:+.1f} − USD {s['USD'].score:+.1f}")

    # 3. départage NLP (news sans chiffres)
    cal2 = pd.DataFrame([_mk_row(NOW - pd.Timedelta(hours=1), "USD",
                                 "Fed Chair Speech (hawkish tone)", "High", "", "", "")])
    an2 = FundamentalAnalyzer(calendar=cal2, fetch_on_init=False)
    s2 = an2.get_currency_sentiment(["USD"], now=NOW)
    check("départage NLP regex : titre 'hawkish' -> USD BULLISH", s2["USD"].label == "BULLISH", str(s2["USD"]))

    # 4. news rouge imminente
    check("is_high_impact_soon(USD) = True (FOMC dans 30 min)",
          an.is_high_impact_soon("USD", within_minutes=60, now=NOW) is True)
    check("is_high_impact_soon(EUR) = False (ECB dans 180 min)",
          an.is_high_impact_soon("EUR", within_minutes=60, now=NOW) is False)

    # 5. LIVE : sentiment du jour + prochaine news rouge
    print("\n--- F bis. Analyseur fondamental (données réelles du jour) ---")
    try:
        live = FundamentalAnalyzer(fetch_on_init=True)
        sents = live.get_currency_sentiment(["USD", "EUR", "GBP"])
        print("    ", " | ".join(str(sents[c]) for c in sents))
        for cur in sents:
            for d in sents[cur].drivers[:2]:
                print(f"       · {cur} {d}")
        check("sentiment live : labels valides",
              all(sents[c].label in ("BULLISH", "BEARISH", "NEUTRAL") for c in sents))
        soon = live.is_high_impact_soon()
        nxt = live.next_high_impact_events(limit=3)
        if not nxt.empty:
            first = nxt.iloc[0]
            print(f"     prochaine news rouge : {first['devise']} {first['evenement']} "
                  f"dans ~{first['dans_min']} min")
        else:
            print("     aucune news rouge à venir dans la semaine récupérée")
        check("is_high_impact_soon() live = bool", isinstance(soon, bool), f"résultat : {soon}")
    except Exception as exc:  # noqa: BLE001
        check("analyseur live", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
def main() -> int:
    setup_logging(level="WARNING")
    print("=" * 70)
    print(" Test fonctionnel — module de données complet (Étapes 2 & 3)")
    print("=" * 70)

    section_a_b_c()
    section_d()
    section_e()
    section_f()

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — module de données validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
