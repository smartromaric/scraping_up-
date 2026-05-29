"""
Step 1 — Scraping complet : Partenaires + Conducteurs + Véhicules
=================================================================
Usage:  python3 step1_scrape_all_vps.py
Env:    UPJUNOO_EMAIL, UPJUNOO_PASSWORD  (ou --email / --password)

Flow:
  1. Login admin
  2. /manage-owners  → liste des partenaires (pagination 500)
  3. Profil chaque partenaire → onglet "Détails du conducteur" → drivers
  4. /fleet-drivers → mapping téléphone → URL profil driver
  5. Cookies Selenium → requests → visite chaque profil → véhicule
  6. Export output/step1_partners_complete.json
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests as http_requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from webdriver_manager.chrome import ChromeDriverManager

# ─── Configuration ────────────────────────────────────────────────────────────
BASE_URL      = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL     = f"{BASE_URL}/login/admin"
OWNERS_URL    = f"{BASE_URL}/manage-owners"
DRIVERS_URL   = f"{BASE_URL}/fleet-drivers"
OUTPUT_DIR    = Path(__file__).parent / "output"
OUTPUT_FILE   = OUTPUT_DIR / "step1_partners_complete.json"

PAGE_LOAD_TIMEOUT = 30
LOGIN_TIMEOUT     = 30
VEHICLE_CONCURRENCY = 10

# Filtre partenaires : Partenaire[s]?-?N avec N >= 1
PARTNER_NAME_RE = re.compile(r'^\s*partenaires?-?\s*(\d+)\s*$', re.I)
PARTNER_MIN = 1


def should_keep_partner(nom: str) -> bool:
    m = PARTNER_NAME_RE.match(nom or "")
    if not m:
        return False
    return int(m.group(1)) >= PARTNER_MIN


# ═════════════════════════════════════════════════════════════════════════════
#  CHROME HEADLESS
# ═════════════════════════════════════════════════════════════════════════════

def setup_driver_headless():
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
#  PAGINATION HELPER
# ═════════════════════════════════════════════════════════════════════════════

def set_page_size_500(driver) -> bool:
    for attempt in range(3):
        try:
            print(f"   🔄 Tentative {attempt+1}/3 pour pagination 500...")
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
                print(f"      ⚠️ Option 500 non trouvée dans {options}")
                time.sleep(2); continue

            select_obj.select_by_visible_text(target)
            print(f"      ✅ Pagination '{target}' sélectionnée")
            time.sleep(3)

            start, last, stable = time.time(), 0, 0
            while time.time() - start < 30:
                time.sleep(1)
                count = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
                if count == last and count > 0:
                    stable += 1
                    if stable >= 3 and count >= 10:
                        print(f"      ✅ Tableau stable: {count} lignes")
                        return True
                else:
                    stable, last = 0, count
            if last > 0:
                print(f"      ✅ {last} lignes chargées")
                return True

        except Exception as e:
            print(f"      ❌ Tentative {attempt+1}: {e}")
            time.sleep(3)
            if attempt < 2:
                driver.refresh(); time.sleep(2)
    return False


def _get_first_row_sig(driver):
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        return rows[0].text[:200] if rows else None
    except:
        return None


def _is_next_disabled(driver):
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item.disabled a.page-link[aria-label='Next']"
        )
        return True
    except NoSuchElementException:
        return False


def go_to_next_page(driver) -> bool:
    if _is_next_disabled(driver):
        return False
    try:
        btn = driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item:not(.disabled) a.page-link[aria-label='Next']"
        )
        if not btn.is_displayed():
            return False
        prev = _get_first_row_sig(driver)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            btn.click()
        except:
            driver.execute_script("arguments[0].click();", btn)
        start = time.time()
        while time.time() - start < PAGE_LOAD_TIMEOUT:
            time.sleep(0.5)
            new_sig = _get_first_row_sig(driver)
            if new_sig and new_sig != prev:
                time.sleep(1.0)
                return True
            if _is_next_disabled(driver):
                return False
        return False
    except NoSuchElementException:
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 1 : LISTE DES PARTENAIRES
# ═════════════════════════════════════════════════════════════════════════════

def scrape_partners_list(driver) -> list:
    """Scrape la liste des partenaires depuis /manage-owners."""
    print(f"\n{'='*60}")
    print("📋 PHASE 1 : Liste des partenaires")
    print(f"{'='*60}")

    partners = []
    driver.get(OWNERS_URL)
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
    )
    set_page_size_500(driver)
    time.sleep(2)

    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    print(f"   🔍 {len(rows)} lignes détectées")

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
                link_doc = cells[3].find_element(By.TAG_NAME, "a")
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
        except StaleElementReferenceException:
            continue
        except Exception as e:
            print(f"      ⚠️ Erreur ligne {i}: {e}")

    print(f"   ✅ {len(partners)} partenaires filtrés")
    return partners


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 2 : CONDUCTEURS PAR PARTENAIRE (profil partenaire)
# ═════════════════════════════════════════════════════════════════════════════

def scrape_drivers_for_partner(driver, partner: dict) -> list:
    """Visite le profil partenaire → onglet conducteurs → extrait la liste."""
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
            try:
                d_cells = dr.find_elements(By.TAG_NAME, "td")
                if len(d_cells) < 4:
                    continue

                drv = {
                    "nom":            d_cells[0].text.strip(),
                    "emplacement":    d_cells[1].text.strip() if len(d_cells) > 1 else "N/A",
                    "telephone":      d_cells[2].text.strip() if len(d_cells) > 2 else "N/A",
                    "type_transport": d_cells[3].text.strip() if len(d_cells) > 3 else "N/A",
                    "type_vehicule":  d_cells[4].text.strip() if len(d_cells) > 4 else "N/A",
                    "document_url":   "N/A",
                    "view_profile":   "N/A",
                    "vehicle": {
                        "type": "N/A",
                        "marque": "N/A",
                        "modele": "N/A",
                        "matricule": "N/A",
                    },
                }

                # Chercher des liens dans la ligne (document / profil)
                for cell in d_cells:
                    try:
                        links = cell.find_elements(By.TAG_NAME, "a")
                        for link in links:
                            href = link.get_attribute("href") or ""
                            if "/document/" in href:
                                drv["document_url"] = href
                                drv["view_profile"] = href.replace("/document/", "/view-profile/")
                            elif "/view-profile/" in href:
                                drv["view_profile"] = href
                    except:
                        pass

                drivers.append(drv)
            except StaleElementReferenceException:
                continue
            except Exception as e:
                print(f"         ⚠️ Erreur driver: {e}")

    except TimeoutException:
        print(f"      ⚠️ Onglet conducteurs non trouvé pour {partner['nom']}")
    except Exception as e:
        print(f"      ❌ Erreur profil {partner['nom']}: {e}")

    return drivers


def scrape_all_partner_drivers(driver, partners: list):
    """Phase 2 : visite chaque profil partenaire pour récupérer ses conducteurs."""
    print(f"\n{'='*60}")
    print("👥 PHASE 2 : Conducteurs par partenaire")
    print(f"{'='*60}")

    total_drivers = 0
    for i, partner in enumerate(partners):
        print(f"   [{i+1}/{len(partners)}] {partner['nom']}...", end=" ", flush=True)
        partner["drivers"] = scrape_drivers_for_partner(driver, partner)
        count = len(partner["drivers"])
        total_drivers += count
        print(f"→ {count} conducteurs")

        # Sauvegarde progressive toutes les 5 itérations
        if (i + 1) % 5 == 0:
            _save_progress(partners, "step1_progress.json")

    print(f"   ✅ Total: {total_drivers} conducteurs pour {len(partners)} partenaires")
    return total_drivers


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 3 : MAPPING TÉLÉPHONE → URL PROFIL (depuis /fleet-drivers)
# ═════════════════════════════════════════════════════════════════════════════

def scrape_fleet_drivers_mapping(driver) -> dict:
    """Scrape /fleet-drivers pour obtenir le mapping téléphone → view_profile URL."""
    print(f"\n{'='*60}")
    print("🔗 PHASE 3 : Mapping téléphone → profil (fleet-drivers)")
    print(f"{'='*60}")

    phone_to_profile = {}
    driver.get(DRIVERS_URL)
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
    )
    set_page_size_500(driver)
    time.sleep(3)

    page_num = 1
    while True:
        print(f"   📄 Page {page_num}...", end=" ", flush=True)
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        count = 0

        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6:
                    continue

                telephone = cells[3].text.strip()
                if not telephone:
                    continue

                document_url = "N/A"
                view_profile = "N/A"
                try:
                    link_el = cells[5].find_element(By.TAG_NAME, "a")
                    document_url = link_el.get_attribute("href")
                    if document_url and "/document/" in document_url:
                        view_profile = document_url.replace("/document/", "/view-profile/")
                except:
                    pass

                if view_profile != "N/A":
                    phone_to_profile[telephone] = {
                        "document_url": document_url,
                        "view_profile": view_profile,
                    }
                    count += 1
            except StaleElementReferenceException:
                continue

        print(f"→ {count} mappings")

        if not go_to_next_page(driver):
            print("   🏁 Fin de pagination fleet-drivers")
            break
        page_num += 1

    print(f"   ✅ {len(phone_to_profile)} mappings téléphone → profil")
    return phone_to_profile


def enrich_drivers_with_profile_urls(partners: list, phone_map: dict) -> int:
    """Enrichit les conducteurs sans URL profil en matchant par téléphone."""
    enriched = 0
    for partner in partners:
        for drv in partner.get("drivers", []):
            if drv.get("view_profile", "N/A") != "N/A":
                continue
            phone = drv.get("telephone", "").strip()
            if phone in phone_map:
                drv["document_url"] = phone_map[phone]["document_url"]
                drv["view_profile"] = phone_map[phone]["view_profile"]
                enriched += 1
    print(f"   🔗 {enriched} conducteurs enrichis avec URL profil via téléphone")
    return enriched


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 4 : ENRICHISSEMENT VÉHICULES (via requests)
# ═════════════════════════════════════════════════════════════════════════════

def _find_key(obj, key):
    """Recherche récursive d'une clé dans un dict/list."""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in [None, "", "null"]:
            return obj[key]
        for v in obj.values():
            res = _find_key(v, key)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = _find_key(item, key)
            if res is not None:
                return res
    return None


