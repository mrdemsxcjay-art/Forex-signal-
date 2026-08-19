# 📡 Forex Signals — Price Action & Smart Money Concepts

Application Python qui **analyse** le marché Forex en temps réel (Price Action + SMC)
et **envoie des signaux via Telegram**, avec dashboard web, backtesting et filtre de
news économiques.

> ⚠️ **IMPORTANT — L'application n'exécute AUCUN trade automatiquement.**
> Elle analyse, score et notifie. Rien de plus. Les signaux ne constituent pas
> un conseil financier : vous restez seul décisionnaire.

## ✅ Stack 100 % gratuite et open source

| Besoin | Outil choisi | Pourquoi |
|---|---|---|
| Bougies Forex | **Yahoo Finance** (`yfinance`) | gratuit, sans clé API, paires majeures en 1m/15m/1h |
| News économiques | **ForexFactory** (JSON public + repli BeautifulSoup) | gratuit, sans clé, impact High/Medium par devise ; le repli HTML a été validé en réel (HTTP 429 absorbé) |
| Notifications | **Telegram Bot API** (`requests`) | gratuit, illimité en pratique, livraison instantanée |
| Dashboard | **Streamlit + Plotly** | open source, professionnel, aucune ligne de HTML |
| Base de données | **SQLite** (bibliothèque standard) | zéro installation, un seul fichier `data/signals.db` |
| Backtesting | **pandas vectorisé** (sur mesure) | simple, transparent, pas de boîte noire |
| Langue | **Python 3.10+** | unique langage du projet |

## 🏗️ Architecture (vue du flux)

```
   Yahoo Finance (bougies)          ForexFactory (news)
   yfinance, ~60 s                  JSON public, impact rouge
          │                                │
          ▼                                ▼
   ┌──────────────────────────────────────────────┐
   │           MOTEUR (src/main.py)               │
   │  boucle : paire -> analyse -> score -> envoi │
   │  ┌────────────┐  ┌──────────┐  ┌──────────┐  │
   │  │ Price      │  │ SMC :    │  │ Filtre   │  │
   │  │ Action     │  │ BOS/CHoCH│  │ news     │  │
   │  │ (PA)       │  │ OB, FVG, │  │ (±30 min)│  │
   │  │            │  │ liquidité│  │          │  │
   │  └────────────┘  └──────────┘  └──────────┘  │
   │           Scoring pondéré 0 → 10             │
   └──────────────┬───────────────────────────────┘
                  │ score ≥ seuil (défaut 7/10)
        ┌─────────┴─────────┐
        ▼                   ▼
   🤖 Telegram          💾 SQLite (data/signals.db)
   (notifications)             │
                               ▼
                    📊 Dashboard Streamlit (lecture seule)
```

**Découplage clé** : le moteur écrit dans SQLite, le dashboard lit SQLite.
Les deux processus sont totalement indépendants (si le dashboard plante,
les signaux partent quand même).

## 📁 Arborescence du projet

```
forex-signals/
├── README.md                  <- ce fichier
├── requirements.txt           <- dépendances figées
├── .env.example               <- template des secrets (copier en .env)
├── .gitignore                 <- exclut .env, logs, données, venv
│
├── config/
│   └── settings.yaml          <- toute la configuration non sensible
│
├── src/                       <- le moteur (le cœur)
│   ├── __init__.py
│   ├── config.py              <- charge settings.yaml + .env (dataclasses)
│   ├── logger.py              <- logs console + fichier tournant
│   ├── main.py                <- boucle principale (étape 5)
│   │
│   ├── data/                  <- COUCHE DONNÉES
│   │   ├── provider.py        <-   interface abstraite + Timeframe (M5..D1) + resample
│   │   ├── yahoo_provider.py  <-   implémentation yfinance (cache, retries, QA)
│   │   └── data_fetcher.py    <-   façade multi-timeframe H1/H4/D1 + fraîcheur
│   │
│   ├── analysis/              <- COUCHE ANALYSE TECHNIQUE
│   │   ├── candles.py         <-   normalisation + resampling H1->H4
│   │   ├── structure.py       <-   swings, BOS, CHoCH
│   │   ├── order_blocks.py    <-   order blocks
│   │   ├── fvg.py             <-   fair value gaps
│   │   ├── liquidity.py       <-   equal highs/lows, sweeps
│   │   ├── price_action.py    <-   engulfing, pin bars, momentum
│   │   └── smc.py             <-   agrégateur du contexte SMC
│   │
│   ├── fundamental/
│   │   ├── economic_calendar.py   <- calendrier ForexFactory (JSON + repli BeautifulSoup)
│   │   └── fundamental_analyzer.py <- sentiment devise (surprise vs forecast + NLP regex)
│   │
│   ├── signals/
│   │   ├── models.py          <- dataclasses Signal, Direction, etc.
│   │   ├── scoring.py         <- pondération 0-10
│   │   └── engine.py          <- orchestration + anti-doublons
│   │
│   ├── notifications/
│     └── telegram.py          <- Bot API Telegram (HTML + retry)
│   │
│   ├── storage/
│   │   └── database.py        <- SQLite (signaux, résultats, stats)
│   │
│   ├── backtest/
│   │   └── backtester.py      <- backtest simple SMC (CHoCH + zone OB, sans look-ahead)
│   └── visualization/
│       └── smc_chart.py       <- graphique interactif des zones (Plotly, offline)
│
├── dashboard/
│   └── app.py                 <- interface Streamlit (lecture SQLite)
│
├── scripts/
│   ├── verify_install.py      <- vérification de l'environnement
│   └── run_backtest.py        <- lanceur de backtest
│
├── logs/                      <- logs tournants (généré)
└── data/                      <- signals.db (généré)
```

