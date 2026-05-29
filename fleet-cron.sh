#!/bin/bash
# Script cron pour exécution automatique sur VPS
# Usage: À mettre dans crontab avec: 0 */6 * * * /chemin/fleet-cron.sh Partenaire1

PARTNER="${1:-Partenaire1}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$APP_DIR/logs"
LOG_FILE="$LOG_DIR/fleet_${PARTNER}_$(date +%Y%m%d_%H%M%S).log"

# Créer dossier logs
mkdir -p "$LOG_DIR"

# Charger l'environnement virtuel
source "$APP_DIR/.venv/bin/activate" 2>/dev/null || source "$APP_DIR/venv/bin/activate" 2>/dev/null

# Exporter les credentials (depuis ~/.bashrc ou définis ici)
export UPJUNOO_EMAIL="${UPJUNOO_EMAIL:-""}"
export UPJUNOO_PASSWORD="${UPJUNOO_PASSWORD:-""}"
export WEBHOOK_URL="${WEBHOOK_URL:-""}"

echo "[$(date)] Démarrage création flotte pour $PARTNER" >> "$LOG_FILE"

# Exécuter le script
python3 "$APP_DIR/create_fleet_vps.py" "$PARTNER" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$(date)] ✅ Succès" >> "$LOG_FILE"
else
    echo "[$(date)] ❌ Échec (code $EXIT_CODE)" >> "$LOG_FILE"
fi

# Garder seulement les 10 derniers logs
ls -t "$LOG_DIR"/fleet_${PARTNER}_*.log | tail -n +11 | xargs -r rm

exit $EXIT_CODE
