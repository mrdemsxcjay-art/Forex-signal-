# COMPTE RENDU DE RÉUNION — COMITÉ DE PILOTAGE DU MOTEUR

**Date** : 2026-08-19 00:05 UTC
**Participants** : Agent 1 (analyste fondamental), Agent 2 (trader SMC), Agent 3 (gestionnaire du risque)
**Ordre du jour** : décider de la prochaine étape du projet de signaux Forex
**Données analysées** : diagnostic système du 2026-08-19 00:01 UTC (fenêtre 14 jours, 3 paires, bougies réelles Yahoo/COMEX, calendrier ForexFactory)

---

## 1. Faits constatés (communs aux trois agents)

| Métrique (14 jours) | EURUSD | GBPUSD | XAUUSD | Total |
|---|---|---|---|---|
| Déclencheurs M15 (CHoCH/BOS) | 100 | 90 | 91 | **281** |
| Biais D1 aligné | 54 % | 28 % | 56 % | — |
| TROIS timeframes alignés | 18 % | 12 % | 22 % | **17,4 %** (49) |
| Alignés + session London/NY | 10 | 8 | 12 | **30 (10,7 %)** |
| Candidats/jour avant scoring | — | — | — | **≈ 2,1** |

Posture opérationnelle : base de signaux **absente** (aucun signal réel émis), Telegram **non configuré**,
boucle continue **absente**, suivi TP/SL des signaux **absent**.

---

## 2. Positions

### Agent 1 — Analyste fondamental
> « Mon calendrier et mon analyse de surprise fonctionnent (validés 34/34, repli BeautifulSoup
> prouvé sur un 429 réel). Mais deux défaillances me préoccupent :
> **(a)** mes news rouges sont le premier filtre de protection du compte — or elles ne protègent
> personne si l'application n'est pas en marche à 13 h 30 UTC un jour de CPI ;
> **(b)** mon sentiment décroît avec le temps (×0,3 au-delà de 12 h) — encore faut-il que quelqu'un
> l'évalue au bon moment. Les fenêtres de news sont courtes et_known d'avance : rater une session
> London Kill Zone par indisponibilité du processus est une perte sèche.
> J'ajoute que 2,1 candidats/jour avant scoring est un débit sain : après le seuil 70, les news
> rouges et le cooldown, je table sur 0,3 à 1 signal/jour — suffisant pour être utile, assez rare
> pour rester de qualité. »

**Vote de l'Agent 1** : exécution continue avant toute nouvelle fonctionnalité.

### Agent 2 — Trader SMC
> « Mes chiffres me satisfont : 17,4 % des déclencheurs passent les trois portes — la stratégie est
> sélective par construction, pas par accident (GBPUSD à 12 % reflète un D1 souvent neutre ces
> deux semaines, pas un défaut du code). Mon moteur analyse 3 000 bougies en 148 ms…
> **et personne ne le fait tourner.** C'est comme avoir un radar éteint.
> Deux exigences de ma part :
> **(a)** la boucle doit tolérer les incidents de source (gel Yahoo de 40 min constaté le 18/08 —
> la dégradation gracieuse existe déjà, il faut simplement qu'un process vivant en profite) ;
> **(b)** je veux le **suivi des résultats** : mes zones n'ont de valeur que si on mesure
> objectivement TP vs SL. Sans statistiques de sortie, on débat d'opinions, pas de données. »

**Vote de l'Agent 2** : boucle continue + résolution automatique TP/SL des signaux émis.

### Agent 3 — Gestionnaire du risque
> « Je classe les risques par espérance de perte :
> **RISQUE 1 — process mort (critique)** : une app de signaux qui ne tourne pas en continu est une
> démo. Toute la chaîne D1→H4→M15→news→scoring est inutile sans exécution permanente.
> **RISQUE 2 — absence de responsabilité (critique)** : chaque signal reste `EN_COURS` à vie.
> Sans `TP_ATTEINT`/`SL_ATTEINT`, impossible d'afficher un winrate honnête, de détecter une
> dérive, ou d'alimenter le backtest par la réalité. C'est mon exigence n°1 de traçabilité.
> **RISQUE 3 — silence opérationnel (majeur)** : ni Telegram configuré, ni preuve de vie.
> L'utilisateur doit savoir que le moteur tourne (heartbeat) et être alerté d'un redémarrage.
> Le dashboard et l'hébergement sont des conforts ; ces trois risques sont des devoirs.
> Le sizing (1 %, 10 000) n'a de sens que si les issues sont enregistrées. »

**Vote de l'Agent 3** : industrialisation temps réel (boucle + suivi TP/SL + Telegram live),
dashboard et hébergement explicitement reportés.

---

## 3. Décision du comité (unanimité 3/3)

> **La prochaine étape est l'INDUSTRIALISATION TEMPS RÉEL (Phase 6).**
> Un système qui produit des signaux doit d'abord les produire toujours, les livrer,
> et rendre compte de leurs issues. Les fonctionnalités de confort viendront après.

### Plan d'action de la Phase 6

| # | Chantier | Contenu | Critère d'acceptation |
|---|---|---|---|
| 6.1 | `src/main.py` — boucle continue | cycle 60 s sur les paires du settings.yaml, isolation d'erreurs par paire (une paire en échec n'arrête rien), arrêt propre Ctrl+C, journal complet | 24 h de fonctionnement sans intervention |
| 6.2 | Suivi des résultats (`SignalTracker`) | pour chaque signal `EN_COURS` : avance des bougies, détection TP/SL/expiration (48 bougies), mise à jour SQLite + notification de clôture | chaque signal passe à `TP_ATTEINT`/`SL_ATTEINT`/`EXPIRE` automatiquement |
| 6.3 | Telegram de production | heartbeat au démarrage, alerte de reconnexion, message de clôture de signal (gain/perte en R) | réception effective sur le chat de l'utilisateur |

### Ordre des phases suivantes (réaffirmé)
- **Phase 7** — Dashboard Streamlit (lit SQLite + cartes SVG, zéro couplage avec le moteur)
- **Phase 8** — Backtest étendu + hébergement gratuit (le process 6.1 étant déjà conçu pour tourner 24/7)

---

## 4. Réserves consignées
- Agent 1 : maintenir le TTL calendrier à 15 min dans la boucle (rafraîchissement news).
- Agent 2 : journaliser la fraîcheur D1/H4/M15 à chaque cycle (détection précoce d'un gel de source).
- Agent 3 : tout signal émis DOIT avoir une issue (TP/SL/EXPIRE) — aucun `EN_COURS` de plus de 48 bougies.

*Le comité valide à l'unanimité. Procéder à la Phase 6.*
