"""Configuration du dashboard partenaires UPJUNOO."""

from __future__ import annotations

import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = SCRIPT_DIR / "output" / "partner_automation"
STATE_FILE = OUTPUT_DIR / "state.json"
ACTIVATION_DIR = OUTPUT_DIR / "rapports_activation"
ZIP_DIR = OUTPUT_DIR / "zip_soir"
DASHBOARD_DIR = OUTPUT_DIR / "dashboard"
JOB_STATUS_FILE = DASHBOARD_DIR / "job_status.json"
SCHEDULER_STATE_FILE = DASHBOARD_DIR / "scheduler_state.json"

NIGHTLY_SCRIPT = SCRIPT_DIR / "nightly_reports_runner.py"
ORCHESTRATOR_SCRIPT = SCRIPT_DIR / "partner_fleet_orchestrator.py"
PAYMENT_SYNC_SCRIPT = SCRIPT_DIR / "sync_payment_history_state.py"
ACTIVATION_SCRIPT = SCRIPT_DIR / "generate_activation_report.py"
EXPORT_SCRIPT = SCRIPT_DIR / "export_partner_fleet_drivers_report.py"
DOWNLOAD_GODSEYE_SCRIPT = SCRIPT_DIR / "download_vehicules_en_ligne.py"
CHAUFFEURS_ACTIFS_LATEST = OUTPUT_DIR / "chauffeurs_actifs_state.xlsx"
CHAUFFEURS_ARCHIVE_DIR = OUTPUT_DIR / "archive_chauffeurs_actifs"

STATIC_DIR = Path(__file__).resolve().parent / "static"

HOST = os.getenv("PARTNER_DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("PARTNER_DASHBOARD_PORT", "8765"))

AUTO_RUN_ENABLED = os.getenv("DASHBOARD_AUTO_RUN", "1").strip() not in ("0", "false", "no")
AUTO_RUN_TIME = os.getenv("DASHBOARD_AUTO_TIME", "17:00").strip()
