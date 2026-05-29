"""
approve_fleet_vps.py
====================
Approbation automatique des cartes grises pour la flotte UpJunoo.

PHASE 0  — Pré-audit (sans browser) :
  • Charge l'Excel immatriculations_*.xlsx → index {plaque_norm → {fichier, immat}}
  • Charge images_ocr/ → index {nom_fichier → Path}
  • Pour Partenaire1 : imprime la liste complète matricules → trouvé/manquant/image OK

PHASE 1  — Par partenaire (organized_by_partner/, sauf UNASSIGNED_DRIVERS) :
  1. Login owner  → partenaire{N}@upjunoo.com  +  UNIVERSAL_PASSWORD
  2. Scrape /manage-fleet → toutes les lignes statut "EN ATTENTE"
     Logs : nombre de lignes, index colonnes, dump header tableau
  3. Pour chaque véhicule EN ATTENTE :
     a. Normalise matricule → cherche dans index Excel
        LOG ✅/❌ avec plaque_raw et plaque_norm
     b. SI trouvé → cherche image locale  LOG ✅/⚠️
     c. Clique ⋮ → Approuver  (stratégie atomique JS + fallback Selenium)
        LOG chaque tentative + screenshot avant/après
     d. Page Documents → clic icône upload  LOG URL + dump dernier bouton vert
     e. /manage-fleet/document-upload/{id} :
        - Remplit "Identifier le numéro"  LOG valeur envoyée
        - Upload image  LOG chemin absolu
        - Clique "Mise à jour"  LOG réponse + URL finale
     f. Statut_approbation = Oui | Non  avec raison précise
  4. Sauvegarde fleet_approval_report_{ts}.json dans le dossier partenaire
  5. HTML KPI global
  6. Slack finale

Usage:
  python3 approve_fleet_vps.py --only Partenaire1          # Test sur 1 partenaire
  python3 approve_fleet_vps.py --dry-run                   # Simulation complète sans browser
  python3 approve_fleet_vps.py --pre-audit Partenaire1     # Pré-audit matricules seulement
  python3 approve_fleet_vps.py                             # Tous les partenaires
  python3 approve_fleet_vps.py --start Partenaire-50       # Reprise depuis

VPS background:
  nohup python3 vps_deploy/approve_fleet_vps.py > logs/approve_fleet.log 2>&1 &
  tail -f logs/approve_fleet.log
"""

import argparse
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
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
# webdriver_manager retiré — on exige chromedriver installé sur le VPS

# ─── Configuration ────────────────────────────────────────────────────────────
BASE_URL            = "https://upjunoo-server-new.junooapps.com"
OWNER_LOGIN_URL     = f"{BASE_URL}/login/owner-login"
MANAGE_FLEET_URL    = f"{BASE_URL}/manage-fleet"

# Chemins
# Sur VPS  : script à la racine ~/upjunoo-scraper/
# En local : script dans scraping/vps_deploy/ → on remonte d'un niveau
_SCRIPT_DIR   = Path(__file__).parent
_PROJECT_ROOT = _SCRIPT_DIR.parent if _SCRIPT_DIR.name == "vps_deploy" else _SCRIPT_DIR

# Tout part de _PROJECT_ROOT (~/upjunoo-scraper/ sur VPS, scraping/ en local)
OUTPUT_DIR         = _PROJECT_ROOT / "output"
REPORTS_DIR        = OUTPUT_DIR / "reports"
LOG_FILE           = OUTPUT_DIR / "approve_fleet.log"
HTML_REPORT_PATH   = OUTPUT_DIR / "approval_kpi_report.html"
ORGANIZED_DIR      = OUTPUT_DIR / "organized_by_partner"
IMAGES_OCR_DIR     = _PROJECT_ROOT / "images_ocr"
EXCEL_PATH         = OUTPUT_DIR / "immatriculations.xlsx"

# Fallbacks Excel
_ALT_EXCEL_PATHS = [
    Path.home() / "Downloads" / "output" / "immatriculations.xlsx",
    _PROJECT_ROOT / "immatriculations.xlsx",
]

UNIVERSAL_PASSWORD  = "123456789@"
WEBHOOK_URL         = os.getenv("WEBHOOK_URL", "")

# Couvre : Partenaire1, partenaire2, Partenaires-51, partenaire-101, partenaire-43, etc.
PARTNER_NAME_RE = re.compile(r'^\s*(partenaires?)[-_\s]*(\d+)\s*$', re.I)

STATUS_EN_ATTENTE_KEYWORDS = ["en attente", "pending", "waiting", "attente"]


# ═════════════════════════════════════════════════════════════════════════════
#  LOG + SLACK
# ═════════════════════════════════════════════════════════════════════════════

