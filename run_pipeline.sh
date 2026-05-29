#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  Pipeline UpJunoo — VPS
#  Usage:
#    ./run_pipeline.sh              → Tout (step1 + step2 + step3)
#    ./run_pipeline.sh 1            → Step 1 seulement
#    ./run_pipeline.sh 2            → Step 2 seulement
#    ./run_pipeline.sh 3            → Step 3 seulement
#    ./run_pipeline.sh 1 2          → Step 1 + 2
#    ./run_pipeline.sh 3 --dry-run  → Step 3 en simulation
# ═══════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")"

# Charger .env si présent
if [ -f .env ]; then
    set -a
    source .env
    set +a
    echo "✅ .env chargé"
fi

# Activer venv si présent
if [ -d venv ]; then
    source venv/bin/activate 2>/dev/null && echo "✅ venv activé"
fi

# Créer le dossier logs
mkdir -p output/logs

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="output/logs/pipeline_${TIMESTAMP}.log"

# Fonction de log
log() {
    echo "$1" | tee -a "$LOG_FILE"
}

log ""
log "═══════════════════════════════════════════════════"
log "  PIPELINE UPJUNOO — $(date)"
log "═══════════════════════════════════════════════════"

# Déterminer les étapes à exécuter
STEPS="${@:-1 2 3}"
EXTRA_ARGS=""

# Séparer les steps des arguments supplémentaires (ex: --dry-run)
STEP_LIST=""
for arg in $STEPS; do
    if [[ "$arg" == --* ]]; then
        EXTRA_ARGS="$EXTRA_ARGS $arg"
    else
        STEP_LIST="$STEP_LIST $arg"
    fi
done
STEP_LIST="${STEP_LIST:- 1 2 3}"

# ── STEP 1 ──
if echo "$STEP_LIST" | grep -qw "1"; then
    log ""
    log "━━━ STEP 1 : Scraping complet ━━━"
    python3 step1_scrape_all_vps.py 2>&1 | tee -a "$LOG_FILE"
    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        log "❌ Step 1 échoué — arrêt du pipeline"
        exit 1
    fi
    log "✅ Step 1 terminé"
fi

# ── STEP 2 ──
if echo "$STEP_LIST" | grep -qw "2"; then
    log ""
    log "━━━ STEP 2 : Organisation ━━━"
    python3 step2_organize.py 2>&1 | tee -a "$LOG_FILE"
    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        log "❌ Step 2 échoué — arrêt du pipeline"
        exit 1
    fi
    log "✅ Step 2 terminé"
fi

# ── STEP 3 ──
if echo "$STEP_LIST" | grep -qw "3"; then
    log ""
    log "━━━ STEP 3 : Mise à jour flotte ━━━"
    python3 step3_update_fleet_vps.py $EXTRA_ARGS 2>&1 | tee -a "$LOG_FILE"
    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        log "⚠️ Step 3 terminé avec des erreurs (voir rapport)"
    fi
    log "✅ Step 3 terminé"
fi

log ""
log "═══════════════════════════════════════════════════"
log "  PIPELINE TERMINÉ — $(date)"
log "  Log complet: $LOG_FILE"
log "═══════════════════════════════════════════════════"
