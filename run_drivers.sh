#!/bin/bash
# Lanceur manuel pour scraping conducteurs
# Usage: ./run_drivers.sh

set -e

# Charger les credentials depuis .env si présent
if [ -f .env ]; then
    echo "📄 Chargement .env..."
    export $(grep -v '^#' .env | xargs)
fi

# Vérifier que les variables sont définies
if [ -z "$UPJUNOO_EMAIL" ] || [ -z "$UPJUNOO_PASSWORD" ]; then
    echo "❌ Erreur: UPJUNOO_EMAIL et UPJUNOO_PASSWORD doivent être définis"
    echo "Crée un fichier .env avec:"
    echo "  UPJUNOO_EMAIL=ton-email"
    echo "  UPJUNOO_PASSWORD=ton-password"
    echo "  WEBHOOK_URL=https://hooks.slack.com/services/..."
    exit 1
fi

# Activer l'environnement virtuel
if [ -d ".venv" ]; then
    source .venv/bin/activate
elif [ -d "venv" ]; then
    source venv/bin/activate
fi

echo "🚀 Lancement scraping conducteurs"
echo "📧 Email: $UPJUNOO_EMAIL"
echo "🔗 Webhook: ${WEBHOOK_URL:-Non configuré}"
echo ""

# Exécuter le script
python3 scrape_drivers_vps.py
