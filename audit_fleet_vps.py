"""
Audit & Mise à jour Flotte — VPS (headless, full auto)
========================================================
Usage:
  python3 audit_fleet_vps.py                     → Tous les partenaires
  python3 audit_fleet_vps.py --only Partenaire-24
  python3 audit_fleet_vps.py --start Partenaire-50
  python3 audit_fleet_vps.py --dry-run           → Simulation sans modification
  python3 audit_fleet_vps.py --report-only       → Juste le rapport, pas de modif

Prérequis : output/step1_partners_complete.json (lancé via step1_scrape_all_vps.py)

Pour chaque partenaire :
  1. Login owner → /manage-fleet → scrape plaques existantes (ACTUELLES)
  2. Compare avec les véhicules attendus (depuis step1)
  3. Manquants → AJOUTE via /manage-fleet/create
  4. En trop  → SIGNALE dans le rapport (+ suppression si implémentée)
  5. Rapport JSON progressif
"""

import argparse
import json
import os
import re
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from webdriver_manager.chrome import ChromeDriverManager

# ─── Configuration ────────────────────────────────────────────────────────────
BASE_URL           = "https://upjunoo-server-new.junooapps.com"
OWNER_LOGIN_URL    = f"{BASE_URL}/login/owner-login"
MANAGE_FLEET_URL   = f"{BASE_URL}/manage-fleet"
CREATE_FLEET_URL   = f"{BASE_URL}/manage-fleet/create"
OUTPUT_DIR         = Path(__file__).parent / "output"
REFERENCE_FILE     = OUTPUT_DIR / "step1_partners_complete.json"
REPORTS_DIR        = OUTPUT_DIR / "reports"
UNIVERSAL_PASSWORD = "123456789@"

PARTNER_NAME_RE = re.compile(r'^\s*partenaires?[-_]?\s*(\d+)\s*$', re.I)

TYPE_UUID_MAP = {
    "CONFORT":          "0d1802c4-3d32-4a96-b3ca-73e650802c62",
    "Camionnette":      "15f90aaa-aa92-40ed-b34e-ce7e51541b7e",
    "MOTO":             "35a673c3-aafe-48b4-8ae8-205e238b043b",
    "Taxi France":      "4644788a-1065-4eb9-bbf6-01a6e394aeed",
    "ECO":              "58eb223b-5ac7-4ed5-9a12-87d24f901dda",
    "moto livraison":   "5f4ef87b-1be7-468d-8140-7379fefbaedf",
    "Camion 14T":       "64d5d311-1f7c-42eb-b0ad-510d9af8cd54",
    "PREMIUM":          "91ccc713-b07f-4971-b5c4-1d1c755c9d3a",
    "Camion":           "95ad84fc-df36-48f7-8c69-e8bb51ad5f8d",
    "CONFORT+":         "990a6e02-ac3d-4354-bccc-eedafb77de71",
    "CARGO":            "c9a337de-fc81-4626-a5f9-2ac7ac1b5e03",
    "CONFORT Lyon":     "dce302c1-c109-4023-9d9d-17b9da8c424c",
    "Semi-remorque":    "e17983aa-af38-4ffc-b88c-37adc3f77dcd",
}


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def derive_owner_email(partner_name: str):
    m = PARTNER_NAME_RE.match(partner_name)
    if not m:
        return None
    # Normaliser : "Partenaire-24" → "partenaire24@upjunoo.com"
    prefix = partner_name.replace("-", "").replace("_", "").replace(" ", "").lower()
    return prefix + "@upjunoo.com"


def normalize_plate(plate: str) -> str:
    """Normalise une plaque pour comparaison (majuscule, sans tirets/espaces)."""
    return plate.strip().upper().replace("-", "").replace(" ", "").replace(".", "")


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
            break

    chromedriver_path = None
    for cd in ["chromedriver", "/usr/bin/chromedriver",
               "/usr/lib/chromium-browser/chromedriver", "/usr/lib/chromium/chromedriver"]:
        if os.path.isfile(cd) or shutil.which(cd):
            chromedriver_path = cd if os.path.isfile(cd) else shutil.which(cd)
            break

    service = Service(chromedriver_path) if chromedriver_path else Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


# ═════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═════════════════════════════════════════════════════════════════════════════

