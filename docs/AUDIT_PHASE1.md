# RAPPORT D'AUDIT — PHASE 1 (LECTURE SEULE)
## Moteur de signaux EUR/USD — refonte Price Action / SMC adaptative

**Date** : 2026-09-10 UTC · **Portée** : intégralité du dépôt (code, config, workflows, tests, données réelles)
**Règle** : aucun fichier moteur modifié pendant cette phase.
**Format** : chaque constat = problème / cause probable / fichier · ligne / impact / correction proposée / méthode de validation.
**Sévérité** : 🔴 CRITIQUE · 🟠 MAJEUR · 🟡 MINEUR

---

## A. SYNTHÈSE EXÉCUTIVE

Le moteur actuel est un **suivi de tendance à portes séquentielles** (D1 EMA200 → H4 EMA → H1 EMA50 → M15 cassure+retest → M5/M30 bonus) avec **score par addition de confluences** et **TP mécanique 3R**. Il fonctionne techniquement (250 cycles, 0 échec CI) mais souffre de **trois défauts de conception** confirmés par les données réelles :

1. **Aucune détection de régime** → il a émis 16 signaux LONG dans un range mort de 30 pips/jour (semaine du 5-9 sept : 0 TP / 10 SL / 3 EXPIRE = **−8,5 R**).
2. **Écart code ↔ configuration** : le pipeline live ignore 7 paramètres YAML (il les délègue à des agents qu'il n'appelle plus) et utilise des constantes internes divergentes.
3. **Un bug de scoring mort-vivant** : le bonus fondamental +10 n'est jamais attribué (écrasé avant le scoring) — les scores live sont amputés de 10 points quand le fondamental aligne.

Le chemin live coexiste avec le **chemin legacy complet** (multi-paires, RiskAgent, scoring 7 composantes) encore utilisé par les tests → les tests valident partiellement le mauvais moteur.

**Données réelles analysées** : base v2 (16 signaux depuis réforme), logs CI de 58 cycles, bougies Yahoo EUR/USD 10 jours.

---

## B. CONSTATS DÉTAILLÉS

### F-01 🔴 Absence totale de Market Regime Engine
- **Problème** : une seule logique (tendance) appliquée à tous les états de marché ; aucun concept RANGE / TRANSITION / CHAOTIC / INSUFFICIENT_DATA.
- **Cause probable** : le pipeline a été construit pendant un régime haussier ( calibrated sur fenêtre trending, jamais re-validée).
- **Fichiers** : `src/agents/eurusd_agent.py` (ensemble) · `src/signals/engine.py` (run_eurusd).
- **Impact** : cause racine démontrée des −8,5 R (16 LONG en range 30 pips ; TP 36 pips inatteignable ; SL 12 pips traversé par le bruit). Amplitude réelle mesurée 27-46 pips/jour vs TP exigé 36 pips.
- **Correction proposée** : Phase 2 — moteur de régime (ADX-less, basé structure + amplitude : range via clustering de swings + compression ATR ; trend via HH/HL BOS répétés + EMA slop ; breakout via cassure + acceptation ; CHAOTIC par défaut) AVANT toute recherche de signal, avec MID_RANGE = NO_TRADE.
- **Validation** : replay 60 jours étiqueté régime par régime ; le filtre aurait dû bloquer ≥ 14 des 16 signaux de la semaine rouge (contre-factuel mesurable).

