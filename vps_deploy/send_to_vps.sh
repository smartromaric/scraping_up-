#!/bin/bash
# Script pour envoyer les fichiers sur le VPS

echo "=== Envoi des fichiers sur le VPS ==="

# Vérifier la connexion
echo "Test connexion..."
ssh -q sysadmin@194.29.101.141 exit
if [ $? -ne 0 ]; then
    echo "❌ Impossible de se connecter au VPS"
    echo "Assure-toi d'être connecté en VPN ou que le VPS est accessible"
    exit 1
fi

# Créer le dossier sur le VPS
echo "Création du dossier sur le VPS..."
ssh sysadmin@194.29.101.141 "mkdir -p ~/upjunoo-scraper/config ~/upjunoo-scraper/output"

# Envoyer les fichiers
echo "Envoi de vps_scraper.py..."
scp vps_scraper.py sysadmin@194.29.101.141:~/upjunoo-scraper/

echo "Envoi des dépendances..."
scp requirements.txt sysadmin@194.29.101.141:~/upjunoo-scraper/

echo "Envoi des données output..."
scp -r output/* sysadmin@194.29.101.141:~/upjunoo-scraper/output/ 2>/dev/null || echo "⚠️ Aucun fichier dans output/ ou erreur"

echo ""
echo "=== Terminé ! ==="
echo "Prochaines étapes sur le VPS :"
echo "1. Se connecter : ssh sysadmin@194.29.101.141"
echo "2. Aller dans le dossier : cd ~/upjunoo-scraper"
echo "3. Activer l'environnement : source venv/bin/activate"
echo "4. Configurer .env : nano .env"
echo "5. Tester : python3 vps_scraper.py"
