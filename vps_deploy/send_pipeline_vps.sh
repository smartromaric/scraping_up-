#!/bin/bash
# Envoi du pipeline complet sur le VPS

VPS_IP="194.29.101.141"
VPS_USER="sysadmin"
REMOTE_DIR="~/upjunoo-scraper"

echo "=== Envoi Pipeline sur le VPS ==="

# Test connexion
echo "Test connexion..."
ssh -q ${VPS_USER}@${VPS_IP} exit
if [ $? -ne 0 ]; then
    echo "❌ Impossible de se connecter au VPS"
    exit 1
fi

# Créer les dossiers
echo "Création des dossiers..."
ssh ${VPS_USER}@${VPS_IP} "mkdir -p ${REMOTE_DIR}/output/reports ${REMOTE_DIR}/output/logs"

# Envoyer les scripts du pipeline
echo "Envoi des scripts..."
SCRIPTS=(
    "../step1_scrape_all_vps.py"
    "../step2_organize.py"
    "../step3_update_fleet_vps.py"
    "../run_pipeline.sh"
    "../requirements.txt"
)

for script in "${SCRIPTS[@]}"; do
    if [ -f "$script" ]; then
        scp "$script" ${VPS_USER}@${VPS_IP}:${REMOTE_DIR}/
        echo "  ✅ $(basename $script)"
    else
        echo "  ⚠️ $(basename $script) introuvable"
    fi
done

# Rendre exécutable
ssh ${VPS_USER}@${VPS_IP} "chmod +x ${REMOTE_DIR}/run_pipeline.sh"

# Vérifier/créer .env
ssh ${VPS_USER}@${VPS_IP} "cd ${REMOTE_DIR} && \
  if [ ! -f .env ]; then \
    echo 'UPJUNOO_EMAIL=' > .env && \
    echo 'UPJUNOO_PASSWORD=' >> .env && \
    echo 'WEBHOOK_URL=' >> .env; \
    echo '⚠️ .env créé — REMPLIS-LE'; \
  fi"

echo ""
echo "✅ Pipeline envoyé !"
echo ""
echo "Prochaines étapes sur le VPS :"
echo "  ssh ${VPS_USER}@${VPS_IP}"
echo "  cd ${REMOTE_DIR}"
echo "  nano .env                      # Vérifier credentials"
echo "  ./run_pipeline.sh              # Tout lancer"
echo "  ./run_pipeline.sh 1 2          # Step 1+2 seulement"
echo "  ./run_pipeline.sh 3 --dry-run  # Step 3 en simulation"
echo ""
echo "En arrière-plan :"
echo "  nohup ./run_pipeline.sh > output/logs/pipeline.log 2>&1 &"
