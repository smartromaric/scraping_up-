"""
Création de Flotte — VPS (headless, full auto)
===============================================
Pour chaque partenaire (Partenaire-X) :
  1. Login owner automatique → derive_owner_email(nom) + UNIVERSAL_PASSWORD
  2. Pour chaque véhicule dans data.json :
     - Skip si type = "N/A" (obligatoire)
     - Créer via /manage-fleet/create (même si marque/modèle/matricule = N/A)
  3. Logout + partenaire suivant
  4. Slack notification finale (vert = succès, rouge = erreur)

Usage:
  python3 create_fleet_vps.py                          # Tous les partenaires
  python3 create_fleet_vps.py --only Partenaire42       # Un seul partenaire
  python3 create_fleet_vps.py --start Partenaire-50     # Reprise depuis
  python3 create_fleet_vps.py --dry-run                 # Simulation (aucune modif)

Background VPS:
  nohup python3 create_fleet_vps.py > create_fleet.log 2>&1 &
"""

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import traceback
import urllib.request
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
CREATE_FLEET_URL   = f"{BASE_URL}/manage-fleet/create"

OUTPUT_DIR         = Path(__file__).parent / "output"
ORGANIZED_DIR      = OUTPUT_DIR / "organized_by_partner"
REPORTS_DIR        = OUTPUT_DIR / "reports"
LOG_FILE           = OUTPUT_DIR / "create_fleet.log"

UNIVERSAL_PASSWORD = "123456789@"
WEBHOOK_URL        = os.getenv("WEBHOOK_URL", "")

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
#  LOG + SLACK
# ═════════════════════════════════════════════════════════════════════════════

