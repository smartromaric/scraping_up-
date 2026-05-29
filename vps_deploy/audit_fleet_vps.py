"""
Audit & Synchronisation Flotte — VPS (headless, full auto)
===========================================================
Source de vérité : output/organized_by_partner/all_partners_enriched.json

Pour chaque partenaire (Partenaire-X) :
  1. Login owner automatique → derive_owner_email(nom) + UNIVERSAL_PASSWORD
  2. Scrape /manage-fleet (pagination 500) → plaques ACTUELLES
  3. Compare avec plaques ATTENDUES (depuis all_partners_enriched.json)
  4. Manquants → AJOUTE via /manage-fleet/create
  5. En trop  → SUPPRIME via menu Action → Supprimer → Yes delete it! → OK
  6. Logout + partenaire suivant
  7. Slack notifications (vert = succès, rouge = erreur)
  8. Rapport JSON progressif dans output/reports/

Usage:
  python3 audit_fleet_vps.py                          # Tous les partenaires (ajout uniquement)
  python3 audit_fleet_vps.py --delete-extras          # Tous + suppression des extras
  python3 audit_fleet_vps.py --only Partenaire42     # Un seul partenaire
  python3 audit_fleet_vps.py --start Partenaire-50    # Reprise depuis
  python3 audit_fleet_vps.py --dry-run                # Simulation (aucune modif)
  python3 audit_fleet_vps.py --report-only            # Juste le rapport

Background VPS:
  nohup python3 audit_fleet_vps.py --delete-extras > audit_fleet.log 2>&1 &
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
REFERENCE_FILE     = OUTPUT_DIR / "organized_by_partner" / "all_partners_enriched.json"
REPORTS_DIR        = OUTPUT_DIR / "reports"
LOG_FILE           = OUTPUT_DIR / "audit_fleet.log"

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


# Regex pour stripper le suffixe ivoirien : "CI" optionnel + 0 à 3 chiffres (code région)
# Exemples : "50796WW CI" → "50796WW" | "50796WWCI 01" → "50796WW" | "AA755KR01" → "AA755KR"
_CI_CANONICAL_RE = re.compile(r'^(.*?[A-Z])(?:CI)?\d{0,3}$')


def normalize_plate(plate: str) -> str:
    """
    Normalise une plaque ivoirienne vers sa forme canonique :
      - Majuscules, sans tirets/espaces/points/slashes
      - Strip du suffixe 'CI' optionnel + code région (0-3 chiffres)
    Les 3 formes suivantes deviennent toutes '50796WW' :
        '50796WW'  /  '50796WW CI'  /  '50796WWCI 01'  /  '50796WW-CI-01'
    """
    if not plate:
        return ""
    raw = str(plate).strip().upper()
    for ch in ("-", " ", ".", "/", "_"):
        raw = raw.replace(ch, "")
    if not raw:
        return ""

    # Essayer de stripper le suffixe CI/région
    m = _CI_CANONICAL_RE.match(raw)
    if m:
        canonical = m.group(1)
        # Garde-fou : ne pas sur-stripper (canonical doit faire ≥ 4 chars)
        if len(canonical) >= 4:
            return canonical

    return raw


# Plaques évidemment invalides à rejeter (évite la pollution type "199 véhicules bidons")
INVALID_PLATE_PATTERNS = {
    "", "N/A", "NA", "XXX", "XXXX", "XXXXX", "XXXXXX",
    "000", "0000", "00000", "000000", "0000000",
    "111", "1111", "11111", "111111",
    "AAAA", "AAAAA", "AAAAAA",
    "TEST", "TESTS", "TESTING",
    "NULL", "NONE", "UNDEFINED",
    "-", "--", "---", "----",
}


def is_valid_plate(plate: str) -> bool:
    """Valide qu'une plaque est plausible (évite XXX, 000000, etc.)."""
    norm = normalize_plate(plate)
    if not norm or len(norm) < 5:
        return False
    if norm in INVALID_PLATE_PATTERNS:
        return False
    # Doit contenir au moins 1 chiffre ET 1 lettre (plaques ivoiriennes type)
    has_digit = any(c.isdigit() for c in norm)
    has_alpha = any(c.isalpha() for c in norm)
    if not (has_digit and has_alpha):
        # Exception : plaques full-digit acceptées si >= 6 chars (ex: 234556)
        if has_digit and len(norm) >= 6 and len(set(norm)) >= 2:
            return True
        return False
    # Rejeter si un seul caractère répété (ex: XXXXXX1)
    if len(set(norm)) < 3:
        return False
    return True

_MIN_PLATE_LEN = 6  # Vraie plaque CI fait au moins 6 caractères normalisés

# Regex STRICTE des plaques CI réelles (sur forme sans ponctuation, majuscules)
# Patterns couverts :
#   50796WW, 4316GZ, 50796WWCI01    → 4-5 digits + 2-4 letters + suffixe optionnel
#   AA755KR, AA121KG, AA755KR01     → 2L + 3-4d + 2-3L + suffixe
#   NGN25M08650                     → 3L + 2d + 1L + 5d
#   ABO202203388, ANY25000134       → 3-4L + 8-12 digits
#   LBMXCBL38T1601485 (VIN)         → 15-17 alphanumériques
_REAL_PLATE_RE = re.compile(
    r'^('
    r'\d{4,5}[A-Z]{2,4}(?:CI)?\d{0,3}'
    r'|[A-Z]{2}\d{3,4}[A-Z]{2,3}(?:CI)?\d{0,3}'
    r'|[A-Z]{2,4}\d{2,4}[A-Z]\d{4,8}'
    r'|[A-Z]{2,4}\d{8,12}'
    r'|[A-Z0-9]{15,17}'
    r')$'
)


# Mots qui indiquent un NOM DE MODÈLE (saisie utilisateur erronée dans le champ plaque)
_MODEL_KEYWORDS = {
    "PLUS", "PRO", "MAX", "KOMPRESSOR", "TDI", "HDI", "GDI",
    "SPORT", "LUXE", "LUXURY", "PREMIUM", "DELUXE",
    "AUTO", "AUTOMATIC", "MANUAL", "HYBRID", "HYBRIDE",
    "DIESEL", "ESSENCE", "BENZIN",
    "VERSION", "EDITION", "LIMITED",
    "MOTORCYCLE", "MOTO", "SCOOTER",
}


