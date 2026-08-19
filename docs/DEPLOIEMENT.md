# Guide de déploiement gratuit 24/7

Objectif : faire tourner `python -m src.main` (moteur) en continu, pour 0 €,
avec redémarrage automatique et supervision par heartbeat Telegram.

---

## 1. Choix de l'hébergement (comparatif honnête)

| Option | Coût | 24/7 réel ? | Verdict |
|---|---|---|---|
| **Oracle Cloud Always Free** (VM Ampere A1, 4 cœurs / 24 Go RAM) | 0 € à vie | Oui | **Recommandé** : seule offre gratuite pérenne sans mise en veille. Région Johannesburg proche de l'Afrique de l'Ouest. |
| PC personnel (ou Raspberry Pi) chez soi | 0 € | Oui si toujours allumé | Très bon pour démarrer : ce projet tourne sur ~200 Mo de RAM. |
| Railway (essai 30 j / 5 $ de crédit) | 0 € l'essai, puis ~1-5 €/mois | Oui (pas de veille, volume persistant) | Très bon pour démarrer en 10 min ; **payant après l'essai** — voir § 5b. |
| Render free tier | 0 € | **Non** : mise en veille après 15 min d'inactivité | Déconseillé pour la boucle ; utilisable seulement via cron externe (hack). |
| Fly.io / Koyeb (paliers gratuits) | 0 € limité | Partiel (crédits/machines éphémères) | Solution de repli si Oracle indisponible. |

> Le moteur consomme peu (CPU seulement aux cycles de ~5 s toutes les 60 s),
> mais il DOIT rester éveillé : la mise en veille = signaux perdus.

## 2. Procédure Oracle Cloud Always Free (recommandée)

