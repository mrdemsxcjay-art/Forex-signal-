"""Notifications Telegram — robot EUR/USD, messages PROFESSIONNELS en HTML.

Mode HTML de la Bot API : gras, monospace pour les prix, hiérarchie nette.
Chaque message est lisible en 5 secondes :
  - ouverture : entête couleur direction, confiance, 5 sections balisées
    (technique / fondamentale / plan / confluences), pied d'avertissement ;
  - clôture   : résultat coloré, analyses de sortie, leçon ;
  - heartbeat : démarrage / arrêt propre.

Zéro dépendance, zéro clé API : fondamental réel (ForexFactory + DXY Yahoo).
"""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone

import requests

from ..signals.models import Signal, pip_spec

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

SEP = "━━━━━━━━━━━━━━━━━━━━"
SUB = "────────────────"

DIR_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}
DIR_FR = {"LONG": "ACHAT (BUY)", "SHORT": "VENTE (SELL)"}


def _e(value) -> str:
    """Échappe une valeur dynamique pour le mode HTML."""
    return html.escape(str(value))


def _px(value: float) -> str:
    return f"<code>{value:.5f}</code>"


# --------------------------------------------------------------------------- #
#  OUVERTURE DE SIGNAL
# --------------------------------------------------------------------------- #
def format_signal_message(signal: Signal) -> str:
    sens_emoji = DIR_EMOJI.get(signal.direction, "⚪")
    tf = signal.timeframes or {}
    fund = signal.fundamental or {}
    pip_size, _ = pip_spec(signal.pair)
    sl_pips = abs(signal.risk.entry - signal.risk.sl) / pip_size
    tp_pips = abs(signal.risk.tp - signal.risk.entry) / pip_size

    lignes = [
        f"<b>{sens_emoji} SIGNAL EUR/USD — {DIR_FR.get(signal.direction, signal.direction)}</b>",
        SEP,
        f"⏰ {_e(signal.created_at.replace(' UTC', ' GMT'))}",
        f"🎯 Confiance : <b>{signal.score}%</b>  ·  Grade <b>{_e(signal.grade)}</b>",
        f"⚖️ Ratio : <b>1:{signal.risk.rr:.0f}</b>  ·  Risque : <b>{_e(signal.risk_label)}</b>",
        f"🔒 Paire suivie : EUR/USD uniquement",
        "",
        "<b>📈 ANALYSE TECHNIQUE</b>",
        SUB,
        f"<b>D1</b> · Tendance de fond",
        f"   {_e(tf.get('D1', '—'))}",
        f"<b>H4</b> · Tendance principale",
        f"   {_e(tf.get('H4', '—'))}",
        f"<b>H1</b> · Direction autorisée",
        f"   {_e(tf.get('H1', '—'))}",
        f"<b>M15</b> · Zone de décision",
        f"   {_e(tf.get('M15', '—'))}",
        f"<b>M5/M30</b> · Timing d'entrée",
        f"   {_e(tf.get('M5/M30', '—'))}",
        "",
        "<b>🌍 ANALYSE FONDAMENTALE</b>",
        SUB,
        f"💵 <b>DXY</b> : {_e(signal.dxy_txt or 'indisponible')}",
        f"📅 <b>News</b> : {_e(signal.news_txt or 'aucune news HIGH EUR/USD < 24 h')}",
        f"🏛️ <b>Biais macro</b> : {_e(signal.macro_bias or 'neutre')}",
    ]
    drivers = fund.get("drivers") or []
    if drivers:
        lignes.append("   Surprises du jour :")
        lignes.extend(f"   ▪ {_e(d)}" for d in drivers[:2])
    lignes.extend([
        "",
        "<b>💰 PLAN DE TRADE</b>",
        SUB,
        f"🔵 <b>Entrée</b>   : {_px(signal.risk.entry)}",
        f"🔴 <b>Stop</b>     : {_px(signal.risk.sl)}  (−{sl_pips:.0f} pips)",
        f"🟢 <b>Objectif</b> : {_px(signal.risk.tp)}  (+{tp_pips:.0f} pips → 1:{signal.risk.rr:.0f})",
        f"📦 <b>Taille</b>   : {signal.risk.lots} lot(s)",
    ])
    if signal.confluences:
        lignes.extend([
            "",
            f"<b>✨ CONFLUENCES ({len(signal.confluences)})</b>",
            SUB,
        ])
        lignes.extend(f"✅ {_e(c)}" for c in signal.confluences[:6])
    lignes.extend([
        "",
        SEP,
        "⚠️ <i>Le trading comporte des risques. DYOR.</i>",
    ])
    return "\n".join(lignes)