### F-02 🔴 Bonus fondamental +10 jamais attribué (bug)
- **Problème** : `fund_view` construit avec `supports_direction=False` codé en dur (ligne ~215) AVANT l'appel à `assess()` ; la valeur correcte (`supports`, ligne 241) n'est calculée qu'APRÈS le scoring, pour le stockage du signal.
- **Cause probable** : refactor en deux temps du pipeline (l'agent attendait un view complet, le moteur n'a jamais été re-câblé).
- **Fichiers** : `src/signals/engine.py` · lignes 212-229 (scoring) vs 241/272 (calcul réel, stockage seul).
- **Impact** : tout setup avec fondamental aligné est noté 10 points trop bas → signaux valides bloqués au seuil 65 ; le champ stocké `supports` est correct → base trompeuse pour l'analyse a posteriori.
- **Correction proposée** : calculer `supports` AVANT `assess()` et passer le view complet (une seule construction).
- **Validation** : test unitaire — calendrier synthétique haussier + scénario aligné → breakdown contient `fondamental_aligne: 10` ; + vérifier sur les 16 signaux réels combien auraient dépassé le seuil.

### F-03 🔴 Paramètres YAML ignorés par le chemin live + divergences code/config
- **Problème** : le pipeline live (`run_eurusd`) ne lit PAS les paramètres de la section `signals:` — ils alimentent des agents (SMCAgent, RiskAgent) qu'il n'appelle plus.

| Paramètre YAML | Valeur YAML | Utilisé par le live ? | Valeur réelle live |
|---|---|---|---|
| `min_score` | 65 | ✅ oui (threshold) | 65 |
| `grades` | 85/75/65 | ✅ oui | idem |
| `max_per_pair_per_day` | 4 | ✅ oui | 4 |
| `cooldown_minutes` | 180 | ✅ oui | 180 |
| `expiry_bars` | 96 | ✅ oui (tracker) | 96 |
| `trigger_max_age_candles` | **12** | ❌ non (SMCAgent legacy) | **18** (hardcodé EURUSDAgent) |
| `zone_proximity_atr` | **1.5** | ❌ non | **1.2** (hardcodé `retest_atr`) |
| `min_rr` | 2.5 | ❌ non (RiskAgent jamais appelé) | 3.0 (`view.rr`) |
| `default_rr` | 3.0 | ❌ non | 3.0 (coïncidence) |
| `risk_pct` / `account_size` | 2.0 / 50 | ❌ non | lot fixe 0.01 |
| `news.block_minutes_before` | 120 | ❌ non | **2 h codé en dur** (coïncidence alignée) |

- **Cause probable** : le pipeline EUR/USD a été greffé sans rebrancher la configuration.
- **Impact** : toute modification YAML de ces clés est **silencieusement sans effet** ; la documentation et la config mentent sur le comportement réel.
- **Correction proposée** : injecter la config dans EURUSDAgent/RiskAgent (ou supprimer les clés mortes) ; test anti-dérive qui ASSERT que chaque clé YAML est lue par le chemin live.
- **Validation** : test de charge config (changer une clé → comportement change) + suppression des valeurs magiques du code.