def log(message: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}][{level}] {message}"
    print(line, flush=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_sep(title: str = ""):
    """Séparateur visuel dans les logs."""
    sep = "─" * 60
    if title:
        log(f"\n{sep}")
        log(f"  ▶  {title}")
        log(sep)
    else:
        log(sep)


def log_dom(driver, label: str, element=None, max_chars: int = 1000):
    """Dump HTML d'un élément pour debug. Toujours appelé sur erreur."""
    try:
        if element is not None:
            html = driver.execute_script("return arguments[0].outerHTML;", element)
        else:
            html = driver.execute_script(
                "return document.querySelector('table') "
                "? document.querySelector('table').outerHTML.substring(0,2000) "
                ": document.body.innerHTML.substring(0,2000);"
            )
        log(f"🔬 DOM[{label}]: {str(html)[:max_chars]}", "DEBUG")
    except Exception as e:
        log(f"🔬 DOM dump échoué [{label}]: {e}", "DEBUG")


def send_slack(message: str, color: str = "#36a64f"):
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


def send_slack_summary(stats: dict):
    total_p       = stats["total_partners"]
    approved      = stats["total_approved"]
    not_found     = stats["total_not_found"]
    img_missing   = stats["total_image_missing"]
    failed        = stats["total_failed"]
    skipped       = stats["total_skipped"]
    total_v       = approved + not_found + img_missing + failed + skipped

    color = "#36a64f" if failed == 0 else "#ff0000"
    status = "✅ APPROBATION TERMINÉE" if failed == 0 else "❌ APPROBATION AVEC ERREURS"

    message = f"""{status}

📊 *Résumé Approbation Carte Grise*
• Partenaires traités : {total_p}
• Véhicules EN ATTENTE traités : {total_v}
• ✅ Approuvés avec succès : {approved}
• ❌ Matricule non trouvé dans Excel : {not_found}
• ⚠️ Image introuvable dans images_ocr : {img_missing}
• 🔴 Échec approbation (erreur) : {failed}
• ⏩ Ignorés (pas EN ATTENTE) : {skipped}

📄 Rapport HTML : {HTML_REPORT_PATH}"""

    send_slack(message, color)


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS — NORMALISATION PLAQUE
# ═════════════════════════════════════════════════════════════════════════════

def normalize_plate(plate: str) -> str:
    """
    Normalise une plaque pour comparaison :
    Majuscules + retire tirets, espaces, points, slashes.
    Ex: 'AA-297-BF' → 'AA297BF', '3736 LV 01' → '3736LV01'
    """
    if not plate:
        return ""
    raw = str(plate).strip().upper()
    for ch in ("-", " ", ".", "/", "_"):
        raw = raw.replace(ch, "")
    return raw


# ═════════════════════════════════════════════════════════════════════════════
#  CHARGEMENT EXCEL — BANQUE D'IMMATRICULATIONS
# ═════════════════════════════════════════════════════════════════════════════

def find_excel_path() -> Path:
    """Trouve le fichier Excel immatriculations, teste plusieurs emplacements."""
    if EXCEL_PATH.exists():
        return EXCEL_PATH
    for alt in _ALT_EXCEL_PATHS:
        if alt.exists():
            return alt
    # Recherche récursive depuis la home
    for candidate in Path.home().rglob("immatriculations_*.xlsx"):
        return candidate
    return None


def load_immatriculations_index(excel_path: Path) -> dict:
    """
    Charge le fichier Excel et retourne un index :
    { matricule_normalisé: {"nom_fichier": "IMG_XXX.jpeg", "immatriculation": "AA-297-BF", ...} }
    """
    try:
        import openpyxl
    except ImportError:
        log("❌ openpyxl non installé. pip3 install openpyxl")
        sys.exit(1)

    log(f"📂 Chargement Excel : {excel_path}")
    wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
    ws = wb.active

    index = {}
    headers = []
    for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
        if row_idx == 0:
            headers = [str(c).strip() if c else "" for c in row]
            log(f"   Colonnes Excel : {headers}")
            continue
        if not any(row):
            continue
        row_dict = dict(zip(headers, row))
        immat_raw = str(row_dict.get("Immatriculation", "") or "").strip()
        nom_fichier = str(row_dict.get("Nom du fichier", "") or "").strip()
        if not immat_raw or immat_raw.upper() in ("N/A", "NONE", ""):
            continue
        norm = normalize_plate(immat_raw)
        if norm:
            index[norm] = {
                "immatriculation": immat_raw,
                "nom_fichier": nom_fichier,
                "marque": str(row_dict.get("Marque", "") or ""),
                "modele": str(row_dict.get("Modèle", "") or ""),
                "couleur": str(row_dict.get("Couleur", "") or ""),
                "url_image": str(row_dict.get("URL image", "") or ""),
            }
    wb.close()
    log(f"   ✅ {len(index)} immatriculations indexées")
    return index


# ═════════════════════════════════════════════════════════════════════════════
#  IMAGES OCR — BANQUE D'IMAGES LOCALE
# ═════════════════════════════════════════════════════════════════════════════

def build_image_index(images_dir: Path) -> dict:
    """
    Construit un index des images disponibles :
    { "IMG_2372.jpeg": Path(...), "IMG_2372.jpg": Path(...), ... }
    (clé = nom de fichier en minuscules)
    """
    if not images_dir.exists():
        log(f"⚠️ Dossier images_ocr introuvable : {images_dir}")
        return {}
    index = {}
    for f in images_dir.iterdir():
        if f.suffix.lower() in (".jpeg", ".jpg", ".png"):
            index[f.name.lower()] = f
    log(f"📷 {len(index)} images indexées dans {images_dir}")
    return index


def find_image_for_filename(nom_fichier: str, image_index: dict) -> Path:
    """
    Trouve le fichier image local correspondant au nom du fichier Excel.
    Essaie avec le nom exact, puis .jpg/.jpeg/.png.
    """
    if not nom_fichier:
        return None
    key = nom_fichier.lower()
    if key in image_index:
        return image_index[key]
    # Essayer les variantes d'extension
    stem = Path(nom_fichier).stem
    for ext in (".jpeg", ".jpg", ".png"):
        alt = (stem + ext).lower()
        if alt in image_index:
            return image_index[alt]
    return None


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS — PARTENAIRES
# ═════════════════════════════════════════════════════════════════════════════

def derive_owner_email(partner_name: str):
    m = PARTNER_NAME_RE.match(partner_name or "")
    if not m:
        return None
    prefix = m.group(1).lower()  # 'partenaire' ou 'partenaires'
    num = m.group(2)
    return f"{prefix}{num}@upjunoo.com"


def normalize_partner_name(name: str) -> str:
    return re.sub(r'[-_\s]', '', name.lower())


def extract_partner_number(name: str) -> int:
    m = PARTNER_NAME_RE.match(name or "")
    return int(m.group(2)) if m else 0


def find_partner_folders(organized_dir: Path) -> list:
    if not organized_dir.exists():
        log(f"❌ Dossier introuvable : {organized_dir}", "ERROR")
        return []
    partners = []
    skipped = []
    for item in organized_dir.iterdir():
        if not item.is_dir():
            continue
        if "unassigned" in item.name.lower():
            continue
        if PARTNER_NAME_RE.match(item.name):
            partners.append(item)
        else:
            skipped.append(item.name)
    if skipped:
        log(f"   ⏩ Dossiers ignorés (non-partenaire) : {skipped[:8]}", "DEBUG")
    partners.sort(key=lambda p: extract_partner_number(p.name))
    log(f"   📁 {len(partners)} partenaires trouvés dans {organized_dir}")
    return partners


def run_pre_audit(partner_name: str, immat_index: dict, image_index: dict):
    """
    Pré-audit sans browser : compare tous les matricules du data.json
    d'un partenaire contre l'index Excel + images_ocr.
    Affiche un tableau lisible dans les logs.
    """
    # Chercher le dossier partenaire
    target = normalize_partner_name(partner_name)
    all_partners = find_partner_folders(ORGANIZED_DIR)
    partner_dir = next((p for p in all_partners if normalize_partner_name(p.name) == target), None)
    if not partner_dir:
        log(f"❌ Partenaire '{partner_name}' non trouvé dans {ORGANIZED_DIR}")
        return

    data_json = partner_dir / "data.json"
    if not data_json.exists():
        log(f"❌ data.json absent : {data_json}")
        return

    with open(data_json, encoding="utf-8") as f:
        data = json.load(f)

    drivers = data.get("drivers", [])
    log_sep(f"PRÉ-AUDIT : {partner_dir.name} — {len(drivers)} conducteurs")
    log(f"{'N°':>3}  {'Conducteur':<35} {'Matricule JSON':<22} {'Norm':<18} {'Excel':^6} {'Image':^6}")
    log("─" * 95)

    found_excel = 0
    found_image = 0
    for i, drv in enumerate(drivers, 1):
        veh = drv.get("vehicle", {})
        mat_raw = veh.get("matricule", "N/A")
        mat_norm = normalize_plate(mat_raw)
        nom = drv.get("nom", "?")[:34]

        if mat_raw in ("N/A", "n/a", "", None):
            log(f"{i:>3}  {nom:<35} {'N/A':<22} {'─':<18} {'─':^6} {'─':^6}")
            continue

        in_excel = immat_index.get(mat_norm)
        excel_icon = "✅" if in_excel else "❌"
        if in_excel:
            found_excel += 1
            img_path = find_image_for_filename(in_excel["nom_fichier"], image_index)
            img_icon = "✅" if img_path else "⚠️"
            if img_path:
                found_image += 1
            img_detail = in_excel["nom_fichier"]
        else:
            img_icon = "─"
            img_detail = ""

        log(f"{i:>3}  {nom:<35} {mat_raw:<22} {mat_norm:<18} {excel_icon:^6} {img_icon:^6}  {img_detail}")

    log("─" * 95)
    log(f"📊 TOTAL : {len(drivers)} conducteurs | ✅ Excel: {found_excel} | ✅ Image: {found_image} | ❌ Excel manquant: {len(drivers)-found_excel}")
    log_sep()


# ═════════════════════════════════════════════════════════════════════════════
#  CHROME HEADLESS
# ═════════════════════════════════════════════════════════════════════════════

def setup_driver(headed: bool = False):
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
    if not headed:
        chrome_options.add_argument("--remote-debugging-port=9223")
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    if not headed:
        for binary in [
            "chromium-browser",
            "/snap/bin/chromium",
            "chromium",
            "google-chrome-stable",
            "google-chrome",
        ]:
            path = binary if os.path.isfile(binary) else shutil.which(binary)
            if path:
                chrome_options.binary_location = path
                break

    chromedriver_path = None
    for cd in [
        "/usr/bin/chromedriver",
        "chromedriver",
        "/snap/bin/chromium.chromedriver",
        "/snap/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/lib/chromium/chromedriver",
    ]:
        if os.path.isfile(cd) or shutil.which(cd):
            chromedriver_path = cd if os.path.isfile(cd) else shutil.which(cd)
            break

    if not chromedriver_path:
        raise RuntimeError("ChromeDriver introuvable — installe-le : sudo apt install chromium-chromedriver")
    service = Service(chromedriver_path)
    return webdriver.Chrome(service=service, options=chrome_options)


def _save_debug_screenshot(driver, label: str):
    try:
        debug_dir = OUTPUT_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = debug_dir / f"{label}_{ts}.png"
        driver.save_screenshot(str(path))
        log(f"         📸 Screenshot: {path}")
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═════════════════════════════════════════════════════════════════════════════

def owner_login(driver, email: str, password: str, max_retries: int = 3) -> bool:
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
            email_input.clear()
            email_input.send_keys(email)
            time.sleep(0.3)
            pwd_input.clear()
            pwd_input.send_keys(password)
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
        driver.get(f"{BASE_URL}/logout")
        time.sleep(1)
    except Exception:
        pass
    try:
        driver.delete_all_cookies()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  SCRAPE FLOTTE — VÉHICULES EN ATTENTE
# ═════════════════════════════════════════════════════════════════════════════

def _find_plate_column_index(driver) -> int:
    try:
        headers = driver.find_elements(By.CSS_SELECTOR, "table thead th")
        for idx, th in enumerate(headers):
            text = (th.text or "").strip().lower()
            if any(kw in text for kw in ["plaque", "immatriculation", "license"]):
                return idx
    except Exception:
        pass
    return 4


def _find_status_column_index(driver) -> int:
    try:
        headers = driver.find_elements(By.CSS_SELECTOR, "table thead th")
        for idx, th in enumerate(headers):
            text = (th.text or "").strip().lower()
            if "statut" in text or "status" in text or "état" in text:
                return idx
    except Exception:
        pass
    return 6


def _set_pagination_max(driver):
    """Passe à la plus grande pagination disponible et attend que le tableau se recharge."""
    try:
        # Attendre que le select ait au moins 2 options chargées
        sel = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select.form-select.form-select-sm.w-auto"))
        )
        WebDriverWait(driver, 10).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR,
                "select.form-select.form-select-sm.w-auto option")) >= 2
        )
        driver.execute_script("""
            var select = arguments[0];
            var maxVal = -1, maxIdx = 0;
            for (var i = 0; i < select.options.length; i++) {
                var n = parseInt(select.options[i].value, 10);
                if (isNaN(n)) n = parseInt(select.options[i].text, 10);
                if (!isNaN(n) && n > maxVal) { maxVal = n; maxIdx = i; }
            }
            select.selectedIndex = maxIdx;
            var nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value').set;
            nativeSetter.call(select, select.options[select.selectedIndex].value);
            select.dispatchEvent(new Event('input', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));
        """, sel)
        # Attendre que le tableau se repeuple après changement de pagination
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
        time.sleep(1)
    except Exception:
        pass


