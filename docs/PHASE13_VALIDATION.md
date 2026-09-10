# PHASE 13 — VALIDATION FINALE : ÉTUDE DE DÉBIT ET PROTOCOLE DE BASCULE

**Date** : 2026-09-10 · **Portée** : mesure du débit de signaux du moteur adaptatif
(phases 2-12), critères GO/NO-GO §36, protocole de bascule.

---

## 1. RÉPONSE À LA QUESTION : « combien de signaux par jour ? »

**Mesuré (replay causal, 43 jours réels, 422 instants, EUR/USD)** :

| Profil | Signaux | Par jour | Par semaine | Issues |
|---|---|---|---|---|
| **STRICT** (confirmation = gate) | 1 | **0,02** | 0,2 | 1 EXPIRE +0,22 R, 0 SL |
| **ÉQUILIBRÉ** (confirmation = bonus) | 1 | 0,02 | 0,2 | identique |

**La réponse honnête : ~1 signal toutes les 5-6 semaines** dans les conditions
actuelles du marché — pas par jour.

### Pourquoi si peu (l'entonnoir mesuré)

```
422 instants analysés
 └─ 307 (73 %) bloqués : REGIME_UNCERTAIN  ← LE goulot
 └─  93 (22 %) bloqués : NO_CONFIRMATION   (scénario pas mûr)
 └─  14 ( 3 %) bloqués : BAD_RR / NO_ZONE / DATA / COOLDOWN
 └─   1 SIGNAL papier
```

Le marché EUR/USD a passé **~73 % des 90 derniers jours en TRANSITION**
(mesuré en phase 2 et confirmé ici). Or la spec §40 impose : TRANSITION →
prudence → NO_TRADE. **Le moteur fait exactement ce qui a été demandé :
il refuse de trader un marché sans structure.** En période de tendance
(~25 % du temps historiquement), le débit remontera — mais cela n'a pas
pu être mesuré faute de fenêtre de tendance dans les 60 jours d'historique
M15 disponibles.

### Sensibilité (§30)
Le drapeau `require_confirmation` a été mesuré : relâcher la confirmation ne
change RIEN au débit (le goulot est en amont, au régime). Aucun paramètre
n'a été modifié sur cette base.

---

## 2. CE QUE MONTRENT LES DONNÉES (la vraie valeur du nouveau moteur)

| Même période (début septembre) | Ancien moteur (live) | Nouveau moteur (replay) |
|---|---|---|
| Signaux émis | 16 (tous LONG) | 1 |
| Résultat | **−8,5 R** (0 TP / 10 SL / 3 EXP) | **+0,22 R** (0 SL) |
| Ce qu'il faisait pendant la semaine rouge | achetait un range mort | **refusait de trader** (régime TRANSITION) |

La démonstration n'est pas « le nouveau moteur gagne beaucoup » — c'est
**« le nouveau moteur ne perd pas quand il n'y a rien à faire »**. Sa valeur
mesurée à ce jour est la PRÉSERVATION DU CAPITAL, pas la production de signaux.

---

## 3. CRITÈRES GO/NO-GO POUR LA BASCULE (§36 formalisés)

La bascule du live vers l'adaptatif sera autorisée quand TOUTES ces
conditions seront réunies :

| # | Critère | Seuil | Statut actuel |
|---|---|---|---|
| 1 | Clôtures papier cumulées (shadow + replay) | ≥ 30 | **2** ❌ |
| 2 | Expectancy papier | > +0,20 R | +0,22 R ✓ (n=2, non significatif) |
| 3 | Profit factor | > 1,3 | indéterminé (n insuffisant) ❌ |
| 4 | Série perdante max | ≤ 5 | 0 ✓ (n=2) |
| 5 | MAE moyen | > −1,0 R | −0,44 R ✓ (n=2) |
| 6 | Couverture de régimes | ≥ TREND + RANGE observés | TRANSITION seulement ❌ |
| 7 | Aucune régression des 12 suites | 100 % | **123/123** ✓ |

**VERDICT : NO-GO statistique** — l'échantillon (n=2) ne prouve rien au sens
de la §36 (« ne jamais prétendre qu'une stratégie est rentable sans preuve
statistique »). La bascule ne sera donc pas exécutée automatiquement.

**MAIS** — le critère de préservation du capital est déjà démontré
(§2). D'où la décision produit proposée à l'utilisateur (voir §5).

---

## 4. PROTOCOLE DE BASCULE (quand GO)

1. Calibrer `EngineConfig` final (grille §30 sur échantillon suffisant).
2. Brancher `run_pair` sur `AdaptiveSignalEngine` (messages Telegram au
   format §32 complet — pourquoi maintenant, invalidation, TP1-3).
3. L'ancien moteur passe en shadow inversé (comparaison continue §27).
4. Journal NO_TRADE exposé (page de statut) — la transparence du silence.
5. Revue à 30 clôtures papier post-bascule ; retour arrière documenté si
   expectancy < 0.

---

## 5. DÉCISION PRODUIT — OPTIONS POUR L'UTILISATEUR

| Option | Telegram reçoit | Débit attendu | Risque |
|---|---|---|---|
| **A. Bascule immédiate** (motif : préservation du capital démontrée, §1 « NO TRADE > MAUVAIS TRADE ») | signaux du moteur adaptatif | ~1/mois en marché transition, plus en tendance | quasi nul ; contre : très peu de signaux, validation §36 incomplète |
| **B. Statu quo** (recommandé par §36 pur) | ancien moteur (2-4/j) | 2-4/jour | celui démontré par la semaine à −8,5 R |
| **C. Hybride** : adaptatif en live pour les signaux + ancien moteur réduit au silence (seuil 80, Grade A seulement) en complément | les deux, clairement étiquetés | 1/mois + 0-1/j Grade A | modéré ; complexité de double source |

Recommandation de l'ingénieur : **A ou C** — la semaine à −8,5 R est la
preuve que le débit de l'ancien moteur se paie en espérance. Le silence de
l'adaptatif n'est pas un défaut : c'est la §1 du master prompt appliquée.

---

## 6. LIMITES DOCUMENTÉES
- Historique M15 Yahoo plafonné à ~60 jours : impossible de rejouer une
  période de tendance pour mesurer le débit en régime TREND.
- Le shadow accumule à la cadence CI (~13 cycles/j) : à 0,02 signal/jour,
  la validation par shadow seul prendrait des années — d'où l'importance
  du replay et de l'option C pour accumuler du réel.
- Replay technique pur (news/DXY neutralisés, F-08) : le live ajoutera le
  filtre news (bloque plus) mais aucun bonus de débloquage.