def looks_like_real_plate(plate_raw: str) -> bool:
    """
    Détecte si une valeur est une VRAIE plaque CI (pas un nom de modèle).
    Retourne False si la valeur ressemble à un modèle (ex: 'X-1 PLUS', 'C200KOMPRESSOR').
    C'est un garde-fou pour éviter de supprimer des véhicules dont le champ plaque
    a été rempli avec le modèle par erreur.
    """
    if not plate_raw:
        return False
    raw = str(plate_raw).strip().upper()
    # 1. Détecter les mots-clés de modèle (ex: "PLUS", "KOMPRESSOR")
    words = re.split(r'[\s\-_./]+', raw)
    for w in words:
        if w in _MODEL_KEYWORDS:
            return False
    # 2. Tester le pattern de plaque CI sur la forme sans ponctuation
    cleaned = re.sub(r'[\s\-_./]', '', raw)
    if not cleaned or len(cleaned) < _MIN_PLATE_LEN:
        return False
    if _REAL_PLATE_RE.match(cleaned):
        return True
    return False


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
    if not headed:
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    if not headed:
        for binary in [
            "chromium-browser",            # /usr/bin/chromium-browser fonctionne sur ce VPS
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
        "/usr/bin/chromedriver",           # chromedriver réel sur ce VPS
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
        service = Service(ChromeDriverManager().install())
    else:
        service = Service(chromedriver_path)
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
        log(f"         📸 Screenshot: {path}")
    except Exception:
        pass


def owner_login(driver, email: str, password: str, max_retries: int = 3) -> bool:
    """Login owner avec retry automatique (timeouts généreux pour connexions lentes)."""
    log(f"      🔐 Login: {email}")

    for attempt in range(1, max_retries + 1):
        try:
            # Nettoyer la session avant chaque tentative
            try:
                driver.delete_all_cookies()
            except Exception:
                pass

            log(f"         → Tentative {attempt}/{max_retries}")
            try:
                driver.get(OWNER_LOGIN_URL)
            except Exception as e:
                log(f"         ⚠️ goto lent ({e}), on continue")

            # Timeout généreux (90s) pour connexions lentes
            wait = WebDriverWait(driver, 90)

            # Attendre l'input email
            email_input = wait.until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
            )))
            # Petit délai pour laisser le JS s'initialiser
            time.sleep(1)

            pwd_input = driver.find_element(
                By.CSS_SELECTOR, "input[type='password'], input[name='password']"
            )
            email_input.clear(); email_input.send_keys(email)
            time.sleep(0.3)
            pwd_input.clear();   pwd_input.send_keys(password)
            time.sleep(0.3)

            # Submit
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

            # Attendre la redirection hors de /login (90s pour connexions lentes)
            WebDriverWait(driver, 90).until(lambda d: "/login" not in d.current_url)
            log(f"         ✅ Connecté → {driver.current_url}")
            return True

        except TimeoutException:
            log(f"         ⚠️ Timeout login (tentative {attempt})")
            if attempt == max_retries:
                _save_debug_screenshot(driver, f"login_fail_{email.split('@')[0]}")
        except Exception as e:
            log(f"         ⚠️ Erreur login tentative {attempt}: {type(e).__name__}")
            if attempt == max_retries:
                _save_debug_screenshot(driver, f"login_fail_{email.split('@')[0]}")

        # Backoff avant retry
        if attempt < max_retries:
            time.sleep(3 * attempt)

    log(f"         ❌ Login échoué après {max_retries} tentatives")
    return False


def owner_logout(driver):
    try:
        driver.delete_all_cookies()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  FLEET SCRAPING
# ═════════════════════════════════════════════════════════════════════════════

def _count_fleet_rows(driver) -> int:
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        return sum(
            1 for r in rows if r.text.strip()
            and "no data" not in r.text.lower()
            and "aucun" not in r.text.lower()
        )
    except Exception:
        return 0


def _wait_table_stable(driver, timeout=60) -> int:
    """
    Attend que le tableau soit stable (nombre de lignes identique pendant 3s).
    Augmente le timeout pour connexions lentes.
    """
    deadline = time.time() + timeout
    last, stable_since = -1, None
    while time.time() < deadline:
        current = _count_fleet_rows(driver)
        if current == last and current > 0:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= 3.0:
                return current
        else:
            stable_since = None
            last = current
        time.sleep(0.7)
    return last if last > 0 else 0


def _set_pagination_500(driver, max_attempts: int = 3) -> bool:
    """
    Définit la pagination à 500 lignes/page.
    Avec retry : si le nombre de lignes ne change pas, on réessaie.
    Retourne True si la pagination a été effectivement changée.
    """
    selector = "select.form-select.form-select-sm.w-auto"

    for attempt in range(1, max_attempts + 1):
        try:
            def _populated(d):
                try:
                    el = d.find_element(By.CSS_SELECTOR, selector)
                    return el if len(el.find_elements(By.TAG_NAME, "option")) >= 2 else False
                except Exception:
                    return False
            sel_el = WebDriverWait(driver, 30).until(_populated)
        except TimeoutException:
            log(f"            ⚠️ Select pagination introuvable (tentative {attempt})")
            continue

        # Lire les options disponibles
        try:
            options_info = driver.execute_script("""
                var s = arguments[0];
                return Array.from(s.options).map(function(o) {
                    return {value: o.value, text: o.text};
                });
            """, sel_el)
            log(f"            🔍 Options pagination : {options_info}")
        except Exception:
            pass

        rows_before = _count_fleet_rows(driver)

        # Appliquer la sélection
        driver.execute_script("""
            var select = arguments[0];
            var found = false;
            for (var i = 0; i < select.options.length; i++) {
                var v = select.options[i].value;
                var t = select.options[i].text.trim();
                if (v === '500' || t === '500') {
                    select.selectedIndex = i; found = true; break;
                }
            }
            // Fallback : prendre la plus grande valeur numérique
            if (!found) {
                var maxVal = -1, maxIdx = -1;
                for (var i = 0; i < select.options.length; i++) {
                    var n = parseInt(select.options[i].value, 10);
                    if (isNaN(n)) n = parseInt(select.options[i].text, 10);
                    if (!isNaN(n) && n > maxVal) { maxVal = n; maxIdx = i; }
                }
                if (maxIdx >= 0) select.selectedIndex = maxIdx;
                else select.selectedIndex = select.options.length - 1;
            }
            var nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value').set;
            nativeSetter.call(select, select.options[select.selectedIndex].value);
            select.dispatchEvent(new Event('input', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));
        """, sel_el)

        # Attendre que les lignes changent OU stabilisent à un nombre différent
        time.sleep(2)
        deadline = time.time() + 30
        while time.time() < deadline:
            rows_now = _count_fleet_rows(driver)
            if rows_now > rows_before:
                log(f"            ✅ Pagination appliquée : {rows_before} → {rows_now} lignes")
                # Attendre la stabilité finale
                _wait_table_stable(driver, timeout=30)
                return True
            if rows_now == rows_before and rows_now > 0:
                # Peut-être que fleet a < 500 donc la pagination ne change rien visiblement
                # On laisse quand même le temps de stabilisation
                time.sleep(2)
            time.sleep(1)

        log(f"            ⚠️ Pagination tentative {attempt} : aucune augmentation ({rows_before} lignes)")
        time.sleep(2)

    # Si on arrive ici : la flotte fait peut-être < 10 lignes donc pagination sans effet visible
    return False


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
        except Exception:
            pass
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            btn.click()
        except Exception:
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
            except Exception:
                pass
            if _is_next_disabled(driver):
                return False
        return False
    except NoSuchElementException:
        return False


