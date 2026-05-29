"""
Step 3 — Mise à jour flotte VPS (headless, full auto)
======================================================
Usage:  python3 step3_update_fleet_vps.py [--start partenaire24] [--only partenaire24]

Lit output/organized_by_partner/ et pour chaque partenaire :
  1. Login owner (partenaire<N>@upjunoo.com / 123456789@)
  2. /manage-fleet → pagination 500 → liste plaques existantes
  3. Crée les véhicules manquants via /manage-fleet/create
  4. Rapport JSON dans output/reports/update_fleet_report.json

Variables d'environnement : aucune (credentials déduits du nom du partenaire)
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
PARTNERS_BASE_DIR  = Path(__file__).parent / "output" / "organized_by_partner"
REPORTS_DIR        = Path(__file__).parent / "output" / "reports"
UNIVERSAL_PASSWORD = "123456789@"

PARTNER_NAME_RE = re.compile(r'^\s*partenaires?-?\s*(\d+)\s*$', re.I)

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


def derive_owner_email(folder_name: str):
    if not PARTNER_NAME_RE.match(folder_name):
        return None
    prefix = folder_name.replace("-", "").replace("_", "").lower()
    return prefix + "@upjunoo.com"


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
    print(f"   🔐 Login: {email}")
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
        print(f"      ✅ Connecté: {driver.current_url}")
        return True
    except Exception as e:
        print(f"      ❌ Login échoué: {e}")
        return False


def owner_logout(driver):
    try:
        driver.delete_all_cookies()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  PAGINATION & TABLE
# ═════════════════════════════════════════════════════════════════════════════

def _count_fleet_rows(driver) -> int:
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        real = 0
        for r in rows:
            txt = r.text.strip()
            if txt and "no data" not in txt.lower() and "aucun" not in txt.lower():
                real += 1
        return real
    except Exception:
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

    def _select_populated(d):
        try:
            el = d.find_element(By.CSS_SELECTOR, selector)
            opts = el.find_elements(By.TAG_NAME, "option")
            return el if len(opts) >= 2 else False
        except:
            return False

    try:
        sel_el = WebDriverWait(driver, 20).until(_select_populated)
    except TimeoutException:
        return False

    rows_before = _count_fleet_rows(driver)

    driver.execute_script("""
        var select = arguments[0];
        var target = arguments[1];
        var found = false;
        for (var i = 0; i < select.options.length; i++) {
            if (select.options[i].value === target || select.options[i].text === target) {
                select.selectedIndex = i; found = true; break;
            }
        }
        if (!found) { select.selectedIndex = select.options.length - 1; }
        var nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLSelectElement.prototype, 'value').set;
        nativeSetter.call(select, select.options[select.selectedIndex].value);
        select.dispatchEvent(new Event('input', { bubbles: true }));
        select.dispatchEvent(new Event('change', { bubbles: true }));
        if (select.__vue__) {
            try { select.__vue__.$emit('input', select.value); } catch(e) {}
            try { select.__vue__.$emit('change', select.value); } catch(e) {}
        }
    """, sel_el, "500")

    time.sleep(1)
    rows_after = _wait_table_stable(driver, timeout=20)

    if rows_after > rows_before:
        return True
    if rows_after == rows_before and rows_before <= 500:
        return True
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


def _go_next_page(driver, timeout=30) -> bool:
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
        while time.time() - start < timeout:
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
#  EXTRACTION PLAQUES EXISTANTES
# ═════════════════════════════════════════════════════════════════════════════

def extract_existing_plates(driver) -> set:
    """Parcourt toutes les pages de /manage-fleet pour lister les plaques."""
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
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
            )
        except:
            break

        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                for cell in cells:
                    text = cell.text.strip().upper()
                    if text and len(text) >= 5 and text not in ["N/A", "", "-"]:
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

def fill_fleet_form(driver, vehicle_data: dict):
    """Remplit et soumet le formulaire /manage-fleet/create."""
    wait = WebDriverWait(driver, 10)
    vehicle = vehicle_data.get("vehicle", {})
    vehicle_type = vehicle.get("type", "CONFORT")
    nom = vehicle_data.get("nom", "Inconnu")

    try:
        # 1. Type
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
        except Exception as e:
            print(f"         ⚠️ Type: {e}")

        # 2. Marque
        brand_input = wait.until(EC.presence_of_element_located((By.ID, "car_brand")))
        brand_input.clear(); brand_input.send_keys(vehicle.get("marque", "N/A"))

        # 3. Modèle
        model_input = wait.until(EC.presence_of_element_located((By.ID, "car_model")))
        model_input.clear(); model_input.send_keys(vehicle.get("modele", "N/A"))

        # 4. Plaque
        plate_input = wait.until(EC.presence_of_element_located((By.ID, "license_plate_number")))
        plate_input.clear(); plate_input.send_keys(vehicle.get("matricule", "N/A"))

        # 5. Couleur
        color_input = wait.until(EC.presence_of_element_located((By.ID, "car_color")))
        color_input.clear(); color_input.send_keys("Noir")

        time.sleep(0.5)

        # 6. Submit
        save_btn = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.btn.btn-primary[type='submit']")
        ))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", save_btn)
        time.sleep(0.3)
        save_btn.click()
        time.sleep(3)

        if "create" in driver.current_url:
            print(f"         ⚠️ Formulaire non redirigé pour {nom}")
        else:
            print(f"         ✅ Véhicule ajouté: {nom} → {vehicle.get('matricule')}")

    except Exception as e:
        print(f"         ❌ Erreur formulaire: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  TRAITEMENT D'UN PARTENAIRE
# ═════════════════════════════════════════════════════════════════════════════

def process_partner(driver, folder: Path) -> dict:
    name = folder.name
    stats = {"name": name, "added": 0, "skipped": 0, "invalid": 0,
             "login_ok": False, "error": None}

    email = derive_owner_email(name)
    if not email:
        stats["error"] = "nom hors filtre"
        return stats

    data_path = folder / "data.json"
    if not data_path.exists():
        stats["error"] = "data.json introuvable"
        return stats

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    drivers_list = data.get("drivers", []) if isinstance(data, dict) else data
    if not drivers_list:
        stats["error"] = "aucun driver"
        return stats

    print(f"\n   {'─'*50}")
    print(f"   🏢 {name} ({len(drivers_list)} conducteurs)")

    if not owner_login(driver, email, UNIVERSAL_PASSWORD):
        stats["error"] = "login échoué"
        return stats
    stats["login_ok"] = True

    # Extraire les plaques existantes
    existing_plates = extract_existing_plates(driver)
    print(f"      📊 {len(existing_plates)} véhicules déjà sur la plateforme")

    # Filtrer ce qui est à créer
    to_create = []
    for vd in drivers_list:
        vehicle = vd.get("vehicle")
        if not vehicle or not isinstance(vehicle, dict):
            stats["invalid"] += 1; continue

        matricule = str(vehicle.get("matricule", "")).strip().upper()
        if (not matricule or matricule == "N/A"
                or vehicle.get("modele", "N/A") == "N/A"
                or vehicle.get("marque", "N/A") == "N/A"):
            stats["invalid"] += 1; continue

        # Comparaison avec et sans tirets
        matricule_clean = matricule.replace("-", "").replace(" ", "")
        found = False
        for plate in existing_plates:
            plate_clean = plate.replace("-", "").replace(" ", "")
            if matricule == plate or matricule_clean == plate_clean:
                found = True; break

        if found:
            stats["skipped"] += 1
        else:
            to_create.append(vd)

    print(f"      ➕ À créer: {len(to_create)} | ⏩ Déjà présents: {stats['skipped']} | ❌ Invalides: {stats['invalid']}")

    if not to_create:
        print(f"      ✅ Rien à créer")
        owner_logout(driver)
        return stats

    # Création des véhicules
    for i, vd in enumerate(to_create, 1):
        matricule = vd["vehicle"]["matricule"]
        print(f"      [{i}/{len(to_create)}] {vd.get('nom', '?')} → {matricule}")

        driver.get(CREATE_FLEET_URL)
        time.sleep(2)
        fill_fleet_form(driver, vd)
        stats["added"] += 1
        existing_plates.add(matricule)

    owner_logout(driver)
    return stats


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Step 3 — Mise à jour flotte (VPS headless)")
    parser.add_argument("--start", help="Reprendre à partir de ce partenaire (ex: Partenaire-24)")
    parser.add_argument("--only",  help="Traiter uniquement ce partenaire (ex: Partenaire-24)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulation : affiche ce qui serait fait sans rien créer")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print("🔄 STEP 3 : Mise à jour flotte (VPS headless)")
    print(f"{'='*60}")

    if not PARTNERS_BASE_DIR.exists():
        print(f"❌ Dossier introuvable: {PARTNERS_BASE_DIR}")
        print("   → Lance d'abord step1 + step2")
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    all_folders = sorted(
        [p for p in PARTNERS_BASE_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.name.lower(),
    )
    targets = [p for p in all_folders if derive_owner_email(p.name)]
    skipped = [p.name for p in all_folders if not derive_owner_email(p.name)]

    # Filtres CLI
    if args.only:
        targets = [p for p in targets if p.name.lower() == args.only.lower()]
        if not targets:
            print(f"❌ '{args.only}' introuvable"); return
        print(f"   🎯 Mode --only: {targets[0].name}")
    elif args.start:
        names = [p.name.lower() for p in targets]
        if args.start.lower() not in names:
            print(f"❌ '{args.start}' introuvable"); return
        idx = names.index(args.start.lower())
        targets = targets[idx:]
        print(f"   ▶️ Reprise depuis {targets[0].name}")

    print(f"   📂 {len(targets)} partenaires à traiter")
    if skipped:
        print(f"   ⏩ Ignorés: {', '.join(skipped)}")

    if args.dry_run:
        print("\n   🧪 MODE DRY-RUN — aucune modification ne sera faite")
        for folder in targets:
            data_path = folder / "data.json"
            if data_path.exists():
                with open(data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                drivers = data.get("drivers", []) if isinstance(data, dict) else data
                vehicles_ok = sum(
                    1 for d in drivers
                    if d.get("vehicle", {}).get("matricule", "N/A") != "N/A"
                    and d.get("vehicle", {}).get("marque", "N/A") != "N/A"
                )
                print(f"      {folder.name}: {len(drivers)} conducteurs, {vehicles_ok} véhicules valides")
        return

    browser = setup_driver_headless()
    all_stats = []

    def _session_alive(d):
        try:
            _ = d.current_url
            return True
        except:
            return False

    try:
        for idx, folder in enumerate(targets, 1):
            print(f"\n▶️ [{idx}/{len(targets)}] {folder.name}")

            if not _session_alive(browser):
                print("   ♻️ Session Chrome perdue, re-création...")
                try: browser.quit()
                except: pass
                browser = setup_driver_headless()

            try:
                stats = process_partner(browser, folder)
            except (InvalidSessionIdException, WebDriverException) as e:
                print(f"   💥 Session morte: {e}")
                try: browser.quit()
                except: pass
                browser = setup_driver_headless()
                try:
                    stats = process_partner(browser, folder)
                except Exception as e2:
                    stats = {"name": folder.name, "added": 0, "skipped": 0,
                             "invalid": 0, "login_ok": False, "error": str(e2)}
            except Exception as e:
                print(f"   💥 Erreur: {e}")
                traceback.print_exc()
                stats = {"name": folder.name, "added": 0, "skipped": 0,
                         "invalid": 0, "login_ok": False, "error": str(e)}

            all_stats.append(stats)

            # Sauvegarde progressive
            report_path = REPORTS_DIR / "update_fleet_report.json"
            report_path.write_text(
                json.dumps(all_stats, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # Résumé final
        print(f"\n{'='*60}")
        print("✨ STEP 3 TERMINÉ — RÉSUMÉ")
        print(f"{'='*60}")
        total_added   = sum(s["added"]   for s in all_stats)
        total_skipped = sum(s["skipped"] for s in all_stats)
        total_invalid = sum(s["invalid"] for s in all_stats)
        failed = [s for s in all_stats if s.get("error") or not s.get("login_ok")]

        print(f"   Partenaires traités : {len(all_stats)}")
        print(f"   ➕ Véhicules ajoutés : {total_added}")
        print(f"   ⏩ Déjà présents : {total_skipped}")
        print(f"   ❌ Données invalides : {total_invalid}")
        if failed:
            print(f"\n   ⚠️ En erreur ({len(failed)}) :")
            for s in failed:
                print(f"      • {s['name']} → {s.get('error') or 'login KO'}")

    except KeyboardInterrupt:
        print("\n🛑 Interrompu.")
    finally:
        time.sleep(2)
        browser.quit()


if __name__ == "__main__":
    main()
