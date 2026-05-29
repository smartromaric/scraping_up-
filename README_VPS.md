# Déploiement VPS - Création de Flotte (Lancement Manuel)

## Résumé du workflow

**Tu contrôles quand lancer** - pas d'automatisation cron

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────┐     ┌────────────┐
│  Toi (manuel)   │────▶│  run_fleet   │────▶│  UpJunoo    │────▶│  Slack     │
│   ./run_fleet.sh│     │     .sh      │     │    Admin    │     │  (rapport) │
└─────────────────┘     └──────────────┘     └─────────────┘     └────────────┘
                              │
                              ▼
                        ┌──────────────┐
                        │ drivers.json │
                        │  (véhicules) │
                        └──────────────┘
```

## 1. Installation sur VPS

```bash
# Cloner ou copier les fichiers
scp -r scraping/ user@vps:/home/user/upjunoo-scraper
ssh user@vps

# Exécuter le setup
cd /home/user/upjunoo-scraper
chmod +x setup_vps.sh
./setup_vps.sh
```

## 2. Configuration

### Méthode A: Variables d'environnement (recommandé)

```bash
# ~/.bashrc ou ~/.profile
export UPJUNOO_EMAIL="admin@example.com"
export UPJUNOO_PASSWORD="supersecret"
export WEBHOOK_URL="https://ton-site.com/webhook/fleet"
```

### Méthode B: Arguments CLI

```bash
python3 create_fleet_vps.py Partenaire1 --email "admin@example.com" --password "secret" --webhook "https://..."
```

## 3. Exécution Manuelle

### Méthode simple (recommandée)

```bash
cd /home/user/upjunoo-scraper

# Scraping conducteurs
./run_drivers.sh

# Création flotte pour un partenaire
./run_fleet.sh Partenaire1
```

### Mode Debug (si pagination 500 échoue)

```bash
# Diagnostic complet pour voir pourquoi la pagination ne marche pas
python3 scrape_drivers_vps.py --debug

# Cela affichera:
# - Tous les <select> trouvés sur la page
# - Leurs classes, names et options
# - Le nombre de lignes détectées
```

### Méthode avancée (arguments CLI)

```bash
cd /home/user/upjunoo-scraper
source venv/bin/activate

# Tout spécifier en ligne de commande
python3 create_fleet_vps.py Partenaire1 \
  --email "admin@example.com" \
  --password "secret" \
  --webhook "https://hooks.slack.com/services/..."
```

### Avant de lancer (checklist)

1. ✅ Véhicules présents dans `output/organized_by_partner/NOM_PARTENAIRE/drivers.json`
2. ✅ Credentials configurés dans fichier `.env`
3. ✅ Slack webhook valide dans `.env`
4. ✅ Chrome/Chromium installé sur le VPS

## 4. Dépannage Pagination 500

Si le script ne récupère pas tous les conducteurs (ex: seulement 10 au lieu de 500+):

### Vérification rapide

```bash
# 1. Lance en mode debug
python3 scrape_drivers_vps.py --email "xxx" --password "xxx" --debug