def _find_plate_column_index(driver) -> int:
    """
    Trouve l'index (0-based) de la colonne 'Numéro de plaque d'immatriculation'
    dans le tableau /manage-fleet. Fallback à 4 (colonne typique) si introuvable.
    """
    try:
        headers = driver.find_elements(By.CSS_SELECTOR, "table thead th")
        for idx, th in enumerate(headers):
            text = (th.text or "").strip().lower()
            # Match explicite sur le header de la colonne plaque
            if any(kw in text for kw in [
                "plaque", "immatriculation", "license", "license plate",
                "numero de plaque", "numéro de plaque"
            ]):
                return idx
    except Exception:
        pass
    # Fallback : 5e colonne (index 4) d'après la structure du tableau
    return 4


def scrape_fleet_plates(driver) -> set:
    """
    Retourne l'ensemble des plaques actuelles (texte brut tel que dans le tableau).
    🎯 Extrait SEULEMENT la cellule de la colonne 'Plaque d'immatriculation'
       (évite de confondre avec la colonne 'Modèle' qui contient aussi des digits+letters).
    """
    all_plates = set()
    log(f"         🌐 Navigation vers {MANAGE_FLEET_URL}")
    driver.get(MANAGE_FLEET_URL)
    try:
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
        )
        log(f"         ✅ Tableau chargé")
    except TimeoutException:
        log(f"         ❌ Tableau non chargé après 60s")
        return all_plates

    # Attendre un peu que les données se chargent via AJAX
    time.sleep(3)

    # Tenter de mettre pagination à 500
    if _set_pagination_500(driver):
        log(f"         📐 Pagination 500/page activée")
    else:
        log(f"         ⚠️ Pagination 500 non trouvée (on continue avec la pagination par défaut)")
    time.sleep(3)

    plate_col_idx = _find_plate_column_index(driver)
    log(f"         🎯 Colonne plaque détectée : index {plate_col_idx}")

    # Log des headers pour diagnostic
    try:
        headers = driver.find_elements(By.CSS_SELECTOR, "table thead th")
        header_texts = [h.text.strip() for h in headers]
        log(f"         📋 Headers table: {header_texts}")
    except Exception:
        pass

    page_num = 1
    while True:
        # Attendre que les lignes soient stables (connexion lente possible)
        rows_count = _wait_table_stable(driver, timeout=60)
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        actual_rows = [r for r in rows if r.text.strip()]
        log(f"         📄 Page {page_num}: {len(actual_rows)} lignes visibles")

        page_plates = set()
        for row_idx, row in enumerate(actual_rows):
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) <= plate_col_idx:
                    continue
                text = cells[plate_col_idx].text.strip().upper()
                if not text or text in ("N/A", "-", ""):
                    continue
                if len(text) < 3:
                    continue
                page_plates.add(text)
            except Exception as e:
                log(f"            ⚠️ Erreur row {row_idx}: {e}")
                continue

        log(f"         ✅ Page {page_num}: {len(page_plates)} plaques extraites")
        if page_plates and page_num == 1:
            # Log d'un échantillon pour vérifier
            sample = list(page_plates)[:5]
            log(f"             Échantillon: {sample}")

        all_plates.update(page_plates)

        if not _go_next_page(driver):
            log(f"         🏁 Fin pagination (page suivante désactivée)")
            break
        page_num += 1
        if page_num > 50:
            log(f"         ⚠️ Limite 50 pages atteinte, stop")
            break
        time.sleep(2)  # Laisser respirer entre pages

    log(f"         📊 Total plaques scrapées: {len(all_plates)}")
    return all_plates


# ═════════════════════════════════════════════════════════════════════════════
#  CRÉATION VÉHICULE  (logique éprouvée de create_fleet.py)
# ═════════════════════════════════════════════════════════════════════════════

def _select_vehicle_type(driver, select_el, vehicle_type: str) -> bool:
    """
    Sélection robuste du type de véhicule.
    Stratégie (copiée de create_fleet.py qui marche à 100%) :
      1. Si dropdown a des options "réelles" (avec value) → match par texte (case insensitive)
      2. Si non trouvé dans options → injection JS avec UUID du mapping
      3. Si dropdown vide (API KO) → injection JS avec UUID directement
    """
    target_type = str(vehicle_type).strip().upper()
    normalized_map = {k.upper(): v for k, v in TYPE_UUID_MAP.items()}

    try:
        select_obj = Select(select_el)
        real_options = [o for o in select_obj.options if o.get_attribute("value")]

        # Cas 1 : dropdown a des options → on essaie le match par texte
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
            log(f"            ❌ Type '{vehicle_type}' inconnu dans TYPE_UUID_MAP")
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
        log(f"            ⚠️ Erreur select_type '{vehicle_type}': {e}")
        return False


def create_vehicle(driver, vehicle_data: dict) -> bool:
    """
    Crée un véhicule via /manage-fleet/create.
    Réutilise la logique gagnante de create_fleet.py :
      - Sélection robuste du type (même si dropdown API vide → injection UUID)
      - Refresh systématique AVANT (navigation fresh)
      - Vérification post-submit via URL
    """
    vehicle = vehicle_data.get("vehicle", {}) or {}
    vehicle_type = vehicle.get("type", "CONFORT")
    marque    = vehicle.get("marque", "N/A")
    modele    = vehicle.get("modele", "N/A")
    matricule = vehicle.get("matricule", "N/A")

    # Garde-fou : refuser les données incomplètes
    if not all([vehicle_type, marque, modele, matricule]) or "N/A" in {marque, modele, matricule}:
        log(f"            ⏩ Saut (données incomplètes) : {marque}/{modele}/{matricule}")
        return False

    wait = WebDriverWait(driver, 30)

    # Navigation fresh vers la page de création (évite les états résiduels)
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
        brand_input.send_keys(marque)

        # ── 3. Modèle ──
        model_input = wait.until(EC.presence_of_element_located((By.ID, "car_model")))
        model_input.clear()
        model_input.send_keys(modele)

        # ── 4. Plaque ──
        plate_input = wait.until(EC.presence_of_element_located((By.ID, "license_plate_number")))
        plate_input.clear()
        plate_input.send_keys(matricule)

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
            log(f"            ⚠️ Pas de redirection après submit pour {matricule}")
            return False
        return True

    except Exception as e:
        log(f"            ❌ Erreur formulaire ajout ({matricule}): {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  SUPPRESSION VÉHICULE
# ═════════════════════════════════════════════════════════════════════════════

def _find_row_by_plate(driver, plate_raw: str):
    """
    Retourne le <tr> contenant la plaque dans la BONNE colonne.
    Match en deux passes :
      1. D'abord : texte brut exact dans la colonne plaque (précis)
      2. Sinon : canonical match dans la colonne plaque uniquement
    Évite de matcher sur la colonne 'Modèle' qui peut contenir des digits+letters.
    """
    target_raw = (plate_raw or "").strip().upper()
    target_canon = normalize_plate(plate_raw)
    plate_col = _find_plate_column_index(driver)

    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    # Passe 1 : match brut exact dans la colonne plaque
    for row in rows:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) <= plate_col:
                continue
            if cells[plate_col].text.strip().upper() == target_raw:
                return row
        except Exception:
            continue
    # Passe 2 : match canonical dans la colonne plaque
    for row in rows:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) <= plate_col:
                continue
            if normalize_plate(cells[plate_col].text) == target_canon:
                return row
        except Exception:
            continue
    return None


