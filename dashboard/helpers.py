"""Fonctions pures du dashboard — testables SANS Streamlit.

Le dashboard (app.py) n'est que de la présentation : toute la logique
(chargement, mise en forme, courbe d'équité, données de démonstration)
vit ici et est couverte par scripts/test_dashboard.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.signals.models import pip_spec

#: Couleurs institutionnelles des issues (cohérentes avec la carte SVG).
RESULT_COLORS = {
    "TP_ATTEINT": "#2ECC71",
    "SL_ATTEINT": "#E74C3C",
    "EXPIRE": "#8FA3BF",
    "EN_COURS": "#F5C542",
}

RESULT_LABELS = {
    "TP_ATTEINT": "Objectif atteint",
    "SL_ATTEINT": "Stop atteint",
    "EXPIRE": "Expiration",
    "EN_COURS": "En cours",
}

LAST_CYCLE_PATH = Path("data/last_cycle.json")


# --------------------------------------------------------------------------- #
def load_last_cycles(path: str | Path = LAST_CYCLE_PATH) -> dict | None:
    """Charge le rapport du dernier cycle moteur (data/last_cycle.json)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def result_color(resultat: str) -> str:
    return RESULT_COLORS.get(str(resultat), "#8FA3BF")


def _fmt_price(pair: str, value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    pip_size, _ = pip_spec(str(pair))
    decimals = 5 if pip_size <= 0.001 else 2
    return f"{float(value):.{decimals}f}"


def prepare_signals_table(df: pd.DataFrame) -> pd.DataFrame:
    """Tableau prêt à afficher : colonnes FR, prix au bon nombre de décimales."""
    if df is None or df.empty:
        return pd.DataFrame(columns=[
            "Date", "Paire", "Sens", "Score", "Grade", "Session",
            "Entrée", "TP", "SL", "R/R", "Issue", "R réalisé",
        ])
    out = pd.DataFrame({
        "Date": df["date"].astype(str).str.replace(" UTC", "", regex=False),
        "Paire": df["paire"].astype(str),
        "Sens": df["type"].astype(str),
        "Score": df["score"].astype(int),
        "Grade": df["grade"].fillna("—"),
        "Session": df["session"].fillna("—"),
        "Entrée": [_fmt_price(p, v) for p, v in zip(df["paire"], df["entree"])],
        "TP": [_fmt_price(p, v) for p, v in zip(df["paire"], df["tp"])],
        "SL": [_fmt_price(p, v) for p, v in zip(df["paire"], df["sl"])],
        "R/R": df["rr"].map(lambda v: f"1:{v:.1f}" if pd.notna(v) else "—"),
        "Issue": df["resultat"].map(lambda r: RESULT_LABELS.get(r, r)),
        "R réalisé": df["exit_r"].map(lambda v: f"{v:+.1f}" if pd.notna(v) else "—"),
        "_resultat": df["resultat"].astype(str),  # pour la coloration
        "_id": df["id"],
    })
    return out.sort_values("_id", ascending=False).drop(columns=["_id"])


def equity_curve(closed: pd.DataFrame) -> pd.DataFrame:
    """Courbe d'équité (R cumulés) des signaux clôturés, triée chronologiquement."""
    if closed is None or closed.empty:
        return pd.DataFrame(columns=["label", "r", "equity"])
    df = closed[closed["exit_r"].notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=["label", "r", "equity"])
    df = df.sort_values("id")
    label = (df["date"].astype(str).str[:16] + " " + df["paire"] + " " + df["type"])
    equity = df["exit_r"].astype(float).cumsum().round(2)
    return pd.DataFrame({"label": label.tolist(), "r": df["exit_r"].tolist(),
                         "equity": equity.tolist()})


def stats_from_frame(df: pd.DataFrame) -> dict:
    """Statistiques calculées soit sur données réelles, soit de démonstration."""
    total = len(df)
    if total == 0:
        return {"total": 0, "open": 0, "closed": 0, "tp": 0, "sl": 0, "expired": 0,
                "winrate": None, "avg_r": None, "total_r": 0.0}
    closed = df[df["resultat"] != "EN_COURS"]
    tp = int((closed["resultat"] == "TP_ATTEINT").sum())
    sl = int((closed["resultat"] == "SL_ATTEINT").sum())
    expired = int((closed["resultat"] == "EXPIRE").sum())
    rs = closed["exit_r"].dropna()
    return {
        "total": total, "open": total - len(closed), "closed": len(closed),
        "tp": tp, "sl": sl, "expired": expired,
        "winrate": round(tp / len(closed), 3) if len(closed) else None,
        "avg_r": round(float(rs.mean()), 3) if len(rs) else None,
        "total_r": round(float(rs.sum()), 2) if len(rs) else 0.0,
    }


def floating_r(row: pd.Series, last_close: float) -> float | None:
    """R flottant d'un signal ouvert au prix courant (None si données manquantes)."""
    try:
        entry, sl = float(row["entree"]), float(row["sl"])
        risk = abs(entry - sl) or 1e-9
        if row["type"] == "LONG":
            return round((last_close - entry) / risk, 2)
        return round((entry - last_close) / risk, 2)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
def demo_signals() -> pd.DataFrame:
    """Jeu de démonstration DÉTERMINISTE (clairement étiqueté comme tel).

    Utilisé quand la base de production est encore vide, pour que le
    dashboard soit explorable dès le premier lancement.
    """
    rows = [
        ("2026-08-11 08:32 UTC", "EURUSD", "LONG", 90, "A+", "London Kill Zone",
         1.0912, 1.0950, 1.0893, 2.0, "TP_ATTEINT", 1.1050, 2.0),
        ("2026-08-11 14:15 UTC", "XAUUSD", "SHORT", 75, "A", "New York Kill Zone",
         2380.50, 2372.10, 2384.70, 2.0, "SL_ATTEINT", 2384.70, -1.0),
        ("2026-08-12 09:47 UTC", "GBPUSD", "LONG", 85, "A+", "London Kill Zone",
         1.2685, 1.2765, 1.2645, 2.0, "TP_ATTEINT", 1.2801, 2.0),
        ("2026-08-13 10:20 UTC", "EURUSD", "SHORT", 70, "B", "London",
         1.1042, 1.1002, 1.1062, 2.0, "EXPIRE", 1.1031, 0.5),
        ("2026-08-14 13:05 UTC", "XAUUSD", "LONG", 80, "A", "New York Kill Zone",
         2412.30, 2430.50, 2403.20, 2.0, "TP_ATTEINT", 2448.80, 2.0),
        ("2026-08-15 08:41 UTC", "EURUSD", "LONG", 72, "B", "London Kill Zone",
         1.1018, 1.1058, 1.0998, 2.0, "SL_ATTEINT", 1.0998, -1.0),
        ("2026-08-18 09:12 UTC", "GBPUSD", "SHORT", 88, "A+", "London Kill Zone",
         1.2762, 1.2682, 1.2802, 2.0, "TP_ATTEINT", 1.2656, 2.0),
        ("2026-08-19 07:55 UTC", "XAUUSD", "SHORT", 76, "A", "London Kill Zone",
         2394.80, 2378.60, 2402.90, 2.0, "EN_COURS", None, None),
    ]
    df = pd.DataFrame(rows, columns=[
        "date", "paire", "type", "score", "grade", "session",
        "entree", "tp", "sl", "rr", "resultat", "exit_price", "exit_r",
    ])
    df.insert(0, "id", range(1, len(df) + 1))
    return df