### 2.1 Créer le compte et la VM
1. Créer un compte sur [cloud.oracle.com](https://cloud.oracle.com) (carte bancaire
   demandée pour vérification, **non débitée** sur l'offre Always Free).
2. Compute > Create instance :
   - **Shape** : VM.Standard.A1.Flex — 2 OCPU / 8 Go (sur le quota gratuit de 4/24) ;
   - **Image** : Ubuntu 22.04 (Canonical) ;
   - **Clé SSH** : générer/télécharger la clé privée ;
   - **Réseau** : laisser par défaut, n'ouvrir QUE le port 22 (SSH).
3. Noter l'IP publique : `ssh -i ~/.ssh/ma_cle ubuntu@<IP>`.

### 2.2 Préparer la VM (une fois connecté)
```bash
sudo apt update && sudo apt -y upgrade
sudo adduser --disabled-password forex            # utilisateur de service
sudo mkdir -p /opt/forex-signals && sudo chown forex:forex /opt/forex-signals
# Envoyer le projet depuis votre machine :
#   scp -i ~/.ssh/ma_cle -r /chemin/forex-signals forex@localhost:/tmp/  (depuis la VM)
#   ou plus simple, depuis votre machine :
#   rsync -avz -e "ssh -i ~/.ssh/ma_cle" ./forex-signals/ ubuntu@<IP>:/tmp/forex-signals/
sudo cp -r /tmp/forex-signals/* /opt/forex-signals/
sudo chown -R forex:forex /opt/forex-signals
```

### 2.3 Installation automatique
```bash
sudo -u forex bash /opt/forex-signals/deploy/install.sh
```
Le script installe le venv, les dépendances, crée `.env`, et active le
service `forex-engine` (redémarrage automatique, journal systemd).

### 2.4 Configurer Telegram et vérifier
```bash
sudo -u forex nano /opt/forex-signals/.env      # token + chat id
sudo systemctl restart forex-engine
sudo journalctl -u forex-engine -f               # doit afficher les cycles
```
Le **heartbeat Telegram** (« MOTEUR DE SIGNAUX — DEMARRAGE ») est la preuve
d'exploitation la plus simple : si vous ne le recevez plus après un
redémarrage annoncé, il y a un problème réseau.

### 2.5 Dashboard (accès privé par tunnel SSH)
```bash
sudo systemctl enable --now forex-dashboard      # écoute 127.0.0.1:8501
# Depuis votre machine :
ssh -i ~/.ssh/ma_cle -L 8501:localhost:8501 forex@<IP>
# puis ouvrir http://localhost:8501
```
Le dashboard n'est JAMAIS exposé sur Internet directement.

## 3. Exploitation quotidienne

| Besoin | Commande |
|---|---|
| Suivre les logs du moteur | `journalctl -u forex-engine -f` |
| État du service | `systemctl status forex-engine` |
| Redémarrer (après édition settings/.env) | `sudo systemctl restart forex-engine` |
| Mettre à jour le code | copier les fichiers puis `sudo systemctl restart forex-engine` |
| Sauvegarder la base | `sqlite3 /opt/forex-signals/data/signals.db ".backup '/opt/forex-signals/data/backup.db'"` (à mettre en cron quotidien) |
| Ouvrir le pare-feu | rien à faire : seul SSH (22) est ouvert |

Sauvegarde automatique quotidienne (crontab de forex) :
```
15 0 * * * sqlite3 /opt/forex-signals/data/signals.db ".backup '/opt/forex-signals/data/backup_$(date +\%u).db'"
```

## 4. Dépannage connu

| Symptôme | Cause probable | Action |
|---|---|---|
| `429 Too Many Requests` ForexFactory | trop d'appels depuis une IP partagée | RAS : repli BeautifulSoup automatique (journal WARNING) |
| Gel de 30-60 min des bougies spot FX | incident connu du flux Yahoo gratuit | RAS : cache + dégradation gracieuse ; le cycle suivant rattrape |
| `[XAUUSD 1d] données PERIMÉ` léger | stamp journalier COMEX vs tolérance | RAS si ponctuel ; persistant = vérifier la sortie Internet |
| Le moteur redémarre en boucle | config YAML invalide / .env vide | `journalctl -u forex-engine -n 50` puis corriger |
| Aucun signal depuis des jours | seuil 70 + marchés sans alignement | Normal (voir replay 30 j) ; ajuster `signals.min_score` si trop strict |

## 5b. Option Railway avec déploiement par push GitHub (tokens)

**Principe** : le moteur tourne en worker Railway (pas de veille, SQLite sur
volume persistant) et chaque `git push` sur `main` redéploie automatiquement
via GitHub Actions (conteneur CLI officiel `ghcr.io/railwayapp/cli` + token).

**Coût réel (2026)** : essai gratuit 30 jours / 5 $ de crédit unique, sans
carte — limites 0,5 Go RAM et volume 0,5 Go par service (le moteur consomme
~300 Mo : OK). Ensuite plan Hobby ~5 $/mois (5 $ d'usage inclus). C'est donc
une solution « gratuite pour tester, payante pour durer » : pour un 24/7
pérenne à 0 €, restez sur Oracle Always Free (§ 2).

### Étape 1 — Pousser le code sur GitHub (token PAT)
```bash
cd forex-signals
git init && git add -A && git commit -m "Moteur de signaux SMC"
# PAT : GitHub > Settings > Developer settings > Personal access tokens
#   (scopes : repo pour un dépôt privé)
git remote add origin https://<VOTRE_USER>:<VOTRE_PAT>@github.com/<VOTRE_USER>/forex-signals.git
git push -u origin main
```
Le `.gitignore` du projet exclut déjà `.env`, `data/`, `logs/`, `.venv/`
(aucun secret ni base dans le dépôt).

### Étape 2 — Projet Railway + service moteur
1. railway.app > New Project > **Deploy from GitHub repo** (ou Empty project,
   le workflow déploiera de toute façon).
2. Créer le service **engine** (Python détecté via `railway.json` à la racine :
   build Nixpacks, start `python -m src.main`, redémarrage ALWAYS).
3. Attacher un **volume** monté sur `/app/data` (SQLite persistante).
4. Variables du service (Settings > Variables) :
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ENABLED=true`.

### Étape 3 — Déploiement automatique par push (token Railway)
1. Railway > Account Settings > **API Tokens** > créer un token.
2. GitHub > dépôt > Settings > Secrets and variables > Actions :
   - `RAILWAY_TOKEN` = le token ci-dessus
   - `RAILWAY_SERVICE_ENGINE` = nom/UUID du service engine
3. C'est tout : le workflow `.github/workflows/deploy-railway.yml` (fourni)
   déploie à chaque push sur `main` (`railway up --service ... --ci --detach`).

### Option dashboard public
Second service Railway (type web) avec start command :
`streamlit run dashboard/app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`
+ variable `DASHBOARD_TOKEN` — l'app est protégée : on y accède par
`https://<domaine>.up.railway.app/?token=<valeur>` (gate intégrée dans
`dashboard/app.py`, inactive en local si la variable est absente).
Décommentez le job `deploy-dashboard` du workflow pour l'automatiser.

### Surveillance / arrêt
- Logs : `railway logs` ou l'onglet Deployments ; heartbeat Telegram = preuve de vie.
- Coût : onglet Usage — surveillez l'épuisement du crédit d'essai.
- Arrêt : Settings > Service > Delete (le volume se détache, à supprimer aussi).

## 5. Option Netlify (ZIP) — site de STATUT statique, pas le moteur

**Netlify n'héberge pas le moteur** : le drag-and-drop ZIP ne publie que des
fichiers statiques, et les fonctions serverless (exécution ~10 s en gratuit,
système de fichiers éphémère) ne peuvent pas faire tourner une boucle 24/7
avec SQLite. Streamlit non plus.

**Architecture hybride recommandée** (gratuite) :

    Moteur 24/7 (Oracle Always Free ou PC)  --export_static.py-->  ZIP
                                                                   |
    Statut public consultable partout  <-- Netlify Drop / API -----+

Procédure :
1. Sur l'hôte du moteur : `python scripts/export_static.py`
   -> génère `data/site/index.html` + `data/netlify_site.zip`
   (KPIs, positions, historique, courbe d'équité SVG pur sans JavaScript,
   transparence du dernier cycle, cartes de signal — mode démo si base vide).
2. Ouvrir https://app.netlify.com/drop et y déposer `netlify_site.zip` :
   le site est en ligne en ~10 secondes (URL `*.netlify.app`).
3. Rafraîchir : relancer le script puis redéposer, ou automatiser avec
   Netlify CLI sur l'hôte :
     pip install netlifyctl  (ou télécharger la CLI)
     netlify deploy --dir=data/site --prod --auth=<VOTRE_TOKEN> --site=<SITE_ID>
   en cron (ex. toutes les heures) ; le token se crée dans l'interface
   Netlify (User settings > Applications > Personal access tokens).

Limites assumées : page statique (pas de rafraîchissement en direct —
elle reflète l'instant de l'export) ; les données restent publiques sur
l'URL netlify.app (ne pas y mettre d'information sensible) ; le vrai
dashboard interactif reste accessible par tunnel SSH (section 2.5).

## 6. Alternative : votre propre machine

```bash
cd forex-signals && source .venv/bin/activate
python -m src.main            # terminal dédié, ou tmux/screen
streamlit run dashboard/app.py
```
Inconvénients : dépend de la veille/connexion électrique ; systemd est
préférable (le dossier `deploy/` fonctionne aussi sur un PC sous Linux).
