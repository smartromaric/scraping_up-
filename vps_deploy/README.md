# UpJunoo Scraper - VPS

Script de scraping pour VPS (mode headless, notifications email).

## Fichiers

- `vps_scraper.py` - Script principal
- `requirements.txt` - Dépendances Python
- `config/` - Configuration (.env)
- `output/` - Données générées

## Installation sur VPS

```bash
# 1. Se connecter au VPS
ssh sysadmin@194.29.101.141

# 2. Créer l'environnement
cd ~/upjunoo-scraper
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Configurer les variables
cp config/.env.example config/.env
nano config/.env  # Éditer avec tes valeurs

# 4. Tester
python3 vps_scraper.py
```

## Configuration .env

```
UPJUNOO_EMAIL=ton-email@upjunoo.com
UPJUNOO_PASSWORD=ton-mot-de-passe
EMAILJS_SERVICE_ID=service_xxx
EMAILJS_TEMPLATE_ID=template_xxx
EMAILJS_PUBLIC_KEY=xxx
RECIPIENT_EMAIL=email-destinataire@example.com
```

## Lancement automatique (cron)

```bash
# Éditer le crontab
crontab -e

# Ajouter pour 2h du matin tous les jours
0 2 * * * cd ~/upjunoo-scraper && source venv/bin/activate && source config/.env && python3 vps_scraper.py >> output/cron.log 2>&1
```

## Logs

Les logs sont dans `output/scraper.log`