def _close_swal_if_any(driver):
    """Ferme toute modale SweetAlert2 résiduelle (sécurité)."""
    try:
        popups = driver.find_elements(By.CSS_SELECTOR, ".swal2-container .swal2-popup")
        if popups:
            try:
                driver.find_element(By.CSS_SELECTOR, ".swal2-confirm").click()
            except Exception:
                pass
            time.sleep(1)
    except Exception:
        pass


def _click_robust(driver, element):
    """Click via JS avec dispatch d'événements complets (contourne les overlays)."""
    try:
        driver.execute_script("""
            var el = arguments[0];
            ['mouseover','mousedown','mouseup','click'].forEach(function(evtName) {
                var evt = new MouseEvent(evtName, {
                    bubbles: true, cancelable: true, view: window
                });
                el.dispatchEvent(evt);
            });
        """, element)
        return True
    except Exception:
        try:
            element.click()
            return True
        except Exception:
            return False


def _find_supprimer_clickable(driver):
    """
    Trouve l'élément 'Supprimer' visible et son plus proche ancêtre cliquable.
    Retourne le plus proche parent <li|button|a|div avec role=button|cursor:pointer>.
    """
    candidates = driver.find_elements(
        By.XPATH,
        "//*[normalize-space(text())='Supprimer' or normalize-space()='Supprimer']"
    )
    for c in candidates:
        try:
            if not c.is_displayed():
                continue
            # Remonter jusqu'à trouver un ancêtre clickable
            clickable = driver.execute_script("""
                var el = arguments[0];
                for (var i = 0; i < 6 && el; i++) {
                    var tag = el.tagName && el.tagName.toLowerCase();
                    var role = el.getAttribute && el.getAttribute('role');
                    var cursor = window.getComputedStyle(el).cursor;
                    if (tag === 'button' || tag === 'a' || tag === 'li'
                        || role === 'button' || role === 'menuitem'
                        || cursor === 'pointer') {
                        return el;
                    }
                    el = el.parentElement;
                }
                return arguments[0];
            """, c)
            return clickable
        except Exception:
            continue
    return None


def _dump_dom_area(driver, description: str, element=None):
    """Dump la structure HTML pour debug."""
    try:
        if element is not None:
            html = driver.execute_script("return arguments[0].outerHTML;", element)
        else:
            html = driver.execute_script(
                "return document.documentElement.outerHTML;"
            )
        # Garder seulement les 2000 premiers chars
        log(f"            🔬 DOM[{description}]: {html[:2000]}")
    except Exception as e:
        log(f"            ⚠️ Dump DOM failed: {e}")


