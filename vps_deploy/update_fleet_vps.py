#!/usr/bin/env python3
"""
update_fleet_vps.py
===================
Synchronisation complète de la flotte de chaque partenaire.

Pour chaque partenaire :
  1. Login → pagination 500
  2. Scrape la flotte en ligne (type, marque, modèle, matricule)
  3. DÉDUP   : détecte les doublons (clé = type+marque+modèle+matricule normalisés)
               → supprime les N-1 copies, garde 1 exemplaire
  4. SYNC    : compare flotte en ligne (après dédup) vs data.json
               → en ligne mais ABSENT du JSON        → SUPPRIMER
               → dans JSON mais ABSENT de la flotte  → CRÉER
  5. Logout → rapport Slack

Source : output/partenaire_drivers_scrape/*_drivers.json
Clé de comparaison : type+marque+modèle+matricule (tous normalisés)

Usage :
  python3 update_fleet_vps.py                         # tous les partenaires
  python3 update_fleet_vps.py --only partenaire1      # un seul
  python3 update_fleet_vps.py --start partenaire-10   # reprendre depuis
  python3 update_fleet_vps.py --end partenaire-50     # s'arrêter à
  python3 update_fleet_vps.py --dedup-only            # juste dédupliquer
  python3 update_fleet_vps.py --dry-run               # simulation (aucune modification)

Background VPS :
  nohup python3 update_fleet_vps.py > logs/update_fleet.log 2>&1 &
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
from collections import Counter
from datetime import datetime
from pathlib import Path

import openpyxl

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR          = Path(__file__).parent
SCRAPE_DIR        = BASE_DIR / "output" / "partenaire_drivers_scrape"
LOG_FILE          = BASE_DIR / "output" / "update_fleet.log"
PARTNER_CREDS_XLS = BASE_DIR / "DOSSIER_PARTENAIRES.xlsx"
PARTNER_CREDS_XLS_FALLBACK = BASE_DIR / "output" / "DOSSIER_PARTENAIRES.xlsx"

BASE_URL          = "https://upjunoo-server-new.junooapps.com"
OWNER_LOGIN_URL   = f"{BASE_URL}/login/owner-login"
MANAGE_FLEET_URL  = f"{BASE_URL}/manage-fleet"
CREATE_FLEET_URL  = f"{BASE_URL}/manage-fleet/create"

UNIVERSAL_PASSWORD = "123456789@"
WEBHOOK_URL        = os.getenv("WEBHOOK_URL", "")


def _load_partner_credentials() -> dict:
    """Charge partenaire_num -> {email, password} depuis DOSSIER_PARTENAIRES.xlsx."""
    table = {}
    xls_path = PARTNER_CREDS_XLS if PARTNER_CREDS_XLS.exists() else PARTNER_CREDS_XLS_FALLBACK
    if not xls_path.exists():
        return table
    try:
        wb = openpyxl.load_workbook(xls_path, read_only=True, data_only=True)
        ws = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            # Ignorer l'en-tête
            if i == 1:
                continue
            num, email, password = (list(row) + [None, None, None])[:3]
            if num is None or email is None:
                continue
            try:
                n = int(num)
            except (ValueError, TypeError):
                continue
            email = str(email).strip()
            if email and "@" in email:
                pwd = str(password).strip() if password is not None else ""
                table[n] = {"email": email, "password": pwd}
    except Exception as e:
        print(f"⚠️ Impossible de lire {xls_path}: {e}")
    return table


PARTNER_CREDENTIALS_MAP: dict = _load_partner_credentials()
PARTNER_EMAIL_MAP: dict = {k: v.get("email", "") for k, v in PARTNER_CREDENTIALS_MAP.items()}

PARTNER_NUM_RE = re.compile(r'(?:partenaires?[-_]?\s*)(\d+)', re.I)
PARTNER_DIR_RE = re.compile(r'^\s*partenaires?[-_]?\s*(\d+)\s*$', re.I)

# Colonnes du tableau /manage-fleet
COL_TYPE   = 0
COL_MARQUE = 1
COL_MODELE = 2
COL_PLAQUE = 4

TYPE_UUID_MAP = {
    "CONFORT":        "0d1802c4-3d32-4a96-b3ca-73e650802c62",
    "Camionnette":    "15f90aaa-aa92-40ed-b34e-ce7e51541b7e",
    "MOTO":           "35a673c3-aafe-48b4-8ae8-205e238b043b",
    "Taxi France":    "4644788a-1065-4eb9-bbf6-01a6e394aeed",
    "ECO":            "58eb223b-5ac7-4ed5-9a12-87d24f901dda",
    "moto livraison": "5f4ef87b-1be7-468d-8140-7379fefbaedf",
    "Camion 14T":     "64d5d311-1f7c-42eb-b0ad-510d9af8cd54",
    "PREMIUM":        "91ccc713-b07f-4971-b5c4-1d1c755c9d3a",
    "Camion":         "95ad84fc-df36-48f7-8c69-e8bb51ad5f8d",
    "CONFORT+":       "990a6e02-ac3d-4354-bccc-eedafb77de71",
    "CARGO":          "c9a337de-fc81-4626-a5f9-2ac7ac1b5e03",
    "CONFORT Lyon":   "dce302c1-c109-4023-9d9d-17b9da8c424c",
    "Semi-remorque":  "e17983aa-af38-4ffc-b88c-37adc3f77dcd",
}

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  LOG + SLACK
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def send_slack(msg: str, color: str = "#36a64f"):
    if not WEBHOOK_URL:
        return
    try:
        payload = json.dumps({
            "username": os.getenv("SLACK_BOT_NAME", "UpJunoo Bot"),
            "icon_emoji": os.getenv("SLACK_ICON_EMOJI", ":car:"),
            "attachments": [{"color": color, "text": msg}]
        }).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"⚠️ Slack erreur: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def norm_str(s: str) -> str:
    """Normalise une chaîne : majuscules, sans espaces/tirets/points."""
    return re.sub(r'[\s\-_./]', '', (s or "").strip().upper())


def make_vehicle_key(type_v: str, marque: str, modele: str, plaque: str) -> tuple:
    """Clé de matching = matricule normalisé uniquement (marque/modèle souvent N/A dans le JSON)."""
    return (norm_str(plaque),)


def derive_email(folder_name: str) -> str:
    m = PARTNER_DIR_RE.match(folder_name or "")
    if not m:
        return ""
    n = int(m.group(1))
    if n in PARTNER_EMAIL_MAP:
        return PARTNER_EMAIL_MAP[n]
    prefix = "partenaires" if 51 <= n <= 100 else "partenaire"
    return f"{prefix}{n}@upjunoo.com"


def partner_num(folder_name: str) -> int:
    m = PARTNER_NUM_RE.search(folder_name or "")
    return int(m.group(1)) if m else 9999


def is_valid_matricule(mat: str) -> bool:
    """Retourne True si le matricule est utilisable (pas vide/N/A/X*)."""
    if not mat or not mat.strip():
        return False
    n = norm_str(mat)
    if n in ("NA", "N/A", "NULL", "NONE", "", "XXXXX", "XXX", "XX", "X", "00000", "0"):
        return False
    if re.fullmatch(r'X+', n):
        return False
    return True


def is_valid_type(t: str) -> bool:
    if not t or not t.strip():
        return False
    n = t.strip().upper()
    return n not in ("N/A", "NA", "", "NULL", "NONE")


def resolve_type(vtype: str, transport_hint: str = "") -> str:
    """
    Retourne un type valide présent dans TYPE_UUID_MAP.
    - Si vtype est déjà valide, on le retourne tel quel.
    - Sinon on devine depuis vtype ou transport_hint :
        - Contient 'moto' / 'livraison'  → MOTO
        - Tout le reste (taxi, confort…)  → ECO
    """
    if vtype and vtype.strip() and vtype.strip() in TYPE_UUID_MAP:
        return vtype.strip()
    hint = (vtype + " " + transport_hint).lower()
    if "moto" in hint or "livraison" in hint:
        return "MOTO"
    return "ECO"


# ─────────────────────────────────────────────────────────────────────────────
#  DÉCOUVRIR LES PARTENAIRES
# ─────────────────────────────────────────────────────────────────────────────

def load_partners_scrape(scrape_dir: Path) -> list:
    """
    Charge les partenaires depuis output/partenaire_drivers_scrape/.
    Traduit la structure conducteurs[]{Nom, vehicle} → drivers[]{nom, vehicle}.
    """

    partners = []
    for json_path in sorted(scrape_dir.glob("*_drivers.json")):
        m = re.search(r'Partenaire-?(\d+)_', json_path.name, re.IGNORECASE)
        if not m:
            continue
        num = int(m.group(1))
        folder_name = f"partenaire-{num}"
        creds = PARTNER_CREDENTIALS_MAP.get(num, {})
        email = creds.get("email") or (
            f"partenaires{num}@upjunoo.com" if 51 <= num <= 100
            else f"partenaire{num}@upjunoo.com"
        )
        password = creds.get("password", "")

        try:
            with open(json_path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            log(f"⚠️ Erreur lecture {json_path.name}: {e}")
            continue

        # Traduire conducteurs[] → drivers[]
        drivers = []
        for c in raw.get("conducteurs", []):
            vehicle = c.get("vehicle") or {}
            transport = c.get("Type de transport", "")
            drivers.append({
                "nom": c.get("Nom", ""),
                "transport_hint": transport,
                "vehicle": {
                    "type":      vehicle.get("type", ""),
                    "marque":    vehicle.get("marque", ""),
                    "modele":    vehicle.get("modele", ""),
                    "matricule": vehicle.get("matricule", ""),
                },
            })

        partners.append({
            "_folder": folder_name,
            "_path":   str(json_path),   # pas de sous-dossier, rapport sauvé à côté
            "email":   email,
            "password": password,
            "drivers": drivers,
        })

    partners.sort(key=lambda p: partner_num(p.get("_folder", "")))
    log(f"   📂 {len(partners)} partenaires chargés depuis partenaire_drivers_scrape/")
    return partners


# ─────────────────────────────────────────────────────────────────────────────
#  CHROME
# ─────────────────────────────────────────────────────────────────────────────

def setup_driver(headed: bool = False, debug_port: int = 9222):
    opts = Options()
    if not headed:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-features=VizDisplayCompositor")
    if not headed:
        opts.add_argument(f"--remote-debugging-port={debug_port}")
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    chrome_binaries = [
        # Linux
        "chromium-browser", "/snap/bin/chromium", "chromium",
        "google-chrome-stable", "google-chrome",
        "/usr/bin/google-chrome", "/usr/bin/chromium",
        # Windows (Chrome)
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for binary in chrome_binaries:
        path = binary if os.path.isfile(binary) else shutil.which(binary)
        if path:
            opts.binary_location = path
            log(f"✅ Chrome: {path}")
            break

    # 1) Essai prioritaire : Selenium Manager (auto-résolution du driver, utile sur Windows)
    try:
        log("ℹ️ Tentative Selenium Manager (driver auto)")
        return webdriver.Chrome(options=opts)
    except Exception as e:
        log(f"⚠️ Selenium Manager indisponible: {str(e).splitlines()[0]}")

    # 2) Fallback : driver explicite
    for cd in [
        # Linux
        "/usr/local/bin/chromedriver", "/usr/bin/chromedriver",
        "/snap/bin/chromium.chromedriver", "chromedriver",
        # Windows
        "chromedriver.exe",
        r"C:\Program Files\ChromeDriver\chromedriver.exe",
    ]:
        found = cd if os.path.isfile(cd) else shutil.which(cd)
        if found:
            log(f"✅ ChromeDriver: {found}")
            return webdriver.Chrome(service=Service(found), options=opts)

    raise RuntimeError(
        "ChromeDriver introuvable. Sous Windows, installe Chrome + ChromeDriver (même version majeure) "
        "ou laisse Selenium Manager télécharger automatiquement le driver."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────────────────────

def login(driver, email: str, password: str = "") -> bool:
    pwd_to_use = password or UNIVERSAL_PASSWORD
    for attempt in range(1, 4):
        try:
            driver.delete_all_cookies()
            driver.get(OWNER_LOGIN_URL)
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[type='email'],input[type='text']")
                )
            )
            time.sleep(0.5)
            em = driver.find_element(By.CSS_SELECTOR, "input[type='email'],input[type='text']")
            pw = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            em.clear(); em.send_keys(email)
            pw.clear(); pw.send_keys(pwd_to_use)
            btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            driver.execute_script("arguments[0].click();", btn)
            WebDriverWait(driver, 30).until(EC.url_contains("/owner-dashboard"))
            log(f"   ✅ Connecté : {email}")
            return True
        except Exception as e:
            log(f"   ⚠️ Login tentative {attempt}/3 : {str(e).splitlines()[0]}")
            time.sleep(3 * attempt)
    log(f"   ❌ Login échoué : {email}")
    return False


def logout(driver):
    try:
        driver.get(f"{BASE_URL}/logout")
        time.sleep(1)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  PAGINATION
# ─────────────────────────────────────────────────────────────────────────────

def set_pagination_500(driver) -> bool:
    """
    Force la pagination à 500 avec plusieurs tentatives.
    Retourne True si la pagination est appliquée, sinon False.
    """
    for attempt in range(3):
        try:
            log(f"   🔄 Pagination 500 tentative {attempt + 1}/3...")
            try:
                sel_el = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "select.form-select.form-select-sm.w-auto, "
                    "select[name='DataTables_Table_0_length'], "
                    "select[data-dt-idx='0']"
                )))
            except Exception:
                sel_el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR, ".dataTables_length select, select.form-select, select"
                )))

            select_obj = Select(sel_el)
            options = [o.text.strip() for o in select_obj.options]
            target = next((o for o in options if "500" in o), None)
            if not target:
                log(f"   ⚠️ Option 500 non trouvée dans {options}")
                time.sleep(1.5)
                continue

            select_obj.select_by_visible_text(target)
            log(f"   ✅ Pagination '{target}' sélectionnée")
            time.sleep(2)

            # Attendre que le tableau se stabilise après changement de pagination
            start = time.time()
            last_count = -1
            stable_rounds = 0
            while time.time() - start < 30:
                time.sleep(1)
                count = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
                if count == last_count and count >= 0:
                    stable_rounds += 1
                    if stable_rounds >= 3:
                        log(f"   ✅ Tableau stable après pagination: {count} ligne(s)")
                        return True
                else:
                    stable_rounds = 0
                    last_count = count

            # Même sans stabilité stricte, on considère la pagination appliquée
            if last_count >= 0:
                log(f"   ✅ Pagination appliquée, {last_count} ligne(s) détectée(s)")
                return True

        except Exception as e:
            log(f"   ⚠️ Échec pagination tentative {attempt + 1}: {str(e).splitlines()[0]}")
            time.sleep(2)
            if attempt < 2:
                try:
                    driver.refresh()
                    time.sleep(2)
                except Exception:
                    pass

    return False


def load_fleet_page(driver):
    """Charge /manage-fleet et force pagination à 500 avant toute action."""
    driver.get(MANAGE_FLEET_URL)
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
        )
    except TimeoutException:
        pass
    time.sleep(0.5)
    ok = set_pagination_500(driver)
    if not ok:
        log("   ⚠️ Impossible de forcer la pagination à 500 (on continue)")
    time.sleep(0.5)


def get_consistent_online_fleet(driver, attempts: int = 3, min_rows: int = 0) -> list:
    """
    Re-scrape plusieurs fois la flotte et retourne la version la plus fiable.
    - Retour immédiat si 2 scrapes consécutifs ont le même volume (>= min_rows)
    - Sinon retourne le snapshot le plus volumineux (garde-fou contre pages partielles)
    """
    best_rows = []
    counts = []
    for i in range(1, attempts + 1):
        load_fleet_page(driver)
        rows = scrape_fleet(driver)
        count = len(rows)
        counts.append(count)
        if count > len(best_rows):
            best_rows = rows
        log(f"   🔎 Recheck flotte {i}/{attempts}: {count} véhicule(s)")

        if len(counts) >= 2 and counts[-1] == counts[-2] and count >= min_rows:
            log(f"   ✅ Flotte stable confirmée: {count} véhicule(s)")
            return rows

    log(f"   ⚠️ Flotte instable (counts={counts}) — on garde le meilleur snapshot: {len(best_rows)}")
    return best_rows


# ─────────────────────────────────────────────────────────────────────────────
#  SCRAPE FLOTTE EN LIGNE
# ─────────────────────────────────────────────────────────────────────────────

def scrape_fleet(driver) -> list:
    """
    Scrape toutes les lignes du tableau de flotte.
    Retourne une liste de dicts : {idx, key, type, marque, modele, plaque, display}
    """
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    except Exception:
        return []

    result = []
    for idx, row in enumerate(rows):
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) <= COL_PLAQUE:
                continue
            type_v  = cells[COL_TYPE].text.strip()
            marque  = cells[COL_MARQUE].text.strip()
            modele  = cells[COL_MODELE].text.strip()
            plaque  = cells[COL_PLAQUE].text.strip()
            if not plaque:
                continue
            key = make_vehicle_key(type_v, marque, modele, plaque)
            result.append({
                "idx": idx,
                "key": key,
                "type": type_v,
                "marque": marque,
                "modele": modele,
                "plaque": plaque,
                "display": f"{type_v}|{marque}|{modele}|{plaque}",
            })
        except Exception:
            continue
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  SUPPRESSION D'UNE LIGNE
# ─────────────────────────────────────────────────────────────────────────────

def _close_swal(driver):
    try:
        driver.execute_script(
            "var b=document.querySelector('.swal2-cancel'); if(b) b.click();"
        )
    except Exception:
        pass


def delete_row_at_index(driver, row_index: int) -> bool:
    """Supprime la ligne à row_index dans le tableau courant."""
    try:
        result = driver.execute_script("""
            var rows = document.querySelectorAll('table tbody tr');
            if (!rows || arguments[0] >= rows.length) return 'no_row';
            var row = rows[arguments[0]];
            var btn = row.querySelector(
                'button.dropdown-toggle, button[data-bs-toggle="dropdown"], .btn-action, button'
            );
            if (!btn) return 'no_btn';
            if (window.bootstrap && window.bootstrap.Dropdown) {
                var dd = window.bootstrap.Dropdown.getOrCreateInstance(btn);
                dd.show();
            } else { btn.click(); }
            return new Promise(function(resolve) {
                setTimeout(function() {
                    var menu = row.querySelector('.dropdown-menu.show, .dropdown-menu');
                    if (!menu) { resolve('no_menu'); return; }
                    var items = menu.querySelectorAll('a, button, li');
                    for (var i = 0; i < items.length; i++) {
                        var t = items[i].textContent.trim().toLowerCase();
                        if (t.includes('supprimer') || t.includes('delete') || t.includes('retirer')) {
                            items[i].click();
                            resolve('clicked');
                            return;
                        }
                    }
                    resolve('no_supprimer');
                }, 400);
            });
        """, row_index)

        if result != "clicked":
            log(f"      ⚠️ delete [{row_index}] : {result}")
            return False

        confirm = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
        )
        time.sleep(0.2)
        driver.execute_script("arguments[0].click();", confirm)

        time.sleep(0.5)
        try:
            ok = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
            )
            driver.execute_script("arguments[0].click();", ok)
        except TimeoutException:
            pass

        end = time.time() + 4
        while time.time() < end:
            if not driver.find_elements(By.CSS_SELECTOR, ".swal2-container .swal2-popup"):
                break
            time.sleep(0.2)

        time.sleep(0.3)
        return True

    except Exception as e:
        log(f"      ❌ Erreur suppression [{row_index}]: {str(e).splitlines()[0]}")
        _close_swal(driver)
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  PHASE 1 : DÉDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────

def dedup_fleet(driver, partner_name: str, dry_run: bool = False) -> dict:
    """
    Détecte et supprime les doublons sur la flotte en ligne.
    Doublon = même (type+marque+modèle+matricule) normalisés.
    Garde le premier exemplaire, supprime les suivants.
    Retourne : {total, duplicates_found, deleted, failed}
    """
    stats = {
        "total": 0,
        "invalid_found": 0,
        "invalid_deleted": 0,
        "duplicates_found": 0,
        "deleted": 0,
        "failed": 0,
    }

    rows = get_consistent_online_fleet(driver, attempts=3, min_rows=1)
    stats["total"] = len(rows)

    if not rows:
        log(f"   ℹ️ Flotte vide — aucune dédup nécessaire")
        return stats

    # 0) Nettoyage des lignes avec immatriculation invalide (NA/N/A/vide/etc.)
    invalid_rows = [r for r in rows if not is_valid_matricule(r["plaque"])]
    stats["invalid_found"] = len(invalid_rows)
    if invalid_rows:
        log(f"   🧹 {len(invalid_rows)} immatriculation(s) invalide(s) détectée(s) à supprimer")
        if dry_run:
            log(f"   🧪 [DRY-RUN] {len(invalid_rows)} immatriculation(s) invalide(s) seraient supprimées")
            stats["invalid_deleted"] = len(invalid_rows)
            stats["deleted"] += len(invalid_rows)
        else:
            max_passes_invalid = 10
            for pass_num in range(1, max_passes_invalid + 1):
                load_fleet_page(driver)
                current_rows = scrape_fleet(driver)
                targets = [r for r in current_rows if not is_valid_matricule(r["plaque"])]
                if not targets:
                    log(f"   ✅ Nettoyage immatriculations invalides terminé (pass {pass_num})")
                    break
                targets.sort(key=lambda r: r["idx"], reverse=True)
                log(f"   🗑️  Pass {pass_num} (invalides) : {len(targets)} à supprimer")
                pass_deleted = 0
                consecutive_fails = 0
                for item in targets:
                    ok = delete_row_at_index(driver, item["idx"])
                    if ok:
                        pass_deleted += 1
                        stats["invalid_deleted"] += 1
                        stats["deleted"] += 1
                        consecutive_fails = 0
                    else:
                        stats["failed"] += 1
                        consecutive_fails += 1
                        if consecutive_fails >= 5:
                            log(f"   ❌ 5 échecs consécutifs — refresh forcé")
                            break
                log(f"      Pass {pass_num} (invalides) : {pass_deleted} supprimé(s)")
            else:
                log(f"   ⚠️ Nettoyage invalides arrêté après {max_passes_invalid} passes (sécurité)")

    # Re-scraper après nettoyage des invalides avant de traiter les doublons
    rows = get_consistent_online_fleet(driver, attempts=3, min_rows=max(stats["total"], 1))
    stats["total"] = len(rows)

    key_counts = Counter(r["key"] for r in rows)
    dups_to_del = [r for r in rows if key_counts[r["key"]] > 1]

    # Parmi les dups, garder seulement le premier de chaque clé → supprimer les suivants
    seen = set()
    to_delete = []
    for r in rows:
        key = r["key"]
        if key in seen and key_counts[key] > 1:
            to_delete.append(r)
        else:
            seen.add(key)

    stats["duplicates_found"] = len(to_delete)

    if not to_delete:
        log(f"   ✅ Aucun doublon détecté ({len(rows)} véhicules)")
        return stats

    unique_dup_keys = sum(1 for c in key_counts.values() if c > 1)
    log(f"   🔍 {len(to_delete)} doublon(s) à supprimer ({unique_dup_keys} véhicule(s) concernés)")
    for key, count in key_counts.most_common():
        if count > 1:
            key_display = "|".join(str(part) for part in key) if isinstance(key, tuple) else str(key)
            log(f"      🔁 x{count} : {key_display}")

    if dry_run:
        log(f"   🧪 [DRY-RUN] {len(to_delete)} doublons seraient supprimés")
        stats["deleted"] = len(to_delete)
        return stats

    # Supprimer en plusieurs passes (refresh entre chaque passe car les index changent)
    max_passes = 15
    for pass_num in range(1, max_passes + 1):
        rows = get_consistent_online_fleet(driver, attempts=2, min_rows=max(stats["total"], 1))

        seen = set()
        to_delete_now = []
        for r in rows:
            key = r["key"]
            if key in seen:
                to_delete_now.append(r)
            else:
                seen.add(key)

        if not to_delete_now:
            log(f"   ✅ Dédup terminée — pass {pass_num} : plus de doublons")
            break

        # Supprimer de bas en haut (index décroissant = pas de décalage)
        to_delete_now.sort(key=lambda r: r["idx"], reverse=True)
        log(f"   🗑️  Pass {pass_num} : {len(to_delete_now)} doublon(s) à supprimer")

        pass_deleted = 0
        consecutive_fails = 0
        for item in to_delete_now:
            ok = delete_row_at_index(driver, item["idx"])
            if ok:
                pass_deleted += 1
                stats["deleted"] += 1
                consecutive_fails = 0
            else:
                stats["failed"] += 1
                consecutive_fails += 1
                if consecutive_fails >= 5:
                    log(f"   ❌ 5 échecs consécutifs — refresh forcé")
                    break

        log(f"      Pass {pass_num} : {pass_deleted} supprimé(s)")
    else:
        log(f"   ⚠️ Dédup arrêtée après {max_passes} passes (sécurité)")

    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  PHASE 2 : CRÉATION DES VÉHICULES MANQUANTS
# ─────────────────────────────────────────────────────────────────────────────

def _select_vehicle_type(driver, select_el, vehicle_type: str) -> bool:
    target = str(vehicle_type).strip().upper()
    normalized_map = {k.upper(): v for k, v in TYPE_UUID_MAP.items()}

    try:
        sel_obj = Select(select_el)
        real_options = [o for o in sel_obj.options if o.get_attribute("value")]

        if real_options:
            for opt in sel_obj.options:
                if opt.text.strip().upper() == target:
                    sel_obj.select_by_visible_text(opt.text)
                    return True

        uuid = normalized_map.get(target)
        if not uuid:
            log(f"      ❌ Type '{vehicle_type}' inconnu dans TYPE_UUID_MAP")
            return False

        driver.execute_script(
            """
            var s = arguments[0];
            var o = document.createElement('option');
            o.value = arguments[1]; o.text = arguments[2];
            s.appendChild(o); s.value = arguments[1];
            s.dispatchEvent(new Event('change', {bubbles: true}));
            """,
            select_el, uuid, vehicle_type
        )
        return True

    except Exception as e:
        log(f"      ⚠️ Erreur select type '{vehicle_type}': {e}")
        return False


def create_vehicle(driver, vehicle_data: dict, dry_run: bool = False) -> bool:
    """Crée un véhicule via /manage-fleet/create. Retourne True si OK."""
    vehicle   = vehicle_data.get("vehicle", {}) or {}
    vtype     = vehicle.get("type", "")
    marque    = vehicle.get("marque", "N/A")
    modele    = vehicle.get("modele", "N/A")
    matricule = vehicle.get("matricule", "N/A")
    nom       = vehicle_data.get("nom", "?")

    vtype = resolve_type(vtype, vehicle_data.get("transport_hint", ""))

    # Nettoyer marque/modele corrompus (plaque ou texte parasite)
    PLAQUE_RE = re.compile(r'^[A-Z]{2}-\d{3}-[A-Z]{2}', re.IGNORECASE)
    if not marque or marque in ("N/A", "") or PLAQUE_RE.match(marque) or marque == "Profil du conducteur":
        marque = "N/A"
    if not modele or modele in ("N/A", "") or modele == "Profil du conducteur":
        modele = "N/A"
    if not matricule or matricule.strip() == "":
        matricule = "N/A"

    if dry_run:
        log(f"      [DRY-RUN] Créerait : {vtype} | {marque} | {modele} | {matricule}")
        return True

    wait = WebDriverWait(driver, 30)
    driver.get(CREATE_FLEET_URL)
    time.sleep(1.5)

    try:
        select_el = wait.until(EC.presence_of_element_located((By.ID, "select_type")))
        if not _select_vehicle_type(driver, select_el, vtype):
            return False

        brand_input = wait.until(EC.presence_of_element_located((By.ID, "car_brand")))
        brand_input.clear()
        brand_input.send_keys(marque if marque and marque != "N/A" else "Non spécifié")

        model_input = wait.until(EC.presence_of_element_located((By.ID, "car_model")))
        model_input.clear()
        model_input.send_keys(modele if modele and modele != "N/A" else "Non spécifié")

        plate_input = wait.until(EC.presence_of_element_located((By.ID, "license_plate_number")))
        plate_input.clear()
        plate_input.send_keys(matricule)

        color_input = wait.until(EC.presence_of_element_located((By.ID, "car_color")))
        color_input.clear()
        color_input.send_keys("Noir")

        time.sleep(0.5)
        save_btn = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.btn.btn-primary[type='submit']")
        ))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", save_btn)
        time.sleep(0.3)
        save_btn.click()
        time.sleep(3)

        if "create" not in driver.current_url:
            log(f"      ✅ Créé : {vtype} | {marque} | {matricule}")
            return True
        else:
            log(f"      ⚠️ Échec création (resté sur /create) : {matricule}")
            return False

    except Exception as e:
        log(f"      ❌ Erreur création {matricule}: {str(e).splitlines()[0]}")
        return False


def sync_fleet(driver, json_drivers: list, online_fleet: list, dry_run: bool = False) -> dict:
    """
    Synchronisation bidirectionnelle flotte en ligne ↔ data.json.
    Clé de comparaison = (type+marque+modèle+matricule) normalisés.

    - En ligne mais ABSENT du JSON  → SUPPRIMER
    - Dans JSON mais ABSENT en ligne → CRÉER

    Retourne : {to_delete, deleted, del_failed, to_add, added, add_failed, skipped,
                deleted_list, added_list, failed_list}
    """
    stats = {
        "to_delete": 0, "deleted": 0, "del_failed": 0,
        "to_add": 0,    "added": 0,  "add_failed": 0,
        "skipped": 0,
        "deleted_list": [],   # [{type, marque, modele, plaque}]
        "added_list": [],     # [{nom, type, marque, modele, matricule, status}]
        "failed_list": [],    # [{nom, type, marque, modele, matricule, status, raison}]
        "dupjson_list": [],   # [{nom, type, marque, modele, matricule, status, doublon_de}]
        "dupsite_list": [],   # [{type, marque, modele, matricule, status}]
    }

    # ── Construire le référentiel JSON (dédupliqué) ────────────────────────
    seen_json_keys = {}   # key -> premier nom rencontré
    json_keys = set()
    valid_json_drivers = []

    for d in json_drivers:
        vehicle   = d.get("vehicle", {}) or {}
        vtype     = vehicle.get("type", "")
        marque    = vehicle.get("marque", "")
        modele    = vehicle.get("modele", "")
        matricule = vehicle.get("matricule", "")
        nom       = d.get("nom", "?")

        if not vtype and not d.get("transport_hint"):
            stats["skipped"] += 1
            continue
        vtype = resolve_type(vtype, d.get("transport_hint", ""))

        if not is_valid_matricule(matricule):
            log(f"      ⚠️ JSON ignoré (immat invalide) : {nom} → {matricule}")
            stats["skipped"] += 1
            continue

        # Nettoyer marque/modele corrompus
        PLAQUE_RE = re.compile(r'^[A-Z]{2}-\d{3}-[A-Z]{2}', re.IGNORECASE)
        if PLAQUE_RE.match(marque) or marque == "Profil du conducteur":
            marque = "N/A"
        if modele == "Profil du conducteur":
            modele = "N/A"

        key = make_vehicle_key(vtype, marque, modele, matricule)
        if key in seen_json_keys:
            # Doublon JSON : garder 1, noter le second avec statut DOUBLON_JSON
            premier = seen_json_keys[key]
            log(f"      🔁 Doublon JSON : {nom} → {matricule} (même clé que '{premier}')")
            stats["dupjson_list"].append({
                "nom": nom, "type": vtype, "marque": marque,
                "modele": modele, "matricule": matricule,
                "status": "DOUBLON_JSON",
                "doublon_de": premier,
            })
            stats["skipped"] += 1
            continue
        seen_json_keys[key] = nom
        json_keys.add(key)
        valid_json_drivers.append((key, d))

    online_keys = set(r["key"] for r in online_fleet)
    online_key_counts = Counter(r["key"] for r in online_fleet)
    duplicate_online_keys = {k for k, c in online_key_counts.items() if c > 1}
    if duplicate_online_keys:
        for row in online_fleet:
            if row["key"] in duplicate_online_keys:
                stats["dupsite_list"].append({
                    "type": row["type"],
                    "marque": row["marque"],
                    "modele": row["modele"],
                    "matricule": row["plaque"],
                    "status": "DOUBLON_SITE",
                })

    # ── Calculer les écarts ──────────────────────────────────────────────────
    to_remove_keys = online_keys - json_keys        # en ligne mais absent du JSON
    to_create_entries = [(k, d) for k, d in valid_json_drivers if k not in online_keys]

    to_remove_rows = [r for r in online_fleet if r["key"] in to_remove_keys]
    stats["to_delete"] = len(to_remove_rows)
    stats["to_add"]    = len(to_create_entries)

    log(f"   📊 En ligne : {len(online_fleet)} | JSON valide : {len(json_keys)} | "
        f"À supprimer : {len(to_remove_rows)} | À créer : {len(to_create_entries)}")

    # ── MATCHING détaillé (logs) ──────────────────────────────────────────────
    def _key_display(key: tuple) -> str:
        if isinstance(key, tuple) and key:
            return key[0]
        return str(key)

    json_only_keys = [k for k, _ in to_create_entries]
    log("   🔎 MATCHING DÉTAILLÉ")
    log(f"      • Site uniquement     : {len(to_remove_keys)}")
    log(f"      • JSON uniquement     : {len(json_only_keys)}")
    log(f"      • Doublons sur site   : {len(duplicate_online_keys)} clé(s)")
    log(f"      • Doublons dans JSON  : {len(stats['dupjson_list'])}")

    def _log_key_block(title: str, keys: list, limit: int = 25):
        if not keys:
            log(f"      {title}: aucun")
            return
        log(f"      {title}:")
        for k in keys[:limit]:
            log(f"         - {_key_display(k)}")
        if len(keys) > limit:
            log(f"         ... +{len(keys) - limit} autre(s)")

    _log_key_block("SITE_ONLY (sur site, absent JSON)", sorted(to_remove_keys), limit=25)
    _log_key_block("JSON_ONLY (dans JSON, absent site)", sorted(json_only_keys), limit=25)
    _log_key_block("DUP_SITE (clés dupliquées côté site)", sorted(duplicate_online_keys), limit=25)

    # ── ÉTAPE A : Supprimer les véhicules hors-JSON ──────────────────────────
    if to_remove_rows:
        log(f"   🗑️  Suppression de {len(to_remove_rows)} véhicule(s) hors-JSON")
        for r in to_remove_rows:
            log(f"      - {r['display']}")

        if not dry_run:
            keys_to_remove = set(r["key"] for r in to_remove_rows)
            deleted_keys = set()
            max_passes = 20
            for pass_num in range(1, max_passes + 1):
                load_fleet_page(driver)
                current_rows = scrape_fleet(driver)
                targets = [r for r in current_rows if r["key"] in keys_to_remove]
                if not targets:
                    log(f"   ✅ Tous les véhicules hors-JSON supprimés (pass {pass_num})")
                    break
                targets.sort(key=lambda r: r["idx"], reverse=True)
                log(f"   🗑️  Pass {pass_num} : {len(targets)} à supprimer")
                pass_del = 0
                consecutive_fails = 0
                for item in targets:
                    ok = delete_row_at_index(driver, item["idx"])
                    if ok:
                        pass_del += 1
                        stats["deleted"] += 1
                        consecutive_fails = 0
                        if item["key"] not in deleted_keys:
                            deleted_keys.add(item["key"])
                            stats["deleted_list"].append({
                                "type": item["type"], "marque": item["marque"],
                                "modele": item["modele"], "matricule": item["plaque"],
                                "status": "SUPPRIME_HORS_JSON",
                            })
                    else:
                        stats["del_failed"] += 1
                        consecutive_fails += 1
                        if consecutive_fails >= 5:
                            log(f"   ❌ 5 échecs consécutifs — refresh forcé")
                            break
                log(f"      Pass {pass_num} : {pass_del} supprimé(s)")
            else:
                log(f"   ⚠️ Arrêt après {max_passes} passes (sécurité)")
        else:
            log(f"   🧪 [DRY-RUN] {len(to_remove_rows)} suppressions simulées")
            stats["deleted"] = len(to_remove_rows)
            for r in to_remove_rows:
                stats["deleted_list"].append({
                    "type": r["type"], "marque": r["marque"],
                    "modele": r["modele"], "matricule": r["plaque"],
                    "status": "SUPPRIME_HORS_JSON",
                })
    else:
        log(f"   ✅ Aucun véhicule hors-JSON à supprimer")

    # ── ÉTAPE B : Créer les véhicules manquants ──────────────────────────────
    if to_create_entries:
        # Revalidation forte juste avant création pour éviter les faux "manquants"
        rechecked_rows = get_consistent_online_fleet(
            driver,
            attempts=3,
            min_rows=max(len(online_fleet), 1),
        )
        rechecked_keys = set(r["key"] for r in rechecked_rows)
        before_filter = len(to_create_entries)
        to_create_entries = [(k, d) for (k, d) in to_create_entries if k not in rechecked_keys]
        filtered_now_present = before_filter - len(to_create_entries)
        if filtered_now_present:
            log(f"   ✅ Revalidation: {filtered_now_present} véhicule(s) déjà présents retirés de la création")

        log(f"   ➕ Création de {len(to_create_entries)} véhicule(s) manquant(s)")
        current_online_keys = set(rechecked_keys)
        for i, (key, d) in enumerate(to_create_entries, 1):
            # Recheck périodique pour éviter les créations en double si l'UI était partielle
            if (i == 1 or (i - 1) % 10 == 0) and not dry_run:
                refreshed_rows = get_consistent_online_fleet(
                    driver,
                    attempts=2,
                    min_rows=max(len(current_online_keys), 1),
                )
                current_online_keys = set(r["key"] for r in refreshed_rows)

            if key in current_online_keys:
                vehicle = d.get("vehicle", {}) or {}
                mat = vehicle.get("matricule", "?")
                nom = d.get("nom", "?")
                log(f"      ⏭️ Déjà présent, création ignorée : {nom} → {mat}")
                stats["skipped"] += 1
                continue

            vehicle = d.get("vehicle", {}) or {}
            mat    = vehicle.get("matricule", "?")
            nom    = d.get("nom", "?")
            vtype  = vehicle.get("type", "?")
            marque = vehicle.get("marque", "?")
            modele = vehicle.get("modele", "?")
            log(f"   [{i}/{len(to_create_entries)}] {nom} → {mat}")
            ok = create_vehicle(driver, d, dry_run=dry_run)
            if ok:
                stats["added"] += 1
                current_online_keys.add(key)
                stats["added_list"].append({
                    "nom": nom, "type": vtype, "marque": marque,
                    "modele": modele, "matricule": mat, "status": "CREE",
                })
            else:
                stats["add_failed"] += 1
                stats["failed_list"].append({
                    "nom": nom, "type": vtype, "marque": marque,
                    "modele": modele, "matricule": mat,
                    "status": "ECHEC_CREATION", "raison": "Erreur lors de la création",
                })
            if not dry_run:
                time.sleep(1)
    else:
        log(f"   ✅ Aucun véhicule manquant à créer")

    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  RAPPORT JSON + EXCEL
# ─────────────────────────────────────────────────────────────────────────────

def save_update_report(partner_dir: Path, dedup_stats: dict, sync_stats: dict):
    """
    Sauvegarde le rapport horodaté JSON + Excel dans le dossier du partenaire.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    dup_deleted  = dedup_stats.get("deleted", 0)
    added_list   = sync_stats.get("added_list", [])
    deleted_list = sync_stats.get("deleted_list", [])
    failed_list  = sync_stats.get("failed_list", [])
    dupjson_list = sync_stats.get("dupjson_list", [])

    report = {
        "generated_at": datetime.now().isoformat(),
        "doublons_en_ligne_supprimes": {"count": dup_deleted},
        "vehicules_hors_json_supprimes": {
            "count": len(deleted_list),
            "vehicules": deleted_list,
        },
        "vehicules_ajoutes": {
            "count": len(added_list),
            "vehicules": added_list,
        },
        "echecs_creation": {
            "count": len(failed_list),
            "vehicules": failed_list,
        },
        "doublons_json": {
            "count": len(dupjson_list),
            "note": "Conducteurs du JSON avec un matricule déjà utilisé par un autre conducteur — 1 gardé, l'autre signalé DOUBLON_JSON",
            "vehicules": dupjson_list,
        },
    }

    json_path = partner_dir / f"update_fleet_report_{timestamp}.json"
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        log(f"   📄 Rapport JSON : {json_path.name}")
    except Exception as e:
        log(f"   ⚠️ Erreur écriture JSON : {e}")

    try:
        import pandas as pd
        excel_path = partner_dir / f"update_fleet_report_{timestamp}.xlsx"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            def _sheet(data_list, name, empty_msg):
                if data_list:
                    pd.DataFrame(data_list).to_excel(writer, sheet_name=name, index=False)
                else:
                    pd.DataFrame({"Info": [empty_msg]}).to_excel(writer, sheet_name=name, index=False)

            _sheet(added_list,   "Vehicules_Ajoutes",   "Aucun véhicule ajouté")
            _sheet(deleted_list, "Supprimes_HorsJSON",  "Aucun véhicule supprimé")
            _sheet(failed_list,  "Echecs_Creation",     "Aucun échec")
            _sheet(dupjson_list, "Doublons_JSON",       "Aucun doublon JSON")
            pd.DataFrame([{
                "Doublons en ligne supprimés": dup_deleted,
                "Hors-JSON supprimés": len(deleted_list),
                "Véhicules ajoutés": len(added_list),
                "Erreurs création": len(failed_list),
                "Doublons JSON signalés": len(dupjson_list),
            }]).to_excel(writer, sheet_name="Resume", index=False)

        log(f"   📊 Rapport Excel : {excel_path.name}")
    except ImportError:
        log(f"   ⚠️ Excel ignoré (pandas/openpyxl non installé)")
    except Exception as e:
        log(f"   ⚠️ Erreur Excel : {e}")


