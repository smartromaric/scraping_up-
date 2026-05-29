#!/usr/bin/env python3
"""
Lance le dashboard web partenaires UPJUNOO.

  python run_partner_dashboard.py

Variables d'environnement :
  PARTNER_DASHBOARD_HOST=127.0.0.1
  PARTNER_DASHBOARD_PORT=8765
  DASHBOARD_AUTO_RUN=1          # extraction auto à 17h
  DASHBOARD_AUTO_TIME=17:00
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from partner_dashboard.api import main

if __name__ == "__main__":
    main()
