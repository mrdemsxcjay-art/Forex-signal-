"""Paper Shadow Mode — PHASE 11.

Le nouveau moteur (AdaptiveSignalEngine, phases 2-10) tourne en parallèle du
moteur live, sur les MÊMES données, à chaque cycle :

    - les signaux du nouveau moteur sont SIMULÉS (papier) : aucune action,
      aucun Telegram — uniquement journalisés ;
    - TOUTES les décisions sont enregistrées, y compris les NO_TRADE avec
      leurs codes §33 (étude du filtre : trop strict ou trop permissif ?) ;
    - les signaux papier sont clôturés sur les bougies réelles avec mesure
      MAE / MFE (§28) ;
    - `scripts/shadow_report.py` compare OLD (live) vs NEW (shadow) selon
      les critères §27/§36.

Règle absolue : le shadow ne doit JAMAIS faire échouer le cycle live.
"""