def save_kpi_summary(partner_dir: Path, folder: str, email: str,
                     dedup_stats: dict, sync_stats: dict):
    """
    Crée ou met à jour kpi_summary.json à la racine du dossier partenaire.
    Conserve un historique des passages avec horodatage.
    """
    kpi_path = partner_dir / "kpi_summary.json"

    dup_deleted  = dedup_stats.get("deleted", 0)
    added        = sync_stats.get("added", 0)
    add_failed   = sync_stats.get("add_failed", 0)
    hors_json    = sync_stats.get("deleted", 0)
    del_failed   = sync_stats.get("del_failed", 0)
    dupjson      = len(sync_stats.get("dupjson_list", []))
    to_delete    = sync_stats.get("to_delete", 0)
    to_add       = sync_stats.get("to_add", 0)

    passage = {
        "date": datetime.now().isoformat(),
        "doublons_en_ligne_supprimes": dup_deleted,
        "vehicules_hors_json_supprimes": hors_json,
        "vehicules_hors_json_echecs": del_failed,
        "vehicules_ajoutes": added,
        "vehicules_ajout_echecs": add_failed,
        "doublons_json_signales": dupjson,
        "total_a_supprimer_detectes": to_delete,
        "total_a_creer_detectes": to_add,
    }

    # Charger ou initialiser
    if kpi_path.exists():
        try:
            with open(kpi_path, encoding="utf-8") as f:
                kpi = json.load(f)
        except Exception:
            kpi = {}
    else:
        kpi = {}

    kpi["partenaire"]   = folder
    kpi["email"]        = email
    kpi["last_update"]  = datetime.now().isoformat()
    kpi["total_passages"] = kpi.get("total_passages", 0) + 1

    # Cumulatifs depuis le début
    kpi["cumul_doublons_supprimes"]   = kpi.get("cumul_doublons_supprimes", 0) + dup_deleted
    kpi["cumul_hors_json_supprimes"]  = kpi.get("cumul_hors_json_supprimes", 0) + hors_json
    kpi["cumul_vehicules_ajoutes"]    = kpi.get("cumul_vehicules_ajoutes", 0) + added
    kpi["cumul_doublons_json"]        = kpi.get("cumul_doublons_json", 0) + dupjson

    # Historique des 10 derniers passages
    historique = kpi.get("historique", [])
    historique.append(passage)
    kpi["historique"] = historique[-10:]   # garder les 10 derniers

    try:
        with open(kpi_path, "w", encoding="utf-8") as f:
            json.dump(kpi, f, ensure_ascii=False, indent=2)
        log(f"   📊 KPI mis à jour : {kpi_path.name}")
    except Exception as e:
        log(f"   ⚠️ Erreur KPI : {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  TRAITEMENT D'UN PARTENAIRE
# ─────────────────────────────────────────────────────────────────────────────

def process_partner(driver, data: dict, dry_run: bool = False,
                    dedup_only: bool = False) -> dict:
    folder = data.get("_folder", "?")
    email  = data.get("email", "")
    password = data.get("password", "")
    drivers_json = data.get("drivers", [])
    # Dossier physique du partenaire (pour sauvegarder le rapport)
    partner_dir = Path(data["_path"]).parent if data.get("_path") else None

    log(f"\n{'='*60}")
    log(f"📂 {folder}  |  📧 {email}")
    log(f"{'='*60}")

    stats = {
        "folder": folder,
        "email": email,
        "dedup": {},
        "sync": {},
        "success": False,
    }

    if not email:
        log(f"   ❌ Email introuvable pour {folder}")
        return stats

    if not login(driver, email, password=password):
        log(f"   ❌ Login échoué")
        stats["login_failed"] = True
        return stats

    # ── PHASE 1 : DÉDUP ──────────────────────────────────────────────────────
    log(f"\n   🔍 Phase 1 — Déduplication")
    dedup_stats = dedup_fleet(driver, folder, dry_run=dry_run)
    stats["dedup"] = dedup_stats
    log(f"   📊 Dédup : {dedup_stats['total']} total | "
        f"{dedup_stats.get('invalid_deleted', 0)} invalides supprimés | "
        f"{dedup_stats['duplicates_found']} trouvés | "
        f"{dedup_stats['deleted']} supprimés | "
        f"{dedup_stats['failed']} échecs")

    # ── PHASE 2 : SYNCHRONISATION (supprimer hors-JSON + créer manquants) ────
    if not dedup_only:
        log(f"\n   🔄 Phase 2 — Synchronisation")

        # Re-scraper la flotte après dédup avec contrôle de stabilité
        online_fleet = get_consistent_online_fleet(
            driver,
            attempts=3,
            min_rows=max(dedup_stats.get("total", 0), 1),
        )
        log(f"   📋 Flotte en ligne après dédup : {len(online_fleet)} véhicules")
        log(f"   📋 Conducteurs dans data.json  : {len(drivers_json)}")

        sync_stats = sync_fleet(driver, drivers_json, online_fleet, dry_run=dry_run)
        stats["sync"] = sync_stats
        log(f"   📊 Sync : "
            f"{sync_stats['to_delete']} à supprimer → {sync_stats['deleted']} supprimés ({sync_stats['del_failed']} échecs) | "
            f"{sync_stats['to_add']} à créer → {sync_stats['added']} créés ({sync_stats['add_failed']} échecs)")
    else:
        log(f"   ⏩ Sync ignorée (--dedup-only)")

    # ── SAUVEGARDE DU RAPPORT + KPI ───────────────────────────────────────
    if partner_dir and partner_dir.exists():
        log(f"\n   📁 Sauvegarde rapport + KPI...")
        save_update_report(
            partner_dir,
            stats.get("dedup", {}),
            stats.get("sync", {}),
        )
        save_kpi_summary(
            partner_dir, folder, email,
            stats.get("dedup", {}),
            stats.get("sync", {}),
        )
    else:
        log(f"   ⚠️ Dossier partenaire introuvable — rapport non sauvegardé")

    logout(driver)
    stats["success"] = True
    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Mise à jour intelligente de la flotte (dédup + ajout manquants)"
    )
    parser.add_argument("--only",       help="Traiter un seul partenaire (ex: partenaire-43)")
    parser.add_argument("--start",      help="Reprendre depuis ce partenaire")
    parser.add_argument("--end",        help="S'arrêter après ce partenaire")
    parser.add_argument("--dedup-only", action="store_true", help="Seulement dédupliquer")
    parser.add_argument("--dry-run",    action="store_true", help="Simulation (aucune modification)")
    parser.add_argument("--headed",      action="store_true", help="Chrome visible (debug local)")
    parser.add_argument("--debug-port",  type=int, default=9222, help="Port debug Chrome (défaut: 9222)")
    args = parser.parse_args()

    log(f"\n{'='*70}")
    log("🔄 UPDATE FLEET VPS — Dédup + Ajout manquants")
    log(f"{'='*70}")
    if args.dry_run:
        log("🧪 MODE DRY-RUN : aucune modification ne sera effectuée")
    if args.dedup_only:
        log("🔍 Mode DEDUP ONLY : seulement suppression des doublons")

    scrape_dir = SCRAPE_DIR
    if not scrape_dir.exists():
        log(f"❌ Dossier introuvable: {scrape_dir}")
        sys.exit(1)
    log(f"📁 Source : partenaire_drivers_scrape/")
    partners = load_partners_scrape(scrape_dir)
    log(f"   📂 {len(partners)} partenaires trouvés")

    # ── Filtres ──────────────────────────────────────────────────────────────
    def _norm(s):
        return re.sub(r'[\s\-_]', '', (s or "").lower())

    if args.only:
        partners = [p for p in partners if _norm(p.get("_folder","")) == _norm(args.only)]
        if not partners:
            log(f"❌ Partenaire '{args.only}' introuvable"); sys.exit(1)
        log(f"🎯 Partenaire ciblé : {partners[0]['_folder']}")

    if args.start and not args.only:
        start_num = partner_num(args.start)
        partners = [p for p in partners if partner_num(p.get("_folder","")) >= start_num]
        log(f"▶️ Reprise depuis numéro >= {start_num}")

    if args.end and not args.only:
        end_num = partner_num(args.end)
        partners = [p for p in partners if partner_num(p.get("_folder","")) <= end_num]
        log(f"🏁 Arrêt après numéro <= {end_num}")

    log(f"   📋 {len(partners)} partenaire(s) à traiter\n")

    send_slack(
        f"🔄 UPDATE FLEET démarré — {len(partners)} partenaires"
        + (" [DRY-RUN]" if args.dry_run else "")
        + (" [DEDUP-ONLY]" if args.dedup_only else ""),
        "#439FE0"
    )

    # ── Setup driver ─────────────────────────────────────────────────────────
    try:
        driver = setup_driver(headed=args.headed, debug_port=args.debug_port)
    except Exception as e:
        log(f"❌ Erreur Chrome : {e}")
        send_slack(f"❌ UPDATE FLEET échoué : Chrome non trouvé\n{e}", "#ff0000")
        sys.exit(1)

    # ── Traitement ───────────────────────────────────────────────────────────
    all_stats = []
    start_time = time.time()

    try:
        for idx, partner in enumerate(partners, 1):
            folder = partner.get("_folder", "?")
            log(f"\n   ▶️ [{idx}/{len(partners)}] {folder}")
            try:
                st = process_partner(
                    driver, partner,
                    dry_run=args.dry_run,
                    dedup_only=args.dedup_only,
                )
                all_stats.append(st)

                # ── Redémarrage Chrome si session invalide ────────────────────
                if st.get("login_failed"):
                    log(f"   🔄 Session Chrome invalide — redémarrage du driver...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    try:
                        driver = setup_driver(headed=args.headed, debug_port=args.debug_port)
                        log(f"   ✅ Chrome redémarré")
                    except Exception as e_restart:
                        log(f"   ❌ Impossible de redémarrer Chrome : {e_restart}")
                        send_slack(f"❌ UPDATE FLEET — Chrome crash irrécupérable à {folder}\n{e_restart}", "#ff0000")
                        break

                # Notification Slack par partenaire
                if st.get("login_failed"):
                    send_slack(f"❌ *{folder}* — login échoué (Chrome redémarré)", "#ff0000")
                else:
                    dd  = st.get("dedup", {})
                    sy  = st.get("sync", {})
                    dup_del   = dd.get("deleted", 0)
                    hors_del  = sy.get("deleted", 0)
                    added     = sy.get("added", 0)
                    add_fail  = sy.get("add_failed", 0)
                    del_fail  = sy.get("del_failed", 0)
                    has_error = (add_fail + del_fail) > 0
                    icon  = "⚠️" if has_error else "✅"
                    color = "#ffaa00" if has_error else "#36a64f"
                    lines = [f"{icon} *{folder}* [{idx}/{len(partners)}]"]
                    if dup_del:
                        lines.append(f"  🔁 Doublons supprimés : {dup_del}")
                    if hors_del:
                        lines.append(f"  🗑️ Hors-JSON supprimés : {hors_del}")
                    if added:
                        lines.append(f"  ➕ Véhicules ajoutés : {added}")
                    if add_fail or del_fail:
                        lines.append(f"  ⚠️ Échecs : sup={del_fail} / ajout={add_fail}")
                    if not (dup_del or hors_del or added or add_fail or del_fail):
                        lines.append("  ✔️ Rien à faire (déjà synchronisé)")
                    send_slack("\n".join(lines), color)

            except Exception as e:
                log(f"   💥 Erreur critique sur {folder}: {e}")
                traceback.print_exc()
                all_stats.append({"folder": folder, "success": False, "error": str(e)})
                send_slack(f"💥 *{folder}* — erreur critique : {str(e).splitlines()[0]}", "#ff0000")

    except KeyboardInterrupt:
        log("\n🛑 Interrompu par l'utilisateur")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # ── Résumé final ─────────────────────────────────────────────────────────
    duration = time.time() - start_time
    success_count = sum(1 for s in all_stats if s.get("success"))
    failed_count  = len(all_stats) - success_count
    total_dup_del   = sum(s.get("dedup", {}).get("deleted", 0) for s in all_stats)
    total_sync_del  = sum(s.get("sync", {}).get("deleted", 0) for s in all_stats)
    total_added     = sum(s.get("sync", {}).get("added", 0) for s in all_stats)
    total_add_fail  = sum(s.get("sync", {}).get("add_failed", 0) for s in all_stats)
    total_del_fail  = sum(s.get("sync", {}).get("del_failed", 0) for s in all_stats)

    log(f"\n{'='*70}")
    log(f"✅ UPDATE FLEET TERMINÉ en {duration/60:.1f} min")
    log(f"   Partenaires traités      : {len(all_stats)}")
    log(f"   ✅ Succès                : {success_count}")
    log(f"   ❌ Échecs login/critique : {failed_count}")
    log(f"   🗑️  Doublons supprimés   : {total_dup_del}")
    log(f"   🗑️  Hors-JSON supprimés  : {total_sync_del}")
    log(f"   ➕ Véhicules ajoutés    : {total_added}")
    log(f"   ⚠️  Échecs suppression   : {total_del_fail}")
    log(f"   ⚠️  Échecs ajout        : {total_add_fail}")
    log(f"{'='*70}")

    color = "#36a64f" if failed_count == 0 else "#ffaa00"
    send_slack(
        f"{'✅' if failed_count == 0 else '⚠️'} UPDATE FLEET terminé en {duration/60:.1f} min\n"
        f"• Partenaires : {len(all_stats)} ({success_count} succès, {failed_count} échecs)\n"
        f"• 🗑️ Doublons supprimés   : {total_dup_del}\n"
        f"• 🗑️ Hors-JSON supprimés  : {total_sync_del}\n"
        f"• ➕ Véhicules ajoutés    : {total_added}\n"
        f"• ⚠️ Échecs               : sup={total_del_fail} / ajout={total_add_fail}",
        color
    )


if __name__ == "__main__":
    main()