def _is_next_disabled(driver) -> bool:
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
        prev_rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        prev_text = prev_rows[0].text[:80] if prev_rows else ""
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
        start = time.time()
        while time.time() - start < 20:
            time.sleep(0.5)
            try:
                rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                new_text = rows[0].text[:80] if rows else ""
                if new_text and new_text != prev_text:
                    time.sleep(1)
                    return True
            except Exception:
                pass
        return False
    except NoSuchElementException:
        return False


def scrape_pending_vehicles(driver) -> list:
    """
    Scrape /manage-fleet : retourne les véhicules EN ATTENTE.
    Logs détaillés : headers tableau, colonnes, chaque ligne avec statut.
    """
    all_pending = []
    log_sep(f"SCRAPE /manage-fleet")
    log(f"   🌐 URL : {MANAGE_FLEET_URL}")
    log(f"   🌐 URL actuelle avant nav : {driver.current_url}")
    driver.get(MANAGE_FLEET_URL)
    try:
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
        )
        log("   ✅ Tableau détecté sur la page")
    except TimeoutException:
        log("   ❌ Tableau /manage-fleet non chargé après 60s", "ERROR")
        _save_debug_screenshot(driver, "no_table_manage_fleet")
        log_dom(driver, "page_body_no_table")
        return []

    time.sleep(3)
    log(f"   🌐 URL après chargement : {driver.current_url}")

    # ── Dump des headers pour comprendre la structure exacte du tableau ──
    try:
        headers_els = driver.find_elements(By.CSS_SELECTOR, "table thead th")
        headers_text = [f"[{i}]{h.text.strip()!r}" for i, h in enumerate(headers_els)]
        log(f"   📋 Headers tableau ({len(headers_text)} cols) : {' | '.join(headers_text)}")
    except Exception as e:
        log(f"   ⚠️ Impossible de lire les headers : {e}")
        log_dom(driver, "table_no_headers")

    _set_pagination_max(driver)
    time.sleep(2)

    plate_col = _find_plate_column_index(driver)
    status_col = _find_status_column_index(driver)
    log(f"   🎯 Colonne Plaque → index {plate_col} | Colonne Statut → index {status_col}")

    page_num = 1
    while True:
        time.sleep(1)
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        actual_rows = [r for r in rows if r.text.strip()]
        log(f"   📄 Page {page_num} : {len(actual_rows)} lignes non-vides")

        for row_idx, row in enumerate(actual_rows):
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) <= max(plate_col, status_col):
                    log(f"      ⚠️ Ligne {row_idx} : {len(cells)} cellules < attendu {max(plate_col,status_col)+1}, skip", "DEBUG")
                    continue

                plate_text = cells[plate_col].text.strip()
                status_text = cells[status_col].text.strip()
                status_lower = status_text.lower()

                if not plate_text or plate_text in ("-", "N/A", ""):
                    continue

                is_pending = any(kw in status_lower for kw in STATUS_EN_ATTENTE_KEYWORDS)
                icon = "🟡" if is_pending else "⚪"
                log(f"      {icon} Ligne {row_idx:>3} | Plaque={plate_text!r:<22} | Statut={status_text!r}", "DEBUG")

                if not is_pending:
                    continue

                # Cherche doc_link dans toutes les cellules
                doc_link = ""
                all_hrefs_found = []
                try:
                    for cell in cells:
                        for a in cell.find_elements(By.TAG_NAME, "a"):
                            href = a.get_attribute("href") or ""
                            if href:
                                all_hrefs_found.append(href)
                            if "document" in href and "manage-fleet" in href:
                                doc_link = href
                                break
                        if doc_link:
                            break
                    if not doc_link:
                        for cell in cells:
                            for c in cell.find_elements(By.CSS_SELECTOR, "a, button"):
                                href = c.get_attribute("href") or ""
                                if "document" in href:
                                    doc_link = href
                                    break
                            if doc_link:
                                break
                except Exception:
                    pass

                log(f"      🟡 EN ATTENTE → {plate_text!r} | doc_link={doc_link!r}")
                if all_hrefs_found and not doc_link:
                    log(f"         ℹ️ Autres liens sur cette ligne : {all_hrefs_found[:4]}", "DEBUG")

                all_pending.append({
                    "plate_raw": plate_text,
                    "plate_norm": normalize_plate(plate_text),
                    "status_text": status_text,
                    "doc_link": doc_link,
                })
            except Exception as e:
                log(f"      ⚠️ Erreur lecture ligne {row_idx} : {e}")
                continue

        if not _go_next_page(driver):
            break
        page_num += 1
        if page_num > 50:
            log("   ⚠️ Limite de 50 pages atteinte")
            break
        time.sleep(1)

    log(f"   📊 Total EN ATTENTE trouvés : {len(all_pending)}")
    if not all_pending:
        log("   ℹ️ Aucun véhicule EN ATTENTE sur ce compte")
        _save_debug_screenshot(driver, "zero_pending")
    log_sep()
    return all_pending


# ═════════════════════════════════════════════════════════════════════════════
#  APPROBATION — PROCESS COMPLET PAR VÉHICULE
# ═════════════════════════════════════════════════════════════════════════════

