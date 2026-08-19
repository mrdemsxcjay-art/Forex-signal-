"""Vérification de l'environnement — Étape 1.

Usage :
    python scripts/verify_install.py            # checks locaux (offline)
    python scripts/verify_install.py --online   # teste aussi les sources de données

Sortie : un tableau [OK]/[ÉCHEC] par vérification + code retour
(0 = tout va bien, 1 = au moins un problème).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Permet l'exécution depuis la racine du projet : python scripts/verify_install.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEPS = [
    # (module importé, nom pip affiché)
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("yfinance", "yfinance"),
    ("requests", "requests"),
    ("yaml", "PyYAML"),
    ("dotenv", "python-dotenv"),
    ("bs4", "beautifulsoup4"),
    ("plotly", "plotly"),
    ("streamlit", "streamlit"),
    ("streamlit_autorefresh", "streamlit-autorefresh"),
]

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def check_python() -> bool:
    ok = sys.version_info >= (3, 10)
    status = "OK" if ok else "ÉCHEC"
    print(f"[{status}] Python {sys.version.split()[0]} (3.10+ requis)")
    return ok


def check_deps() -> bool:
    all_ok = True
    for module, pip_name in DEPS:
        try:
            mod = importlib.import_module(module)
            try:  # __version__ quand il existe, sinon métadonnées pip
                version = mod.__version__
            except AttributeError:
                version = importlib.metadata.version(pip_name)
            print(f"[OK]      {pip_name:<22} {version}")
        except ImportError:
            print(f"[ÉCHEC]   {pip_name:<22} absent -> pip install {pip_name}")
            all_ok = False
    return all_ok


def check_config() -> bool:
    try:
        from src.config import load_config

        cfg = load_config()
        print(f"[OK]      config/settings.yaml -> {cfg.app_name}")
        print(f"[OK]      {len(cfg.trading.pairs)} paires : {', '.join(cfg.trading.pairs)}")
        return True
    except Exception as exc:  # noqa: BLE001 — on veut TOUT capturer ici
        print(f"[ÉCHEC]   chargement de la configuration : {exc}")
        return False


def check_env() -> bool:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        print("[OK]      fichier .env présent")
        return True
    print("[ATTENTION] .env absent — copiez .env.example en .env et remplissez")
    print("           le token Telegram (requis seulement à partir de l'étape 6).")
    return True  # pas bloquant à l'étape 1


def check_logging() -> bool:
    try:
        from src.logger import setup_logging

        logger = setup_logging(level="INFO")
        logger.info("Test de journalisation — si vous lisez ceci, les logs marchent.")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[ÉCHEC]   journalisation : {exc}")
        return False


def check_online() -> bool:
    """Tests réseau (optionnels) : bougies Yahoo + calendrier ForexFactory."""
    all_ok = True

    try:
        import yfinance as yf

        df = yf.download(
            "EURUSD=X", period="1d", interval="1h",
            progress=False, auto_adjust=True,
        )
        if df is not None and not df.empty:
            last = df.iloc[-1]
            close = last["Close"]
            print(f"[OK]      Yahoo Finance : EURUSD=X, dernière clôture H1 = {float(close.iloc[0] if hasattr(close, 'iloc') else close):.5f}")
        else:
            print("[ÉCHEC]   Yahoo Finance : réponse vide")
            all_ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"[ÉCHEC]   Yahoo Finance : {exc}")
        all_ok = False

    try:
        import requests

        resp = requests.get(FF_CALENDAR_URL, timeout=15)
        n_events = len(resp.json()) if resp.ok else 0
        if resp.ok and n_events > 0:
            print(f"[OK]      ForexFactory : calendrier récupéré ({n_events} événements cette semaine)")
        else:
            print(f"[ÉCHEC]   ForexFactory : HTTP {resp.status_code}")
            all_ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"[ÉCHEC]   ForexFactory : {exc}")
        all_ok = False

    return all_ok


def main() -> int:
    print("=" * 62)
    print(" Vérification de l'environnement — Forex Signals SMC")
    print("=" * 62)

    results = [check_python(), check_deps(), check_config(), check_env(), check_logging()]

    if "--online" in sys.argv:
        print("-" * 62)
        results.append(check_online())

    print("=" * 62)
    if all(results):
        print(" RÉSULTAT : environnement prêt ✔  (Étape 1 validée)")
        return 0
    print(" RÉSULTAT : corrigez les points [ÉCHEC] ci-dessus ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
