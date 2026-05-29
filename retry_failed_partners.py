"""
Retry des partenaires qui ont échoué (0 conducteurs) dans partenaires.json.
Login auto + re-scrape uniquement les partenaires vides, puis met à jour le JSON/CSV.

Usage :
    export UPJUNOO_EMAIL="admin@upjunoo.com"
    export UPJUNOO_PASSWORD='123456789'
    python retry_failed_partners.py
"""

import json
import time
import traceback
from pathlib import Path

from scrape_partners import (
    BASE_URL,
    JSON_OUT,
    auto_login,
    export_all,
    get_credentials,
    scrape_drivers_for_partner,
)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

MAX_RETRIES = 2  # nombre de tentatives par partenaire échoué


def load_partners():
    if not JSON_OUT.exists():
        raise FileNotFoundError(f"❌ {JSON_OUT} introuvable — lance scrape_partners.py d'abord.")
    return json.loads(JSON_OUT.read_text(encoding="utf-8"))


def main():
    partners = load_partners()
    failed = [p for p in partners if not p.get("drivers")]
    print(f"\n📊 Total partenaires : {len(partners)}")
    print(f"❌ Partenaires à 0 conducteurs : {len(failed)}")
    for p in failed:
        print(f"   - {p.get('nom')} ({p.get('profile_url')})")
    if not failed:
        print("✅ Rien à refaire.")
        return

    email, password = get_credentials()

    opts = Options()
    opts.add_argument("--window-size=1600,1000")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

    try:
        auto_login(driver, email, password)

        for i, partner in enumerate(failed, 1):
            print(f"\n[{i}/{len(failed)}] Retry : {partner.get('nom')}")
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    drivers = scrape_drivers_for_partner(driver, partner)
                except Exception as e:
                    print(f"    ⚠️  Tentative {attempt}/{MAX_RETRIES} erreur : {e}")
                    drivers = []
                if drivers:
                    partner["drivers"] = drivers
                    print(f"    ✅ {len(drivers)} conducteurs récupérés (tentative {attempt}).")
                    break
                else:
                    print(f"    ⚠️  Tentative {attempt}/{MAX_RETRIES} → 0 conducteur.")
                    time.sleep(3)

            # Sauvegarde progressive toutes les 3 tentatives
            if i % 3 == 0 or i == len(failed):
                export_all(partners)

        # Sauvegarde finale
        export_all(partners)

        # Récap
        still_failed = [p for p in partners if not p.get("drivers")]
        print("\n" + "=" * 60)
        print(f"✅ Retry terminé : {len(failed) - len(still_failed)}/{len(failed)} récupérés.")
        if still_failed:
            print(f"⚠️  Toujours vides : {len(still_failed)}")
            for p in still_failed:
                print(f"   - {p.get('nom')} → {p.get('profile_url')}")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ ERREUR : {e}")
        traceback.print_exc()
    finally:
        time.sleep(3)
        driver.quit()


if __name__ == "__main__":
    main()
