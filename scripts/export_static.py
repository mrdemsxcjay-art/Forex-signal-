"""Exporte un SITE STATIQUE de statut + ZIP prêt pour Netlify (drag-and-drop).

Pourquoi : Netlify n'héberge que du statique — le MOTEUR (python -m src.main,
boucle 24/7 avec SQLite) doit rester sur une VM gratuite (Oracle) ou un PC.
Ce script publie un instantané consultable depuis n'importe où :
    KPIs · positions ouvertes · historique · courbe d'équité SVG pur ·
    cartes de signal SVG · état du dernier cycle moteur.

Usage :
    python scripts/export_static.py          # -> data/site/ + data/netlify_site.zip
    python scripts/export_static.py --demo   # force le jeu de démonstration

Déploiement : https://app.netlify.com/drop  (glisser-déposer le zip).
Rafraîchissement : relancer ce script sur l'hôte (cron) puis redéposer le zip,
ou utiliser l'API Netlify (voir docs/DEPLOIEMENT.md).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.helpers import (
    RESULT_COLORS,
    demo_signals,
    equity_curve,
    load_last_cycles,
    stats_from_frame,
)
from src.storage.database import SignalDatabase

ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT / "data" / "site"
ZIP_PATH = ROOT / "data" / "netlify_site.zip"

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0E1726; color: #EAF0F8;
       font-family: 'Segoe UI', Arial, sans-serif; padding: 28px; }
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 22px; margin: 18px 0 6px; }
h2 { font-size: 16px; margin: 26px 0 12px; color: #F5C542;
     letter-spacing: 1px; text-transform: uppercase; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 12px; }
.card { background: #16233A; border-radius: 10px; padding: 14px 16px;
        border-left: 3px solid #F5C542; }
.card .k { color: #8FA3BF; font-size: 12px; text-transform: uppercase; }
.card .v { font-size: 24px; font-weight: 700; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th { text-align: left; color: #8FA3BF; font-weight: 600; padding: 8px 10px;
     border-bottom: 1px solid #223350; }
td { padding: 8px 10px; border-bottom: 1px solid #1A2A45; }
tr:hover td { background: #142038; }
.demo { background: #3A2E16; color: #F5C542; padding: 10px 14px; border-radius: 8px;
        border-left: 3px solid #F5C542; font-weight: 600; margin: 12px 0; }
.pill { padding: 2px 10px; border-radius: 12px; font-weight: 700; font-size: 12px; }
.paircard { background: #16233A; border-radius: 10px; padding: 14px;
            border-left: 3px solid #8FA3BF; }
.scorebar { background: #223350; border-radius: 6px; height: 8px; margin-top: 8px; }
.scorebar > div { height: 8px; border-radius: 6px; background: #1F5FA8; }
.blockers { color: #8FA3BF; font-size: 12px; margin-top: 6px; }
.footer { color: #8FA3BF; font-size: 12px; margin-top: 34px; border-top: 1px solid #223350;
          padding-top: 14px; line-height: 1.6; }
.svgcard { margin: 14px 0; border-radius: 12px; overflow: hidden;
           background: #0E1726; border: 1px solid #223350; }
.pos { background: #16233A; border-radius: 10px; padding: 14px; margin: 10px 0; }
"""

LOGO = """
<svg width="230" height="54" viewBox="0 0 230 54" xmlns="http://www.w3.org/2000/svg">
  <rect x="2" y="6" width="46" height="42" rx="9" fill="#16233A"/>
  <line x1="14" y1="12" x2="14" y2="42" stroke="#2ECC71" stroke-width="3"/>
  <rect x="10" y="20" width="8" height="14" rx="2" fill="#2ECC71"/>
  <line x1="25" y1="8" x2="25" y2="46" stroke="#E74C3C" stroke-width="3"/>
  <rect x="21" y="14" width="8" height="16" rx="2" fill="#E74C3C"/>
  <line x1="36" y1="16" x2="36" y2="38" stroke="#F5C542" stroke-width="3"/>
  <rect x="32" y="22" width="8" height="10" rx="2" fill="#F5C542"/>
  <text x="58" y="26" fill="#EAF0F8" font-size="17" font-weight="700">SIGNAUX FOREX</text>
  <text x="58" y="44" fill="#8FA3BF" font-size="12">SMC multi-agents · statut public</text>
</svg>
"""


def pill(resultat: str) -> str:
    color = RESULT_COLORS.get(resultat, "#8FA3BF")
    label = {"TP_ATTEINT": "Objectif", "SL_ATTEINT": "Stop",
             "EXPIRE": "Expiration", "EN_COURS": "En cours"}.get(resultat, resultat)
    return (f'<span class="pill" style="background:{color}22;color:{color}">'
            f"{label}</span>")


def kpi_card(label: str, value: str) -> str:
    return f'<div class="card"><div class="k">{label}</div><div class="v">{value}</div></div>'