def owner_login(driver, email: str, password: str) -> bool:
    print(f"      🔐 Login: {email}")
    driver.get(OWNER_LOGIN_URL)
    wait = WebDriverWait(driver, 30)
    try:
        email_input = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
        )))
        pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password']")
        email_input.clear(); email_input.send_keys(email)
        pwd_input.clear();   pwd_input.send_keys(password)
        try:
            driver.find_element(By.XPATH, "//button[@type='submit']").click()
        except NoSuchElementException:
            pwd_input.submit()
        wait.until(lambda d: "/login" not in d.current_url)
        print(f"         ✅ Connecté")
        return True
    except Exception as e:
        print(f"         ❌ Login échoué: {e}")
        return False


def owner_logout(driver):
    try:
        driver.delete_all_cookies()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  FLEET SCRAPING (plaques actuelles sur la plateforme)
# ═════════════════════════════════════════════════════════════════════════════

def _count_fleet_rows(driver) -> int:
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        return sum(1 for r in rows if r.text.strip()
                   and "no data" not in r.text.lower()
                   and "aucun" not in r.text.lower())
    except:
        return 0


def _wait_table_stable(driver, timeout=30) -> int:
    deadline = time.time() + timeout
    last, stable_since = -1, None
    while time.time() < deadline:
        current = _count_fleet_rows(driver)
        if current == last and current > 0:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= 2.0:
                return current
        else:
            stable_since = None
            last = current
        time.sleep(0.5)
    return last if last > 0 else 0


def _set_pagination_500(driver) -> bool:
    selector = "select.form-select.form-select-sm.w-auto"
    try:
        def _populated(d):
            try:
                el = d.find_element(By.CSS_SELECTOR, selector)
                return el if len(el.find_elements(By.TAG_NAME, "option")) >= 2 else False
            except:
                return False
        sel_el = WebDriverWait(driver, 15).until(_populated)
    except TimeoutException:
        return False

    rows_before = _count_fleet_rows(driver)
    driver.execute_script("""
        var select = arguments[0];
        var found = false;
        for (var i = 0; i < select.options.length; i++) {
            if (select.options[i].value === '500' || select.options[i].text.trim() === '500') {
                select.selectedIndex = i; found = true; break;
            }
        }
        if (!found) { select.selectedIndex = select.options.length - 1; }
        var nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLSelectElement.prototype, 'value').set;
        nativeSetter.call(select, select.options[select.selectedIndex].value);
        select.dispatchEvent(new Event('input', { bubbles: true }));
        select.dispatchEvent(new Event('change', { bubbles: true }));
    """, sel_el)
    time.sleep(1)
    _wait_table_stable(driver, timeout=20)
    return True


def _is_next_disabled(driver):
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item.disabled a.page-link[aria-label='Next']"
        )
        return True
    except NoSuchElementException:
        return False


def _go_next_page(driver) -> bool:
    if _is_next_disabled(driver):
        return False
    try:
        btn = driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item:not(.disabled) a.page-link[aria-label='Next']"
        )
        if not btn.is_displayed():
            return False
        prev_text = ""
        try:
            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            prev_text = rows[0].text[:100] if rows else ""
        except:
            pass
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            btn.click()
        except:
            driver.execute_script("arguments[0].click();", btn)
        start = time.time()
        while time.time() - start < 30:
            time.sleep(0.5)
            try:
                rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                new_text = rows[0].text[:100] if rows else ""
                if new_text and new_text != prev_text:
                    time.sleep(1)
                    return True
            except:
                pass
            if _is_next_disabled(driver):
                return False
        return False
    except NoSuchElementException:
        return False


def scrape_fleet_plates(driver) -> set:
    """Login déjà fait — scrape toutes les plaques de /manage-fleet."""
    all_plates = set()

    driver.get(MANAGE_FLEET_URL)
    try:
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table")))
    except TimeoutException:
        return all_plates

    _set_pagination_500(driver)
    time.sleep(2)

    page_num = 1
    while True:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                for cell in cells:
                    text = cell.text.strip().upper()
                    if text and len(text) >= 4 and text not in ["N/A", "", "-"]:
                        if any(c.isdigit() for c in text) and any(c.isalpha() for c in text):
                            all_plates.add(text)
                            break
            except:
                continue

        if not _go_next_page(driver):
            break
        page_num += 1
        if page_num > 50:
            break

    return all_plates


# ═════════════════════════════════════════════════════════════════════════════
#  CRÉATION VÉHICULE
# ═════════════════════════════════════════════════════════════════════════════

