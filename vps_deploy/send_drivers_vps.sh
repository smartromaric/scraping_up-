#!/bin/bash
# Envoi scrape_drivers_vps.py sur le VPS

VPS_IP="194.29.101.141"
VPS_USER="sysadmin"

echo "=== Envoi scrape_drivers_vps.py sur le VPS ==="

# Test connexion
echo "Test connexion..."
ssh -q ${VPS_USER}@${VPS_IP} exit
if [ $? -ne 0 ]; then
    echo "❌ Impossible de se connecter au VPS"
    exit 1
fi

# Créer le dossier
echo "Création du dossier..."
ssh ${VPS_USER}@${VPS_IP} "mkdir -p ~/upjunoo-scraper"

# Envoyer le script
echo "Envoi de scrape_drivers_vps.py..."
scp ../scrape_drivers_vps.py ${VPS_USER}@${VPS_IP}:~/upjunoo-scraper/

# Envoyer requirements si différent
echo "Envoi de requirements.txt..."
scp ../requirements.txt ${VPS_USER}@${VPS_IP}:~/upjunoo-scraper/

# Créer le .env si inexistant
ssh ${VPS_USER}@${VPS_IP} "cd ~/upjunoo-scraper && \
  if [ ! -f .env ]; then \
    echo \"UPJUNOO_EMAIL=\" > .env && \
    echo \"UPJUNOO_PASSWORD=\" >> .env && \
    echo \"WEBHOOK_URL=\" >> .env; \
    echo '⚠️ .env créé - REMPLIS-LE avec tes credentials'; \
  fi"

echo ""
echo "✅ Terminé !"
echo ""
echo "Prochaines étapes sur le VPS :"
echo "  ssh ${VPS_USER}@${VPS_IP}"
echo "  cd ~/upjunoo-scraper"
echo "  nano .env  # Remplir EMAIL, PASSWORD, WEBHOOK_URL"
echo "  python3 scrape_drivers_vps.py"
