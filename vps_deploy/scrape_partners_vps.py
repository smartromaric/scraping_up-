"""
Script VPS - Scraping Partenaires + Conducteurs UpJunoo (Full Auto)
====================================================================
Usage: python scrape_partners_vps.py [--webhook URL]

Phase 1 : Extrait la liste des partenaires depuis /manage-owners.
Phase 2 : Navigue dans chaque profil pour extraire leurs conducteurs.
Export JSON + CSV + rapport webhook.

Variables d'environnement:
  UPJUNOO_EMAIL    - Email de connexion
  UPJUNOO_PASSWORD - Mot de passe
  WEBHOOK_URL      - URL du webhook Slack (optionnel)
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from webdriver_manager.chrome import ChromeDriverManager

# ─── Configuration ───────────────────────────────────────────────────────────
BASE_URL   = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL  = f"{BASE_URL}/login/admin"
OWNERS_URL = f"{BASE_URL}/manage-owners"
OUTPUT_DIR = Path(__file__).parent / "output"

# Filtre : Partenaire[s]?-?N avec N >= 1 (sans limite haute)
PARTNER_NAME_RE = re.compile(r'^\s*partenaires?-?\s*(\d+)\s*$', re.I)
PARTNER_MIN = 1

PAGE_LOAD_TIMEOUT = 30
LOGIN_TIMEOUT     = 30


# ═════════════════════════════════════════════════════════════════════════════
#  RAPPORT
# ═════════════════════════════════════════════════════════════════════════════

class PartnersReport:
    def __init__(self):
        self.start_time = datetime.now()
        self.end_time: Optional[datetime] = None
        self.total_partners = 0
        self.total_drivers  = 0
        self.errors: List[str] = []

    def add_error(self, msg: str):
        self.errors.append({"message": msg, "timestamp": datetime.now().isoformat()})

    def finalize(self, partners: int, drivers: int):
        self.end_time      = datetime.now()
        self.total_partners = partners
        self.total_drivers  = drivers

    def to_dict(self) -> Dict:
        duration = (self.end_time - self.start_time).total_seconds() if self.end_time else 0
        status   = "success" if not self.errors else ("partial" if self.total_partners > 0 else "failed")
        return {
            "script": "scrape_partners",
            "status": status,
            "summary": {
                "total_partners":      self.total_partners,
                "total_drivers":       self.total_drivers,
                "duration_seconds":    duration,
                "errors_count":        len(self.errors),
            },
            "timing": {
                "started":          self.start_time.isoformat(),
                "finished":         self.end_time.isoformat() if self.end_time else None,
                "duration_seconds": duration,
            },
            "errors": self.errors,
        }

    def to_markdown(self) -> str:
        d = self.to_dict()
        emoji = "✅" if d["status"] == "success" else ("⚠️" if d["status"] == "partial" else "❌")
        return f"""{emoji} **Rapport Scraping Partenaires**

**Statut:** {'Succès' if d['status'] == 'success' else 'Succès partiel' if d['status'] == 'partial' else 'Échec'}
**Durée:** {d['summary']['duration_seconds']:.1f}s

