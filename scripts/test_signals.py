"""Test fonctionnel du moteur de signaux multi-agents — Phase 5.

Usage :  python scripts/test_signals.py

SECTIONS
  A. Scoring unitaire (fonction pure) : pondérations exactes de la spec.
  B. Scénario synthétique complet : D1 haussier + H4 (sweep->CHoCH->OB->FVG)
     + M15 (déclencheur CHoCH récent, prix dans la zone) + news EUR haussières
     => signal LONG 100/100 A+, plan de risque exact, SQLite, carte SVG,
     message Telegram formaté (sans emoji).
  C. Les portes de sécurité : désalignement M15, sentiment contraire (75),
     hors session (90), news rouge imminente (blocage), seuil, cooldown.
  D. Données réelles : cycle complet EURUSD + XAUUSD (issue libre, sans crash).
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.agents.fundamental_agent import FundamentalAgent
from src.analysis.candles import compute_atr
from src.config import load_config
from src.fundamental.economic_calendar import CALENDAR_COLUMNS
from src.fundamental.fundamental_analyzer import FundamentalAnalyzer
from src.notifications.telegram import format_signal_message
from src.signals.engine import SignalEngine
from src.signals.models import FundamentalView, MultiTFView
from src.signals.scoring import compute_score, get_session, grade_of
from src.storage.database import SignalDatabase

results: list[bool] = []
NOW = pd.Timestamp("2026-08-19 08:32:00", tz="UTC")      # London Kill Zone
NOW_OFF = pd.Timestamp("2026-08-19 22:10:00", tz="UTC")  # hors session


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def make_index(n, freq):
    return pd.date_range("2026-07-01", periods=n, freq=freq, tz="UTC")


def to_df(rows, index):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index[: len(rows)])
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


# --------------------------------------------------------------------------- #
#  Séries synthétiques (construites à la main, déterministes)
# --------------------------------------------------------------------------- #
def d1_bullish(n=60):
    """Tendance haussière journalière : biais D1 = bullish + BOS haussiers."""
    rows, prev = [], 1.0900
    for i in range(n):
        level = 1.0950 + 0.004 * math.sin(i / 5.0) + 0.0006 * i
        rows.append((prev, max(prev, level) + 0.0012, min(prev, level) - 0.0012, level))
        prev = level
    return to_df(rows, make_index(n, "1D"))


H4_SYN = [
    (1.1140, 1.1150, 1.1066, 1.1070),  # haut du range H4 (pour premium/discount)
    (1.1070, 1.1076, 1.1052, 1.1056),
    (1.1056, 1.1062, 1.1040, 1.1044),
    (1.1044, 1.1050, 1.1014, 1.1024),  # creux A
    (1.1024, 1.1034, 1.1020, 1.1030),
    (1.1030, 1.1040, 1.1024, 1.1034),
    (1.1034, 1.1044, 1.1010, 1.1020),  # creux B (equal lows ~1.1012)
    (1.1020, 1.1028, 1.1018, 1.1024),
    (1.1024, 1.1032, 1.1016, 1.1020),
    (1.1020, 1.1024, 1.0992, 1.1018),  # 9 : SWEEP des creux égaux
    (1.1018, 1.1022, 1.1006, 1.1008),  # 10 : bougie OB baissière + BOS baissier
    (1.1008, 1.1090, 1.1006, 1.1084),  # 11 : displacement -> CHoCH haussier
    (1.1084, 1.1104, 1.1072, 1.1098),  # 12 : crée le FVG [1.1022, 1.1072]
    (1.1098, 1.1108, 1.1080, 1.1088),
    (1.1088, 1.1094, 1.1064, 1.1070),  # retrace dans le FVG
    (1.1070, 1.1076, 1.1052, 1.1060),
]

M15_SYN = H4_SYN[:12] + [
    # 12 : crée le FVG M15 [1.1022, 1.1068]
    (1.1084, 1.1092, 1.1068, 1.1074),
    (1.1074, 1.1078, 1.1056, 1.1062),
    (1.1062, 1.1066, 1.1044, 1.1050),
    # 15 : prix 1.1042 — dans le FVG, à 20 pips au-dessus de l'OB H4
    (1.1050, 1.1054, 1.1036, 1.1042),
]


def mirror_bullish_to_bearish(df):
    """Renverse un scénario haussier en baissier (miroir des prix) : OHLC valide."""
    out = df.copy()
    out["open"] = 2.0 - df["open"]
    out["close"] = 2.0 - df["close"]
    out["high"] = 2.0 - df["low"]
    out["low"] = 2.0 - df["high"]
    return out[["open", "high", "low", "close", "volume"]]


def h4_df():
    return to_df(H4_SYN, make_index(len(H4_SYN), "4h"))


def m15_df():
    return to_df(M15_SYN, make_index(len(M15_SYN), "15min"))


# --------------------------------------------------------------------------- #
#  Calendriers synthétiques déterministes
# --------------------------------------------------------------------------- #
def calendar(eur_bullish=True, red_news_in=None, now=NOW):
    rows = []
    if eur_bullish:
        rows.append([now - pd.Timedelta(hours=2), "EUR", "CPI m/m", "High", "0.5%", "0.3%", "0.2%"])
    else:
        rows.append([now - pd.Timedelta(hours=2), "EUR", "CPI m/m", "High", "0.1%", "0.3%", "0.3%"])
    if red_news_in is not None:
        rows.append([now + red_news_in, "USD", "FOMC Statement", "High", "", "", ""])
    else:
        rows.append([now + pd.Timedelta(hours=3), "USD", "FOMC Statement", "High", "", "", ""])
    return pd.DataFrame(rows, columns=CALENDAR_COLUMNS)


def fundamental_agent(cal):
    return FundamentalAgent(FundamentalAnalyzer(calendar=cal, fetch_on_init=False))


def fresh_engine(tmp, cal, config=None):
    """Moteur isolé : base temporaire + fondamental injecté + Telegram off."""
    db = SignalDatabase(Path(tmp) / "signals.db")
    cfg = config or load_config()
    return SignalEngine(
        config=cfg, database=db,
        fundamental=fundamental_agent(cal),
        telegram=None or _null_sender(),
    )


def _null_sender():
    from src.notifications.telegram import TelegramSender

    return TelegramSender("", "", enabled=False)


# --------------------------------------------------------------------------- #
def section_a():
    print("\n--- A. Scoring unitaire (pondérations exactes de la spécification) ---")
    check("session 08:32 = London Kill Zone", get_session(NOW) == ("London Kill Zone", True),
          str(get_session(NOW)))
    check("session 22:10 = hors session", get_session(NOW_OFF)[1] is False, str(get_session(NOW_OFF)))

    mv = MultiTFView(
        d1_bias="bullish", d1_event="BOS bullish", h4_supports=True, h4_reason="x",
        m15_trigger="bullish", m15_trigger_kind="CHoCH", m15_trigger_age=3,
        current_price=1.1040, atr_m15=0.002,
        ob_near={"timeframe": "H4", "zone_bottom": 1.1006, "zone_top": 1.1022},
        fvg_near={"timeframe": "H4", "fill_pct": 40},
        sweep_recent={"label": "swept lows"},
        premium_discount="discount", pd_position_pct=40.0, direction="LONG",
    )
    fv = FundamentalView("BULLISH", 2.0, True, ["CPI m/m: 0.5% vs prév 0.3%"])

    score, breakdown, conf = compute_score("LONG", mv, fv, NOW)
    check("toutes confluences -> 100/100", score == 100, f"{score} {breakdown}")
    check("7 composantes présentes", set(breakdown) == {"fondamental", "order_block", "fvg",
          "choch", "session", "premium_discount", "sweep"})
    check("grade : 85->A+, 84->A, 74->B, 69->None",
          grade_of(85) == "A+" and grade_of(84) == "A" and grade_of(74) == "B" and grade_of(69) is None)

    score2, _, _ = compute_score("LONG", mv, fv, NOW_OFF)
    check("hors session -> 90", score2 == 90, str(score2))
    mv_no_fvg = MultiTFView(**{**mv.__dict__, "fvg_near": None})
    score3, _, _ = compute_score("LONG", mv_no_fvg, fv, NOW)
    check("sans FVG -> 85", score3 == 85, str(score3))
    fv_off = FundamentalView("BEARISH", -2.0, False, [])
    score4, _, _ = compute_score("LONG", mv, fv_off, NOW)
    check("sentiment contraire -> 75 (au-dessus du seuil)", score4 == 75, str(score4))


def section_b(tmp):
    print("\n--- B. Scénario synthétique aligné : D1+H4+M15 => signal 100/100 ---")
    engine = fresh_engine(tmp, calendar())
    report = engine.run_on_frames("EURUSD", d1_bullish(), h4_df(), m15_df(), now=NOW)

    check("alignement D1/H4/M15 validé", report.aligned, str(report.alignment_detail))
    check("score = 100 (toutes composantes)", report.score == 100, str(report.breakdown))
    check("candidat LONG", report.candidate_direction == "LONG")
    sig = report.signal
    check("signal émis, grade A+", sig is not None and sig.grade == "A+")
    if sig is None:
        return

    # Plan de risque exact : entrée = bord de l'OB H4 (1.1022), SL = bas OB - 0.1 ATR
    from src.config import load_config
    rr_cfg = float(load_config().signals.default_rr or 2.0)
    atr15 = float(compute_atr(m15_df())[-1])
    expected_sl = 1.1006 - 0.1 * atr15
    expected_tp = 1.1022 + rr_cfg * (1.1022 - expected_sl)
    ok = abs(sig.risk.entry - 1.1022) < 1e-9 and abs(sig.risk.sl - expected_sl) < 1e-6 \
        and abs(sig.risk.tp - expected_tp) < 1e-6 and abs(sig.risk.rr - rr_cfg) < 1e-9
    check("plan : entrée 1.1022 / SL bas d'OB − 0.1×ATR / TP = 2R", ok,
          f"entrée {sig.risk.entry}, SL {sig.risk.sl}, TP {sig.risk.tp}, R/R {sig.risk.rr}")
    check("session = London Kill Zone", sig.session == "London Kill Zone", sig.session)
    check("sizing : 1 % de 10 000 -> lots cohérents", 0 < sig.risk.lots <= 1.0, f"{sig.risk.lots} lots")

    # Persistance
    check("signal enregistré en SQLite", report.stored and sig.db_id is not None, f"id={sig.db_id}")
    recent = engine.db.recent(limit=5)
    check("ligne lisible (paire/type/score/resultat)", len(recent) == 1
          and recent.iloc[0]["paire"] == "EURUSD" and recent.iloc[0]["type"] == "LONG"
          and recent.iloc[0]["resultat"] == "EN_COURS"
          and recent.iloc[0]["score"] == 100)

    # Telegram : texte formaté, PROPRE (aucun emoji)
    msg = format_signal_message(sig)
    # La spec FINALE du robot EUR/USD impose les sections avec emojis (📊🎯🔒)
    check("message conforme aux sections emojis de la spec",
          "\U0001F4CA" in msg and "\U0001F3AF" in msg)
    check("message au format professionnel HTML EUR/USD",
          "SIGNAL EUR/USD" in msg and "ACHAT (BUY)" in msg
          and "ANALYSE TECHNIQUE" in msg and "ANALYSE FONDAMENTALE" in msg
          and "PLAN DE TRADE" in msg and "CONFLUENCES" in msg
          and "DXY" in msg and "DYOR" in msg and "<b>" in msg)
    print("\n    Message Telegram généré :")
    for line in msg.splitlines()[:14]:
        print("    " + line)

    # Carte SVG
    check("carte SVG générée", report.card_path is not None and Path(report.card_path).exists(),
          str(report.card_path))
    svg = Path(report.card_path).read_text(encoding="utf-8")
    check("carte SVG : icônes vectorielles, zéro emoji",
          svg.startswith("<svg") and "CONFLUENCES" in svg and "<path" in svg
          and not any(0x1F000 <= ord(c) <= 0x1FAFF for c in svg))

    # Telegram non configuré : dégradation gracieuse (signal quand même stocké)
    check("Telegram non configuré -> non bloquant", report.telegram_sent is False and report.stored)

    return engine


def section_c(tmp, engine_b):
    print("\n--- C. Portes de sécurité ---")

    # C1 : déclencheur M15 inversé -> désalignement, AUCUN signal
    eng = fresh_engine(tmp + "_c1", calendar())
    rep = eng.run_on_frames("EURUSD", d1_bullish(), h4_df(),
                            mirror_bullish_to_bearish(m15_df()), now=NOW)
    check("M15 baissier contre D1 haussier -> bloqué (biais D1)",
          rep.signal is None and any("biais D1" in b for b in rep.blockers), str(rep.blockers))

    # C2 : sentiment fondamental contraire -> 75/100, toujours >= 70
    eng = fresh_engine(tmp + "_c2", calendar(eur_bullish=False))
    rep = eng.run_on_frames("EURUSD", d1_bullish(), h4_df(), m15_df(), now=NOW)
    check("sentiment contraire -> 75/100 (seuil franchi)",
          rep.signal is not None and rep.score == 75 and rep.signal.grade == "A",
          f"score {rep.score}")

    # C3 : hors session -> 90, composante session à 0
    #     (news calée sur NOW_OFF : le sentiment décroît avec l'ancienneté)
    eng = fresh_engine(tmp + "_c3", calendar(now=NOW_OFF))
    rep = eng.run_on_frames("EURUSD", d1_bullish(), h4_df(), m15_df(), now=NOW_OFF)
    check("hors session -> 90 (session = 0)",
          rep.signal is not None and rep.score == 90 and rep.breakdown["session"] == 0,
          f"score {rep.score}, breakdown {rep.breakdown}")

    # C4 : news rouge imminente -> l'agent 3 bloque
    eng = fresh_engine(tmp + "_c4", calendar(red_news_in=pd.Timedelta(minutes=30)))
    rep = eng.run_on_frames("EURUSD", d1_bullish(), h4_df(), m15_df(), now=NOW)
    check("news rouge dans 30 min -> rejet par l'agent risque",
          rep.signal is None and any("news" in b for b in rep.blockers), str(rep.blockers))

    # C5 : seuil relevé à 95 -> score 90 refusé
    eng = fresh_engine(tmp + "_c5", calendar(now=NOW_OFF))
    eng.threshold = 95
    rep = eng.run_on_frames("EURUSD", d1_bullish(), h4_df(), m15_df(), now=NOW_OFF)
    check("seuil 95 -> score 90 refusé", rep.signal is None
          and any("score 90" in b for b in rep.blockers))

    # C6 : cooldown après le signal de la section B
    rep = engine_b.run_on_frames("EURUSD", d1_bullish(), h4_df(), m15_df(),
                                 now=NOW + pd.Timedelta(minutes=30))
    check("cooldown anti-spam après émission", rep.signal is None
          and any("cooldown" in b for b in rep.blockers), str(rep.blockers))


def section_d():
    print("\n--- D. Cycle réel (EURUSD + XAUUSD, issue libre) ---")
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        engine = SignalEngine(config=cfg, database=SignalDatabase(Path(tmp) / "live.db"),
                              fundamental=FundamentalAgent(), telegram=_null_sender())
        for pair in ("EURUSD", "XAUUSD"):
            try:
                rep = engine.run_pair(pair)
                detail = f"score {rep.score}, aligné={rep.aligned}"
                if rep.signal:
                    detail += f" -> SIGNAL {rep.signal.direction} {rep.signal.score}/100 ({rep.signal.grade})"
                else:
                    detail += f", bloqué par : {rep.blockers[:2]}"
                check(f"cycle {pair} exécuté sans crash", rep is not None, detail)
            except Exception as exc:  # noqa: BLE001
                check(f"cycle {pair}", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
def main():
    from src.logger import setup_logging

    setup_logging(level="WARNING")
    print("=" * 70)
    print(" Test fonctionnel — moteur de signaux multi-agents (Phase 5)")
    print("=" * 70)

    section_a()
    with tempfile.TemporaryDirectory() as tmp:
        engine_b = section_b(tmp)
        section_c(tmp, engine_b)
    section_d()

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — moteur validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
