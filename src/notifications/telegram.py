"""Notifications Telegram — robot EUR/USD UNIQUEMENT (formats de la spec).

Deux messages :
  - SIGNAL EUR/USD (ouverture) : technique 5 portes + fondamentale (DXY,
    news, biais macro) + plan 1:3 + confiance % + niveau de risque ;
  - CLÔTURE EUR/USD : résultat, analyse de sortie technique et fondamentale,
    leçon retenue.

La règle n°1 s'applique aussi ici : ces formats ne concernent que EUR/USD.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import requests

from ..signals.models import Signal, pip_spec

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

PAIR_REPLY = "Je suis configure uniquement pour EUR/USD pour maximiser la precision."


def _fmt(pair: str, value: float) -> str:
    return f"{value:.5f}"


def format_signal_message(signal: Signal) -> str:
    """Format d'ouverture EUR/USD — spec finale."""
    sens = "BUY" if signal.direction == "LONG" else "SELL"
    tf = signal.timeframes or {}
    fund = signal.fundamental or {}
    pip_size, _ = pip_spec(signal.pair)
    sl_pips = abs(signal.risk.entry - signal.risk.sl) / pip_size
    tp_pips = abs(signal.risk.tp - signal.risk.entry) / pip_size
    return "\n".join([
        f"\U0001F4CA SIGNAL EUR/USD - {sens}",
        f"\u23F0 {signal.created_at} | Confiance: {signal.score}% | Paire: EUR/USD UNIQUEMENT",
        "",
        "\U0001F4C8 TECHNIQUE EUR/USD:",
        f"D1: {tf.get('D1', '—')}",
        f"H4: {tf.get('H4', '—')}",
        f"H1: {tf.get('H1', '—')}",
        f"M15: {tf.get('M15', '—')}",
        f"M5/M30: {tf.get('M5/M30', '—')}",
        "",
        "\U0001F30D FONDAMENTAL EUR/USD:",
        f"DXY: {signal.dxy_txt or 'indisponible'}",
        f"News: {signal.news_txt or '—'}",
        f"Biais macro: {signal.macro_bias or 'neutre'}",
        f"Risque: {signal.risk_label}",
        "",
        "\U0001F3AF PLAN EUR/USD:",
        f"Entree: {_fmt(signal.pair, signal.risk.entry)}",
        f"SL: {_fmt(signal.pair, signal.risk.sl)} ({sl_pips:.0f} pips)",
        f"TP: {_fmt(signal.pair, signal.risk.tp)} ({tp_pips:.0f} pips = R:R 1:{signal.risk.rr:.0f})",
        "",
        "Confluences :",
        *(f"  - {c}" for c in signal.confluences[:6]),
        "",
        "Trading = risque. DYOR.",
    ])


def format_closure_message(resolution: dict, signal_row, stats: dict,
                           dxy_txt: str = "") -> str:
    """Format de clôture EUR/USD — analyse de sortie + leçon."""
    issue = {"TP_ATTEINT": "TP", "SL_ATTEINT": "SL", "EXPIRE": "EXPIRATION"}.get(
        resolution["resultat"], resolution["resultat"])
    try:
        gates = json.loads(signal_row["timeframes"] or "{}")
    except (TypeError, json.JSONDecodeError):
        gates = {}

    exit_tech = {
        "TP_ATTEINT": "objectif 1:3 atteint ; la structure H1 reste intacte, sortie sur ratio",
        "SL_ATTEINT": "stop structurel touché ; la zone de retest a cede, invalidation courte",
        "EXPIRE": "24 h sans resolution : absence de dynamique apres l'entree",
    }.get(resolution["resultat"], "sortie standard")
    if gates.get("H1"):
        exit_tech += f" (contexte entree : H1 {gates['H1']})"

    exit_fund = f"DXY {dxy_txt}" if dxy_txt else "DXY indisponible a la cloture"

    lecon = {
        "TP_ATTEINT": "L'alignement D1/H4/H1 + retest paie au 1:3 : laisser courir les gagnants reste la bonne politique.",
        "SL_ATTEINT": "Plan respecte, marche contraire : -1R maîtrise, aucune revenge trade. La prochaine confluence viendra.",
        "EXPIRE": "Pas de dynamique = information : sortir sans dommage plutot que subir.",
    }.get(resolution["resultat"], "Journaliser la sortie.")

    pip_size, _ = pip_spec(str(resolution["paire"]))
    entry = float(signal_row["entree"])
    pips = abs(float(resolution["exit_price"]) - entry) / pip_size
    signe = "+" if resolution["exit_r"] >= 0 else "-"

    return "\n".join([
        f"\U0001F512 CLOTURE EUR/USD - {issue}",
        f"Resultat: {signe}{pips:.0f} pips | {resolution['exit_r']:+.1f}R (R:R 1:3)",
        f"Analyse sortie technique: {exit_tech}",
        f"Analyse sortie fondamentale: {exit_fund}",
        f"Lecon: {lecon}",
        "",
        f"Solde cumule : {stats.get('total_r', 0.0):+.1f}R sur {stats.get('closed', 0)} cloture(s) "
        f"(winrate {stats['winrate'] * 100:.0f} %)" if stats.get("winrate") is not None
        else f"Solde cumule : {stats.get('total_r', 0.0):+.1f}R",
        "",
        "Trading = risque. DYOR.",
    ])


class TelegramSender:
    """Envoi des messages EUR/USD via la Bot API Telegram."""

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True,
                 timeout: int = 15, max_retries: int = 3) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.enabled = enabled and bool(self.bot_token) and bool(self.chat_id)
        self.timeout = timeout
        self.max_retries = max_retries

    @classmethod
    def from_config(cls, cfg) -> "TelegramSender":
        return cls(cfg.telegram.bot_token, cfg.telegram.chat_id, cfg.telegram.enabled)

    def send_text(self, text: str) -> bool:
        if not self.enabled:
            logger.warning("Telegram non configure — envoi ignore")
            return False
        url = API_URL.format(token=self.bot_token)
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                if resp.ok and resp.json().get("ok"):
                    return True
                logger.warning("Telegram HTTP %s : %s", resp.status_code, resp.text[:120])
            except requests.RequestException as exc:
                logger.warning("Telegram tentative %d/%d echouee : %s", attempt, self.max_retries, exc)
            time.sleep(1.0 * attempt)
        return False

    def send_signal(self, signal: Signal) -> bool:
        return self.send_text(format_signal_message(signal))