def create_vehicle(driver, vehicle_data: dict) -> bool:
    """Crée un véhicule via /manage-fleet/create. Retourne True si succès."""
    vehicle = vehicle_data.get("vehicle", {})
    vehicle_type = vehicle.get("type", "CONFORT")
    wait = WebDriverWait(driver, 10)

    driver.get(CREATE_FLEET_URL)
    time.sleep(2)

    try:
        # Type
        try:
            select_el = wait.until(EC.presence_of_element_located((By.ID, "select_type")))
            select_obj = Select(select_el)
            target_type = str(vehicle_type).strip().upper()
            matched = None
            for opt in select_obj.options:
                if opt.text.strip().upper() == target_type:
                    matched = opt.text; break
            if matched:
                select_obj.select_by_visible_text(matched)
            else:
                normalized_map = {k.upper(): v for k, v in TYPE_UUID_MAP.items()}
                uuid = normalized_map.get(target_type)
                if uuid:
                    driver.execute_script("""
                        var s=arguments[0]; var opt=document.createElement('option');
                        opt.value=arguments[1]; opt.text=arguments[2];
                        s.appendChild(opt); s.value=arguments[1];
                        s.dispatchEvent(new Event('change',{bubbles:true}));
                    """, select_el, uuid, vehicle_type)
        except Exception:
            pass

        # Marque
        brand_input = wait.until(EC.presence_of_element_located((By.ID, "car_brand")))
        brand_input.clear(); brand_input.send_keys(vehicle.get("marque", "N/A"))

        # Modèle
        model_input = wait.until(EC.presence_of_element_located((By.ID, "car_model")))
        model_input.clear(); model_input.send_keys(vehicle.get("modele", "N/A"))

        # Plaque
        plate_input = wait.until(EC.presence_of_element_located((By.ID, "license_plate_number")))
        plate_input.clear(); plate_input.send_keys(vehicle.get("matricule", "N/A"))

        # Couleur
        color_input = wait.until(EC.presence_of_element_located((By.ID, "car_color")))
        color_input.clear(); color_input.send_keys("Noir")

        time.sleep(0.5)

        # Submit
        save_btn = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.btn.btn-primary[type='submit']")
        ))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", save_btn)
        time.sleep(0.3)
        save_btn.click()
        time.sleep(3)

        return "create" not in driver.current_url

    except Exception as e:
        print(f"            ❌ Erreur formulaire: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  AUDIT D'UN PARTENAIRE
# ═════════════════════════════════════════════════════════════════════════════

def audit_partner(driver, partner_data: dict, dry_run: bool = False) -> dict:
    """
    Audit complet d'un partenaire :
    - Compare fleet actuelle vs véhicules attendus
    - Ajoute les manquants (si pas dry_run)
    - Signale les extras
    """
    name = partner_data.get("nom", "?")
    stats = {
        "name": name,
        "login_ok": False,
        "error": None,
        "drivers_count": 0,
        "expected_vehicles": 0,
        "fleet_current": 0,
        "ok": 0,
        "missing_added": 0,
        "missing_failed": 0,
        "extras": 0,
        "invalid_data": 0,
        "extra_plates": [],
        "missing_plates": [],
    }

    email = derive_owner_email(name)
    if not email:
        stats["error"] = "nom hors filtre"
        return stats

    drivers = partner_data.get("drivers", [])
    stats["drivers_count"] = len(drivers)

    # Construire la liste des plaques ATTENDUES (depuis step1)
    expected_plates = {}  # normalized_plate → vehicle_data
    for drv in drivers:
        vehicle = drv.get("vehicle", {})
        matricule = vehicle.get("matricule", "N/A")
        if (not matricule or matricule == "N/A"
                or vehicle.get("marque", "N/A") == "N/A"
                or vehicle.get("modele", "N/A") == "N/A"):
            stats["invalid_data"] += 1
            continue
        norm = normalize_plate(matricule)
        if norm:
            expected_plates[norm] = drv

    stats["expected_vehicles"] = len(expected_plates)

    if not expected_plates and not drivers:
        stats["error"] = "aucun conducteur"
        return stats

    # Login
    if not owner_login(driver, email, UNIVERSAL_PASSWORD):
        stats["error"] = "login échoué"
        return stats
    stats["login_ok"] = True

    # Scrape fleet actuelle
    fleet_plates_raw = scrape_fleet_plates(driver)
    fleet_plates_normalized = {normalize_plate(p): p for p in fleet_plates_raw}
    stats["fleet_current"] = len(fleet_plates_raw)

    print(f"         📊 Attendu: {len(expected_plates)} | Actuel: {len(fleet_plates_raw)}")

    # ── COMPARAISON ──
    # 1. Plaques OK (dans les deux)
    # 2. Manquantes (attendues mais pas dans fleet)
    # 3. En trop (dans fleet mais pas attendues)

    expected_norms = set(expected_plates.keys())
    fleet_norms = set(fleet_plates_normalized.keys())

    ok_plates = expected_norms & fleet_norms
    missing_norms = expected_norms - fleet_norms
    extra_norms = fleet_norms - expected_norms

    stats["ok"] = len(ok_plates)
    stats["extras"] = len(extra_norms)
    stats["extra_plates"] = [fleet_plates_normalized[n] for n in extra_norms]
    stats["missing_plates"] = [expected_plates[n]["vehicle"]["matricule"] for n in missing_norms]

    # Affichage
    if missing_norms:
        print(f"         ➕ Manquants: {len(missing_norms)}")
    if extra_norms:
        print(f"         🔴 En trop: {len(extra_norms)}")
        for n in list(extra_norms)[:5]:
            print(f"            • {fleet_plates_normalized[n]}")
        if len(extra_norms) > 5:
            print(f"            ... +{len(extra_norms) - 5} autres")
    if not missing_norms and not extra_norms:
        print(f"         ✅ Flotte OK ({len(ok_plates)} véhicules)")

    # ── AJOUT DES MANQUANTS ──
    if missing_norms and not dry_run:
        print(f"         🔄 Ajout des {len(missing_norms)} véhicules manquants...")
        for norm in missing_norms:
            drv_data = expected_plates[norm]
            matricule = drv_data["vehicle"]["matricule"]
            print(f"            [{stats['missing_added']+stats['missing_failed']+1}/{len(missing_norms)}] "
                  f"{drv_data.get('nom', '?')} → {matricule}")
            success = create_vehicle(driver, drv_data)
            if success:
                stats["missing_added"] += 1
            else:
                stats["missing_failed"] += 1
    elif missing_norms and dry_run:
        print(f"         🧪 [DRY-RUN] {len(missing_norms)} véhicules seraient ajoutés")
        stats["missing_added"] = 0

    # ── SUPPRESSION DES EXTRAS ──
    # TODO: implémenter la suppression quand la manipulation sera décrite
    # Pour l'instant on signale seulement dans le rapport
    if extra_norms:
        print(f"         ⚠️  {len(extra_norms)} véhicules en trop signalés dans le rapport")

    owner_logout(driver)
    return stats


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Audit & mise à jour flotte (VPS headless)")
    parser.add_argument("--start", help="Reprendre à partir de ce partenaire")
    parser.add_argument("--only",  help="Traiter uniquement ce partenaire")
    parser.add_argument("--dry-run", action="store_true", help="Simulation sans modification")
    parser.add_argument("--report-only", action="store_true", help="Juste le rapport (pas d'ajout)")
    parser.add_argument("--input", default=str(REFERENCE_FILE), help="JSON de référence (step1)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print("🔍 AUDIT FLOTTE — VPS")
    print(f"{'='*60}")

    # Charger données de référence
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"❌ Fichier de référence introuvable: {input_path}")
        print("   → Lance d'abord: python3 step1_scrape_all_vps.py")
        return
    
    with open(input_path, "r", encoding="utf-8") as f:
        partners = json.load(f)

    print(f"   📂 {len(partners)} partenaires chargés depuis {input_path.name}")

    # Filtrer les partenaires valides
    valid_partners = [p for p in partners if derive_owner_email(p.get("nom", ""))]
    skipped = [p["nom"] for p in partners if not derive_owner_email(p.get("nom", ""))]

    if args.only:
        valid_partners = [p for p in valid_partners if p["nom"].lower() == args.only.lower()]
        if not valid_partners:
            print(f"❌ '{args.only}' introuvable"); return
        print(f"   🎯 Mode --only: {valid_partners[0]['nom']}")
    elif args.start:
        names = [p["nom"].lower() for p in valid_partners]
        if args.start.lower() not in names:
            print(f"❌ '{args.start}' introuvable"); return
        idx = names.index(args.start.lower())
        valid_partners = valid_partners[idx:]
        print(f"   ▶️ Reprise depuis {valid_partners[0]['nom']}")

    print(f"   📋 {len(valid_partners)} partenaires à auditer")
    if skipped:
        print(f"   ⏩ Ignorés: {len(skipped)}")

    dry_run = args.dry_run or args.report_only
    if dry_run:
        print(f"   🧪 MODE {'DRY-RUN' if args.dry_run else 'REPORT-ONLY'}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Lancer Chrome
    browser = setup_driver_headless()
    all_stats = []

    def _session_alive(d):
        try:
            _ = d.current_url
            return True
        except:
            return False

    start_time = time.time()

    try:
        for idx, partner in enumerate(valid_partners, 1):
            print(f"\n   ▶️ [{idx}/{len(valid_partners)}] {partner['nom']}")

            if not _session_alive(browser):
                print("      ♻️ Session Chrome perdue, re-création...")
                try: browser.quit()
                except: pass
                browser = setup_driver_headless()

            try:
                stats = audit_partner(browser, partner, dry_run=dry_run)
            except (InvalidSessionIdException, WebDriverException) as e:
                print(f"      💥 Session morte: {e}")
                try: browser.quit()
                except: pass
                browser = setup_driver_headless()
                try:
                    stats = audit_partner(browser, partner, dry_run=dry_run)
                except Exception as e2:
                    stats = {"name": partner["nom"], "login_ok": False, "error": str(e2),
                             "drivers_count": 0, "expected_vehicles": 0, "fleet_current": 0,
                             "ok": 0, "missing_added": 0, "missing_failed": 0,
                             "extras": 0, "invalid_data": 0, "extra_plates": [], "missing_plates": []}
            except Exception as e:
                print(f"      💥 Erreur: {e}")
                traceback.print_exc()
                stats = {"name": partner["nom"], "login_ok": False, "error": str(e),
                         "drivers_count": 0, "expected_vehicles": 0, "fleet_current": 0,
                         "ok": 0, "missing_added": 0, "missing_failed": 0,
                         "extras": 0, "invalid_data": 0, "extra_plates": [], "missing_plates": []}

            all_stats.append(stats)

            # Sauvegarde progressive
            report_path = REPORTS_DIR / "audit_fleet_report.json"
            report_path.write_text(
                json.dumps(all_stats, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # ══════════════════════════════════════════════════════════
        # RÉSUMÉ FINAL
        # ══════════════════════════════════════════════════════════
        duration = time.time() - start_time

        print(f"\n{'='*60}")
        print("✨ AUDIT TERMINÉ — RÉSUMÉ")
        print(f"{'='*60}")
        
        total_ok      = sum(s["ok"] for s in all_stats)
        total_added   = sum(s["missing_added"] for s in all_stats)
        total_failed  = sum(s["missing_failed"] for s in all_stats)
        total_extras  = sum(s["extras"] for s in all_stats)
        total_invalid = sum(s["invalid_data"] for s in all_stats)
        failed_login  = [s for s in all_stats if not s.get("login_ok")]
        partners_with_extras = [s for s in all_stats if s["extras"] > 0]

        print(f"   ⏱️  Durée: {duration/60:.1f} min")
        print(f"   🏢 Partenaires traités: {len(all_stats)}")
        print(f"   ✅ Véhicules OK: {total_ok}")
        print(f"   ➕ Ajoutés: {total_added}")
        if total_failed:
            print(f"   ❌ Ajouts échoués: {total_failed}")
        print(f"   🔴 EN TROP (à supprimer): {total_extras}")
        print(f"   ⚠️  Données invalides: {total_invalid}")

        if partners_with_extras:
            print(f"\n   🔴 PARTENAIRES AVEC VÉHICULES EN TROP ({len(partners_with_extras)}) :")
            for s in partners_with_extras:
                print(f"      • {s['name']}: {s['extras']} en trop "
                      f"(attendu {s['expected_vehicles']}, actuel {s['fleet_current']})")
                for plate in s["extra_plates"][:3]:
                    print(f"        - {plate}")
                if len(s["extra_plates"]) > 3:
                    print(f"        ... +{len(s['extra_plates'])-3}")

        if failed_login:
            print(f"\n   ⚠️ Login échoué ({len(failed_login)}) :")
            for s in failed_login:
                print(f"      • {s['name']} → {s.get('error')}")

        # Rapport final enrichi
        final_report = {
            "timestamp": datetime.now().isoformat(),
            "duration_seconds": duration,
            "dry_run": dry_run,
            "summary": {
                "partners_total": len(all_stats),
                "vehicles_ok": total_ok,
                "vehicles_added": total_added,
                "vehicles_add_failed": total_failed,
                "vehicles_extras": total_extras,
                "invalid_data": total_invalid,
                "login_failed": len(failed_login),
            },
            "partners_with_extras": [
                {"name": s["name"], "extras": s["extras"], "extra_plates": s["extra_plates"]}
                for s in partners_with_extras
            ],
            "details": all_stats,
        }
        report_path = REPORTS_DIR / "audit_fleet_report.json"
        report_path.write_text(
            json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n   📄 Rapport: {report_path}")

    except KeyboardInterrupt:
        print("\n🛑 Interrompu.")
    finally:
        time.sleep(2)
        browser.quit()


if __name__ == "__main__":
    main()