# --------------------------------------------------------------------------- #
#  CLÔTURE DE SIGNAL
# --------------------------------------------------------------------------- #
def format_closure_message(resolution: dict, signal_row, stats: dict,
                           dxy_txt: str = "") -> str:
    import json as _json

    resultat = str(resolution["resultat"])
    issue = {"TP_ATTEINT": "🟢 OBJECTIF ATTEINT (TP)",
             "SL_ATTEINT": "🔴 STOP ATTEINT (SL)",
             "EXPIRE": "🟡 EXPIRATION (24 h)"}.get(resultat, resultat)
    try:
        gates = _json.loads(signal_row["timeframes"] or "{}")
    except (TypeError, _json.JSONDecodeError):
        gates = {}

    exit_tech = {
        "TP_ATTEINT": "Objectif 1:3 touché ; la structure H1 est restée intacte, sortie sur ratio planifiée.",
        "SL_ATTEINT": "Stop structurel touché ; la zone de retest a cédé, le setup est invalidé à court terme.",
        "EXPIRE": "24 h sans résolution : absence de dynamique après l'entrée.",
    }.get(resultat, "Sortie standard.")
    if gates.get("H1"):
        exit_tech += f" (Contexte d'entrée : H1 {_e(gates['H1'])})"

    lecon = {
        "TP_ATTEINT": "L'alignement D1/H4/H1 + retest paie au 1:3 — laisser courir les gagnants reste la bonne politique.",
        "SL_ATTEINT": "Plan respecté, marché contraire : −1R maîtrisé, aucune revenge trade. La prochaine confluence viendra.",
        "EXPIRE": "Pas de dynamique = information : sortir sans dommage plutôt que subir.",
    }.get(resultat, "Journaliser la sortie.")

    pip_size, _ = pip_spec(str(resolution["paire"]))
    entry = float(signal_row["entree"])
    pips = abs(float(resolution["exit_price"]) - entry) / pip_size
    signe = "+" if resolution["exit_r"] >= 0 else "−"
    winrate = stats.get("winrate")
    winrate_txt = f" · réussite <b>{winrate * 100:.0f}%</b>" if winrate is not None else ""

    return "\n".join([
        f"<b>🔒 CLÔTURE EUR/USD — {issue}</b>",
        SEP,
        f"📌 {_e(resolution['paire'])} {_e(resolution['type'])} (#{resolution['id']})",
        f"💵 Résultat : <b>{signe}{pips:.0f} pips → {resolution['exit_r']:+.1f}R</b> (ratio 1:3)",
        f"📈 Solde cumulé : <b>{stats.get('total_r', 0.0):+.1f}R</b> sur {stats.get('closed', 0)} clôture(s){winrate_txt}",
        "",
        f"<b>📊 Sortie technique</b>",
        SUB,
        f"   {exit_tech}",
        "",
        f"<b>🌍 Sortie fondamentale</b>",
        SUB,
        f"   DXY {_e(dxy_txt) if dxy_txt else 'indisponible à la clôture'}",
        "",
        f"<b>🧠 Leçon</b>",
        SUB,
        f"   {lecon}",
        "",
        SEP,
        "⚠️ <i>Le trading comporte des risques. DYOR.</i>",
    ])


# --------------------------------------------------------------------------- #
#  HEARTBEAT (démarrage / arrêt)
# --------------------------------------------------------------------------- #
def format_heartbeat(started: bool, info: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%d %b %Y • %H:%M GMT")
    if started:
        body = [
            "<b>🚀 MOTEUR EUR/USD — DÉMARRAGE</b>",
            SEP,
            "🔒 Paire : <b>EUR/USD uniquement</b>",
            f"⏱️ Cycle d'analyse : {info.get('interval', '?')} s",
            f"🎯 Confiance minimale : <b>{info.get('threshold', '?')}%</b>",
            "⚖️ Ratio 1:3 · News HIGH &lt; 2 h = blocage",
            f"⏰ {now}",
            "",
            "Analyse uniquement — aucun ordre exécuté. DYOR.",
        ]
    else:
        body = [
            "<b>🛑 MOTEUR EUR/USD — ARRÊT PROPRE</b>",
            SEP,
            f"🔁 Cycles : {info.get('cycles', 0)}",
            f"📨 Signaux émis : {info.get('signals', 0)}",
            f"🔒 Clôtures : {info.get('resolutions', 0)}",
            f"🧯 Erreurs isolées : {info.get('errors', 0)}",
            f"⏱️ Durée : {info.get('uptime', '?')}",
            f"⏰ {now}",
        ]
    return "\n".join(body)


# --------------------------------------------------------------------------- #
#  ENVOI
# --------------------------------------------------------------------------- #
class TelegramSender:
    """Envoi des messages EUR/USD (HTML) via la Bot API Telegram."""

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
            logger.warning("Telegram non configuré — envoi ignoré")
            return False
        url = API_URL.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                if resp.ok and resp.json().get("ok"):
                    return True
                logger.warning("Telegram HTTP %s : %s", resp.status_code, resp.text[:160])
                # repli texte brut si un caractère HTML pose problème
                if "can't parse entities" in resp.text:
                    payload["parse_mode"] = "HTML"
                    payload["text"] = html.escape(text)
            except requests.RequestException as exc:
                logger.warning("Telegram tentative %d/%d échouée : %s", attempt, self.max_retries, exc)
            time.sleep(1.0 * attempt)
        return False

    def send_signal(self, signal: Signal) -> bool:
        return self.send_text(format_signal_message(signal))