## 🚀 Installation (une seule fois)

Python **3.10 à 3.13** requis ([python.org](https://www.python.org/downloads/)).

**1. Créer le dossier et entrer dedans** (ou cloner si déjà versionné)

```bash
mkdir forex-signals && cd forex-signals
```

**2. Créer l'environnement virtuel**

```bash
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

**3. Installer les dépendances**

```bash
pip install -r requirements.txt
```

**4. Vérifier l'installation**

```bash
python scripts/verify_install.py --online
```

Résultat attendu : `environnement prêt ✔` — toutes les lignes `[OK]`.

**5. Configurer Telegram (optionnel à ce stade, requis à l'étape 6)**

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

Puis suivre les instructions dans `.env.example` (@BotFather pour le token,
@userinfobot pour votre chat id).

## 🧠 Moteur de signaux multi-agents (étape 6)

Trois agents à responsabilité unique, orchestres par `src/signals/engine.py` :
Agent 1 fondamental (news -> sentiment), Agent 2 SMC (biais D1 -> zones H4 ->
déclencheur M15), Agent 3 risque (validation, R/R, sizing). Signal émis
SEULEMENT si les 3 timeframes sont alignés ET score >= 70/100.

```bash
python scripts/test_signals.py       # 30 vérifications (scoring, portes, SQLite, SVG)
```

Persistance : `data/signals.db` (SQLite). Cartes de signal : `data/cards/*.svg`
(icônes SVG professionnelles, zéro emoji). Notifications Telegram prêtes
(remplir `.env`) — non bloquantes si absentes.

## 🔬 Analyse SMC (moteur livré, étape 5)

```bash
python scripts/test_smc.py            # 16 vérifications (patterns exacts + anti-repaint + réel)
python scripts/plot_smc.py EURUSD 15m # graphique interactif -> data/charts/smc_EURUSD_15m.html
python scripts/backtest_smc.py EURUSD 15m   # backtest CHoCH -> data/backtest/
```

Usage programmatique :

```python
from src.analysis.smc import SMCEngine
result = SMCEngine("XAUUSD", "15m").analyze(df)   # dict JSON-ready
SMCEngine.save_json(result, "data/smc.json")      # export disque
```

## ▶️ Utilisation en production

```bash
python -m src.main                                    # boucle continue (Ctrl+C = arrêt propre)
python -m src.main --once                             # un seul cycle (diagnostic / cron)
streamlit run dashboard/app.py                        # dashboard (lecture seule, port 8501)
python scripts/test_dashboard.py                      # 10 vérifications
```

Chaque cycle : données D1/H4/M15 fraîches -> agents -> scoring -> signal
(éventuel) -> SQLite + Telegram + carte SVG, puis clôture automatique des
signaux ouverts (TP_ATTEINT / SL_ATTEINT / EXPIRE après 48 bougies M15).

## ▶️ Utilisation (à venir, étapes suivantes)

```bash
# Moteur de signaux (terminal 1)
python -m src.main

# Dashboard (terminal 2)
streamlit run dashboard/app.py
```

## 🗺️ Roadmap de construction (validation étape par étape)

| Étape | Contenu | État |
|---|---|---|
| 1 | Architecture + environnement | ✅ |
| 2 | Moteur de données temps réel (yfinance + cache + reconnexion) | ✅ |
| 3 | Module de données complet : multi-TF + calendrier ForexFactory + fondamental | ✅ |
| 4 | Price Action | intégré au moteur SMC ✅ |
| 5 | SMC : BOS/CHoCH, Order Blocks, FVG, liquidité (anti-repaint prouvé) | ✅ |
| 6 | Moteur multi-agents (fondamental/SMC/risque) + scoring /100, seuil 70 | ✅ |
| 6b | Industrialisation : boucle continue + tracker TP/SL + heartbeat | ✅ |
| 7 | Notifications Telegram (cartes SVG pro, clôtures, heartbeat) | ✅ |
| 8 | Dashboard Streamlit (5 onglets, transparence cycles) | ✅ |
| 9 | Backtesting : SMC simple + replay complet du moteur (comparaison de seuils) | ✅ |
| 10 | Hébergement gratuit (Oracle Always Free, systemd, install.sh) | ✅ |

## 🖥️ Hébergement gratuit 24/7

Guide complet : **`docs/DEPLOIEMENT.md`** (Oracle Cloud Always Free recommandé,
procédure pas-à-pas, services systemd durcis, sauvegarde SQLite, dépannage).
Installation en une commande sur l'hôte : `bash deploy/install.sh`.
Suite de tests complète : `python scripts/run_all_tests.py` (119 vérifications).

## ⚖️ Avertissements

- Logiciel fourni à but éducatif et d'analyse ; aucune performance n'est garantie.
- Les données Yahoo Finance sont des flux composites **indicatifs** (léger délai
  possible vs. broker) : parfait pour analyser, insuffisant pour exécuter.
- Le trading sur marge comporte un risque élevé de perte en capital.
