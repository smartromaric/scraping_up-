#!/bin/bash
# Script d'installation pour VPS Ubuntu/Debian

echo "=== Setup UpJunoo Scraper sur VPS ==="

# Mettre à jour le système
echo "[1/6] Mise à jour du système..."
sudo apt update && sudo apt upgrade -y

# Installer Python et pip
echo "[2/6] Installation Python..."
sudo apt install -y python3 python3-pip python3-venv

# Installer Chrome et ChromeDriver
echo "[3/6] Installation Chrome..."
sudo apt install -y chromium-browser chromium-chromedriver

# Créer le répertoire de l'application
echo "[4/6] Configuration répertoire..."
APP_DIR="$HOME/upjunoo-scraper"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Créer environnement virtuel
echo "[5/6] Création environnement virtuel..."
python3 -m venv venv
source venv/bin/activate

# Installer dépendances
echo "[6/6] Installation dépendances Python..."
pip install selenium requests webdriver-manager

echo ""
echo "=== Installation terminée! ==="
echo ""
echo "Prochaines étapes:"
echo "1. Copie tes fichiers dans: $APP_DIR"
echo "2. Configure les variables d'environnement:"
echo "   export UPJUNOO_EMAIL='ton-email'"
echo "   export UPJUNOO_PASSWORD='ton-password'"
echo "   export WEBHOOK_URL='https://ton-webhook.com/endpoint'  # Pour notifications"
echo ""
echo "3. Teste le script: python3 create_fleet_vps.py Partenaire1"
echo "4. Configure le cron ou systemd (voir fleet-cron.sh et fleet.service)"