def _wait_swal(driver, timeout_s: float = 3.0) -> bool:
    """Attend jusqu'à timeout_s que .swal2-confirm soit présent. Retourne True si trouvé."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if driver.find_elements(By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"):
            return True
        time.sleep(0.1)
    return False


def _try_click_supprimer_all_strategies(driver, debug: bool = False) -> bool:
    """
    Essaie plusieurs stratégies pour cliquer sur 'Supprimer' dans le dropdown.
    Retourne True si une des stratégies a déclenché la SweetAlert2 confirmation.
    """
    def _attempt(el, label: str) -> bool:
        """Essaie 3 méthodes de click sur `el` et vérifie SweetAlert2."""
        # Méthode 1 : JS natif .click() — déclenche handlers Vue/jQuery
        try:
            driver.execute_script("arguments[0].click();", el)
            if _wait_swal(driver, 2.5):
                if debug:
                    log(f"            ✅ SweetAlert2 via JS .click() [{label}]")
                return True
        except Exception as ex:
            if debug:
                log(f"            🔬 JS .click() échoué [{label}]: {ex}")

        # Méthode 2 : Selenium native click (vrai événement souris OS)
        try:
            el.click()
            if _wait_swal(driver, 2.5):
                if debug:
                    log(f"            ✅ SweetAlert2 via Selenium .click() [{label}]")
                return True
        except Exception as ex:
            if debug:
                log(f"            🔬 Selenium .click() échoué [{label}]: {ex}")

        # Méthode 3 : ActionChains move + click
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(driver).move_to_element(el).pause(0.2).click().perform()
            if _wait_swal(driver, 2.5):
                if debug:
                    log(f"            ✅ SweetAlert2 via ActionChains [{label}]")
                return True
        except Exception as ex:
            if debug:
                log(f"            🔬 ActionChains échoué [{label}]: {ex}")

        return False

    # ── Collecter les éléments Supprimer candidats ──────────────────────────
    # Priorité 1 : via la classe Bootstrap dropdown-item dans un menu .show
    supprimer_items = []
    try:
        open_menus = driver.find_elements(By.CSS_SELECTOR, ".dropdown-menu.show")
        for menu in open_menus:
            items = menu.find_elements(By.CSS_SELECTOR, ".dropdown-item")
            for item in items:
                txt = (item.text or "").strip()
                if "Supprimer" in txt and _is_visible_safe(item):
                    supprimer_items.append((item, f"dropdown-item in .show menu ('{txt}')"))
    except Exception as ex:
        if debug:
            log(f"            🔬 Collect via .dropdown-menu.show: {ex}")

    # Priorité 2 : XPATH texte global (fallback)
    try:
        candidates = driver.find_elements(
            By.XPATH,
            "//*[normalize-space(text())='Supprimer' or normalize-space()='Supprimer']"
        )
        for c in candidates:
            if _is_visible_safe(c):
                supprimer_items.append((c, f"XPATH global (tag={c.tag_name})"))
    except Exception as ex:
        if debug:
            log(f"            🔬 Collect via XPATH global: {ex}")

    if debug:
        log(f"            🔬 Candidats Supprimer trouvés : {len(supprimer_items)}")
        for el, lbl in supprimer_items[:5]:
            try:
                html = driver.execute_script("return arguments[0].outerHTML;", el)
                log(f"            🔬   [{lbl}] HTML: {html[:300]}")
            except Exception:
                pass

    # ── Essayer chaque candidat ──────────────────────────────────────────────
    for el, label in supprimer_items:
        # S'assurer que le dropdown est toujours ouvert (il peut se fermer)
        open_count = len(driver.find_elements(By.CSS_SELECTOR, ".dropdown-menu.show"))
        if open_count == 0 and debug:
            log(f"            ⚠️ Dropdown fermé avant tentative [{label}]")
        # Scroll into view
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass
        time.sleep(0.15)
        if _attempt(el, label):
            return True

    if debug:
        log(f"            ❌ Aucune stratégie n'a déclenché SweetAlert2")
    return False


def _is_visible_safe(el) -> bool:
    try:
        return el.is_displayed()
    except Exception:
        return False


def delete_vehicle(driver, plate_raw: str, debug: bool = False) -> bool:
    """
    Supprime un véhicule via son menu Action.
    Flux : bouton (⋮) → Supprimer → Yes delete it! → OK → refresh.
    Debug=True → logs détaillés + screenshots + dump DOM.
    """
    # 1. Trouver la ligne avec la plaque
    row = _find_row_by_plate(driver, plate_raw)
    if row is None:
        log(f"            ⚠️ Ligne introuvable pour {plate_raw}")
        return False

    if debug:
        log(f"            🔬 Ligne trouvée pour {plate_raw}")
        _dump_dom_area(driver, "row", row)

    try:
        # DUMP DE LA DERNIÈRE CELLULE POUR COMPRENDRE LA STRUCTURE
        if debug:
            try:
                last_cell = row.find_element(By.CSS_SELECTOR, "td:last-child")
                log(f"            🔬 Structure dernière cellule :")
                _dump_dom_area(driver, "last_cell", last_cell)
            except Exception:
                pass

        # Scroll sur la ligne AVANT toute interaction
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
        time.sleep(0.8)

        if debug:
            _save_debug_screenshot(driver, f"before_click_{normalize_plate(plate_raw)}")

        # 2. Trouver le bouton Action avec PLUSIEURS stratégies
        action_btn = None
        strategies = [
            ("Bootstrap dropdown toggle",
             "td:last-child [data-bs-toggle='dropdown']"),
            ("any button in last cell",
             "td:last-child button"),
            ("any clickable in last cell",
             "td:last-child [role='button'], td:last-child a"),
        ]
        for label, sel in strategies:
            try:
                found = row.find_element(By.CSS_SELECTOR, sel)
                action_btn = found
                if debug:
                    log(f"            🔬 Bouton trouvé via: {label}")
                break
            except NoSuchElementException:
                if debug:
                    log(f"            🔬 Pas trouvé via: {label}")
                continue

        if action_btn is None:
            log(f"            ❌ Bouton ⋮ introuvable pour {plate_raw}")
            return False

        if debug:
            _dump_dom_area(driver, "action_btn", action_btn)

        # 3+4. Ouvrir le dropdown ET cliquer Supprimer de façon atomique
        #       (le dropdown se referme si on attend trop entre les deux actions)
        swal_appeared = False

        # ── Stratégie ATOMIQUE : JS ouvre le dropdown ──────────────────────────
        if debug:
            log(f"            🔬 Stratégie atomique JS : show dropdown")
        try:
            driver.execute_script("""
                var btn = arguments[0];
                if (window.bootstrap && window.bootstrap.Dropdown) {
                    var dd = window.bootstrap.Dropdown.getInstance(btn)
                           || new window.bootstrap.Dropdown(btn);
                    dd.show();
                } else {
                    btn.click();
                }
            """, action_btn)
            # Bootstrap dd.show() est synchrone pour l'ajout de classe CSS
            # mais on laisse 300ms pour que le DOM soit mis à jour
            time.sleep(0.3)
            if debug:
                visible_menus = driver.find_elements(By.CSS_SELECTOR, ".dropdown-menu.show")
                log(f"            🔬 Menus .show après atomique: {len(visible_menus)}")
            # Cliquer Supprimer via JS .click() directement sur le bon élément
            clicked_info = driver.execute_script("""
                var menus = document.querySelectorAll('.dropdown-menu.show, .dropdown-menu');
                for (var m = 0; m < menus.length; m++) {
                    var items = menus[m].querySelectorAll('.dropdown-item, a');
                    for (var i = 0; i < items.length; i++) {
                        var txt = items[i].textContent.trim();
                        if (txt === 'Supprimer' || txt.indexOf('Supprimer') !== -1) {
                            items[i].click();
                            return 'clicked:' + items[i].tagName + ':' + txt;
                        }
                    }
                }
                return 'no_supprimer_found';
            """)
            if debug:
                log(f"            🔬 Résultat click Supprimer JS: {clicked_info}")
            if _wait_swal(driver, 3.0):
                swal_appeared = True
                if debug:
                    log(f"            ✅ SweetAlert2 après stratégie atomique JS")
        except Exception as e:
            if debug:
                log(f"            🔬 Stratégie atomique échouée: {e}")

        # ── Stratégie B : Selenium click natif sur le bouton, puis chercher Supprimer ──
        if not swal_appeared:
            if debug:
                log(f"            🔬 Stratégie B : Selenium .click() + _try_click_supprimer")
            try:
                action_btn.click()
                time.sleep(0.4)
            except Exception as e:
                if debug:
                    log(f"            🔬 Selenium .click() btn échoué: {e}")
                try:
                    from selenium.webdriver.common.action_chains import ActionChains
                    ActionChains(driver).move_to_element(action_btn).pause(0.2).click().perform()
                    time.sleep(0.4)
                except Exception as e2:
                    if debug:
                        log(f"            🔬 ActionChains btn échoué: {e2}")
            swal_appeared = _try_click_supprimer_all_strategies(driver, debug=debug)

        if debug:
            _save_debug_screenshot(driver, f"after_action_click_{normalize_plate(plate_raw)}")

        if not swal_appeared:
            log(f"            ❌ Impossible de faire apparaître SweetAlert2 'Yes, delete it!' pour {plate_raw}")
            _save_debug_screenshot(driver, f"no_swal_{normalize_plate(plate_raw)}")
            # Dump tout le body pour analyse
            if debug:
                _dump_dom_area(driver, "body_no_swal")
            return False

        # 4. Cliquer "Yes, delete it!"
        try:
            confirm_btn = WebDriverWait(driver, 15).until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, ".swal2-popup .swal2-confirm")
            ))
            time.sleep(0.3)
            if debug:
                log(f"            🔬 SweetAlert2 'Are you sure?' visible, click confirm")
            driver.execute_script("arguments[0].click();", confirm_btn)
        except TimeoutException:
            log(f"            ❌ .swal2-confirm non cliquable pour {plate_raw}")
            return False

        # 5. SweetAlert2 "Succès" → "OK"
        time.sleep(0.5)
        try:
            ok_btn = WebDriverWait(driver, 60).until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, ".swal2-popup .swal2-confirm")
            ))
            time.sleep(0.3)
            if debug:
                log(f"            🔬 SweetAlert2 'Succès' visible, click OK")
            driver.execute_script("arguments[0].click();", ok_btn)
        except TimeoutException:
            log(f"            ⚠️ Modal succès non apparue (on continue)")

        # 6. Attendre disparition de la modale (max 3s)
        start = time.time()
        while time.time() - start < 3:
            if not driver.find_elements(By.CSS_SELECTOR, ".swal2-container .swal2-popup"):
                break
            time.sleep(0.2)

        # 7. Petite pause pour que le DOM se mette à jour
        time.sleep(1)
        log(f"            ✅ {plate_raw} supprimée")
        return True

    except Exception as e:
        log(f"            ❌ Erreur suppression {plate_raw}: {type(e).__name__}: {e}")
        if debug:
            traceback.print_exc()
        _close_swal_if_any(driver)
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  AUDIT D'UN PARTENAIRE
# ═════════════════════════════════════════════════════════════════════════════

def audit_partner(driver, partner_data: dict, dry_run: bool = False,
                  delete_extras: bool = False, debug_delete: bool = False,
                  max_deletes: int = 0, skip_additions: bool = False) -> dict:
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
        "extras_deleted": 0,
        "extras_failed": 0,
        "invalid_data": 0,
        "extra_plates": [],
        "missing_plates": [],
    }

    if "UNASSIGNED" in name.upper():
        stats["error"] = "UNASSIGNED (ignoré)"
        return stats

    email = derive_owner_email(name)
    if not email:
        stats["error"] = "nom hors filtre"
        return stats

    drivers = partner_data.get("drivers", [])
    stats["drivers_count"] = len(drivers)

    # Plaques ATTENDUES (avec validation stricte)
    expected_plates = {}
    for drv in drivers:
        vehicle = drv.get("vehicle", {}) or {}
        matricule = vehicle.get("matricule", "N/A")
        marque    = vehicle.get("marque", "N/A")
        modele    = vehicle.get("modele", "N/A")
        # Données de base manquantes
        if marque in (None, "", "N/A") or modele in (None, "", "N/A"):
            stats["invalid_data"] += 1
            continue
        # Plaque invalide (XXX, 000000, etc.)
        if not is_valid_plate(matricule):
            stats["invalid_data"] += 1
            log(f"         ⚠️ Plaque invalide ignorée : '{matricule}' ({drv.get('nom', '?')})")
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

    # Scrape flotte actuelle
    fleet_plates_raw = scrape_fleet_plates(driver)

    # Grouper les plaques par forme canonique (permet de détecter les doublons CI old/new)
    fleet_plate_groups = {}  # {canonical_norm: [raw1, raw2, ...]}
    for p in fleet_plates_raw:
        norm = normalize_plate(p)
        if not norm:
            continue
        fleet_plate_groups.setdefault(norm, []).append(p)

    stats["fleet_current"] = len(fleet_plates_raw)
    stats["fleet_unique_canonical"] = len(fleet_plate_groups)
    duplicates_in_fleet = {n: g for n, g in fleet_plate_groups.items() if len(g) > 1}
    stats["duplicates_in_fleet"] = {n: g for n, g in duplicates_in_fleet.items()}

    log(f"         📊 Attendu: {len(expected_plates)} | Actuel: {len(fleet_plates_raw)} "
        f"(canoniques uniques: {len(fleet_plate_groups)})")
    if duplicates_in_fleet:
        log(f"         🔁 Doublons détectés dans la flotte: {len(duplicates_in_fleet)} plaques "
            f"ont plusieurs formats (ex: 'XXX CI' + 'XXXCI 01')")
        for norm, group in list(duplicates_in_fleet.items())[:3]:
            log(f"             • {norm} → {group}")

    expected_norms = set(expected_plates.keys())
    fleet_norms = set(fleet_plate_groups.keys())

    ok_plates     = expected_norms & fleet_norms
    missing_norms = expected_norms - fleet_norms
    extra_norms   = fleet_norms - expected_norms

    stats["ok"] = len(ok_plates)
    stats["extras"] = len(extra_norms)
    # Pour extras : on liste TOUS les raw correspondant à ces canonicals
    extras_raw_to_delete = []
    for n in extra_norms:
        extras_raw_to_delete.extend(fleet_plate_groups[n])
    stats["extra_plates"] = extras_raw_to_delete
    stats["missing_plates"] = [expected_plates[n]["vehicle"]["matricule"] for n in missing_norms]

    if missing_norms:
        log(f"         ➕ Manquants: {len(missing_norms)}")
    if extra_norms:
        log(f"         🔴 En trop: {len(extra_norms)}")
    if not missing_norms and not extra_norms:
        log(f"         ✅ Flotte OK ({len(ok_plates)} véhicules)")

    # ── AJOUT DES MANQUANTS ──
    if missing_norms and not dry_run and not skip_additions:
        log(f"         🔄 Ajout {len(missing_norms)} véhicules...")
        for i, norm in enumerate(missing_norms, 1):
            drv_data = expected_plates[norm]
            matricule = drv_data["vehicle"]["matricule"]
            log(f"            [{i}/{len(missing_norms)}] {drv_data.get('nom', '?')} → {matricule}")
            if create_vehicle(driver, drv_data):
                stats["missing_added"] += 1
            else:
                stats["missing_failed"] += 1
    elif missing_norms and dry_run:
        log(f"         🧪 [DRY-RUN] {len(missing_norms)} véhicules seraient ajoutés")

    # ── SUPPRESSION : EXTRAS + DOUBLONS ──
    # • Extras   = dans la flotte web mais PAS dans le JSON → à supprimer
    # • Doublons = même canonical N fois dans la flotte    → garder 1, supprimer N-1

    # 1. Extras (raw plates dont le canonical n'est pas dans expected_norms)
    extras_raw_to_delete = []
    for n in extra_norms:
        extras_raw_to_delete.extend(fleet_plate_groups[n])
    stats["extra_plates"] = extras_raw_to_delete

    # 2. Doublons (raw plates en surplus pour les canonicals déjà dans la flotte)
    duplicates_to_delete = []
    for norm, group in fleet_plate_groups.items():
        if len(group) > 1:
            for dup in group[1:]:
                duplicates_to_delete.append(dup)
    stats["duplicates_to_delete"] = len(duplicates_to_delete)

    # Fusion : on supprime d'abord les doublons, puis les extras
    all_to_delete = duplicates_to_delete + [
        p for p in extras_raw_to_delete if p not in duplicates_to_delete
    ]

    if duplicates_to_delete:
        log(f"         🔁 Doublons à supprimer : {len(duplicates_to_delete)}")
    if extras_raw_to_delete:
        log(f"         🔴 Extras à supprimer : {len(extras_raw_to_delete)}")

    if all_to_delete and delete_extras and not dry_run:
        log(f"         �️  Total à supprimer : {len(all_to_delete)} "
            f"({len(duplicates_to_delete)} doublons + {len(extras_raw_to_delete)} extras)")
        log(f"         🌐 Rechargement /manage-fleet pour la suppression")
        driver.get(MANAGE_FLEET_URL)
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
            )
        except TimeoutException:
            log(f"         ❌ Tableau non chargé pour suppression")
        time.sleep(1)
        if _set_pagination_500(driver):
            log(f"         📐 Pagination 500/page activée")
        time.sleep(1)
        _wait_table_stable(driver, timeout=30)
        rows_now = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
        log(f"         ✅ Tableau stable avec {rows_now} lignes")

        deleted_total = 0
        failed_total = 0
        for i, plate in enumerate(all_to_delete, 1):
            norm = normalize_plate(plate)
            if max_deletes and deleted_total + failed_total >= max_deletes:
                log(f"            🛑 Limite --max-deletes={max_deletes} atteinte, stop")
                break
            tag = "🔁 DOUBLON" if plate in duplicates_to_delete else "🔴 EXTRA"
            log(f"            [{i}/{len(all_to_delete)}] {tag} {plate} (→ {norm})")
            if delete_vehicle(driver, plate, debug=debug_delete):
                deleted_total += 1
            else:
                failed_total += 1
        stats["extras_deleted"] = deleted_total
        stats["extras_failed"] = failed_total

    elif all_to_delete and dry_run:
        log(f"         🧪 [DRY-RUN] {len(all_to_delete)} suppression(s) prévues :")
        for p in all_to_delete[:20]:
            tag = "DOUBLON" if p in duplicates_to_delete else "EXTRA"
            log(f"                 • [{tag}] {p} (canonical={normalize_plate(p)})")
        if len(all_to_delete) > 20:
            log(f"                 ... et {len(all_to_delete)-20} autres")
    elif not all_to_delete:
        log(f"         ✅ Aucun doublon ni extra dans la flotte")

    owner_logout(driver)
    return stats


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Audit & sync flotte (VPS headless)")
    parser.add_argument("--start", help="Reprendre à partir de ce partenaire")
    parser.add_argument("--only",  help="Traiter uniquement ce partenaire")
    parser.add_argument("--dry-run", action="store_true", help="Simulation (aucune modif)")
    parser.add_argument("--report-only", action="store_true", help="Rapport seul")
    parser.add_argument("--delete-extras", action="store_true",
                        help="Active la suppression des véhicules en trop")
    parser.add_argument("--headed", action="store_true",
                        help="Lance Chrome en mode visible (debug local)")
    parser.add_argument("--debug-delete", action="store_true",
                        help="Debug verbeux pour la suppression (DOM dumps + screenshots)")
    parser.add_argument("--max-deletes", type=int, default=0,
                        help="Limite le nombre de suppressions par partenaire (0=illimité)")
    parser.add_argument("--skip-additions", action="store_true",
                        help="Ne pas ajouter de véhicules manquants (debug suppression uniquement)")
    parser.add_argument("--clear-only", action="store_true",
                        help="Vider entièrement la flotte de chaque partenaire (sans ajout)")
    parser.add_argument("--input", default=str(REFERENCE_FILE),
                        help="JSON de référence (all_partners_enriched.json)")
    args = parser.parse_args()

    log(f"\n{'='*60}")
    log("🔍 AUDIT FLOTTE — VPS")
    log(f"{'='*60}")

    input_path = Path(args.input)
    if not input_path.exists():
        log(f"❌ Fichier de référence introuvable: {input_path}")
        send_slack(f"❌ audit_fleet: Fichier introuvable — {input_path}", "#ff0000")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        partners = json.load(f)

    log(f"   📂 {len(partners)} partenaires chargés depuis {input_path.name}")

    # Filtrer : exclure UNASSIGNED + nom non-conforme
    valid_partners = []
    skipped = []
    for p in partners:
        nm = p.get("nom", "")
        if "UNASSIGNED" in nm.upper():
            skipped.append(nm); continue
        if not derive_owner_email(nm):
            skipped.append(nm); continue
        valid_partners.append(p)

    def _partner_num(p: dict) -> int:
        """Extrait le numéro du partenaire pour le tri (Partenaire-42 → 42)."""
        m = PARTNER_NAME_RE.match(p.get("nom", "") or "")
        return int(m.group(1)) if m else 9999

    # Trier par numéro croissant : partenaire-1, partenaire-2, ... partenaire-120
    valid_partners.sort(key=_partner_num)

    def _norm_name(s: str) -> str:
        """Normalise un nom de partenaire : minuscules, sans tirets/espaces/underscores."""
        return (s or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")

    if args.only:
        target = _norm_name(args.only)
        matched = [p for p in valid_partners if _norm_name(p["nom"]) == target]
        # Fallback : match par numéro extrait (partenaire42 == partenaires-42)
        if not matched:
            m_target = PARTNER_NAME_RE.match(args.only or "")
            if m_target:
                num = m_target.group(1)
                matched = [p for p in valid_partners
                           if (PARTNER_NAME_RE.match(p["nom"] or "")
                               and PARTNER_NAME_RE.match(p["nom"]).group(1) == num)]
        if not matched:
            available = [p["nom"] for p in valid_partners[:10]]
            log(f"❌ '{args.only}' introuvable. Exemples disponibles: {available}")
            sys.exit(1)
        valid_partners = matched
        log(f"   🎯 Mode --only: {valid_partners[0]['nom']}")
    elif args.start:
        target = _norm_name(args.start)
        names_norm = [_norm_name(p["nom"]) for p in valid_partners]
        if target not in names_norm:
            log(f"❌ '{args.start}' introuvable"); sys.exit(1)
        idx = names_norm.index(target)
        valid_partners = valid_partners[idx:]
        log(f"   ▶️ Reprise depuis {valid_partners[0]['nom']}")

    log(f"   📋 {len(valid_partners)} partenaires à auditer")
    if skipped:
        log(f"   ⏩ Ignorés: {len(skipped)}")

    dry_run = args.dry_run or args.report_only
    delete_extras = args.delete_extras and not dry_run

    if dry_run:
        log(f"   🧪 MODE {'DRY-RUN' if args.dry_run else 'REPORT-ONLY'}")
    if delete_extras:
        log(f"   🗑️  SUPPRESSION DES EXTRAS ACTIVÉE")
    else:
        log(f"   ℹ️ Suppression désactivée (utilise --delete-extras pour activer)")

    send_slack(
        f"🚀 Audit flotte démarré — {len(valid_partners)} partenaires"
        f" | delete={delete_extras} | dry_run={dry_run}",
        "#439FE0"
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / "audit_fleet_report.json"

    browser = setup_driver_headless(headed=args.headed)
    all_stats = []

    def _session_alive(d):
        try:
            _ = d.current_url
            return True
        except Exception:
            return False

    start_time = time.time()

    try:
        for idx, partner in enumerate(valid_partners, 1):
            log(f"\n   ▶️ [{idx}/{len(valid_partners)}] {partner['nom']}")

            if not _session_alive(browser):
                log("      ♻️ Session Chrome perdue, re-création...")
                try: browser.quit()
                except Exception: pass
                browser = setup_driver_headless(headed=args.headed)

            # ── MODE CLEAR-ONLY : vider entièrement la flotte ──────────────
            if args.clear_only:
                email = derive_owner_email(partner["nom"])
                if not owner_login(browser, email, UNIVERSAL_PASSWORD):
                    log(f"      ❌ Login échoué")
                    continue
                # Compter le total initial
                plates_initial = scrape_fleet_plates(browser)
                log(f"      🗑️  {len(plates_initial)} véhicule(s) à supprimer")
                if dry_run:
                    log(f"      🧪 [DRY-RUN] Suppression simulée")
                    owner_logout(browser)
                    continue
                deleted = 0
                consecutive_fails = 0
                while True:
                    # Recharger la page → voir ce qui reste
                    browser.get(MANAGE_FLEET_URL)
                    try:
                        WebDriverWait(browser, 30).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
                        )
                    except TimeoutException:
                        pass
                    time.sleep(1)
                    _set_pagination_500(browser)
                    time.sleep(1)
                    # Lire la première plaque visible
                    rows = browser.find_elements(By.CSS_SELECTOR, "table tbody tr")
                    if not rows:
                        log(f"      ✅ Flotte vide — {deleted} supprimés")
                        break
                    try:
                        col = _find_plate_column_index(browser)
                        cells = rows[0].find_elements(By.TAG_NAME, "td")
                        plate = cells[col].text.strip() if len(cells) > col else ""
                    except Exception:
                        plate = ""
                    if not plate:
                        log(f"      ⚠️ Impossible de lire la plaque de la 1ère ligne")
                        consecutive_fails += 1
                        if consecutive_fails >= 5:
                            log(f"      ❌ Trop d'échecs, arrêt")
                            break
                        continue
                    log(f"      [{deleted+1}] 🗑️  {plate} ({len(rows)} restants)")
                    if delete_vehicle(browser, plate):
                        deleted += 1
                        consecutive_fails = 0
                    else:
                        consecutive_fails += 1
                        if consecutive_fails >= 5:
                            log(f"      ❌ 5 échecs consécutifs, arrêt")
                            break
                owner_logout(browser)
                continue
            # ───────────────────────────────────────────────────────────────

            try:
                stats = audit_partner(browser, partner,
                                      dry_run=dry_run,
                                      delete_extras=delete_extras,
                                      debug_delete=args.debug_delete,
                                      max_deletes=args.max_deletes,
                                      skip_additions=args.skip_additions)
            except (InvalidSessionIdException, WebDriverException) as e:
                log(f"      💥 Session morte: {e}")
                try: browser.quit()
                except Exception: pass
                browser = setup_driver_headless(headed=args.headed)
                try:
                    stats = audit_partner(browser, partner,
                                          dry_run=dry_run,
                                          delete_extras=delete_extras,
                                          debug_delete=args.debug_delete,
                                          max_deletes=args.max_deletes,
                                          skip_additions=args.skip_additions)
                except Exception as e2:
                    stats = {"name": partner["nom"], "login_ok": False, "error": str(e2),
                             "drivers_count": 0, "expected_vehicles": 0, "fleet_current": 0,
                             "ok": 0, "missing_added": 0, "missing_failed": 0,
                             "extras": 0, "extras_deleted": 0, "extras_failed": 0,
                             "invalid_data": 0, "extra_plates": [], "missing_plates": []}
            except Exception as e:
                log(f"      💥 Erreur: {e}")
                traceback.print_exc()
                stats = {"name": partner["nom"], "login_ok": False, "error": str(e),
                         "drivers_count": 0, "expected_vehicles": 0, "fleet_current": 0,
                         "ok": 0, "missing_added": 0, "missing_failed": 0,
                         "extras": 0, "extras_deleted": 0, "extras_failed": 0,
                         "invalid_data": 0, "extra_plates": [], "missing_plates": []}

            all_stats.append(stats)

            # Sauvegarde progressive
            report_path.write_text(
                json.dumps(all_stats, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            # Slack par partenaire (synthèse courte)
            if stats.get("login_ok"):
                line = (
                    f"✅ {stats['name']} | OK={stats['ok']} | "
                    f"➕ {stats['missing_added']}/{stats['missing_added']+stats['missing_failed']} "
                    f"| 🗑️ {stats['extras_deleted']}/{stats['extras_deleted']+stats['extras_failed']}"
                )
                color = "#36a64f"
                if stats['missing_failed'] or stats['extras_failed']:
                    color = "#ffaa00"
                send_slack(line, color)
            elif stats.get("error"):
                send_slack(f"❌ {stats['name']} — {stats['error']}", "#ff0000")

        # ══════════════════════════════════════════════════════════
        # RÉSUMÉ FINAL
        # ══════════════════════════════════════════════════════════
        duration = time.time() - start_time

        total_ok        = sum(s["ok"] for s in all_stats)
        total_added     = sum(s["missing_added"] for s in all_stats)
        total_add_fail  = sum(s["missing_failed"] for s in all_stats)
        total_deleted   = sum(s["extras_deleted"] for s in all_stats)
        total_del_fail  = sum(s["extras_failed"] for s in all_stats)
        total_extras    = sum(s["extras"] for s in all_stats)
        total_invalid   = sum(s["invalid_data"] for s in all_stats)
        failed_login    = [s for s in all_stats if not s.get("login_ok")]

        log(f"\n{'='*60}")
        log("✨ AUDIT TERMINÉ")
        log(f"{'='*60}")
        log(f"   ⏱️  Durée: {duration/60:.1f} min")
        log(f"   🏢 Partenaires traités: {len(all_stats)}")
        log(f"   ✅ Véhicules OK: {total_ok}")
        log(f"   ➕ Ajoutés: {total_added} (échecs: {total_add_fail})")
        log(f"   🗑️  Supprimés: {total_deleted} (échecs: {total_del_fail})")
        log(f"   🔴 Extras totaux: {total_extras}")
        log(f"   ⚠️  Données invalides: {total_invalid}")
        if failed_login:
            log(f"   ❌ Login échoué: {len(failed_login)}")

        final_report = {
            "timestamp": datetime.now().isoformat(),
            "duration_seconds": duration,
            "dry_run": dry_run,
            "delete_extras": delete_extras,
            "summary": {
                "partners_total": len(all_stats),
                "vehicles_ok": total_ok,
                "vehicles_added": total_added,
                "vehicles_add_failed": total_add_fail,
                "vehicles_deleted": total_deleted,
                "vehicles_delete_failed": total_del_fail,
                "vehicles_extras_total": total_extras,
                "invalid_data": total_invalid,
                "login_failed": len(failed_login),
            },
            "details": all_stats,
        }
        report_path.write_text(
            json.dumps(final_report, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        log(f"   📄 Rapport: {report_path}")

        # Slack final
        recap = (
            f"✨ Audit flotte TERMINÉ\n"
            f"• Partenaires: {len(all_stats)}\n"
            f"• ✅ OK: {total_ok}\n"
            f"• ➕ Ajoutés: {total_added} (échecs {total_add_fail})\n"
            f"• 🗑️ Supprimés: {total_deleted} (échecs {total_del_fail})\n"
            f"• ⏱️ {duration/60:.1f} min"
        )
        color = "#36a64f" if (total_add_fail == 0 and total_del_fail == 0 and not failed_login) else "#ffaa00"
        send_slack(recap, color)

    except KeyboardInterrupt:
        log("\n🛑 Interrompu.")
        send_slack("🛑 Audit flotte interrompu manuellement", "#ffaa00")
    except Exception as e:
        log(f"\n💥 Erreur fatale: {e}")
        traceback.print_exc()
        send_slack(f"❌ audit_fleet CRASH — {e}", "#ff0000")
    finally:
        time.sleep(2)
        try: browser.quit()
        except Exception: pass


if __name__ == "__main__":
    main()
