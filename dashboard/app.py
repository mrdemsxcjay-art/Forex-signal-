"""Dashboard professionnel — Signaux Forex SMC (lecture seule).

Zéro couplage avec le moteur : ce dashboard lit
    - data/signals.db     (historique + issues, écrit par le moteur)
    - data/last_cycle.json(transparence : pourquoi un signal part ou non)
    - données de marché   (analyse SMC à la demande, cache 5 min)

Lancement :
    streamlit run dashboard/app.py
    (ou : python -m streamlit run dashboard/app.py --server.port 8501)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh
import streamlit.components.v1 as components

from dashboard.helpers import (
    RESULT_COLORS,
    demo_signals,
    equity_curve,
    floating_r,
    load_last_cycles,
    prepare_signals_table,
    stats_from_frame,
)
from src.analysis.smc import SMCEngine
from src.data.data_fetcher import DataFetcher
from src.signals.models import pip_spec
from src.storage.database import SignalDatabase
from src.visualization.smc_chart import build_smc_figure

# --------------------------------------------------------------------------- #
#  Configuration de page + thème professionnel sombre (sans emoji)
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Signaux Forex — SMC", layout="wide",
                   initial_sidebar_state="expanded")

# Protection facultative pour un hébergement public (Railway/Render) :
# définir DASHBOARD_TOKEN dans l'environnement, puis accéder à l'URL avec
# ?token=<valeur>. Non défini = comportement local inchangé.
import os as _os

_DASH_TOKEN = _os.getenv("DASHBOARD_TOKEN", "").strip()
if _DASH_TOKEN:
    _provided = st.query_params.get("token") or ""
    if _provided != _DASH_TOKEN:
        st.warning("Accès protégé : ajoutez ?token=<DASHBOARD_TOKEN> à l'URL.")
        st.stop()

st.markdown("""
<style>
    .stApp { background: #0E1726; color: #EAF0F8; }
    section[data-testid="stSidebar"] { background: #16233A; }
    h1, h2, h3, h4 { color: #EAF0F8; font-family: 'Segoe UI', Arial, sans-serif; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        background-color: #16233A; border-radius: 8px 8px 0 0;
        padding: 8px 18px; color: #8FA3BF; font-weight: 600;
    }
    .stTabs [aria-selected="true"] { background-color: #1F3355; color: #F5C542; }
    div[data-testid="stMetric"] {
        background: #16233A; border-radius: 10px; padding: 12px 16px;
        border-left: 3px solid #F5C542;
    }
    .demo-banner {
        background: #3A2E16; color: #F5C542; padding: 8px 14px;
        border-radius: 8px; border-left: 3px solid #F5C542; font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)

st_autorefresh(interval=60_000, key="dashboard-refresh")

LOGO_SVG = """
<svg width="230" height="54" viewBox="0 0 230 54" xmlns="http://www.w3.org/2000/svg">
  <rect x="2" y="6" width="46" height="42" rx="9" fill="#16233A"/>
  <line x1="14" y1="12" x2="14" y2="42" stroke="#2ECC71" stroke-width="3"/>
  <rect x="10" y="20" width="8" height="14" rx="2" fill="#2ECC71"/>
  <line x1="25" y1="8" x2="25" y2="46" stroke="#E74C3C" stroke-width="3"/>
  <rect x="21" y="14" width="8" height="16" rx="2" fill="#E74C3C"/>
  <line x1="36" y1="16" x2="36" y2="38" stroke="#F5C542" stroke-width="3"/>
  <rect x="32" y="22" width="8" height="10" rx="2" fill="#F5C542"/>
  <text x="58" y="26" fill="#EAF0F8" font-size="17" font-weight="700"
        font-family="Segoe UI, Arial">SIGNAUX FOREX</text>
  <text x="58" y="44" fill="#8FA3BF" font-size="12"
        font-family="Segoe UI, Arial">SMC multi-agents · D1/H4/M15</text>
</svg>
"""


# --------------------------------------------------------------------------- #
#  Données (cache : lecture seule, jamais d'écriture ici)
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def get_db() -> SignalDatabase:
    return SignalDatabase(ROOT / "data" / "signals.db")


@st.cache_data(ttl=60, show_spinner=False)
def signals_frame() -> pd.DataFrame:
    try:
        return get_db().recent(limit=200)
    except Exception:  # noqa: BLE001 — base absente au tout premier lancement
        return pd.DataFrame()


@st.cache_data(ttl=120, show_spinner=False)
def last_close(pair: str) -> float | None:
    try:
        df = DataFetcher().get_candles(pair, "15m", lookback_days=2)
        return float(df["close"].iloc[-1]) if not df.empty else None
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(ttl=300, show_spinner="Analyse SMC en cours...")
def smc_view(pair: str, tf: str):
    df = DataFetcher().get_candles(pair, tf, lookback_days=30)
    result = SMCEngine(pair, tf).analyze(df)
    return result, df


# --------------------------------------------------------------------------- #
#  Barre latérale
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown(LOGO_SVG, unsafe_allow_html=True)
    st.caption("Analyse uniquement — aucun ordre exécuté. DYOR.")

    real_df = signals_frame()
    demo_mode = st.checkbox(
        "Mode démonstration", value=real_df.empty,
        help="Affiche un jeu d'exemple quand la base de production est vide.",
    )
    if demo_mode:
        st.markdown('<div class="demo-banner">DONNÉES DE DÉMONSTRATION</div>',
                    unsafe_allow_html=True)
    signals = demo_signals() if demo_mode else real_df
    stats = stats_from_frame(signals)

    st.subheader("Compte de signaux")
    c1, c2 = st.columns(2)
    c1.metric("Total", stats["total"])
    c2.metric("En cours", stats["open"])
    c1.metric("Winrate", f"{stats['winrate'] * 100:.0f} %" if stats["winrate"] is not None else "—")
    c2.metric("R cumulé", f"{stats['total_r']:+.1f}")

    st.subheader("État du moteur")
    cycles = load_last_cycles(ROOT / "data" / "last_cycle.json")
    if cycles:
        st.success(f"Cycle {cycles['cycle']} — {cycles['updated_at'][:19]} (UTC)")
        st.caption(f"{cycles.get('signals_total', 0)} signal(s) émis · "
                   f"{cycles.get('resolutions_total', 0)} clôture(s)")
    else:
        st.warning("Moteur inactif — lancez : python -m src.main")

# --------------------------------------------------------------------------- #
#  Onglets
# --------------------------------------------------------------------------- #
st.markdown("# Tableau de bord")
tab_signals, tab_why, tab_perf, tab_smc, tab_cards = st.tabs(
    ["Signaux", "Pourquoi aucun signal", "Performance", "Analyse SMC", "Cartes de signal"]
)

# ---- 1. Signaux ------------------------------------------------------------
with tab_signals:
    open_df = signals[signals["resultat"] == "EN_COURS"] if not signals.empty else signals.iloc[0:0]
    st.subheader(f"Positions ouvertes ({len(open_df)})")
    if open_df.empty:
        st.info("Aucune position ouverte.")
    else:
        for _, row in open_df.iterrows():
            price = last_close(str(row["paire"]))
            pip_size, _ = pip_spec(str(row["paire"]))
            r_now = floating_r(row, price) if price else None
            cols = st.columns((2, 1, 1, 1, 1))
            cols[0].markdown(
                f"**{row['paire'][:3]}/{row['paire'][3:]} {row['type']}** · "
                f"score {row['score']} ({row['grade']}) · {row['session']}"
            )
            cols[1].metric("Prix actuel", f"{price:.5f}" if price else "—")
            cols[2].metric("R flottant", f"{r_now:+.1f}" if r_now is not None else "—")
            d_tp = (float(row["tp"]) - price) / pip_size if price else None
            d_sl = (price - float(row["sl"])) / pip_size if price else None
            if row["type"] == "SHORT":
                d_tp, d_sl = -d_tp if d_tp else None, -d_sl if d_sl else None
            cols[3].metric("→ TP (pips)", f"{d_tp:.0f}" if d_tp is not None else "—")
            cols[4].metric("→ SL (pips)", f"{d_sl:.0f}" if d_sl is not None else "—")

    st.subheader("Historique")
    table = prepare_signals_table(signals)
    if table.empty:
        st.info("Aucun signal en base — le moteur émettra ici dès qu'un setup "
                "aligné D1/H4/M15 avec un score ≥ 70 apparaîtra.")
    else:
        styler = table.style.map(
            lambda v: f"color: {RESULT_COLORS.get(v, 'inherit')}; font-weight: 700",
            subset=["_resultat"],
        ).hide(axis="index")
        st.dataframe(styler, use_container_width=True, height=420)

# ---- 2. Pourquoi aucun signal ----------------------------------------------
with tab_why:
    st.subheader("Transparence du dernier cycle (par paire)")
    if not cycles:
        st.info("Aucun cycle moteur enregistré. Lancez `python -m src.main` : "
                "chaque cycle publie ici son diagnostic complet.")
    else:
        cols = st.columns(len(cycles["pairs"]) or 1)
        for col, (pair, info) in zip(cols, cycles["pairs"].items()):
            with col:
                state = "SIGNAL" if info.get("signal") else (
                    "aligné" if info.get("aligned") else "bloqué")
                color = "#2ECC71" if info.get("signal") else (
                    "#F5C542" if info.get("aligned") else "#8FA3BF")
                st.markdown(
                    f"<div style='background:#16233A;border-radius:10px;"
                    f"padding:14px;border-left:3px solid {color}'>"
                    f"<b style='font-size:16px'>{pair}</b><br>"
                    f"<span style='color:{color}'>{state}</span><br>"
                    f"Score : <b>{info.get('score', 0)}/100</b><br>"
                    f"<span style='color:#8FA3BF;font-size:12px'>"
                    + " · ".join(f"{k}={v}" for k, v in (info.get("timeframes") or {}).items())
                    + "</span></div>",
                    unsafe_allow_html=True,
                )
                for blocker in info.get("blockers", []):
                    st.caption(f"· {blocker}")
        # Détail du scoring d'une paire
        pair_names = list(cycles["pairs"].keys())
        if pair_names:
            chosen = st.selectbox("Décomposition du score", pair_names)
            breakdown = cycles["pairs"][chosen].get("breakdown") or {}
            if breakdown:
                fig = go.Figure(go.Bar(
                    x=list(breakdown.values()), y=list(breakdown.keys()),
                    orientation="h", marker_color="#1F5FA8",
                ))
                fig.add_vline(x=70, line_color="#E74C3C", line_dash="dot",
                              annotation_text="seuil 70")
                fig.update_layout(template="plotly_dark", height=280,
                                  margin=dict(l=10, r=10, t=30, b=10),
                                  paper_bgcolor="rgba(0,0,0,0)",
                                  plot_bgcolor="rgba(0,0,0,0)",
                                  xaxis_title="points")
                st.plotly_chart(fig, use_container_width=True)

# ---- 3. Performance ----------------------------------------------------------
with tab_perf:
    closed = signals[signals["resultat"] != "EN_COURS"] if not signals.empty else signals
    eq = equity_curve(closed)
    if eq.empty:
        st.info("Aucune clôture à ce jour — courbe d'équité dès le premier "
                "TP_ATTEINT / SL_ATTEINT / EXPIRE.")
    else:
        left, right = st.columns((3, 2))
        with left:
            st.subheader("Courbe d'équité (R cumulés)")
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(range(1, len(eq) + 1)), y=eq["equity"], mode="lines+markers",
                line=dict(color="#F5C542", width=2),
                text=eq["label"], hovertemplate="%{text}<br>%{y:+.1f}R<extra></extra>",
            ))
            fig.update_layout(template="plotly_dark", height=380,
                              xaxis_title="n-ième clôture", yaxis_title="R cumulés",
                              margin=dict(l=10, r=10, t=20, b=10),
                              paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)
        with right:
            st.subheader("Issues")
            counts = closed["resultat"].value_counts()
            fig = go.Figure(go.Pie(
                labels=[f"{k} ({v})" for k, v in counts.items()], values=counts.values,
                hole=0.62, marker=dict(colors=[RESULT_COLORS.get(k, "#8FA3BF")
                                               for k in counts.index]),
            ))
            fig.update_layout(template="plotly_dark", height=380,
                              margin=dict(l=10, r=10, t=20, b=10),
                              paper_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)

        by_pair = closed.groupby("paire")["exit_r"].sum().sort_values()
        fig = go.Figure(go.Bar(
            x=by_pair.values, y=by_pair.index, orientation="h",
            marker_color=["#2ECC71" if v >= 0 else "#E74C3C" for v in by_pair.values],
        ))
        fig.update_layout(template="plotly_dark", height=260,
                          xaxis_title="R cumulés", margin=dict(l=10, r=10, t=30, b=10),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.subheader("R par paire")
        st.plotly_chart(fig, use_container_width=True)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Clôturés", stats["closed"])
    m2.metric("TP", stats["tp"])
    m3.metric("SL", stats["sl"])
    m4.metric("R moyen", f"{stats['avg_r']:+.2f}" if stats["avg_r"] is not None else "—")

# ---- 4. Analyse SMC en direct -------------------------------------------------
with tab_smc:
    c1, c2 = st.columns((1, 3))
    pair = c1.selectbox("Paire", ["EURUSD", "GBPUSD", "XAUUSD"], index=0)
    tf = c2.selectbox("Timeframe", ["15m", "1h", "4h"], index=0)
    try:
        result, df = smc_view(pair, tf)
        ctx = result["context"]
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Tendance", result["trend"]["state"])
        k2.metric("ATR", round(result["atr"], 6))
        k3.metric("OB actifs proches",
                  "sous" if ctx["nearest_ob_below"] else "—",
                  f"{ctx['nearest_ob_below']['distance_atr']} ATR"
                  if ctx["nearest_ob_below"] else "aucun")
        k4.metric("Pools intacts",
                  f"{ctx['untouched_pools_below']} bas / {ctx['untouched_pools_above']} haut")
        st.plotly_chart(build_smc_figure(result, df, last_n=250),
                        use_container_width=True)
        st.caption("OB vert/rouge (grisé = invalidé) · FVG bleu/orange · pointillés "
                   "jaunes = liquidité intacte · diamants orange = CHoCH · ⚡ = sweep")
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Données momentanément indisponibles ({type(exc).__name__}). "
                   "Réessayez dans un instant.")

# ---- 5. Cartes de signal --------------------------------------------------------
with tab_cards:
    cards_dir = ROOT / "data" / "cards"
    cards = sorted(cards_dir.glob("*.svg"), reverse=True) if cards_dir.exists() else []
    if not cards:
        st.info("Aucune carte générée — elles apparaissent à chaque signal émis "
                "(data/cards/).")
    else:
        names = [c.name for c in cards]
        chosen = st.selectbox("Carte", names, index=0)
        components.html((cards_dir / chosen).read_text(encoding="utf-8"),
                        height=760, scrolling=True)

st.caption("Source : Yahoo Finance / ForexFactory · Moteur : analyse SMC multi-agents · "
           "Ce tableau de bord ne constitue pas un conseil financier.")
