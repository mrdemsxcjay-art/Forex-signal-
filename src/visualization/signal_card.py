"""Carte de signal SVG professionnelle — icônes vectorielles, AUCUN emoji.

Raison d'être : la spécification exige des icônes professionnelles SVG.
Telegram ne rendant pas le SVG, la carte SVG est le visuel « officiel » du
signal (dashboard, archives, aperçu) ; le message Telegram garde la mise en
page texte à filets (━) équivalente.

Tout est dessiné en SVG pur (cercles, chemins, arcs) : fichier autonome,
affichable dans n'importe quel navigateur/visionneuse, sans dépendance.
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from ..signals.models import Signal, pip_spec

# Palette professionnelle (sobre, contrastée)
BG = "#0E1726"
PANEL = "#16233A"
ACCENT_LONG = "#2ECC71"
ACCENT_SHORT = "#E74C3C"
TEXT = "#EAF0F8"
MUTED = "#8FA3BF"
GOLD = "#F5C542"

FONT = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"


def _decimals(pair: str) -> int:
    pip_size, _ = pip_spec(pair)
    return 5 if pip_size <= 0.001 else 3 if pip_size <= 0.05 else 2


# --------------------------------------------------------------------------- #
#  Icônes SVG (24x24, stroke professionnel)
# --------------------------------------------------------------------------- #
def icon_arrow(direction: str, x: float, y: float, color: str) -> str:
    d = "M12 4 L19 14 L14.5 14 L14.5 20 L9.5 20 L9.5 14 L5 14 Z"
    if direction == "SHORT":
        d = "M12 20 L5 10 L9.5 10 L9.5 4 L14.5 4 L14.5 10 L19 10 Z"
    return (f'<g transform="translate({x},{y})">'
            f'<path d="{d}" fill="{color}"/></g>')


def icon_target(x: float, y: float, color: str) -> str:
    return (
        f'<g transform="translate({x},{y})" fill="none" stroke="{color}" stroke-width="2">'
        f'<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/>'
        f'<circle cx="12" cy="12" r="1" fill="{color}"/></g>'
    )


def icon_shield(x: float, y: float, color: str) -> str:
    return (
        f'<g transform="translate({x},{y})">'
        f'<path d="M12 3 L20 6 V12 C20 17 16.5 20 12 21 C7.5 20 4 17 4 12 V6 Z" '
        f'fill="none" stroke="{color}" stroke-width="2"/>'
        f'<path d="M8.5 12 L11 14.5 L16 9" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></g>'
    )


def icon_crosshair(x: float, y: float, color: str) -> str:
    return (
        f'<g transform="translate({x},{y})" fill="none" stroke="{color}" stroke-width="2">'
        f'<circle cx="12" cy="12" r="7"/><path d="M12 2 V7 M12 17 V22 M2 12 H7 M17 12 H22"/>'
        f'<circle cx="12" cy="12" r="1.2" fill="{color}"/></g>'
    )


def icon_scale(x: float, y: float, color: str) -> str:
    return (
        f'<g transform="translate({x},{y})" fill="none" stroke="{color}" stroke-width="2">'
        f'<path d="M12 4 V20 M6 20 H18 M12 5 L5 9 M12 5 L19 9"/>'
        f'<path d="M2.5 13 A2.8 2.8 0 0 0 7.5 13 L5 9 Z" fill="{color}" stroke="none"/>'
        f'<path d="M16.5 13 A2.8 2.8 0 0 0 21.5 13 L19 9 Z" fill="{color}" stroke="none"/></g>'
    )


def icon_clock(x: float, y: float, color: str) -> str:
    return (
        f'<g transform="translate({x},{y})" fill="none" stroke="{color}" stroke-width="2">'
        f'<circle cx="12" cy="12" r="9"/><path d="M12 7 V12 L15.5 14" '
        f'stroke-linecap="round"/></g>'
    )


def icon_check(x: float, y: float, color: str) -> str:
    return (
        f'<g transform="translate({x},{y})" fill="none" stroke="{color}" stroke-width="2.6" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="M4.5 12.5 L9.5 17.5 L19.5 6.5"/></g>'
    )


def _gauge(cx: float, cy: float, r: float, score: int) -> str:
    """Jauge de score : arc 240°, aiguille, valeur centrale."""
    import math

    start, end = 150, 390  # degrés
    angle = math.radians(start + (end - start) * min(score, 100) / 100)
    nx, ny = cx + (r - 8) * math.cos(angle), cy + (r - 8) * math.sin(angle)
    arc = (
        f'M {cx + r * math.cos(math.radians(start))} {cy + r * math.sin(math.radians(start))} '
        f'A {r} {r} 0 1 1 {cx + r * math.cos(math.radians(end))} {cy + r * math.sin(math.radians(end))}'
    )
    return (
        f'<path d="{arc}" fill="none" stroke="{PANEL}" stroke-width="10" stroke-linecap="round"/>'
        f'<path d="{arc}" fill="none" stroke="{GOLD}" stroke-width="10" stroke-linecap="round" '
        f'stroke-dasharray="{2 * math.pi * r * 240 / 360 * min(score, 100) / 100:.1f} 999"/>'
        f'<line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{TEXT}" stroke-width="2.5"/>'
        f'<circle cx="{cx}" cy="{cy}" r="4" fill="{TEXT}"/>'
        f'<text x="{cx}" y="{cy + r * 0.62}" text-anchor="middle" fill="{TEXT}" '
        f'font-family="{FONT}" font-size="30" font-weight="700">{score}</text>'
        f'<text x="{cx}" y="{cy + r * 0.62 + 18}" text-anchor="middle" fill="{MUTED}" '
        f'font-family="{FONT}" font-size="12">SCORE / 100</text>'
    )


# --------------------------------------------------------------------------- #
def render_signal_card(signal: Signal) -> str:
    """Rend la carte SVG complète (chaîne) — intégrable en dashboard."""
    pair_disp = f"{signal.pair[:3]}/{signal.pair[3:]}"
    accent = ACCENT_LONG if signal.direction == "LONG" else ACCENT_SHORT
    dec = _decimals(signal.pair)
    risk = signal.risk
    width, height = 660, 700
    y0 = 0

    rows = [
        (icon_crosshair, "Entree", f"{risk.entry:.{dec}f}"),
        (icon_target, "Objectif TP", f"{risk.tp:.{dec}f}  (+{risk.tp_pips:.0f} pips)"),
        (icon_shield, "Stop SL", f"{risk.sl:.{dec}f}  (-{risk.risk_pips:.0f} pips)"),
        (icon_scale, "Risque/Rendement", f"1 : {risk.rr:.1f}   ({risk.lots} lots)"),
        (icon_clock, "Session", signal.session),
    ]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT}">',
        f'<rect width="{width}" height="{height}" rx="18" fill="{BG}"/>',
        # ---- bandeau ----
        f'<rect x="0" y="{y0}" width="{width}" height="96" rx="18" fill="{PANEL}"/>',
        f'<rect x="0" y="{y0 + 78}" width="{width}" height="18" fill="{PANEL}"/>',
        f'<rect x="28" y="{y0 + 26}" width="6" height="44" rx="3" fill="{accent}"/>',
        icon_arrow(signal.direction, 46, y0 + 26, accent),
        f'<text x="88" y="{y0 + 52}" fill="{TEXT}" font-size="30" font-weight="700">'
        f'{escape(pair_disp)}</text>',
        f'<text x="88" y="{y0 + 76}" fill="{accent}" font-size="17" font-weight="600" '
        f'letter-spacing="2">SIGNAL {signal.direction} · GRADE {escape(signal.grade)}</text>',
        f'<rect x="{width - 150}" y="{y0 + 30}" width="120" height="40" rx="20" '
        f'fill="none" stroke="{GOLD}" stroke-width="1.6"/>',
        f'<text x="{width - 90}" y="{y0 + 56}" text-anchor="middle" fill="{GOLD}" '
        f'font-size="19" font-weight="700">{signal.score}/100</text>',
        # ---- jauge ----
        _gauge(width / 2, 230, 90, signal.score),
    ]

    # ---- lignes du plan ----
    y = 380
    for icon_fn, label, value in rows:
        parts.append(f'<rect x="28" y="{y}" width="{width - 56}" height="52" rx="10" fill="{PANEL}"/>')
        parts.append(icon_fn(44, y + 14, MUTED))
        parts.append(f'<text x="84" y="{y + 32}" fill="{MUTED}" font-size="14">{escape(label)}</text>')
        parts.append(f'<text x="{width - 44}" y="{y + 32}" text-anchor="end" fill="{TEXT}" '
                     f'font-size="16" font-weight="600">{escape(value)}</text>')
        y += 62

    # ---- confluences ----
    y += 8
    parts.append(f'<text x="32" y="{y}" fill="{TEXT}" font-size="14" font-weight="700" '
                 f'letter-spacing="1.5">CONFLUENCES DETECTEES</text>')
    y += 14
    for conf in signal.confluences[:6]:
        y += 26
        parts.append(icon_check(32, y - 18, ACCENT_LONG if signal.direction == "LONG" else ACCENT_SHORT))
        parts.append(f'<text x="64" y="{y}" fill="{TEXT}" font-size="13.5">'
                     f'{escape(str(conf)[:64])}</text>')

    # ---- pied ----
    parts.append(f'<line x1="28" y1="{height - 64}" x2="{width - 28}" y2="{height - 64}" '
                 f'stroke="{PANEL}" stroke-width="2"/>')
    parts.append(f'<text x="32" y="{height - 40}" fill="{MUTED}" font-size="12.5">'
                 f'{escape(signal.created_at)} · {escape(pair_disp)} · moteur multi-agents SMC</text>')
    parts.append(f'<text x="32" y="{height - 20}" fill="{MUTED}" font-size="11.5">'
                 f'Trading = risque. DYOR — signal analytique, pas un conseil financier.</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def save_signal_card(signal: Signal, out_dir: str | Path = "data/cards") -> Path:
    """Écrit la carte SVG sur disque : data/cards/signal_<paire>_<id>.svg."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = signal.created_at.replace(":", "").replace(" ", "_").replace("UTC", "")
    path = out_dir / f"signal_{signal.pair}_{stamp}.svg"
    path.write_text(render_signal_card(signal), encoding="utf-8")
    return path