def log(message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    print(line, flush=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def send_slack(message: str, color: str = "#36a64f"):
    """Notification Slack (vert = succès, rouge = erreur)."""
    if not WEBHOOK_URL:
        return
    try:
        payload = json.dumps({
            "username": os.getenv("SLACK_BOT_NAME", "UpJunoo Bot"),
            "icon_emoji": os.getenv("SLACK_ICON_EMOJI", ":car:"),
            "attachments": [{"color": color, "text": message}]
        }).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"⚠️ Slack erreur: {e}")


def send_slack_summary(success_partners: list, failed_partners: list, total_created: int, total_skipped_type: int, total_skipped_matricule: int):
    """Envoie un résumé final à Slack."""
    total_partners = len(success_partners) + len(failed_partners)
    
    if failed_partners:
        color = "#ff0000"  # Rouge
        status = "❌ TERMINÉ AVEC ERREURS"
    else:
        color = "#36a64f"  # Vert
        status = "✅ TERMINÉ AVEC SUCCÈS"
    
    message = f"""{status}

📊 **Résumé Création de Flotte**
• Partenaires traités : {total_partners}
• Succès : {len(success_partners)}
• Échecs : {len(failed_partners)}
• Véhicules créés : {total_created}
• Véhicules ignorés (type N/A) : {total_skipped_type}
• Véhicules ignorés (matricule N/A) : {total_skipped_matricule}
"""
    
    if success_partners:
        message += f"\n✅ **Succès ({len(success_partners)})** : " + ", ".join(success_partners[:10])
        if len(success_partners) > 10:
            message += f"... et {len(success_partners) - 10} autres"
    
    if failed_partners:
        message += f"\n\n❌ **Échecs ({len(failed_partners)})** : " + ", ".join(failed_partners)
    
    send_slack(message, color)


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def derive_owner_email(partner_name: str):
    """Partenaire-24 / partenaires-24 / partenaire24 → partenaire24@upjunoo.com"""
    m = PARTNER_NAME_RE.match(partner_name or "")
    if not m:
        return None
    num = m.group(1)
    return f"partenaire{num}@upjunoo.com"


def normalize_partner_name(name: str) -> str:
    """Normalise le nom du partenaire pour comparaison."""
    return re.sub(r'[-_\s]', '', name.lower())


def extract_partner_number(name: str) -> int:
    """Extrait le numéro du partenaire pour le tri."""
    m = PARTNER_NAME_RE.match(name or "")
    if m:
        return int(m.group(1))
    return 0


def find_partner_folders(organized_dir: Path) -> list:
    """Trouve tous les dossiers partenaires dans organized_by_partner."""
    if not organized_dir.exists():
        return []
    
    partners = []
    for item in organized_dir.iterdir():
        if item.is_dir() and PARTNER_NAME_RE.match(item.name):
            partners.append(item)
    
    # Trier par numéro de partenaire
    partners.sort(key=lambda p: extract_partner_number(p.name))
    return partners


def load_partner_data(partner_dir: Path) -> list:
    """Charge les données du partenaire depuis data.json."""
    data_file = partner_dir / "data.json"
    if not data_file.exists():
        log(f"   ⚠️ Fichier {data_file} introuvable")
        return []
    
    try:
        with open(data_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Structure attendue : {"drivers": [...]}
        if isinstance(data, dict) and "drivers" in data:
            return data["drivers"]
        elif isinstance(data, list):
            return data
        else:
            log(f"   ⚠️ Structure JSON inattendue dans {data_file}")
            return []
    except Exception as e:
        log(f"   ❌ Erreur lecture {data_file}: {e}")
        return []


# ═════════════════════════════════════════════════════════════════════════════
#  CHROME HEADLESS
# ═════════════════════════════════════════════════════════════════════════════

def setup_driver_headless(headed: bool = False):
    chrome_options = Options()
    if not headed:
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--disable-background-networking")
    # Options supplémentaires pour stabilité VPS
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-features=VizDisplayCompositor")
    chrome_options.add_argument("--disable-features=IsolateOrigins,site-per-process")
    chrome_options.add_argument("--disable-site-isolation-trials")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    if not headed:
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    # Détection du binaire Chrome/Chromium
    chrome_binary = None
    for binary in [
        "chromium-browser",
        "/snap/bin/chromium",
        "chromium",
        "google-chrome-stable",
        "google-chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/lib/chromium-browser/chromium-browser",
    ]:
        path = binary if os.path.isfile(binary) else shutil.which(binary)
        if path:
            chrome_binary = path
            chrome_options.binary_location = path
            log(f"✅ Chrome trouvé : {path}")
            break
    
    if not chrome_binary:
        raise RuntimeError("Chrome/Chromium non trouvé ! Installe-le avec : sudo apt install chromium-browser")

    # Toujours utiliser ChromeDriverManager pour éviter les incompatibilités
    try:
        log("⏳ Installation ChromeDriver via ChromeDriverManager...")
        chromedriver_path = ChromeDriverManager().install()
        service = Service(chromedriver_path)
        log(f"✅ ChromeDriver installé : {chromedriver_path}")
    except Exception as e:
        log(f"⚠️ ChromeDriverManager a échoué : {e}")
        # Fallback sur chromedriver système
        chromedriver_path = None
        for cd in ["/usr/bin/chromedriver", "chromedriver", "/snap/bin/chromium.chromedriver"]:
            if os.path.isfile(cd) or shutil.which(cd):
                chromedriver_path = cd if os.path.isfile(cd) else shutil.which(cd)
                break
        if chromedriver_path:
            service = Service(chromedriver_path)
        else:
            raise RuntimeError("ChromeDriver non trouvé !")
    
    return webdriver.Chrome(service=service, options=chrome_options)


# ═════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═════════════════════════════════════════════════════════════════════════════

def _save_debug_screenshot(driver, label: str):
    """Sauvegarde un screenshot en cas d'erreur pour debug."""
    try:
        debug_dir = OUTPUT_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = debug_dir / f"{label}_{ts}.png"
        driver.save_screenshot(str(path))
        log(f"      📸 Screenshot: {path}")
    except Exception:
        pass


def owner_login(driver, email: str, password: str, max_retries: int = 3) -> bool:
    """Login owner avec retry automatique."""
    log(f"   🔐 Login: {email}")

    for attempt in range(1, max_retries + 1):
        try:
            try:
                driver.delete_all_cookies()
            except Exception:
                pass

            log(f"      → Tentative {attempt}/{max_retries}")
            try:
                driver.get(OWNER_LOGIN_URL)
            except Exception as e:
                log(f"      ⚠️ goto lent ({e}), on continue")

            wait = WebDriverWait(driver, 90)

            email_input = wait.until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
            )))
            time.sleep(1)

            pwd_input = driver.find_element(
                By.CSS_SELECTOR, "input[type='password'], input[name='password']"
            )
            email_input.clear(); email_input.send_keys(email)
            time.sleep(0.3)
            pwd_input.clear();   pwd_input.send_keys(password)
            time.sleep(0.3)

            try:
                btn = driver.find_element(By.XPATH, "//button[@type='submit']")
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.2)
                try:
                    btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", btn)
            except NoSuchElementException:
                pwd_input.submit()

            WebDriverWait(driver, 90).until(lambda d: "/login" not in d.current_url)
            log(f"      ✅ Connecté → {driver.current_url}")
            return True

        except TimeoutException:
            log(f"      ⚠️ Timeout login (tentative {attempt})")
            if attempt == max_retries:
                _save_debug_screenshot(driver, f"login_fail_{email.split('@')[0]}")
        except Exception as e:
            log(f"      ⚠️ Erreur login tentative {attempt}: {type(e).__name__}")
            if attempt == max_retries:
                _save_debug_screenshot(driver, f"login_fail_{email.split('@')[0]}")

        if attempt < max_retries:
            time.sleep(3 * attempt)

    log(f"      ❌ Login échoué après {max_retries} tentatives")
    return False


