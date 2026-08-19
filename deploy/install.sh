#!/usr/bin/env bash
# ============================================================
# Installateur idempotent — Forex Signals SMC (hôte Linux/Ubuntu)
# Usage : bash deploy/install.sh
# Effet : venv + dépendances + .env + services systemd (moteur 24/7)
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "== 1/6 Dépendances système =="
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3-venv python3-pip
else
    echo "   apt-get indisponible : assurez-vous d'avoir python3 + venv."
fi

echo "== 2/6 Environnement virtuel =="
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
./.venv/bin/python -c "import pandas, yfinance, streamlit, plotly; print('   dépendances OK')"

echo "== 3/6 Configuration =="
if [ ! -f .env ]; then
    cp .env.example .env
    echo "   .env créé depuis le modèle : REMPLISSEZ le token Telegram (voir .env.example)."
else
    echo "   .env déjà présent (conservé)."
fi
mkdir -p logs data

echo "== 4/6 Vérification de l'environnement =="
./.venv/bin/python scripts/verify_install.py || echo "   (vérifiez les points en échec ci-dessus)"

echo "== 5/6 Services systemd =="
if command -v systemctl >/dev/null 2>&1; then
    sudo cp deploy/forex-engine.service deploy/forex-dashboard.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now forex-engine
    echo "   forex-engine activé (redémarrage auto, journal : journalctl -u forex-engine -f)"
    echo "   dashboard (optionnel, accessible via tunnel SSH) :"
    echo "     sudo systemctl enable --now forex-dashboard"
else
    echo "   systemd indisponible : lancez manuellement './.venv/bin/python -m src.main'."
fi

echo "== 6/6 Terminé =="
echo "   Moteur   : systemctl status forex-engine"
echo "   Logs     : journalctl -u forex-engine -f"
echo "   Dashboard: ssh -L 8501:localhost:8501 <utilisateur>@<serveur>  puis http://localhost:8501"