def _wait_swal(driver, timeout_s: float = 3.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if driver.find_elements(By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"):
            return True
        time.sleep(0.1)
    return False


def _find_row_by_plate(driver, plate_raw: str):
    """Trouve la ligne du tableau correspondant à ce matricule."""
    target_raw = (plate_raw or "").strip().upper()
    target_norm = normalize_plate(plate_raw)
    plate_col = _find_plate_column_index(driver)

    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    for row in rows:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) <= plate_col:
                continue
            cell_text = cells[plate_col].text.strip().upper()
            if cell_text == target_raw or normalize_plate(cell_text) == target_norm:
                return row
        except Exception:
            continue
    return None


def _click_approuver_in_dropdown(driver, row, plate_raw: str = "") -> bool:
    """
    Ouvre le menu ⋮ et clique 'Approuver'.
    Logs détaillés + screenshot avant/après.
    """
    log(f"         🔽 Ouverture dropdown pour {plate_raw!r}")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
    time.sleep(0.5)
    _save_debug_screenshot(driver, f"before_dropdown_{normalize_plate(plate_raw)}")

    # ── Trouver le bouton ⋮ dans la dernière cellule ──
    action_btn = None
    for sel in [
        "td:last-child [data-bs-toggle='dropdown']",
        "td:last-child button",
        "td:last-child [role='button']",
        "td:last-child a",
    ]:
        try:
            found = row.find_element(By.CSS_SELECTOR, sel)
            action_btn = found
            log(f"         🔘 Bouton ⋮ trouvé via selector {sel!r}", "DEBUG")
            break
        except NoSuchElementException:
            continue

    if action_btn is None:
        log("         ❌ Bouton ⋮ introuvable dans la dernière cellule")
        log_dom(driver, f"row_no_btn_{normalize_plate(plate_raw)}", row)
        return False

    # ── Stratégie A : JS atomique — ouvre dropdown + clique Approuver ──
    try:
        driver.execute_script("""
            var btn = arguments[0];
            if (window.bootstrap && window.bootstrap.Dropdown) {
                var dd = window.bootstrap.Dropdown.getInstance(btn)
                       || new window.bootstrap.Dropdown(btn);
                dd.show();
            } else { btn.click(); }
        """, action_btn)
        time.sleep(0.5)

        open_menus = driver.find_elements(By.CSS_SELECTOR, ".dropdown-menu.show")
        log(f"         🔽 Menus .show après ouverture JS : {len(open_menus)}", "DEBUG")

        clicked = driver.execute_script("""
            var menus = document.querySelectorAll('.dropdown-menu.show, .dropdown-menu');
            for (var m = 0; m < menus.length; m++) {
                var items = menus[m].querySelectorAll('.dropdown-item, a, li');
                for (var i = 0; i < items.length; i++) {
                    var txt = (items[i].textContent || '').trim();
                    if (txt === 'Approuver' || txt.indexOf('Approuver') !== -1) {
                        items[i].click();
                        return 'JS_clicked:' + txt;
                    }
                }
            }
            // Dump des items visibles pour debug
            var dbg = [];
            menus.forEach(function(m){
                m.querySelectorAll('.dropdown-item, a, li').forEach(function(it){
                    dbg.push((it.textContent||'').trim());
                });
            });
            return 'not_found|items=' + dbg.join(',');
        """)
        log(f"         🔽 Résultat JS Approuver : {clicked}", "DEBUG")
        if "JS_clicked" in str(clicked):
            time.sleep(1)
            _save_debug_screenshot(driver, f"after_approuver_{normalize_plate(plate_raw)}")
            log(f"         ✅ 'Approuver' cliqué via JS pour {plate_raw!r}")
            return True
    except Exception as e:
        log(f"         ⚠️ Stratégie JS échouée : {e}")

    # ── Stratégie B : Selenium natif ──
    log("         🔄 Stratégie B : Selenium natif", "DEBUG")
    try:
        action_btn.click()
        time.sleep(0.5)
        approuver_items = driver.find_elements(
            By.XPATH,
            "//*[normalize-space(text())='Approuver' or normalize-space()='Approuver']"
        )
        log(f"         🔍 Items 'Approuver' trouvés via XPATH : {len(approuver_items)}", "DEBUG")
        for item in approuver_items:
            if item.is_displayed():
                item.click()
                time.sleep(1)
                _save_debug_screenshot(driver, f"after_approuver_B_{normalize_plate(plate_raw)}")
                log(f"         ✅ 'Approuver' cliqué via Selenium pour {plate_raw!r}")
                return True
    except Exception as e:
        log(f"         ⚠️ Stratégie B échouée : {e}")

    _save_debug_screenshot(driver, f"dropdown_fail_{normalize_plate(plate_raw)}")
    log_dom(driver, f"dropdown_fail_{normalize_plate(plate_raw)}")
    return False


def _find_upload_button_and_click(driver) -> bool:
    """
    Sur la page Documents (/manage-fleet/document/{id}),
    trouve et clique l'icône upload (bouton vert).
    Logs détaillés de tous les liens/boutons trouvés.
    """
    time.sleep(2)
    current_url = driver.current_url
    log(f"         📄 Page Documents URL : {current_url}")

    # Dump de tous les liens visibles pour debug
    try:
        all_links = driver.find_elements(By.TAG_NAME, "a")
        hrefs = [a.get_attribute("href") or "" for a in all_links if a.is_displayed()]
        log(f"         🔗 Liens visibles sur page ({len(hrefs)}) : {hrefs[:8]}", "DEBUG")
    except Exception:
        pass

    # ── Stratégies dans l'ordre de priorité ──
    strategies = [
        ("lien direct document-upload dans href",   "a[href*='document-upload']"),
        ("bouton avec i.bi-upload",                  "button i.bi-upload, button i.bi-cloud-upload"),
        ("bouton vert btn-success",                  "button.btn-success"),
        ("lien dans tableau td",                     "table tbody td a"),
        ("tout bouton visible dans td",               "table tbody td button"),
    ]

    for label, sel in strategies:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elements:
                if el.is_displayed():
                    href = el.get_attribute("href") or ""
                    if "document-upload" in href:
                        log(f"         🟢 Upload btn via [{label}] → {href}")
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        time.sleep(0.3)
                        try:
                            el.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", el)
                        time.sleep(2)
                        log(f"         🌐 URL après clic upload : {driver.current_url}")
                        return True
        except Exception:
            continue

    # ── Fallback : tous les liens de la page ──
    try:
        for link in driver.find_elements(By.TAG_NAME, "a"):
            href = link.get_attribute("href") or ""
            if "document-upload" in href and link.is_displayed():
                log(f"         🟢 Upload via fallback global → {href}")
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", link)
                time.sleep(0.3)
                try:
                    link.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", link)
                time.sleep(2)
                log(f"         🌐 URL après clic upload : {driver.current_url}")
                return True
    except Exception:
        pass

    # ── Fallback 2 : navigation directe via URL /document/{id} → /document-upload/1/{id} ──
    if "/manage-fleet/document/" in current_url:
        doc_id = current_url.rstrip("/").split("/")[-1]
        upload_url = f"{BASE_URL}/manage-fleet/document-upload/1/{doc_id}"
        log(f"         🔗 Fallback nav directe : {upload_url}")
        driver.get(upload_url)
        time.sleep(2)
        log(f"         🌐 URL après nav directe : {driver.current_url}")
        if "document-upload" in driver.current_url:
            return True

    log("         ❌ Bouton upload introuvable sur toutes les stratégies")
    _save_debug_screenshot(driver, "upload_btn_not_found")
    log_dom(driver, "upload_page_no_btn")
    return False


def upload_document(driver, image_path: Path, immatriculation_officielle: str, dry_run: bool = False) -> bool:
    """
    Sur /manage-fleet/document-upload/{id} :
    1. Remplit le champ numéro  2. Upload image  3. Clique Mise à jour
    Logs détaillés de chaque étape.
    """
    if dry_run:
        log(f"         [DRY-RUN] Uploadrait : {image_path.name} | Numéro : {immatriculation_officielle}")
        return True

    log(f"         📝 Page upload URL : {driver.current_url}")
    _save_debug_screenshot(driver, "upload_form_loaded")
    time.sleep(1)

    # Dump de tous les inputs de la page pour debug
    try:
        all_inputs = driver.find_elements(By.CSS_SELECTOR, "input, textarea, select")
        inputs_info = []
        for inp in all_inputs:
            try:
                inputs_info.append({
                    "type": inp.get_attribute("type"),
                    "name": inp.get_attribute("name"),
                    "id": inp.get_attribute("id"),
                    "placeholder": inp.get_attribute("placeholder"),
                    "visible": inp.is_displayed()
                })
            except Exception:
                pass
        log(f"         📋 Inputs sur la page ({len(inputs_info)}) : {inputs_info}", "DEBUG")
    except Exception:
        pass

    try:
        # ── 1. Renseigner "Identifier le numéro" ──
        id_fields = driver.find_elements(
            By.CSS_SELECTOR,
            "input[placeholder*='numéro' i], input[placeholder*='numero' i], "
            "input[placeholder*='identifier' i], input[name*='number' i], "
            "input[name*='numero' i], input[id*='number' i], input[name*='plate' i]"
        )
        if not id_fields:
            id_fields = driver.find_elements(By.CSS_SELECTOR, "input[type='text']")

        id_field = None
        for f in id_fields:
            if not (f.is_displayed() and f.is_enabled()):
                continue
            label_text = ""
            try:
                fid = f.get_attribute("id") or ""
                label_els = driver.find_elements(By.CSS_SELECTOR, f"label[for='{fid}']")
                if label_els:
                    label_text = label_els[0].text.lower()
            except Exception:
                pass
            if "nom" not in label_text:
                id_field = f
                log(f"         ✏️ Champ numéro trouvé : id={f.get_attribute('id')!r} placeholder={f.get_attribute('placeholder')!r} label={label_text!r}", "DEBUG")
                break

        if id_field:
            id_field.clear()
            id_field.send_keys(immatriculation_officielle)
            log(f"         ✏️ Numéro renseigné : {immatriculation_officielle!r}")
        else:
            log("         ⚠️ Champ numéro non trouvé — on continue sans renseigner")

        # ── 2. Upload image ──
        file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
        log(f"         📁 Inputs file trouvés : {len(file_inputs)}", "DEBUG")
        if not file_inputs:
            log("         ❌ Aucun input[type=file] sur la page")
            log_dom(driver, "no_file_input")
            return False

        file_input = None
        for fi in file_inputs:
            try:
                driver.execute_script("arguments[0].style.display='block'; arguments[0].style.visibility='visible';", fi)
                file_input = fi
                break
            except Exception:
                continue

        if not file_input:
            log("         ❌ Aucun input file accessible")
            return False

        abs_path = str(image_path.absolute())
        log(f"         📎 Envoi image : {abs_path}")
        file_input.send_keys(abs_path)
        time.sleep(1.5)
        _save_debug_screenshot(driver, "after_file_upload")

        # ── 3. Cliquer "Mise à jour" ──
        submit_btn = None
        submit_kws = ["mise à jour", "mise a jour", "update", "save", "enregistrer", "valider", "soumettre", "submit"]
        for sel in ["button[type='submit']", "button.btn-primary", "button.btn-success", "input[type='submit']"]:
            candidates = driver.find_elements(By.CSS_SELECTOR, sel)
            for c in candidates:
                if c.is_displayed() and c.is_enabled():
                    btn_text = (c.text or c.get_attribute("value") or "").lower()
                    log(f"         🔘 Bouton candidat : {btn_text!r}", "DEBUG")
                    if any(kw in btn_text for kw in submit_kws):
                        submit_btn = c
                        break
                    elif not submit_btn:
                        submit_btn = c
            if submit_btn and any(kw in (submit_btn.text or "").lower() for kw in submit_kws):
                break

        if not submit_btn:
            log("         ❌ Bouton 'Mise à jour' introuvable")
            _save_debug_screenshot(driver, "no_submit_btn")
            log_dom(driver, "no_submit_btn")
            return False

        btn_label = (submit_btn.text or submit_btn.get_attribute("value") or "?").strip()
        log(f"         🖱️ Clic sur bouton submit : {btn_label!r}")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_btn)
        time.sleep(0.3)
        try:
            submit_btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", submit_btn)

        time.sleep(3)
        url_after = driver.current_url
        log(f"         🌐 URL après submit : {url_after}")
        _save_debug_screenshot(driver, "after_submit")

        if "document-upload" not in url_after:
            log(f"         ✅ Redirection réussie → {url_after}")
            return True

        # Vérifier les indicateurs de succès sur la page
        success_els = driver.find_elements(
            By.CSS_SELECTOR, ".alert-success, .swal2-success, .toast-success, [class*='success']"
        )
        if success_els:
            log(f"         ✅ Indicateur succès détecté : {success_els[0].text.strip()!r}")
            return True

        log("         ⚠️ Toujours sur document-upload après submit — succès supposé")
        return True

    except Exception as e:
        log(f"         ❌ Exception upload_document : {e}")
        _save_debug_screenshot(driver, "upload_exception")
        return False