def owner_logout(driver):
    try:
        driver.delete_all_cookies()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  CRÉATION VÉHICULE
# ═════════════════════════════════════════════════════════════════════════════

def _select_vehicle_type(driver, select_el, vehicle_type: str) -> bool:
    """Sélection robuste du type de véhicule."""
    target_type = str(vehicle_type).strip().upper()
    normalized_map = {k.upper(): v for k, v in TYPE_UUID_MAP.items()}

    try:
        select_obj = Select(select_el)
        real_options = [o for o in select_obj.options if o.get_attribute("value")]

        # Cas 1 : dropdown a des options → match par texte
        if real_options:
            matched_text = None
            for opt in select_obj.options:
                if opt.text.strip().upper() == target_type:
                    matched_text = opt.text
                    break
            if matched_text:
                select_obj.select_by_visible_text(matched_text)
                return True

        # Cas 2 & 3 : pas trouvé OU dropdown vide → injection JS via UUID
        uuid = normalized_map.get(target_type)
        if not uuid:
            log(f"         ❌ Type '{vehicle_type}' inconnu dans TYPE_UUID_MAP")
            return False

        driver.execute_script(
            """
            var select = arguments[0];
            var opt = document.createElement('option');
            opt.value = arguments[1];
            opt.text  = arguments[2];
            select.appendChild(opt);
            select.value = arguments[1];
            select.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            select_el, uuid, vehicle_type
        )
        return True

    except Exception as e:
        log(f"         ⚠️ Erreur select_type '{vehicle_type}': {e}")
        return False


def create_vehicle(driver, vehicle_data: dict, dry_run: bool = False) -> bool:
    """
    Crée un véhicule via /manage-fleet/create.
    Retourne True si créé avec succès, False sinon.
    """
    vehicle = vehicle_data.get("vehicle", {}) or {}
    vehicle_type = vehicle.get("type", "")
    marque    = vehicle.get("marque", "N/A")
    modele    = vehicle.get("modele", "N/A")
    matricule = vehicle.get("matricule", "N/A")
    nom       = vehicle_data.get("nom", "Conducteur inconnu")
    
    # SKIP si type est N/A, vide ou null
    if not vehicle_type or str(vehicle_type).strip().upper() in ("N/A", "NA", "", "NULL", "NONE"):
        log(f"      ⏩ SKIP (type N/A) : {nom}")
        return None  # None = skipped
    
    # SKIP si matricule est N/A, vide ou null (règle: on crée uniquement si matricule valide)
    if not matricule or str(matricule).strip().upper() in ("N/A", "NA", "", "NULL", "NONE"):
        log(f"      ⏩ SKIP (matricule N/A) : {nom} | Type: {vehicle_type}")
        return None  # None = skipped
    
    # En mode dry-run, on simule uniquement
    if dry_run:
        log(f"      [DRY-RUN] Créerait : {vehicle_type} | {marque} | {modele} | {matricule}")
        return True

    wait = WebDriverWait(driver, 30)

    # Navigation fresh vers la page de création
    driver.get(CREATE_FLEET_URL)
    time.sleep(2)

    try:
        # ── 1. Type (select id="select_type") ──
        select_el = wait.until(EC.presence_of_element_located((By.ID, "select_type")))
        if not _select_vehicle_type(driver, select_el, vehicle_type):
            return False

        # ── 2. Marque ──
        brand_input = wait.until(EC.presence_of_element_located((By.ID, "car_brand")))
        brand_input.clear()
        brand_input.send_keys(marque if marque and marque != "N/A" else "Non spécifié")

        # ── 3. Modèle ──
        model_input = wait.until(EC.presence_of_element_located((By.ID, "car_model")))
        model_input.clear()
        model_input.send_keys(modele if modele and modele != "N/A" else "Non spécifié")

        # ── 4. Plaque ──
        plate_input = wait.until(EC.presence_of_element_located((By.ID, "license_plate_number")))
        plate_input.clear()
        plate_input.send_keys(matricule if matricule and matricule != "N/A" else "Non spécifié")

        # ── 5. Couleur (toujours "Noir") ──
        color_input = wait.until(EC.presence_of_element_located((By.ID, "car_color")))
        color_input.clear()
        color_input.send_keys("Noir")

        time.sleep(0.5)

        # ── 6. Submit ──
        save_btn = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.btn.btn-primary[type='submit']")
        ))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", save_btn)
        time.sleep(0.3)
        save_btn.click()
        time.sleep(3)

        # Succès = on n'est plus sur /create (redirection)
        if "create" in driver.current_url:
            log(f"      ⚠️ Échec création (resté sur /create) : {matricule}")
            return False
        else:
            log(f"      ✅ Créé : {vehicle_type} | {marque} | {matricule}")
            return True

    except Exception as e:
        log(f"      ❌ Erreur création {matricule}: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def save_partner_report(partner_dir: Path, created_list: list, not_created_list: list):
    """
    Sauvegarde les rapports JSON et Excel dans le dossier du partenaire.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Rapport JSON (toujours généré)
    report_json = {
        "generated_at": datetime.now().isoformat(),
        "total_drivers": len(created_list) + len(not_created_list),
        "vehicules_crees": {
            "count": len(created_list),
            "drivers": created_list
        },
        "vehicules_non_crees": {
            "count": len(not_created_list),
            "drivers": not_created_list
        }
    }
    
    json_path = partner_dir / f"fleet_creation_report_{timestamp}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_json, f, ensure_ascii=False, indent=2)
    log(f"   📄 Rapport JSON : {json_path}")
    
    # Rapport Excel avec 2 feuilles
    try:
        import pandas as pd
        
        excel_path = partner_dir / f"fleet_creation_report_{timestamp}.xlsx"
        
        with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
            # Feuille 1 : Véhicules créés
            if created_list:
                df_created = pd.DataFrame(created_list)
                df_created.to_excel(writer, sheet_name='Vehicules_Crees', index=False)
            else:
                pd.DataFrame({"Message": ["Aucun véhicule créé"]}).to_excel(
                    writer, sheet_name='Vehicules_Crees', index=False
                )
            
            # Feuille 2 : Véhicules non créés
            if not_created_list:
                df_not_created = pd.DataFrame(not_created_list)
                df_not_created.to_excel(writer, sheet_name='Vehicules_Non_Crees', index=False)
            else:
                pd.DataFrame({"Message": ["Tous les véhicules ont été créés"]}).to_excel(
                    writer, sheet_name='Vehicules_Non_Crees', index=False
                )
        
        log(f"   📊 Rapport Excel : {excel_path}")
        
    except ImportError:
        log(f"   ⚠️ Excel non généré - pandas/openpyxl non installé")
        log(f"   💡 Pour installer : pip3 install pandas openpyxl --break-system-packages")
        log(f"   💡 Ou avec venv : python3 -m venv .venv && source .venv/bin/activate && pip3 install pandas openpyxl")
        
        # Fallback : créer des CSV temporaires pour ne pas perdre les données
        if created_list:
            csv_path = partner_dir / f"vehicules_crees_{timestamp}.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["nom", "type", "marque", "modele", "matricule", "status"])
                writer.writeheader()
                writer.writerows(created_list)
            log(f"   📝 Fallback CSV créés : {csv_path}")
        
        if not_created_list:
            csv_path = partner_dir / f"vehicules_non_crees_{timestamp}.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["nom", "type", "marque", "modele", "matricule", "status", "raison"])
                writer.writeheader()
                writer.writerows(not_created_list)
            log(f"   📝 Fallback CSV non créés : {csv_path}")
            
    except Exception as e:
        log(f"   ⚠️ Erreur génération Excel : {e}")


def process_partner(driver, partner_dir: Path, dry_run: bool = False) -> dict:
    """
    Traite un partenaire : login + création de tous ses véhicules.
    Retourne un dict avec les stats.
    """
    partner_name = partner_dir.name
    email = derive_owner_email(partner_name)
    
    if not email:
        log(f"❌ Nom partenaire invalide : {partner_name}")
        return {"success": False, "created": 0, "skipped": 0, "error": "Nom invalide"}
    
    log(f"\n{'='*60}")
    log(f"📂 Partenaire : {partner_name}")
    log(f"📧 Email : {email}")
    log(f"{'='*60}")
    
    # Chargement des données
    drivers = load_partner_data(partner_dir)
    if not drivers:
        log(f"   ℹ️ Aucun conducteur à traiter pour {partner_name}")
        return {"success": True, "created": 0, "skipped": 0, "error": None}
    
    log(f"   📊 {len(drivers)} conducteurs trouvés")
    
    # Login
    if not dry_run:
        if not owner_login(driver, email, UNIVERSAL_PASSWORD):
            return {"success": False, "created": 0, "skipped": 0, "error": "Login échoué"}
    
    # Listes pour le rapport
    created_list = []
    not_created_list = []
    
    # Création des véhicules
    created = 0
    skipped_type = 0
    skipped_matricule = 0
    failed = 0
    
    for i, vehicle_data in enumerate(drivers, 1):
        nom = vehicle_data.get('nom', 'Inconnu')
        vehicle = vehicle_data.get("vehicle", {}) or {}
        vehicle_type = vehicle.get("type", "")
        marque = vehicle.get("marque", "N/A")
        modele = vehicle.get("modele", "N/A")
        matricule = vehicle.get("matricule", "N/A")
        
        log(f"   [{i}/{len(drivers)}] {nom}")
        
        result = create_vehicle(driver, vehicle_data, dry_run=dry_run)
        
        if result is True:
            created += 1
            created_list.append({
                "nom": nom,
                "type": vehicle_type,
                "marque": marque,
                "modele": modele,
                "matricule": matricule,
                "status": "CREE"
            })
        elif result is None:
            # Déterminer pourquoi on a skip
            if not vehicle_type or str(vehicle_type).strip().upper() in ("N/A", "NA", "", "NULL", "NONE"):
                skipped_type += 1
                reason = "Type N/A"
            elif not matricule or str(matricule).strip().upper() in ("N/A", "NA", "", "NULL", "NONE"):
                skipped_matricule += 1
                reason = "Matricule N/A"
            else:
                skipped_type += 1
                reason = "Type N/A (fallback)"
            
            not_created_list.append({
                "nom": nom,
                "type": vehicle_type if vehicle_type else "N/A",
                "marque": marque,
                "modele": modele,
                "matricule": matricule if matricule else "N/A",
                "status": "NON_CREE",
                "raison": reason
            })
        else:
            failed += 1
            not_created_list.append({
                "nom": nom,
                "type": vehicle_type if vehicle_type else "N/A",
                "marque": marque,
                "modele": modele,
                "matricule": matricule if matricule else "N/A",
                "status": "NON_CREE",
                "raison": "Erreur lors de la création"
            })
        
        # Petite pause entre véhicules
        if not dry_run:
            time.sleep(1)
    
    # Logout
    if not dry_run:
        owner_logout(driver)
    
    # Sauvegarder les rapports
    save_partner_report(partner_dir, created_list, not_created_list)
    
    log(f"   ✅ Terminé : {created} créés, {skipped_type} ignorés (type N/A), {skipped_matricule} ignorés (matricule N/A), {failed} échecs")
    
    return {
        "success": failed == 0,
        "created": created,
        "skipped_type": skipped_type,
        "skipped_matricule": skipped_matricule,
        "failed": failed,
        "error": None if failed == 0 else f"{failed} échecs"
    }


def main():
    parser = argparse.ArgumentParser(description="Création automatique de flotte VPS")
    parser.add_argument("--only", help="Traiter un seul partenaire (ex: Partenaire42)")
    parser.add_argument("--start", help="Reprendre depuis ce partenaire")
    parser.add_argument("--dry-run", action="store_true", help="Simulation sans création")
    parser.add_argument("--headed", action="store_true", help="Mode visible (pas headless)")
    args = parser.parse_args()
    
    log("\n" + "="*70)
    log("🚗 CREATE FLEET VPS — Création automatique de flotte")
    log("="*70)
    
    if args.dry_run:
        log("🧪 MODE DRY-RUN : Aucune modification ne sera faite")
    
    # Trouver les partenaires
    all_partners = find_partner_folders(ORGANIZED_DIR)
    
    if not all_partners:
        log(f"❌ Aucun partenaire trouvé dans {ORGANIZED_DIR}")
        send_slack(f"❌ Création flotte échouée : aucun partenaire trouvé dans {ORGANIZED_DIR}", "#ff0000")
        sys.exit(1)
    
    log(f"📁 {len(all_partners)} partenaires trouvés")
    
    # Filtrer si --only
    if args.only:
        target = normalize_partner_name(args.only)
        partners_to_process = [p for p in all_partners if normalize_partner_name(p.name) == target]
        if not partners_to_process:
            log(f"❌ Partenaire '{args.only}' non trouvé")
            sys.exit(1)
        log(f"🎯 Un seul partenaire : {partners_to_process[0].name}")
    else:
        partners_to_process = all_partners
    
    # Filtrer si --start
    if args.start:
        start_num = extract_partner_number(args.start)
        partners_to_process = [p for p in partners_to_process if extract_partner_number(p.name) >= start_num]
        log(f"🚀 Reprise depuis le partenaire >= {args.start}")
    
    # Stats globales
    success_partners = []
    failed_partners = []
    total_created = 0
    total_skipped_type = 0
    total_skipped_matricule = 0
    
    # Setup driver
    driver = None
    if not args.dry_run:
        try:
            driver = setup_driver_headless(headed=args.headed)
        except Exception as e:
            log(f"❌ Erreur driver Chrome : {e}")
            send_slack(f"❌ Création flotte échouée : erreur driver Chrome - {e}", "#ff0000")
            sys.exit(1)
    
    # Traitement
    try:
        for partner_dir in partners_to_process:
            try:
                result = process_partner(driver, partner_dir, dry_run=args.dry_run)
                
                if result["success"]:
                    success_partners.append(partner_dir.name)
                else:
                    failed_partners.append(f"{partner_dir.name} ({result.get('error', 'erreur')})")
                
                total_created += result["created"]
                total_skipped_type += result["skipped_type"]
                total_skipped_matricule += result["skipped_matricule"]
                
            except Exception as e:
                log(f"❌ Erreur critique sur {partner_dir.name}: {e}")
                failed_partners.append(f"{partner_dir.name} (exception: {e})")
                traceback.print_exc()
    
    except KeyboardInterrupt:
        log("\n🛑 Interrompu par l'utilisateur")
    
    finally:
        if driver:
            driver.quit()
    
    # Résumé final
    log("\n" + "="*70)
    log("📊 RÉSUMÉ FINAL")
    log("="*70)
    log(f"✅ Partenaires réussis : {len(success_partners)}")
    log(f"❌ Partenaires échoués : {len(failed_partners)}")
    log(f"🚗 Véhicules créés : {total_created}")
    log(f"⏩ Véhicules ignorés (type N/A) : {total_skipped_type}")
    log(f"⏩ Véhicules ignorés (matricule N/A) : {total_skipped_matricule}")
    log("="*70)
    
    # Slack notification
    send_slack_summary(success_partners, failed_partners, total_created, total_skipped_type, total_skipped_matricule)
    
    # Code de sortie
    sys.exit(0 if not failed_partners else 1)


if __name__ == "__main__":
    main()