**Résumé:**
- 🏢 Partenaires: {d['summary']['total_partners']}
- 👥 Conducteurs totaux: {d['summary']['total_drivers']}
- ❌ Erreurs: {d['summary']['errors_count']}
"""


# ═════════════════════════════════════════════════════════════════════════════
#  WEBHOOK
# ═════════════════════════════════════════════════════════════════════════════

def send_webhook(report: PartnersReport, webhook_url: str) -> bool:
    if not webhook_url:
        return False
    is_slack = "slack.com" in webhook_url.lower()
    try:
        d = report.to_dict()
        s = d["summary"]
        if is_slack:
            color = "#36a64f" if d["status"] == "success" else "#ff9900" if d["status"] == "partial" else "#ff0000"
            payload = {
                "text": f"Scraping partenaires: {s['total_partners']} partenaires, {s['total_drivers']} conducteurs",
                "username": os.getenv("SLACK_BOT_NAME", "UpJunoo Partners"),
                "icon_emoji": os.getenv("SLACK_ICON_EMOJI", ":office:"),
                "attachments": [{
                    "color": color,
                    "title": "Rapport Scraping Partenaires",
                    "fields": [
                        {"title": "🏢 Partenaires",  "value": str(s["total_partners"]),   "short": True},
                        {"title": "👥 Conducteurs",  "value": str(s["total_drivers"]),    "short": True},
                        {"title": "⏱️ Durée",        "value": f"{s['duration_seconds']:.1f}s", "short": True},
                        {"title": "❌ Erreurs",      "value": str(s["errors_count"]),     "short": True},
                    ],
                    "footer": "UpJunoo VPS",
                    "ts": int(datetime.now().timestamp()),
                }],
            }
        else:
            payload = {"text": report.to_markdown(), "data": d}

        resp = requests.post(webhook_url, json=payload,
                             headers={"Content-Type": "application/json"}, timeout=30)
        resp.raise_for_status()
        print(f"✅ Webhook envoyé (HTTP {resp.status_code})")
        return True
    except Exception as e:
        print(f"❌ Échec webhook: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  CHROME HEADLESS
# ═════════════════════════════════════════════════════════════════════════════

def setup_driver_headless():
    """Initialise Chrome en mode headless pour VPS."""
    import shutil
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument("--remote-debugging-port=9222")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--single-process")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.0")

    for binary in ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable"]:
        path = shutil.which(binary)
        if path:
            chrome_options.binary_location = path
            print(f"   ✅ Binaire Chrome: {path}")
            break

    chromedriver_path = None
    for cd in ["chromedriver", "/usr/bin/chromedriver",
               "/usr/lib/chromium-browser/chromedriver", "/usr/lib/chromium/chromedriver"]:
        if os.path.isfile(cd) or shutil.which(cd):
            chromedriver_path = cd if os.path.isfile(cd) else shutil.which(cd)
            print(f"   ✅ ChromeDriver: {chromedriver_path}")
            break

    service = Service(chromedriver_path) if chromedriver_path else Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


# ═════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═════════════════════════════════════════════════════════════════════════════

def auto_login(driver, email: str, password: str) -> bool:
    print(f"🔐 Connexion à {LOGIN_URL}...")
    wait = WebDriverWait(driver, LOGIN_TIMEOUT)
    try:
        driver.get(LOGIN_URL)
        email_input = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
        )))
        pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password']")
        email_input.clear(); email_input.send_keys(email)
        pwd_input.clear();   pwd_input.send_keys(password)
        try:
            driver.find_element(By.XPATH, "//button[@type='submit']").click()
        except:
            pwd_input.submit()
        wait.until(lambda d: "/login" not in d.current_url)
        print(f"✅ Connecté: {driver.current_url}")
        return True
    except Exception as e:
        print(f"❌ Erreur connexion: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  PAGINATION
# ═════════════════════════════════════════════════════════════════════════════

def set_page_size_500(driver) -> bool:
    for attempt in range(3):
        try:
            print(f"🔄 Tentative {attempt+1}/3 pour pagination 500...")
            try:
                sel_el = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "select.form-select.form-select-sm.w-auto, "
                    "select[name='DataTables_Table_0_length'], "
                    "select[data-dt-idx='0']"
                )))
            except:
                sel_el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR, ".dataTables_length select, select.form-select"
                )))

            select_obj = Select(sel_el)
            options = [o.text.strip() for o in select_obj.options]
            target  = next((o for o in options if "500" in o), None)
            if not target:
                print(f"   ⚠️ Option 500 non trouvée dans {options}")
                time.sleep(2); continue

            select_obj.select_by_visible_text(target)
            print(f"   ✅ Pagination '{target}' sélectionnée")
            time.sleep(3)

            start, last, stable = time.time(), 0, 0
            while time.time() - start < 30:
                time.sleep(1)
                count = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
                if count == last and count > 0:
                    stable += 1
                    if stable >= 3 and count >= 50:
                        print(f"   ✅ Tableau stable: {count} lignes")
                        return True
                else:
                    stable, last = 0, count
            if last > 0:
                print(f"   ✅ Pagination activée: {last} lignes")
                return True

        except Exception as e:
            print(f"   ❌ Erreur tentative {attempt+1}: {e}")
            time.sleep(3)
            if attempt < 2:
                driver.refresh(); time.sleep(2)

    return False


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 1 : LISTE DES PARTENAIRES
# ═════════════════════════════════════════════════════════════════════════════

def should_keep_partner(nom: str) -> bool:
    m = PARTNER_NAME_RE.match(nom or "")
    if not m:
        return False
    return int(m.group(1)) >= PARTNER_MIN


def scrape_global_list(driver) -> list:
    partners = []
    try:
        print(f"\n📍 Ouverture de {OWNERS_URL}...")
        driver.get(OWNERS_URL)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )

        ok = set_page_size_500(driver)
        if not ok:
            print("⚠️ Pagination 500 impossible – on continue avec ce qui est chargé")

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        print(f"🔍 {len(rows)} lignes détectées dans le tableau partenaires")

        for i, row in enumerate(rows):
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6:
                    continue

                nom       = cells[0].text.strip()
                email     = cells[1].text.strip()
                telephone = cells[2].text.strip()

                document_url = "N/A"
                profile_url  = "N/A"
                owner_id     = "N/A"

                try:
                    link_doc     = cells[3].find_element(By.TAG_NAME, "a")
                    document_url = link_doc.get_attribute("href")
                    if document_url and "/document/" in document_url:
                        owner_id    = document_url.split("/document/")[-1]
                        profile_url = f"{BASE_URL}/manage-owners/view-profile/{owner_id}"
                except:
                    pass

                if not should_keep_partner(nom):
                    continue

                partners.append({
                    "nom":          nom,
                    "email":        email,
                    "telephone":    telephone,
                    "document_url": document_url,
                    "profile_url":  profile_url,
                    "owner_id":     owner_id,
                    "drivers":      [],
                })
            except Exception as e:
                print(f"  ⚠️ Erreur ligne {i}: {e}")

        print(f"✅ {len(partners)} partenaires filtrés (Partenaire[s]?-?N, N>={PARTNER_MIN})")
    except Exception as e:
        print(f"❌ Erreur extraction liste: {e}")

    return partners


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 2 : CONDUCTEURS PAR PARTENAIRE
# ═════════════════════════════════════════════════════════════════════════════

def scrape_drivers_for_partner(driver, partner: dict) -> list:
    drivers = []
    if not partner["profile_url"] or partner["profile_url"] == "N/A":
        return []
    try:
        driver.get(partner["profile_url"])
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((
            By.XPATH,
            "//a[contains(text(), 'Détails du conducteur')] | //a[contains(text(), 'Driver Details')]"
        )))

        tab = driver.find_element(
            By.XPATH,
            "//a[contains(text(), 'Détails du conducteur')] | //a[contains(text(), 'Driver Details')]"
        )
        driver.execute_script("arguments[0].click();", tab)
        time.sleep(2)

        driver_rows = driver.find_elements(By.CSS_SELECTOR, ".tab-pane.active table tbody tr")
        if not driver_rows:
            driver_rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

        for dr in driver_rows:
            d_cells = dr.find_elements(By.TAG_NAME, "td")
            if len(d_cells) >= 4:
                drivers.append({
                    "nom":            d_cells[0].text.strip(),
                    "emplacement":    d_cells[1].text.strip() if len(d_cells) > 1 else "N/A",
                    "telephone":      d_cells[2].text.strip() if len(d_cells) > 2 else "N/A",
                    "type_transport": d_cells[3].text.strip() if len(d_cells) > 3 else "N/A",
                    "type_vehicule":  d_cells[4].text.strip() if len(d_cells) > 4 else "N/A",
                })
    except Exception as e:
        print(f"      ❌ Erreur profil {partner['nom']}: {e}")

    return drivers


# ═════════════════════════════════════════════════════════════════════════════
#  EXPORT
# ═════════════════════════════════════════════════════════════════════════════

def export_all(data: list, report: PartnersReport):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # JSON
    json_path = OUTPUT_DIR / f"partenaires_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ JSON: {json_path}")

    # CSV résumé
    csv_path = OUTPUT_DIR / f"partenaires_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Nom Entreprise", "Email", "Portable", "URL Document", "URL Profil", "Nombre Conducteurs"])
        for row in data:
            writer.writerow([
                row.get("nom", ""),
                row.get("email", ""),
                row.get("telephone", ""),
                row.get("document_url", ""),
                row.get("profile_url", ""),
                len(row.get("drivers", [])),
            ])
    print(f"✅ CSV: {csv_path}")

    # Rapport JSON
    report_path = OUTPUT_DIR / f"partners_report_{ts}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
    print(f"📄 Rapport: {report_path}")


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Scraping partenaires UpJunoo (VPS)")
    parser.add_argument("--webhook",  help="URL webhook",    default=os.getenv("WEBHOOK_URL"))
    parser.add_argument("--email",    help="Email admin",    default=os.getenv("UPJUNOO_EMAIL"))
    parser.add_argument("--password", help="Mot de passe",   default=os.getenv("UPJUNOO_PASSWORD"))
    parser.add_argument("--skip-drivers", action="store_true",
                        help="Phase 1 uniquement (pas de crawl profil conducteurs)")
    args = parser.parse_args()

    if not args.email or not args.password:
        print("❌ Erreur: UPJUNOO_EMAIL et UPJUNOO_PASSWORD requis")
        sys.exit(1)

    report = PartnersReport()
    driver = setup_driver_headless()

    try:
        if not auto_login(driver, args.email, args.password):
            report.add_error("Échec authentification")
            report.finalize(0, 0)
            export_all([], report)
            if args.webhook:
                send_webhook(report, args.webhook)
            sys.exit(1)

        # ── Phase 1 ──────────────────────────────────────────────────────────
        partners = scrape_global_list(driver)

        if not partners:
            print("❌ Aucun partenaire trouvé.")
            report.add_error("Aucun partenaire trouvé")
            report.finalize(0, 0)
            export_all([], report)
            if args.webhook:
                send_webhook(report, args.webhook)
            sys.exit(1)

        export_all(partners, report)

        # ── Phase 2 ──────────────────────────────────────────────────────────
        if not args.skip_drivers:
            print(f"\n🚀 Phase 2 : crawl des conducteurs pour {len(partners)} partenaires...")
            total_drivers = 0
            for i, partner in enumerate(partners):
                print(f"  [{i+1}/{len(partners)}] {partner['nom']}...")
                partner["drivers"] = scrape_drivers_for_partner(driver, partner)
                count = len(partner["drivers"])
                total_drivers += count
                print(f"    ✅ {count} conducteurs")

                if (i + 1) % 5 == 0 or (i + 1) == len(partners):
                    export_all(partners, report)

            report.finalize(len(partners), total_drivers)
        else:
            report.finalize(len(partners), 0)

        print("\n" + "="*50)
        print(f"🎉 TERMINÉ")
        print(f"🏢 {report.total_partners} partenaires | 👥 {report.total_drivers} conducteurs")
        print("="*50)

        if args.webhook:
            send_webhook(report, args.webhook)

        print("\n" + report.to_markdown())

    except Exception as e:
        report.add_error(f"Erreur critique: {traceback.format_exc()}")
        report.finalize(0, 0)
        export_all([], report)
        if args.webhook:
            send_webhook(report, args.webhook)
    finally:
        driver.quit()

    sys.exit(0 if report.to_dict()["status"] in ("success", "partial") else 1)


if __name__ == "__main__":
    main()
