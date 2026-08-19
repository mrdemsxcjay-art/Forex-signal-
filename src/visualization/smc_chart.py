"""Graphique interactif des zones SMC détectées (Plotly, 100 % offline).

`build_smc_figure` -> figure réutilisable (dashboard Streamlit).
`plot_smc`         -> écrit le HTML autonome (scripts/plot_smc.py).

Rendu : bougies + rectangles OB (verts/rouges, grisés si invalidés),
rectangles FVG (bleu/orange), lignes pointillées des pools de liquidité
intacts, marqueurs BOS/CHoCH (triangles/losanges) et sweeps (⚡).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

COLORS = {
    "ob_bull": "rgba(38,166,91,0.18)",
    "ob_bear": "rgba(239,83,80,0.18)",
    "ob_dead": "rgba(120,120,120,0.08)",
    "fvg_bull": "rgba(41,128,220,0.15)",
    "fvg_bear": "rgba(230,150,20,0.15)",
    "pool": "rgba(255,215,0,0.9)",
}


def build_smc_figure(result: dict, df: pd.DataFrame, last_n: int = 300,
                     title: str | None = None, height: int = 680) -> go.Figure:
    """Construit la figure Plotly complète (aucun fichier écrit)."""
    window = df.iloc[-last_n:]
    t0 = window.index[0]
    t_end = window.index[-1]
    x = [ts.isoformat() for ts in window.index]

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=x, open=window["open"], high=window["high"], low=window["low"],
        close=window["close"], name="Bougies", increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
    ))

    ev = result["events"]

    # --- Order Blocks (rectangles) ---
    for ob in ev["order_blocks"]:
        start = pd.Timestamp(ob["origin_time"])
        end = pd.Timestamp(ob["invalidated_at"] or t_end)
        if end < t0:
            continue
        start = max(start, t0)
        color = (COLORS["ob_dead"] if ob["status"] == "invalidated"
                 else COLORS["ob_bull"] if ob["direction"] == "bullish"
                 else COLORS["ob_bear"])
        fig.add_shape(type="rect", x0=start.isoformat(), x1=end.isoformat(),
                      y0=ob["zone_bottom"], y1=ob["zone_top"],
                      fillcolor=color, line=dict(width=0))

    # --- FVG (rectangles) ---
    for fvg in ev["fair_value_gaps"]:
        start = pd.Timestamp(fvg["displacement_time"])
        end = pd.Timestamp(fvg["filled_at"] or t_end)
        if end < t0:
            continue
        start = max(start, t0)
        color = COLORS["fvg_bull"] if fvg["direction"] == "bullish" else COLORS["fvg_bear"]
        fig.add_shape(type="rect", x0=start.isoformat(), x1=end.isoformat(),
                      y0=fvg["zone_bottom"], y1=fvg["zone_top"],
                      fillcolor=color, line=dict(width=0))

    # --- Pools de liquidité intacts (lignes pointillées) ---
    for pool in ev["liquidity"]["equal_highs"] + ev["liquidity"]["equal_lows"]:
        if pool["status"] != "untouched":
            continue
        start = max(pd.Timestamp(pool["times"][-1]), t0)
        fig.add_shape(type="line", x0=start.isoformat(), x1=t_end.isoformat(),
                      y0=pool["level"], y1=pool["level"],
                      line=dict(color=COLORS["pool"], width=1, dash="dot"))

    # --- BOS / CHoCH (marqueurs) ---
    def _markers(events, ev_type, symbol, color):
        pts = [(e["break_time"], e["break_close"]) for e in events if e["type"] == ev_type]
        pts = [(t, y) for t, y in pts if pd.Timestamp(t) >= t0]
        if pts:
            fig.add_trace(go.Scatter(
                x=[p[0] for p in pts], y=[p[1] for p in pts], mode="markers+text",
                text=[ev_type] * len(pts), textposition="bottom center",
                textfont=dict(size=8, color=color),
                marker=dict(symbol=symbol, size=11, color=color),
                name=ev_type, showlegend=True,
            ))

    _markers(ev["structure"], "BOS", "triangle-up", "#26a69a")
    _markers(ev["structure"], "CHoCH", "diamond", "#ffa726")

    # --- Sweeps ---
    sweeps = [s for s in ev["liquidity"]["sweeps"] if pd.Timestamp(s["time"]) >= t0]
    if sweeps:
        fig.add_trace(go.Scatter(
            x=[s["time"] for s in sweeps], y=[s["level"] for s in sweeps],
            mode="text", text=["⚡"] * len(sweeps), textfont=dict(size=14),
            name="Sweep", showlegend=True,
        ))

    fig.update_layout(
        title=title or f"{result['pair']} {result['timeframe']} — zones SMC "
                       f"(tendance : {result['trend']['state']})",
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        height=height,
        margin=dict(l=40, r=20, t=60, b=30),
        legend=dict(orientation="h", y=1.02, x=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def plot_smc(result: dict, df: pd.DataFrame, out_path: str | Path,
             last_n: int = 300, title: str | None = None) -> Path:
    """Génère le HTML autonome (plotly.js embarqué) — ouvrable hors ligne."""
    fig = build_smc_figure(result, df, last_n=last_n, title=title)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(out_path, include_plotlyjs=True)
    return out_path