def _fetch_vehicle_info(session, view_profile_url: str) -> dict:
    """Requête HTTP pour récupérer les infos véhicule depuis la page profil."""
    vehicle = {"type": "N/A", "marque": "N/A", "modele": "N/A", "matricule": "N/A"}
    try:
        resp = session.get(view_profile_url, timeout=15)
        if resp.status_code != 200:
            return vehicle
        if "/login" in resp.url:
            return vehicle

        soup = BeautifulSoup(resp.text, "html.parser")
        app_div = soup.find("div", id="app")
        if not app_div or not app_div.get("data-page"):
            return vehicle

        data_page = json.loads(app_div["data-page"])

        v_type      = _find_key(data_page, "vehicle_type_name")
        v_marque    = _find_key(data_page, "car_make_name")
        v_modele    = _find_key(data_page, "car_model_name")
        v_matricule = _find_key(data_page, "car_number")

        if v_type:      vehicle["type"]      = str(v_type).upper()
        if v_marque:    vehicle["marque"]    = str(v_marque).upper()
        if v_modele:    vehicle["modele"]    = str(v_modele).upper()
        if v_matricule: vehicle["matricule"] = str(v_matricule).upper()

    except Exception:
        pass

    return vehicle


def enrich_vehicles(partners: list, selenium_cookies: list):
    """Phase 4 : récupère les infos véhicule via HTTP requests (multi-thread)."""
    print(f"\n{'='*60}")
    print("🚗 PHASE 4 : Enrichissement véhicules")
    print(f"{'='*60}")

    # Construire la session requests avec les cookies Selenium
    session = http_requests.Session()
    for cookie in selenium_cookies:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"))

    # Collecter tous les drivers à enrichir
    to_enrich = []
    for partner in partners:
        for drv in partner.get("drivers", []):
            url = drv.get("view_profile", "N/A")
            if url != "N/A" and drv["vehicle"]["matricule"] == "N/A":
                to_enrich.append(drv)

    if not to_enrich:
        print("   ℹ️ Aucun conducteur à enrichir (pas de lien profil ou déjà enrichis)")
        return

    print(f"   🔄 {len(to_enrich)} conducteurs à enrichir...")
    enriched = 0
    errors = 0

    # Multi-thread pour la rapidité
    def _enrich_one(drv):
        return drv, _fetch_vehicle_info(session, drv["view_profile"])

    with ThreadPoolExecutor(max_workers=VEHICLE_CONCURRENCY) as executor:
        futures = {executor.submit(_enrich_one, drv): drv for drv in to_enrich}
        done = 0

        for future in as_completed(futures):
            done += 1
            try:
                drv, vehicle = future.result()
                drv["vehicle"] = vehicle
                if vehicle["matricule"] != "N/A":
                    enriched += 1
                else:
                    errors += 1
            except Exception:
                errors += 1

            if done % 50 == 0 or done == len(to_enrich):
                print(f"      ⏳ {done}/{len(to_enrich)} traités ({enriched} OK, {errors} sans véhicule)")

    print(f"   ✅ {enriched}/{len(to_enrich)} véhicules récupérés ({errors} sans données)")