def equity_svg(eq) -> str:
    """Courbe d'équité en SVG pur (aucune librairie, aucun JavaScript)."""
    if eq is None or eq.empty:
        return '<div class="card">Aucune clôture à ce jour — la courbe apparaîtra \
au premier TP/SL/EXPIRE.</div>'
    w, h, pad = 760, 240, 42
    values = [0.0] + [float(v) for v in eq["equity"]]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    def x(i): return pad + i * (w - 2 * pad) / (len(values) - 1)
    def y(v): return h - pad - (v - lo) * (h - 2 * pad) / span
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    area = f"M {x(0):.1f},{y(lo):.1f} L " + pts.replace(" ", " L ") + \
           f" L {x(len(values)-1):.1f},{y(lo):.1f} Z"
    last = values[-1]
    color = "#2ECC71" if last >= 0 else "#E74C3C"
    zero_y = y(0) if lo <= 0 <= hi else None
    zero_line = (f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{w-pad}" y2="{zero_y:.1f}" '
                 f'stroke="#8FA3BF" stroke-dasharray="4 4" stroke-width="1"/>') if zero_y else ""
    return f'''<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" width="100%">
      <path d="{area}" fill="{color}18"/>
      <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5"
                stroke-linejoin="round"/>
      {zero_line}
      <text x="{pad}" y="{pad-14}" fill="#8FA3BF" font-size="12">{hi:+.1f}R</text>
      <text x="{pad}" y="{h-pad+22}" fill="#8FA3BF" font-size="12">{lo:+.1f}R</text>
      <text x="{w-pad}" y="{h-pad+22}" fill="{color}" font-size="13"
            text-anchor="end" font-weight="700">Solde : {last:+.1f}R</text>
      <text x="{w//2}" y="{h-8}" fill="#8FA3BF" font-size="11"
            text-anchor="middle">clôtures, ordre chronologique</text>
    </svg>'''