### F-04 🟠 TP mécanique 3R + SL plancher élargi — RR jamais vérifié contre la structure
- **Problème** : `tp = entry ± 3 × risk` quel que soit le contexte ; SL élargi à 12 pips min puis TP suivi mécaniquement (§21 de la nouvelle spec violée par conception).
- **Fichiers** : `src/agents/eurusd_agent.py` (bloc plan, lignes ~180-192) · `src/signals/engine.py` (RiskPlan inline).
- **Impact** : dans un range de 30 pips, TP 36 pips = 0 % de chance mesurée ; 3 EXPIRE + une partie des 10 SL en découlent directement.
- **Correction proposée** : Phase 8 — TP dynamiques (TP1 liquidité proche, TP2 opposé de range/swing, TP3 extension HTF) ; RR disponible calculé AVANT entrée ; si RR < seuil → NO_TRADE (pas d'invention de TP).
- **Validation** : replay comparatif 3R-fixe vs dynamique sur 60 j (expectancy, TP rate, EXPIRE rate) ; MAE/MFE pour vérifier le réalisme des cibles.

### F-05 🟠 Scoring = machine à additionner (violation §18-19 future spec)
- **Problème** : le score additionne des confluences (base 50 + bonus) et le seuil décide ; les « portes » ne couvrent que 4 niveaux ; un haut score ne peut pas être compensé par un mauvais emplacement… sauf que l'emplacement (location) n'existe pas : un signal en plein milieu de range passe si la tendance H4 est alignée.
- **Fichiers** : `src/agents/eurusd_agent.py` (bloc confiance).
- **Impact** : signaux techniquement conformes mais contextuellement nuls (semaine rouge = preuve).
- **Correction** : Phase 10 — séparation HARD GATES (régime, localisation, structure, liquidité, confirmation, RR, cible) vs SCORE (classement des setups déjà valides uniquement).
- **Validation** : tests comportementaux spec §35 (score 95 + MID_RANGE → NO_TRADE, etc.).

### F-06 🟠 Chemin legacy complet toujours vivant et testé
- **Problème** : `run_on_frames` + `_legacy_run_pair` + `SMCAgent` + `RiskAgent.validate` + `compute_score` (7 composantes) coexistent avec le pipeline EUR/USD ; **la majorité de `test_signals.py` (sections A/B/C) teste le chemin legacy**, pas le live.
- **Fichiers** : `src/signals/engine.py` · `src/agents/smc_agent.py` · `src/agents/risk_agent.py` · `src/signals/scoring.py` · `scripts/test_signals.py`.
- **Impact** : faux sentiment de couverture ; une régression du moteur live peut passer 30/30.
- **Correction** : Phase 10/13 — re-pointer les tests sur le moteur live, archiver le chemin legacy (`_legacy` → suppression ou dossier `legacy/`), garder le backtester SMC comme outil isolé.
- **Validation** : `run_all_tests` avec coverage du module `eurusd_agent` ≥ 85 % des branches de décision.

### F-07 🟠 Biais de sélection dans le replay de calibration
- **Problème** : `collect_instants()` sélectionne les instants de test à partir de `SMCEngine.analyze(m15)` sur l'historique COMPLET → les événements utilisés pour choisir les instants incluent des swings confirmés par des bougies futures (k=2) et des états structurels non encore observables à l'instant évalué.
- **Fichiers** : `src/backtest/engine_replay.py` · ligne 73.
- **Impact** : calibration optimiste (les instants sont choisis avec la connaissance du futur) ; toute décision de seuil issue de ce replay est suspecte.
- **Correction** : Phase 12 — replay événementiel pur : itérer chronologiquement et ne considérer à chaque pas QUE les événements confirmés à cette bougie (réutiliser la preuve anti-repaint existante) ; + walk-forward et out-of-sample.
- **Validation** : le nouveau replay doit retrouver exactement les mêmes événements que la preuve anti-repaint (fenêtres croissantes) ; recalibrer les seuils sur cette base saine.

### F-08 🟠 Replay ≠ live : bonus asymétriques
- **Problème** : le replay score sans DXY (+10) ni fondamental (+10 — mort aussi en live à cause de F-02, mais pas par conception) ; le live reçoit le bonus DXY. Fenêtre M5 réelle ~21 j étiquetée « 30 jours ».
- **Fichiers** : `src/backtest/engine_replay.py` · `src/data/yahoo_provider.py` (limite M5).
- **Impact** : le seuil calibré en replay ne correspond pas au seuil vécu en live (écart jusqu'à +10 points).
- **Correction** : rejouer AVEC DXY historique (récupérable) et fondamental neutralisé explicitement + documenter la fenêtre réelle par timeframe.
- **Validation** : distribution des scores replay vs live sur la même période (test de cohérence).

### F-09 🟠 Ré-émission possible du même événement structurel (anti-stacking absent)
- **Problème** : fenêtre de déclencheur 18 bougies M15 (4,5 h) > cooldown 180 min → après expiration du cooldown, la MÊME cassure peut re-qualifier et ré-émettre un signal « nouveau » sur le même setup.
- **Fichiers** : `src/agents/eurusd_agent.py` (fenêtre 18) · `src/signals/engine.py` (`_spam_blockers`).
- **Impact** : signaux empilés sur un seul événement (aggravé en range) ; la semaine rouge contient des entrées identiques répétées.
- **Correction** : Phase 9 — dédoublonnage structurel : hash (niveau cassé, direction, type) persisté ; un événement ne peut produire qu'un signal ; re-entry uniquement sur NOUVELLE structure indépendante.
- **Validation** : test comportemental spec §25 (SELL → SL → même zone sans nouveau sweep/MSS → NO_TRADE) + audit base (0 hash dupliqué).

### F-10 🟠 Fraîcheur des données non imposée comme gate
- **Problème** : `DataFetcher` journalise la fraîcheur mais `run_eurusd` consomme les bougies sans vérifier — un gel du flux Yahoo (observé 40 min le 18/08) est silencieux.
- **Fichiers** : `src/signals/engine.py` (run_pair) · `src/data/data_fetcher.py`.
- **Impact** : décisions sur données périmées (zones/EMA fausses) sans trace dans les bloqueurs.
- **Correction** : hard gate DATA_FRESHNESS par timeframe (âge > n×période → NO_TRADE + raison journalisée).
- **Validation** : test avec bougies tronquées artificiellement → NO_TRADE-DATA_STALE.

### F-11 🟡 MAE / MFE non mesurés
- **Problème** : le tracker ne calcule ni l'excursion adverse maximale, ni l'excursion favorable ; la base n'a pas les colonnes.
- **Fichiers** : `src/signals/tracker.py` · `src/storage/database.py`.
- **Impact** : impossible de diagnostiquer « SL trop serré ? » vs « setup mauvais ? » (question ouverte de la semaine rouge).
- **Correction** : Phase 11/12 — colonnes `mae_r`, `mfe_r`, `time_to_tp`, `time_to_sl` calculées à la clôture ; analyse agrégée par régime/setup.
- **Validation** : sur les 13 clôtures réelles, recalculer MAE/MFE rétroactivement et publier le diagnostic.

### F-12 🟡 Journal des NO_TRADE non persistant
- **Problème** : `data/last_cycle.json` est écrasé à chaque cycle (dernier état seulement) ; l'historique des refus n'existe que dans les logs CI (périssables 90 j).
- **Impact** : impossible d'étudier si le filtre est trop strict/permissif dans la durée (spec §33).
- **Correction** : table `decisions` (SQLite) : timestamp, régime, raison NO_TRADE, score au moment du refus. Volume maîtrisé (1 ligne/refus).
- **Validation** : requête : distribution des raisons sur 7 jours.

### F-13 🟡 Tests du nouveau pipeline quasi inexistants
- **Problème** : `test_signals.py` couvre le legacy ; le live n'est testé que par 2 assertions (règle n°1 + smoke run_pair).
- **Impact** : aucune protection contre les régressions de gates (F-02 est passé au travers).
- **Correction** : Phase 13 — suite comportementale complète (range/trend/breakout/liquidité/structure/anti-overtrading, spec §35) sur séries synthétiques déterministes.
- **Validation** : coverage branches ≥ 85 % ; chaque gate a son test NO_TRADE.

### F-14 🟡 Divers paramètres magiques non calibrés par les données
- **Problème** : EMA 200/50, lookback confirmation 6/3, `wick_ratio` 2.0, DXY ±0.10/0.25/0.6, fenêtre « 6 h » du risque ÉLEVE, session 7-17, `min_risk_atr` 0.5 — tous codés en dur, jamais comparés à des alternatives (violation §30 de la nouvelle spec).
- **Correction** : Phase 12 — grille de candidats par paramètre, replay, choix robuste hors échantillon ; tout paramètre retenu migre en YAML branché (cf. F-03).
- **Validation** : tableau candidats/résultats publié avant tout choix.

### F-15 🟡 Qualité des données fondamentales
- **Problème** : repli HTML ForexFactory interprète les heures de la page comme UTC (approximation assumée) ; TTL calendrier 15 min.
- **Impact** : décalage possible des fenêtres de news (blocage < 2 h) — faible probabilité, impact fort si news charnière.
- **Correction** : préférer le flux JSON (déjà primaire), journaliser chaque usage du repli, durcir le TTL.
- **Validation** : comparer 7 jours d'heures JSON vs HTML.

### F-16 🟡 Artefacts documentaires
- **Problème** : README affiche des états obsolètes ; `backtester.py` (CHoCH 2R) ne reflète aucune stratégie en production ; carte SVG au format ancien.
- **Impact** : confusion pour tout repreneur (humain ou IA).
- **Correction** : mise à jour à la Phase 13 ; marquer le backtester legacy.
- **Validation** : relecture croisée doc ↔ tests.

---

## C. POINTS VÉRIFIÉS SANS ANOMALIE (pour mémoire)

- **Anti-repaint** : preuve par fenêtres croissantes déjà en place et passante (`test_smc.py` section B) — à réutiliser pour le replay événementiel (F-07).
- **Look-ahead dans les indicateurs** : EMA/RSI/engulfing sont causaux (calculs en série, `iloc[-1]`).
- **Bougies clôturées** : `only_closed` appliqué partout, resampling H4 déterministe (origin=epoch).
- **Idempotence des envois** : un seul moteur (lerelais A/B supprimé), groupe de concurrence GitHub actif.
- **Clôture conservatrice** : SL testé avant TP dans la même bougie ; expiry 96 bougies conforme config.
- **Base SQLite** : migration automatique, stats exactes, page de statut réparée (lit la base v2).

## D. DONNÉES RÉELLES ANALYSÉES (preuves)

- **Semaine post-réforme** : 16 signaux (tous LONG — verrou D1 haussier), 13 clôturés → 0 TP / 10 SL / 3 EXPIRE = **−8,5 R** ; grades 65→90 (même le 90/100 a stoppé).
- **Marché同期** : amplitude journalière EUR/USD 27-46 pips, variation 4 jours +0,00 % → régime RANGE non détecté (F-01).
- **CI** : ~250 cycles, 0 échec ; blocages dominants « H1 non alignée » (12×/15 cycles).

## E. ORDRE D'IMPLÉMENTATION PROPOSÉ (conforme au master prompt)

| Phase | Contenu | Corrige |
|---|---|---|
| 2 | Market Regime Engine + gates régime/localisation | F-01, F-05 (début) |
| 3 | Structure Engine (HH/HL/LH/LL, MSS, interne/externe) | socle |
| 4 | Liquidity Engine (PDH/PDL/PWH/PWL, EQH/EQL, sweep vs vrai breakout) | socle |
| 5 | Price Action Engine (patterns comportementaux) | F-14 partiel |
| 6 | SMC Engine (OB/FVG = jamais suffisant seul) | F-05 |
| 7 | Location + Scénarios conditionnels | F-01, F-05 |
| 8 | SL/TP dynamiques + RR disponible avant entrée | F-04 |
| 9 | Anti-overtrading (dédoublonnage structurel, re-entry) | F-09 |
| 10 | Hard Gates vs Score + re-branchement config + tests live | F-02, F-03, F-05, F-06 |
| 11 | Paper Shadow Mode + MAE/MFE + journal NO_TRADE | F-11, F-12 |
| 12 | Replay événementiel sans biais + walk-forward + calibration | F-07, F-08, F-14 |
| 13 | Validation finale + docs | F-13, F-16 |

**Décision demandée** : valider ce rapport pour engager la PHASE 2 (Market Regime Engine — conception + séries synthétiques de référence + replay étiqueté, toujours sans toucher au moteur live : shadow d'abord).