def approve_vehicle(driver, vehicle: dict, immat_info: dict, image_path: Path, dry_run: bool = False) -> bool:
    """
    Processus complet d'approbation d'un véhicule — navigation directe via doc_link :
    1. Utilise le doc_link (UUID unique par véhicule, déjà scrapé) pour aller
       directement sur la page Documents DU BON véhicule
    2. Sur la page Documents → construit l'URL d'upload /document-upload/1/{uuid}
    3. Sur /document-upload → remplit numéro + upload image carte grise → Mise à jour

    IMPORTANT : On n'utilise PAS le clic "Approuver" du dropdown car le JS du site
    redirige toujours vers le même premier véhicule (bug confirmé dans les logs).
    Le doc_link scrapé est fiable car chaque ligne du tableau a son propre UUID.
    """
    plate_raw  = vehicle["plate_raw"]
    plate_norm = normalize_plate(plate_raw)
    immat_off  = immat_info["immatriculation"]
    doc_link   = vehicle.get("doc_link", "")

    if dry_run:
        log(f"         [DRY-RUN] {plate_raw!r} → {immat_off!r} | Image : {image_path.name}")
        return True

    log_sep(f"APPROBATION : {plate_raw}  →  {immat_off}")

    # ── 1. Vérifier qu'on a un doc_link valide ──
    if not doc_link or "/manage-fleet/document/" not in doc_link:
        log(f"         ❌ doc_link manquant ou invalide pour {plate_raw!r} : {doc_link!r}")
        _save_debug_screenshot(driver, f"no_doc_link_{plate_norm}")
        return False

    # Extraire l'UUID du doc_link
    doc_uuid = doc_link.rstrip("/").split("/")[-1]
    log(f"         🔑 UUID document : {doc_uuid}")
    log(f"         🔗 doc_link : {doc_link}")

    # ── 2. Naviguer directement vers la page Documents du véhicule ──
    log(f"         🔄 Navigation directe vers la page Documents")
    driver.get(doc_link)
    time.sleep(3)

    current_url = driver.current_url
    log(f"         🌐 URL page Documents : {current_url}")
    _save_debug_screenshot(driver, f"doc_page_{plate_norm}")

    # Vérifier qu'on est bien sur la bonne page avec le bon UUID
    if doc_uuid not in current_url:
        log(f"         ❌ UUID {doc_uuid} absent de l'URL courante : {current_url}")
        _save_debug_screenshot(driver, f"wrong_page_{plate_norm}")
        return False

    # ── 2b. Vérifier le statut sur la page Documents ──
    # "Non téléchargé" → on upload | "En attente d'approbation" → déjà uploadé, skip
    try:
        doc_status_el = driver.find_element(By.CSS_SELECTOR, "table tbody td .badge, table tbody td span")
        doc_status_text = (doc_status_el.text or "").strip().lower()
        log(f"         📋 Statut document : {doc_status_el.text.strip()!r}")

        if any(kw in doc_status_text for kw in ["approbation", "approval", "approuv"]):
            log(f"         ⏭️ Carte grise déjà uploadée (statut: {doc_status_el.text.strip()!r}) — SKIP")
            return True  # retourne True car c'est déjà fait, pas une erreur
    except Exception as e:
        log(f"         ⚠️ Impossible de lire le statut document : {e} — on continue")

    # ── 3. Construire l'URL d'upload et naviguer directement ──
    upload_url = f"{BASE_URL}/manage-fleet/document-upload/1/{doc_uuid}"
    log(f"         � Navigation vers upload : {upload_url}")
    driver.get(upload_url)
    time.sleep(3)

    current_url = driver.current_url
    log(f"         🌐 URL page upload : {current_url}")
    _save_debug_screenshot(driver, f"upload_page_{plate_norm}")

    if "document-upload" not in current_url:
        log(f"         ❌ Pas sur document-upload — URL : {current_url}")
        _save_debug_screenshot(driver, f"not_on_upload_{plate_norm}")
        return False

    # Vérifier que l'UUID est bien dans l'URL d'upload (sécurité)
    if doc_uuid not in current_url:
        log(f"         ❌ UUID {doc_uuid} absent de l'URL upload : {current_url}")
        _save_debug_screenshot(driver, f"wrong_upload_{plate_norm}")
        return False

    log(f"         ✅ Sur la page upload du bon véhicule (UUID={doc_uuid})")

    # ── 4. Remplir et soumettre le formulaire ──
    return upload_document(driver, image_path, immat_off, dry_run=False)