def build_html(signals, stats: dict, cycles: dict | None, cards: list[Path]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    demo = getattr(build_html, "_demo", False)

    kpis = "".join([
        kpi_card("Signaux", str(stats["total"])),
        kpi_card("En cours", str(stats["open"])),
        kpi_card("Winrate", f"{stats['winrate']*100:.0f} %" if stats["winrate"] is not None else "—"),
        kpi_card("R cumulé", f"{stats['total_r']:+.1f}"),
        kpi_card("TP / SL", f"{stats['tp']} / {stats['sl']}"),
    ])

    # Positions ouvertes
    open_df = signals[signals["resultat"] == "EN_COURS"] if not signals.empty else signals.iloc[0:0]
    if open_df.empty:
        positions = '<div class="card">Aucune position ouverte.</div>'
    else:
        rows = []
        for _, r in open_df.iterrows():
            rows.append(
                f'<div class="pos"><b>{r["paire"][:3]}/{r["paire"][3:]} {r["type"]}</b> '
                f'· score {r["score"]} ({r.get("grade") or "—"}) · {r.get("session") or ""}<br>'
                f'<span style="color:#8FA3BF">entrée {r["entree"]} · TP {r["tp"]} · '
                f'SL {r["sl"]} · depuis {str(r["date"])[:16]}</span></div>')
        positions = "".join(rows)

    # Historique
    if signals.empty:
        history = '<div class="card">Aucun signal en base.</div>'
    else:
        head = ("<tr><th>Date</th><th>Paire</th><th>Sens</th><th>Score</th>"
                "<th>Entrée</th><th>TP</th><th>SL</th><th>R/R</th>"
                "<th>Issue</th><th>R</th></tr>")
        body = []
        for _, r in signals.head(60).iterrows():
            r_txt = f'{r["exit_r"]:+.1f}' if r["exit_r"] is not None and str(r["exit_r"]) != "None" else "—"
            body.append(
                f'<tr><td>{str(r["date"])[:16]}</td><td>{r["paire"]}</td>'
                f'<td>{r["type"]}</td><td>{r["score"]}</td>'
                f'<td>{r["entree"]}</td><td>{r["tp"]}</td><td>{r["sl"]}</td>'
                f'<td>1:{r["rr"] if r["rr"] is not None else "—"}</td>'
                f'<td>{pill(r["resultat"])}</td><td>{r_txt}</td></tr>')
        history = f'<table>{head}{"".join(body)}</table>'

    # État du dernier cycle
    if not cycles or not cycles.get("pairs"):
        cycle_html = ('<div class="card">Moteur sans rapport récent — '
                      'lancez python -m src.main sur l\'hôte.</div>')
    else:
        cards_html = []
        for pair, info in cycles["pairs"].items():
            score = int(info.get("score") or 0)
            state = "SIGNAL" if info.get("signal") else ("aligné" if info.get("aligned") else "bloqué")
            border = "#2ECC71" if info.get("signal") else ("#F5C542" if info.get("aligned") else "#8FA3BF")
            tf = info.get("timeframes") or {}
            blockers = "<br>".join(f"· {b}" for b in info.get("blockers", []))
            cards_html.append(
                f'<div class="paircard" style="border-left-color:{border}">'
                f'<b>{pair}</b> — <span style="color:{border}">{state}</span><br>'
                f'score {score}/100<div class="scorebar"><div style="width:{score}%"></div></div>'
                f'<div class="blockers">D1={tf.get("D1","—")} · H4={tf.get("H4","—")} · '
                f'M15={tf.get("M15","—")}<br>{blockers}</div></div>')
        cycle_html = (f'<div class="grid">{"".join(cards_html)}</div>'
                      f'<div class="blockers" style="margin-top:10px">Dernier cycle : '
                      f'{cycles.get("cycle", "?")} · mis à jour '
                      f'{str(cycles.get("updated_at", ""))[:19]} UTC</div>')

    # Cartes de signal (SVG inline)
    cards_html = "".join(
        f'<div class="svgcard">{p.read_text(encoding="utf-8")}</div>'
        for p in cards[:2]
    ) or '<div class="card">Aucune carte générée (apparaissent à chaque signal).</div>'

    demo_banner = ('<div class="demo">DONNÉES DE DÉMONSTRATION — la base de production '
                   'est vide sur cet instantané.</div>') if demo else ""

    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signaux Forex — Statut</title>
<style>{CSS}</style></head>
<body><div class="wrap">
{LOGO}
{demo_banner}
<h2>Compte de signaux</h2><div class="grid">{kpis}</div>
<h2>Positions ouvertes</h2>{positions}
<h2>Historique</h2>{history}
<h2>Courbe d'équité</h2><div class="card">{equity_svg(equity_curve(signals[signals['resultat'] != 'EN_COURS'] if not signals.empty else signals))}</div>
<h2>Dernier cycle moteur (transparence)</h2>{cycle_html}
<h2>Dernières cartes de signal</h2>{cards_html}
<div class="footer">
  Instantané statique généré le {now} — le moteur (analyse 24/7) tourne sur son hôte ;
  cette page est publiée sur Netlify et ne constitue pas un conseil financier. DYOR.<br>
  Source : Yahoo Finance / ForexFactory · Moteur SMC multi-agents ·
  rafraîchissement : relancer scripts/export_static.py puis redéposer le zip.
</div>
</div></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Export du site statique (Netlify)")
    parser.add_argument("--demo", action="store_true", help="force le jeu de démonstration")
    args = parser.parse_args()

    db_path = ROOT / "data" / "signals.db"
    signals = None
    if db_path.exists() and not args.demo:
        db = SignalDatabase(db_path)
        signals = db.recent(limit=200)
    demo = signals is None or signals.empty
    build_html._demo = demo
    if demo:
        signals = demo_signals()

    stats = stats_from_frame(signals)
    cycles = load_last_cycles(ROOT / "data" / "last_cycle.json")
    cards_dir = ROOT / "data" / "cards"
    cards = sorted(cards_dir.glob("*.svg"), reverse=True) if cards_dir.exists() else []

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    html = build_html(signals, stats, cycles, cards)
    index = SITE_DIR / "index.html"
    index.write_text(html, encoding="utf-8")

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(index, arcname="index.html")
        for card in cards[:6]:
            zf.write(card, arcname=f"cards/{card.name}")

    # Auto-vérification
    with zipfile.ZipFile(ZIP_PATH) as zf:
        names = zf.namelist()
        ok_html = "index.html" in names
        content_ok = "SIGNAUX FOREX" in html and "Courbe d'équité" in html \
            and "Dernier cycle moteur" in html
        no_emoji = not any(0x1F000 <= ord(c) <= 0x1FAFF for c in html)

    mode = "DÉMONSTRATION" if demo else "PRODUCTION"
    print(f"Site statique généré  : {index.relative_to(ROOT)}  ({len(html):,} caractères)")
    print(f"Mode                  : {mode} · stats : {stats['total']} signaux, "
          f"winrate {stats['winrate']}, {stats['total_r']:+.1f}R")
    print(f"ZIP Netlify           : {ZIP_PATH.relative_to(ROOT)} "
          f"({ZIP_PATH.stat().st_size / 1024:.0f} Ko, {len(names)} fichier(s))")
    print(f"Auto-vérification     : index={'OK' if ok_html else 'ÉCHEC'}, "
          f"sections={'OK' if content_ok else 'ÉCHEC'}, sans emoji={'OK' if no_emoji else 'ÉCHEC'}")
    print("\nDéploiement : https://app.netlify.com/drop -> glisser-déposer "
          f"{ZIP_PATH.name} (site en ligne en ~10 secondes).")
    return 0 if (ok_html and content_ok and no_emoji) else 1


if __name__ == "__main__":
    sys.exit(main())