# ═════════════════════════════════════════════════════════════════════════════
#  EXPORT
# ═════════════════════════════════════════════════════════════════════════════

def _save_progress(partners, filename):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(partners, f, ensure_ascii=False, indent=2)


def export_final(partners: list, duration_seconds: float):
    """Sauvegarde le résultat final."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # JSON principal
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(partners, f, ensure_ascii=False, indent=2)
    print(f"   ✅ JSON: {OUTPUT_FILE}")

    # Statistiques
    total_drivers = sum(len(p.get("drivers", [])) for p in partners)
    total_vehicles = sum(
        1 for p in partners for d in p.get("drivers", [])
        if d.get("vehicle", {}).get("matricule", "N/A") != "N/A"
    )

    # Rapport
    report = {
        "timestamp": datetime.now().isoformat(),
        "duration_seconds": duration_seconds,
        "total_partners": len(partners),
        "total_drivers": total_drivers,
        "total_vehicles_enriched": total_vehicles,
        "partners_summary": [
            {
                "nom": p["nom"],
                "drivers_count": len(p.get("drivers", [])),
                "vehicles_count": sum(
                    1 for d in p.get("drivers", [])
                    if d.get("vehicle", {}).get("matricule", "N/A") != "N/A"
                ),
            }
            for p in partners
        ],
    }
    report_path = OUTPUT_DIR / "step1_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"   📄 Rapport: {report_path}")

    return report


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Step 1 — Scraping complet partenaires + conducteurs + véhicules")
    parser.add_argument("--email",    default=os.getenv("UPJUNOO_EMAIL"),    help="Email admin")
    parser.add_argument("--password", default=os.getenv("UPJUNOO_PASSWORD"), help="Mot de passe")
    parser.add_argument("--skip-vehicles", action="store_true",
                        help="Ne pas enrichir les véhicules (plus rapide)")
    args = parser.parse_args()

    if not args.email or not args.password:
        print("❌ UPJUNOO_EMAIL et UPJUNOO_PASSWORD requis")
        sys.exit(1)

    start_time = time.time()
    driver = setup_driver_headless()

    try:
        # ── Auth ──
        if not auto_login(driver, args.email, args.password):
            sys.exit(1)

        # ── Phase 1 : Partenaires ──
        partners = scrape_partners_list(driver)
        if not partners:
            print("❌ Aucun partenaire trouvé.")
            sys.exit(1)

        # ── Phase 2 : Conducteurs par partenaire ──
        total_drivers = scrape_all_partner_drivers(driver, partners)
        _save_progress(partners, "step1_progress.json")

        # ── Phase 3 : Mapping téléphone → URL profil ──
        phone_map = scrape_fleet_drivers_mapping(driver)
        enrich_drivers_with_profile_urls(partners, phone_map)
        _save_progress(partners, "step1_progress.json")

        # ── Phase 4 : Véhicules ──
        if not args.skip_vehicles:
            selenium_cookies = driver.get_cookies()
            enrich_vehicles(partners, selenium_cookies)
        else:
            print("\n⏩ Enrichissement véhicules ignoré (--skip-vehicles)")

        # ── Export final ──
        duration = time.time() - start_time
        report = export_final(partners, duration)

        print(f"\n{'='*60}")
        print(f"🎉 STEP 1 TERMINÉ en {duration/60:.1f} min")
        print(f"   🏢 {report['total_partners']} partenaires")
        print(f"   👥 {report['total_drivers']} conducteurs")
        print(f"   🚗 {report['total_vehicles_enriched']} véhicules enrichis")
        print(f"{'='*60}")

    except Exception as e:
        print(f"\n❌ Erreur critique: {traceback.format_exc()}")
        # Sauver ce qu'on a
        _save_progress(partners if 'partners' in dir() else [], "step1_crash_save.json")
        sys.exit(1)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