# ═════════════════════════════════════════════════════════════════════════════
#  RAPPORT — JSON + HTML KPI
# ═════════════════════════════════════════════════════════════════════════════

def update_partner_report(partner_dir: Path, approval_results: list):
    """
    Met à jour ou crée un fleet_creation_report dans le dossier partenaire
    avec les champs statut_approbation.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = partner_dir / f"fleet_approval_report_{timestamp}.json"

    report = {
        "generated_at": datetime.now().isoformat(),
        "partner": partner_dir.name,
        "total": len(approval_results),
        "approved": sum(1 for r in approval_results if r.get("statut_approbation") == "Oui"),
        "not_approved": sum(1 for r in approval_results if r.get("statut_approbation") == "Non"),
        "vehicles": approval_results
    }

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        log(f"   📄 Rapport JSON: {report_path}")
    except Exception as e:
        log(f"   ⚠️ Erreur écriture JSON : {e}")


def save_approval_excel(partner_dir: Path, approval_results: list):
    """
    Sauvegarde un rapport Excel multi-onglets dans le dossier partenaire :
    - Approuvés, Non trouvés Excel, Images manquantes, Échecs, Résumé
    """
    try:
        import pandas as pd
    except ImportError:
        log(f"   ⚠️ Excel ignoré (pandas non installé)")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    excel_path = partner_dir / f"fleet_approval_report_{timestamp}.xlsx"

    approved_list   = [r for r in approval_results if r.get("statut_approbation") == "Oui"]
    not_found_list  = [r for r in approval_results if "non trouvé" in (r.get("raison") or "").lower()]
    img_missing_list = [r for r in approval_results if "image" in (r.get("raison") or "").lower()]
    failed_list     = [r for r in approval_results if "erreur" in (r.get("raison") or "").lower()]

    try:
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            def _sheet(data_list, name, empty_msg):
                if data_list:
                    pd.DataFrame(data_list).to_excel(writer, sheet_name=name, index=False)
                else:
                    pd.DataFrame({"Info": [empty_msg]}).to_excel(writer, sheet_name=name, index=False)

            _sheet(approved_list,    "Approuves",        "Aucun véhicule approuvé")
            _sheet(not_found_list,   "Non_Trouves_Excel", "Aucun matricule manquant")
            _sheet(img_missing_list, "Images_Manquantes", "Aucune image manquante")
            _sheet(failed_list,      "Echecs",            "Aucun échec")
            pd.DataFrame([{
                "Total traités": len(approval_results),
                "Approuvés": len(approved_list),
                "Matricule non trouvé": len(not_found_list),
                "Image manquante": len(img_missing_list),
                "Erreurs": len(failed_list),
            }]).to_excel(writer, sheet_name="Resume", index=False)

        log(f"   📊 Rapport Excel : {excel_path.name}")
    except Exception as e:
        log(f"   ⚠️ Erreur Excel : {e}")


def save_kpi_summary(partner_dir: Path, partner_name: str, email: str, stats: dict):
    """
    Crée ou met à jour kpi_summary.json à la racine du dossier partenaire.
    Conserve un historique des 10 derniers passages avec horodatage.
    """
    kpi_path = partner_dir / "kpi_summary.json"

    approved    = stats.get("approved", 0)
    not_found   = stats.get("not_found", 0)
    img_missing = stats.get("img_missing", 0)
    failed      = stats.get("failed", 0)

    passage = {
        "date": datetime.now().isoformat(),
        "approved": approved,
        "not_found_excel": not_found,
        "image_missing": img_missing,
        "failed": failed,
    }

    if kpi_path.exists():
        try:
            with open(kpi_path, encoding="utf-8") as f:
                kpi = json.load(f)
        except Exception:
            kpi = {}
    else:
        kpi = {}

    kpi["partenaire"]   = partner_name
    kpi["email"]        = email
    kpi["last_approval"] = datetime.now().isoformat()
    kpi["total_approval_passages"] = kpi.get("total_approval_passages", 0) + 1

    kpi["cumul_approved"]      = kpi.get("cumul_approved", 0) + approved
    kpi["cumul_not_found"]     = kpi.get("cumul_not_found", 0) + not_found
    kpi["cumul_img_missing"]   = kpi.get("cumul_img_missing", 0) + img_missing
    kpi["cumul_failed"]        = kpi.get("cumul_failed", 0) + failed

    historique = kpi.get("approval_historique", [])
    historique.append(passage)
    kpi["approval_historique"] = historique[-10:]

    try:
        with open(kpi_path, "w", encoding="utf-8") as f:
            json.dump(kpi, f, ensure_ascii=False, indent=2)
        log(f"   📊 KPI mis à jour : {kpi_path.name}")
    except Exception as e:
        log(f"   ⚠️ Erreur KPI : {e}")


def generate_html_kpi_report(all_results: list, global_stats: dict, output_path: Path):
    """
    Génère un rapport HTML KPI avec :
    - Statistiques globales en haut
    - Tableau détaillé par partenaire + véhicule
    """
    ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    total_p = global_stats["total_partners"]
    approved = global_stats["total_approved"]
    not_found = global_stats["total_not_found"]
    img_missing = global_stats["total_image_missing"]
    failed = global_stats["total_failed"]
    total_v = approved + not_found + img_missing + failed

    pct_approved = round(approved / total_v * 100, 1) if total_v > 0 else 0

    # Générer les lignes du tableau
    rows_html = ""
    for r in all_results:
        status_cell = r.get("statut_approbation", "Non")
        reason = r.get("raison", "")
        if status_cell == "Oui":
            badge = '<span class="badge badge-success">✅ Oui</span>'
        else:
            badge = f'<span class="badge badge-danger">❌ Non</span>'

        rows_html += f"""
        <tr>
            <td>{r.get('partenaire', '')}</td>
            <td>{r.get('plate_raw', '')}</td>
            <td>{r.get('immatriculation_officielle', '-')}</td>
            <td>{r.get('nom_fichier_image', '-')}</td>
            <td>{badge}</td>
            <td><small>{reason}</small></td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Rapport Approbation Carte Grise — UpJunoo</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5; color: #333; }}
        .header {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            color: white; padding: 30px 40px;
        }}
        .header h1 {{ font-size: 28px; margin-bottom: 6px; }}
        .header p {{ opacity: 0.7; font-size: 14px; }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 30px 20px; }}
        .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 30px; }}
        .kpi-card {{
            background: white; border-radius: 12px; padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            text-align: center;
        }}
        .kpi-card .value {{ font-size: 36px; font-weight: 700; margin-bottom: 4px; }}
        .kpi-card .label {{ font-size: 13px; color: #666; }}
        .kpi-card.green .value {{ color: #27ae60; }}
        .kpi-card.red .value {{ color: #e74c3c; }}
        .kpi-card.orange .value {{ color: #e67e22; }}
        .kpi-card.blue .value {{ color: #2980b9; }}
        .kpi-card.gray .value {{ color: #95a5a6; }}
        .progress-bar-wrap {{
            background: white; border-radius: 12px; padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 30px;
        }}
        .progress-bar-wrap h3 {{ font-size: 15px; margin-bottom: 12px; color: #555; }}
        .progress-bar {{ height: 24px; background: #eee; border-radius: 12px; overflow: hidden; }}
        .progress-fill {{
            height: 100%; background: linear-gradient(90deg, #27ae60, #2ecc71);
            border-radius: 12px; transition: width 1s;
            display: flex; align-items: center; justify-content: center;
            color: white; font-size: 13px; font-weight: 600;
        }}
        .table-wrap {{
            background: white; border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08); overflow: hidden;
        }}
        .table-header {{
            padding: 20px 24px; border-bottom: 1px solid #eee;
            display: flex; justify-content: space-between; align-items: center;
        }}
        .table-header h2 {{ font-size: 18px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        thead th {{
            background: #f8f9fa; padding: 12px 16px;
            text-align: left; font-size: 12px;
            text-transform: uppercase; color: #888;
            letter-spacing: 0.5px; border-bottom: 2px solid #eee;
        }}
        tbody td {{ padding: 12px 16px; border-bottom: 1px solid #f0f0f0; font-size: 13px; }}
        tbody tr:hover {{ background: #fafbfc; }}
        .badge {{ padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }}
        .badge-success {{ background: #d4edda; color: #155724; }}
        .badge-danger {{ background: #f8d7da; color: #721c24; }}
        .badge-warning {{ background: #fff3cd; color: #856404; }}
        .footer {{ text-align: center; padding: 20px; color: #aaa; font-size: 12px; }}
        .search-input {{
            padding: 8px 14px; border: 1px solid #ddd; border-radius: 8px;
            font-size: 13px; width: 250px;
        }}
    </style>
</head>
<body>
<div class="header">
    <h1>🚗 Rapport Approbation Carte Grise</h1>
    <p>UpJunoo — Généré le {ts}</p>
</div>
<div class="container">
    <div class="kpi-grid">
        <div class="kpi-card blue">
            <div class="value">{total_p}</div>
            <div class="label">Partenaires traités</div>
        </div>
        <div class="kpi-card gray">
            <div class="value">{total_v}</div>
            <div class="label">Véhicules EN ATTENTE</div>
        </div>
        <div class="kpi-card green">
            <div class="value">{approved}</div>
            <div class="label">✅ Approuvés</div>
        </div>
        <div class="kpi-card red">
            <div class="value">{not_found}</div>
            <div class="label">❌ Matricule non trouvé</div>
        </div>
        <div class="kpi-card orange">
            <div class="value">{img_missing}</div>
            <div class="label">⚠️ Image manquante</div>
        </div>
        <div class="kpi-card red">
            <div class="value">{failed}</div>
            <div class="label">🔴 Erreur approbation</div>
        </div>
    </div>

    <div class="progress-bar-wrap">
        <h3>Taux d'approbation global : {pct_approved}%</h3>
        <div class="progress-bar">
            <div class="progress-fill" style="width:{pct_approved}%">{pct_approved}%</div>
        </div>
    </div>

    <div class="table-wrap">
        <div class="table-header">
            <h2>Détail par véhicule</h2>
            <input class="search-input" type="text" id="searchInput" placeholder="Rechercher..."
                   onkeyup="filterTable()">
        </div>
        <table id="dataTable">
            <thead>
                <tr>
                    <th>Partenaire</th>
                    <th>Matricule flotte</th>
                    <th>Matricule officiel</th>
                    <th>Image</th>
                    <th>Approbation</th>
                    <th>Raison</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
</div>
<div class="footer">UpJunoo Bot — approve_fleet_vps.py — {ts}</div>
<script>
function filterTable() {{
    var input = document.getElementById('searchInput').value.toLowerCase();
    var rows = document.querySelectorAll('#dataTable tbody tr');
    rows.forEach(function(row) {{
        row.style.display = row.textContent.toLowerCase().includes(input) ? '' : 'none';
    }});
}}
</script>
</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"📊 Rapport HTML KPI généré : {output_path}")


# ═════════════════════════════════════════════════════════════════════════════
#  TRAITEMENT PAR PARTENAIRE
# ═════════════════════════════════════════════════════════════════════════════

def process_partner(driver, partner_dir: Path, immat_index: dict, image_index: dict,
                    dry_run: bool = False) -> dict:
    """
    Traite un partenaire complet.
    Retourne un dict de stats + la liste des résultats par véhicule.
    """
    partner_name = partner_dir.name
    email = derive_owner_email(partner_name)

    if not email:
        log(f"❌ Nom partenaire invalide : {partner_name}")
        return {"success": False, "approved": 0, "not_found": 0, "img_missing": 0, "failed": 0, "results": []}

    log(f"\n{'='*60}")
    log(f"📂 Partenaire : {partner_name}")
    log(f"📧 Email : {email}")
    log(f"{'='*60}")

    # Login
    if not dry_run:
        if not owner_login(driver, email, UNIVERSAL_PASSWORD):
            return {"success": False, "approved": 0, "not_found": 0, "img_missing": 0, "failed": 0, "results": []}

    # Scrape initial pour compter
    if dry_run:
        log("   🧪 DRY-RUN : simulation du scrape")
        pending_vehicles = []
    else:
        pending_vehicles = scrape_pending_vehicles(driver)

    if not pending_vehicles:
        log(f"   ℹ️ Aucun véhicule EN ATTENTE pour {partner_name}")
        if not dry_run:
            owner_logout(driver)
        return {"success": True, "approved": 0, "not_found": 0, "img_missing": 0, "failed": 0, "results": []}

    log(f"   🔍 {len(pending_vehicles)} véhicules EN ATTENTE à traiter")

    results = []
    stats = {"approved": 0, "not_found": 0, "img_missing": 0, "failed": 0}
    total = len(pending_vehicles)

    for i, vehicle in enumerate(pending_vehicles, 1):
        plate_raw = vehicle["plate_raw"]
        plate_norm = vehicle["plate_norm"]

        log(f"\n   [{i}/{total}] Plaque: {plate_raw} (normalisée: {plate_norm})")

        result = {
            "partenaire": partner_name,
            "plate_raw": plate_raw,
            "plate_norm": plate_norm,
            "immatriculation_officielle": "",
            "nom_fichier_image": "",
            "statut_approbation": "Non",
            "raison": "",
        }

        # Chercher dans l'index Excel
        immat_info = immat_index.get(plate_norm)
        if not immat_info:
            log(f"      ❌ Matricule non trouvé dans Excel : {plate_raw} ({plate_norm})")
            result["raison"] = "Matricule non trouvé dans Excel"
            result["statut_approbation"] = "Non"
            stats["not_found"] += 1
            results.append(result)
            continue

        result["immatriculation_officielle"] = immat_info["immatriculation"]
        result["nom_fichier_image"] = immat_info["nom_fichier"]
        log(f"      ✅ Trouvé dans Excel : {immat_info['immatriculation']} | Image: {immat_info['nom_fichier']}")

        # Chercher l'image locale
        image_path = find_image_for_filename(immat_info["nom_fichier"], image_index)
        if not image_path:
            log(f"      ⚠️ Image introuvable dans images_ocr : {immat_info['nom_fichier']}")
            result["raison"] = f"Image introuvable dans images_ocr : {immat_info['nom_fichier']}"
            result["statut_approbation"] = "Non"
            stats["img_missing"] += 1
            results.append(result)
            continue

        log(f"      📷 Image trouvée : {image_path}")

        # Lancer l'approbation (table sera rechargée au prochain tour)
        success = approve_vehicle(driver, vehicle, immat_info, image_path, dry_run=dry_run)
        if success:
            result["statut_approbation"] = "Oui"
            result["raison"] = "Approuvé avec succès"
            stats["approved"] += 1
            log(f"      ✅ APPROUVÉ : {plate_raw}")
        else:
            result["statut_approbation"] = "Non"
            result["raison"] = "Erreur lors de l'approbation"
            stats["failed"] += 1
            log(f"      ❌ ÉCHEC approbation : {plate_raw}")

        results.append(result)
        time.sleep(1)

    # Sauvegarder les rapports partenaire (JSON + Excel + KPI)
    if results:
        update_partner_report(partner_dir, results)
        save_approval_excel(partner_dir, results)
        save_kpi_summary(partner_dir, partner_name, email or "", stats)

    if not dry_run:
        owner_logout(driver)

    log(f"\n   📊 {partner_name} : {stats['approved']} approuvés | "
        f"{stats['not_found']} non trouvés | "
        f"{stats['img_missing']} images manquantes | "
        f"{stats['failed']} erreurs")

    return {
        "success": stats["failed"] == 0,
        **stats,
        "results": results
    }


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Approbation automatique des cartes grises — VPS")
    parser.add_argument("--only",       help="Traiter un seul partenaire (ex: Partenaire1)")
    parser.add_argument("--start",      help="Reprendre depuis ce partenaire (ex: Partenaire-50)")
    parser.add_argument("--end",        help="Arrêter après ce numéro de partenaire (ex: 120)")
    parser.add_argument("--dry-run",    action="store_true", help="Simulation sans browser")
    parser.add_argument("--headed",     action="store_true", help="Mode visible (pas headless)")
    parser.add_argument("--pre-audit",  metavar="PARTENAIRE",
                        help="Pré-audit matricules seulement (ex: --pre-audit Partenaire1)")
    args = parser.parse_args()

    log("\n" + "=" * 70)
    log("🚗 APPROVE FLEET VPS — Approbation automatique des cartes grises")
    log(f"   📁 organized_by_partner : {ORGANIZED_DIR}")
    log(f"   📊 Excel              : {EXCEL_PATH}")
    log(f"   🖼️  images_ocr         : {IMAGES_OCR_DIR}")
    log("=" * 70)

    if args.dry_run:
        log("🧪 MODE DRY-RUN : aucune modification ne sera faite")

    # ── Charger les données de référence ──
    excel_path = find_excel_path()
    if not excel_path:
        log(f"❌ Fichier Excel immatriculations introuvable !")
        log(f"   Chemins testés : {EXCEL_PATH}, {_ALT_EXCEL_PATHS}")
        send_slack("❌ approve_fleet_vps : fichier Excel immatriculations introuvable", "#ff0000")
        sys.exit(1)

    immat_index = load_immatriculations_index(excel_path)
    if not immat_index:
        log("❌ Aucune immatriculation chargée depuis l'Excel")
        sys.exit(1)

    image_index = build_image_index(IMAGES_OCR_DIR)
    if not image_index:
        log(f"⚠️ Aucune image dans {IMAGES_OCR_DIR} — les véhicules seront marqués 'image manquante'")

    # ── Trouver les partenaires ──
    all_partners = find_partner_folders(ORGANIZED_DIR)
    if not all_partners:
        log(f"❌ Aucun partenaire trouvé dans {ORGANIZED_DIR}")
        send_slack(f"❌ approve_fleet_vps : aucun partenaire trouvé dans {ORGANIZED_DIR}", "#ff0000")
        sys.exit(1)

    log(f"📁 {len(all_partners)} partenaires détectés")

    # Filtres
    if args.only:
        target = normalize_partner_name(args.only)
        all_partners = [p for p in all_partners if normalize_partner_name(p.name) == target]
        if not all_partners:
            log(f"❌ Partenaire '{args.only}' non trouvé — dossiers disponibles :")
            for p in find_partner_folders(ORGANIZED_DIR)[:10]:
                log(f"   • {p.name}")
            sys.exit(1)
        log(f"🎯 Un seul partenaire ciblé : {all_partners[0].name}")

    if args.start:
        start_num = extract_partner_number(args.start)
        all_partners = [p for p in all_partners if extract_partner_number(p.name) >= start_num]
        log(f"🚀 Reprise depuis partenaire >= {args.start}")

    if args.end:
        end_num = int(re.sub(r'\D', '', args.end))
        all_partners = [p for p in all_partners if extract_partner_number(p.name) <= end_num]
        log(f"🏁 Arrêt après partenaire <= {args.end}")

    # ── Setup driver ──
    driver = None
    if not args.dry_run:
        try:
            driver = setup_driver(headed=args.headed)
        except Exception as e:
            log(f"❌ Erreur driver Chrome : {e}")
            send_slack(f"❌ approve_fleet_vps : erreur driver Chrome — {e}", "#ff0000")
            sys.exit(1)

    # ── Slack de démarrage ──
    send_slack(
        f"🚗 APPROVE FLEET démarré — {len(all_partners)} partenaires"
        + (" [DRY-RUN]" if args.dry_run else ""),
        "#439FE0"
    )

    # ── Traitement ──
    global_stats = {
        "total_partners": 0,
        "total_approved": 0,
        "total_not_found": 0,
        "total_image_missing": 0,
        "total_failed": 0,
        "total_skipped": 0,
    }
    all_results = []
    start_time = time.time()
    total_partners = len(all_partners)

    try:
        for idx, partner_dir in enumerate(all_partners, 1):
            global_stats["total_partners"] += 1
            log(f"\n   ▶️ [{idx}/{total_partners}] {partner_dir.name}")
            try:
                result = process_partner(driver, partner_dir, immat_index, image_index, dry_run=args.dry_run)
                global_stats["total_approved"]      += result["approved"]
                global_stats["total_not_found"]     += result["not_found"]
                global_stats["total_image_missing"] += result["img_missing"]
                global_stats["total_failed"]        += result["failed"]
                all_results.extend(result.get("results", []))

                # ── Slack par partenaire (détail par véhicule) ──
                has_error = result["failed"] > 0
                icon  = "⚠️" if has_error else "✅"
                color = "#ffaa00" if has_error else "#36a64f"
                partner_results = result.get("results", [])
                total_v = len(partner_results)

                lines = [f"{icon} *{partner_dir.name}* [{idx}/{total_partners}] — {total_v} véhicule(s) EN ATTENTE"]
                lines.append("")

                # Détail par véhicule
                for vr in partner_results:
                    plaque = vr.get("plate_raw", "?")
                    immat  = vr.get("immatriculation_officielle", "")
                    statut = vr.get("statut_approbation", "Non")
                    raison = vr.get("raison", "")
                    if statut == "Oui":
                        lines.append(f"  ✅ `{plaque}` → {immat} — Approuvé + carte grise uploadée")
                    elif "non trouvé" in raison.lower():
                        lines.append(f"  ❌ `{plaque}` — Matricule non trouvé dans Excel")
                    elif "image" in raison.lower():
                        lines.append(f"  ⚠️ `{plaque}` → {immat} — Image introuvable")
                    else:
                        lines.append(f"  🔴 `{plaque}` → {immat} — Erreur: {raison}")

                # Résumé chiffré
                if total_v > 0:
                    lines.append("")
                    lines.append(f"📊 ✅ {result['approved']} | ❌ {result['not_found']} | ⚠️ {result['img_missing']} | 🔴 {result['failed']}")

                if not partner_results:
                    lines.append("  ✔️ Aucun véhicule EN ATTENTE")

                send_slack("\n".join(lines), color)

            except Exception as e:
                log(f"❌ Erreur critique sur {partner_dir.name}: {e}")
                traceback.print_exc()
                global_stats["total_failed"] += 1
                send_slack(f"💥 *{partner_dir.name}* [{idx}/{total_partners}] — erreur critique : {str(e).splitlines()[0]}", "#ff0000")

    except KeyboardInterrupt:
        log("\n🛑 Interrompu par l'utilisateur")

    finally:
        if driver:
            driver.quit()

    # ── Résumé ──
    duration = time.time() - start_time
    log("\n" + "=" * 70)
    log(f"📊 RÉSUMÉ FINAL — {duration/60:.1f} min")
    log("=" * 70)
    log(f"📂 Partenaires traités  : {global_stats['total_partners']}")
    log(f"✅ Approuvés            : {global_stats['total_approved']}")
    log(f"❌ Matricule non trouvé : {global_stats['total_not_found']}")
    log(f"⚠️ Image manquante      : {global_stats['total_image_missing']}")
    log(f"🔴 Erreurs              : {global_stats['total_failed']}")
    log("=" * 70)

    # ── Rapport HTML ──
    if all_results:
        generate_html_kpi_report(all_results, global_stats, HTML_REPORT_PATH)

    # ── Slack final avec durée ──
    total_v = (global_stats["total_approved"] + global_stats["total_not_found"]
               + global_stats["total_image_missing"] + global_stats["total_failed"])
    failed = global_stats["total_failed"]
    color_final = "#36a64f" if failed == 0 else "#ff0000"
    status_final = "✅ APPROBATION TERMINÉE" if failed == 0 else "❌ APPROBATION AVEC ERREURS"
    send_slack(
        f"{status_final} en {duration/60:.1f} min\n\n"
        f"📊 *Résumé Approbation Carte Grise*\n"
        f"• Partenaires traités : {global_stats['total_partners']}\n"
        f"• Véhicules EN ATTENTE traités : {total_v}\n"
        f"• ✅ Approuvés avec succès : {global_stats['total_approved']}\n"
        f"• ❌ Matricule non trouvé : {global_stats['total_not_found']}\n"
        f"• ⚠️ Image manquante : {global_stats['total_image_missing']}\n"
        f"• 🔴 Erreurs : {failed}\n"
        f"📄 Rapport HTML : {HTML_REPORT_PATH}",
        color_final
    )

    sys.exit(0 if global_stats["total_failed"] == 0 else 1)


if __name__ == "__main__":
    main()
