#!/bin/bash
# Lancement rapide de scrape_drivers_vps.py sur le VPS

echo "=== Lancement scrape_drivers_vps.py sur le VPS ==="

VPS_IP="194.29.101.141"
VPS_USER="sysadmin"

# Exécuter sur le VPS
ssh ${VPS_USER}@${VPS_IP} "
    cd ~/upjunoo-scraper &&
    source venv/bin/activate 2>/dev/null || echo '⚠️ Pas de venv, utilisation python3 global' &&
    
    # Vérifier .env
    if [ ! -f .env ]; then
        echo '❌ Fichier .env introuvable'
        echo 'Création...'
        echo 'UPJUNOO_EMAIL=admin@upjunoo.com' > .env
        echo 'UPJUNOO_PASSWORD=123456789' >> .env
        echo 'WEBHOOK_URL=' >> .env
        echo '⚠️ Remplis le fichier .env avec tes vrais credentials'
        exit 1
    fi &&
    
    echo '🚀 Lancement du scraping...' &&
    python3 scrape_drivers_vps.py 2>&1 | tee logs/scrape_$(date +%Y%m%d_%H%M%S).log
"

echo ""
echo "✅ Terminé"
echo "Pour voir les logs: ssh ${VPS_USER}@${VPS_IP} 'tail -f ~/upjunoo-scraper/logs/scrape_*.log'"
