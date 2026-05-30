#!/usr/bin/env bash
# Installation / mise à jour du dashboard partenaires sur le VPS.
# Usage (sur le VPS, en root) :
#   curl -sSL .../install_dashboard_vps.sh | bash
#   ou : bash deploy/install_dashboard_vps.sh

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/smartromaric/scraping_up-.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/scraping_up-}"
PORT="${PARTNER_DASHBOARD_PORT:-8770}"

echo "==> Répertoire : $INSTALL_DIR"

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  git clone "$REPO_URL" "$INSTALL_DIR"
else
  git -C "$INSTALL_DIR" pull --ff-only
fi

cd "$INSTALL_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements-partner-dashboard.txt
.venv/bin/pip install -r requirements.txt

mkdir -p output/partner_automation

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo ""
  echo "!! Créez .env avec UPJUNOO_EMAIL et UPJUNOO_PASSWORD :"
  echo "   nano $INSTALL_DIR/.env"
fi

if [[ ! -f output/partner_automation/state.json ]]; then
  echo '{"partners":{}}' > output/partner_automation/state.json
  echo "!! state.json vide créé — copiez votre state local si besoin."
fi

cp deploy/partner-dashboard.service /etc/systemd/system/partner-dashboard.service
systemctl daemon-reload
systemctl enable partner-dashboard
systemctl restart partner-dashboard

if command -v ufw >/dev/null 2>&1; then
  ufw allow "${PORT}/tcp" comment "UPJUNOO dashboard" || true
fi

echo ""
echo "Dashboard : http://$(hostname -I | awk '{print $1}'):${PORT}/"
systemctl status partner-dashboard --no-pager || true