# 2. Regarde la sortie - tu dois voir:
# 🔄 Tentative 1/3 pour pagination 500...
#    Options disponibles: ['10', '25', '50', '100', '500']
#    ✅ Option '500' sélectionnée
#    📊 Lignes avant attente: 10
#    ⏳ Chargement... 100 lignes
#    ⏳ Chargement... 250 lignes
#    ✅ Tableau stable: 500 lignes
```

### Problèmes courants

| Symptôme | Cause probable | Solution |
|----------|----------------|----------|
| "Option 500 non trouvée" | Le select n'a pas l'option 500 | Vérifie manuellement dans le navigateur |
| "Tableau stable: 10 lignes" | La pagination n'a pas changé | Le script détecte et avertit, vérifie UpJunoo |
| "0 select trouvé(s)" | La page n'a pas chargé le tableau | Attendre plus longtemps ou rafraîchir |

### Forcer l'arrêt si pagination échoue

Dans `scrape_drivers_vps.py`, décommente ces lignes (vers ligne 527):
```python
# Option: arrêter ici plutôt que de continuer avec des données incomplètes
report.finalize(0, 0)
driver.quit()
sys.exit(1)
```

## 5. Rapport de fin (Webhook)

Le script envoie un POST JSON à l'URL configurée:

```json
{
  "text": "✅ **Rapport Création Flotte - Partenaire1**...",
  "data": {
    "script": "create_fleet",
    "partner": "Partenaire1",
    "status": "success|partial|failed",
    "summary": {
      "total": 10,
      "success": 8,
      "failed": 1,
      "skipped": 1,
      "duration_seconds": 45.2
    },
    "details": {
      "success": [...],
      "failed": [...],
      "skipped": [...]
    }
  }
}
```

### Exemples d'intégrations webhook

#### Slack
```bash
export WEBHOOK_URL="https://hooks.slack.com/services/WORKSPACE_ID/CHANNEL_ID/TOKEN_A_CONFIGURER"
```

#### Discord
```bash
export WEBHOOK_URL="https://discord.com/api/webhooks/000000000000000000/XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
```

#### Telegram Bot
Utilise `webhook_example_receiver.py` et ajoute l'envoi Telegram dedans.

## 5. Architecture fichiers

```
upjunoo-scraper/
├── create_fleet_vps.py          # Script principal VPS
├── fleet-cron.sh                # Wrapper cron
├── fleet.service                # Service systemd
├── webhook_example_receiver.py  # Exemple receiver
├── setup_vps.sh                 # Script d'installation
├── output/organized_by_partner/
│   └── Partenaire1/
│       ├── drivers.json         # Données véhicules
│       └── fleet_report_*.json  # Rapports générés
└── logs/                        # Logs d'exécution
```

## 6. Monitoring

### Logs locaux
```bash
# Voir les logs du cron
tail -f ~/upjunoo-scraper/logs/fleet_Partenaire1_*.log

# Voir le dernier rapport
ls -t output/organized_by_partner/Partenaire1/fleet_report_*.json | head -1 | xargs cat | jq .
```

### Webhook receiver (optionnel)

Lance sur une machine distante pour recevoir les rapports:

```bash
pip install flask
python webhook_example_receiver.py
# Écoute sur http://0.0.0.0:5000/webhook/fleet
```

## 7. Redistribution des conducteurs (18 avril 2026) — TERMINÉ ✅

### Objectif
Répartir les conducteurs non assignés (rattachés à "Partenaire UPJUNOO CI") vers 120 partenaires pour atteindre **100 conducteurs par partenaire**.

### Résultat final

| Étape | Conducteurs | Méthode | Statut |
|-------|-------------|---------|--------|
| Phase 1 — auto | 546 | `redistribute_drivers.py` (Selenium) | ✅ OK |
| Phase 2 — manuel | 9 | Affectation manuelle via l'admin | ✅ OK |
| **Total** | **555** | | **✅ Complet** |

### 9 conducteurs assignés manuellement

| # | Conducteur | ID | Partenaire cible |
|---|-----------|-----|-----------------|
| 1 | DIARRA KALILOU | 14457 | Partenaire-116 |
| 2 | AMLIMAH KOMLANVI | 9103 | Partenaire-104 |
| 3 | SOME TOHO SALOMON | 8234 | Partenaire-118 |
| 4 | SANKARA ABDOU MOUMOUNI | 7653 | Partenaire-118 |
| 5 | INSA DIABATE | 7498 | Partenaire-115 |
| 6 | DJANGONE-BI KALOU OBED | 7462 | Partenaire-115 |
| 7 | COULIBALY ABDOUAYE | 6814 | Partenaire-103 |
| 8 | beugre kouadio fabrice | 6808 | Partenaire-103 |
| 9 | Diomandé Souatie | — | Partenaire-103 |

### Partenaires complétés à 100

- **Partenaire-116** : 99 → 100
- **Partenaire-104** : 99 → 100
- **Partenaire-118** : 98 → 100
- **Partenaire-115** : 98 → 100
- **Partenaire-103** : 97 → 100

### Scripts utilisés

- `scrape_partners.py` — scraping des 120 partenaires et comptage conducteurs
- `scrape_drivers_vps.py` — scraping des conducteurs non assignés
- `redistribute_drivers.py` — assignation automatique via Selenium
- `match_and_organize.py` — organisation des données par partenaire

## Dépendances supprimées (vs version locale)

| Avant (manuel) | Après (VPS auto) |
|----------------|------------------|
| `input()` connexion manuelle | `auto_login()` avec credentials |
| `input()` choix partenaire | Argument CLI `$1` |
| `input()` fermeture | `driver.quit()` auto |
| Chrome GUI | Chrome headless |
| Pas de notification | Webhook HTTP |
