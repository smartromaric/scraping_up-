import argparse
import csv
import io
import re
import shutil
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait


# Encodage UTF-8 Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


# ─── Config ───────────────────────────────────────────────────────────────────
BASE_URL = "https://upjunoo-server-new.junooapps.com"
ADMIN_LOGIN_URL = f"{BASE_URL}/login/admin"
MANAGE_OWNERS_URL = f"{BASE_URL}/manage-owners"

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
INPUT_CSV = PROJECT_ROOT / "output" / "partner_drivers_details.csv"
DEFAULT_OUTPUT_XLSX = SCRIPT_DIR / "output" / "drivers_check_by_partner.xlsx"

ADMIN_EMAIL = "admin@upjunoo.com"
ADMIN_PASSWORD = "123456789"


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{ts}] [{level}]"
    if level == "OK":
        prefix = f"\033[92m{prefix}\033[0m"
    elif level == "WARNING":
        prefix = f"\033[93m{prefix}\033[0m"
    elif level == "ERROR":
        prefix = f"\033[91m{prefix}\033[0m"
    print(f"{prefix} {msg}", flush=True)


def normalize_text(value: str) -> str:
    value = (value or "").strip().lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"\s+", " ", value)
    return value


def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1920,1080")
    try:
        return webdriver.Chrome(options=opts)
    except Exception:
        svc = Service(shutil.which("chromedriver.exe") or "chromedriver.exe")
        return webdriver.Chrome(service=svc, options=opts)


def wait_for_table_data(driver: webdriver.Chrome, timeout: int = 30, min_rows: int = 1) -> bool:
    start = time.time()
    ignore_words = ["chargement", "loading", "aucune", "no data", "vide", "en attente"]
    while (time.time() - start) < timeout:
        try:
            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            if len(rows) >= min_rows:
                txt = rows[0].text.lower()
                if not any(w in txt for w in ignore_words):
                    return True
            time.sleep(1)
        except Exception:
            time.sleep(1)
    return False


def highlight(driver: webdriver.Chrome, element: Any, color: str = "blue", duration: float = 1) -> None:
    try:
        driver.execute_script(f"arguments[0].style.border='4px solid {color}';", element)
        if duration > 0:
            time.sleep(duration)
    except Exception:
        pass


def set_page_size(driver: webdriver.Chrome, size: int = 500) -> bool:
    try:
        time.sleep(3)
        selectors = [
            "select.form-select-sm",
            "select[name*='_length']",
            ".dataTables_length select",
            "select.form-select",
        ]
        element = None
        for sel in selectors:
            try:
                element = driver.find_element(By.CSS_SELECTOR, sel)
                if element:
                    break
            except Exception:
                continue
        if not element:
            return False
        select = Select(element)
        target = str(size)
        current_val = ""
        try:
            current_val = select.first_selected_option.get_attribute("value") or ""
        except Exception:
            current_val = ""

        if current_val != target:
            highlight(driver, element, "orange")
            options = [o.get_attribute("value") for o in select.options]
            if target in options:
                select.select_by_value(target)
            else:
                # Comportement identique au script de référence: max dispo
                select.select_by_index(len(select.options) - 1)
            log(f"   [PAGE] Passage a {target} (ou max)... Attente de chargement...")
            time.sleep(5)
            wait_for_table_data(driver, timeout=60, min_rows=2)
            return True
        log(f"   [PAGE] Taille deja positionnee a {current_val or target}.")
        return True
    except Exception as exc:
        log(f"   [PAGE] Erreur : {exc}", "WARNING")
        return False


def try_datatable_search(driver: webdriver.Chrome, query: str, timeout: int = 8) -> bool:
    try:
        search_box = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[type='search'], .dataTables_filter input"),
            ),
        )
        search_box.clear()
        search_box.send_keys(query)
        time.sleep(2)
        return True
    except Exception:
        return False


def row_contains_query(row: Any, query_norm: str) -> bool:
    try:
        row_text = normalize_text(row.text or "")
        return query_norm in row_text
    except Exception:
        return False


def find_row_by_cell_text_with_pagination(
    driver: webdriver.Chrome,
    target_text: str,
    max_pages: int = 30,
) -> Optional[Any]:
    query_norm = normalize_text(target_text)
    if not query_norm:
        return None

    next_btn_xpath = "//li[contains(@class, 'next') and not(contains(@class, 'disabled'))]/a"
    for _ in range(max_pages):
        if not wait_for_table_data(driver, timeout=15, min_rows=1):
            break
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            if not row.is_displayed():
                continue
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                for cell in cells:
                    if query_norm in normalize_text(cell.text or ""):
                        return row
            except Exception:
                continue
        try:
            next_btn = driver.find_element(By.XPATH, next_btn_xpath)
            driver.execute_script("arguments[0].click();", next_btn)
            time.sleep(2)
        except Exception:
            break
    return None


def open_partner_profile(driver: webdriver.Chrome, wait: WebDriverWait, row: Any) -> bool:
    try:
        old_url = driver.current_url
        highlight(driver, row, "blue", duration=0.4)
        profile_btn = None
        try:
            profile_btn = row.find_element(
                By.XPATH,
                ".//a[contains(@href, 'profile') or contains(@href, 'view') or contains(@href, 'edit')]",
            )
        except Exception:
            pass
        if not profile_btn:
            candidates = row.find_elements(By.CSS_SELECTOR, "a.btn, button.btn, a, button")
            if candidates:
                profile_btn = candidates[-1]
        if not profile_btn:
            return False
        log("   [CLICK] Ouverture du profil partenaire...")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", profile_btn)
        highlight(driver, profile_btn, "green", duration=0.4)
        time.sleep(0.8)
        driver.execute_script("arguments[0].click();", profile_btn)
        # Aligne le comportement avec sync_admin_fleet_status.py:
        # attendre une vraie transition, puis laisser le profil se stabiliser.
        wait.until(
            lambda d: (
                d.current_url != old_url
                or "profile" in d.current_url.lower()
                or len(
                    d.find_elements(
                        By.XPATH,
                        "//a[contains(., 'flotte') or contains(., 'Flotte') or contains(., 'Détails de la flotte')]",
                    ),
                )
                > 0
            ),
        )
        log(f"   [NAV] Transition profil detectee (URL: {driver.current_url})")
        time.sleep(30)
        return True
    except Exception:
        return False


def open_fleet_tab(driver: webdriver.Chrome, wait: WebDriverWait) -> bool:
    selectors = [
        "//a[contains(., 'flotte') or contains(., 'Flotte')]",
        "//span[contains(., 'flotte') or contains(., 'Flotte')]",
        "//a[contains(., 'Détails de la flotte')]",
        "//button[contains(., 'Flotte')]",
    ]
    try:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        tab = None
        for sel in selectors:
            elements = driver.find_elements(By.XPATH, sel)
            for el in elements:
                if el.is_displayed():
                    tab = el
                    break
            if tab:
                break
        if not tab:
            return False
        log("   [CLICK] Passage a l'onglet 'Details de la flotte'...")
        highlight(driver, tab, "green", duration=0.4)
        driver.execute_script("arguments[0].click();", tab)
        time.sleep(5)
        wait_for_table_data(driver, timeout=30, min_rows=1)
        page_set_ok = set_page_size(driver, size=500)
        if not page_set_ok:
            log("[WARN] Impossible de forcer la pagination flotte a 500.", "WARNING")
        else:
            log("   [OK] Pagination flotte reglee (500 ou max).", "OK")
        wait_for_table_data(driver, timeout=30, min_rows=1)
        return True
    except Exception:
        return False


def search_driver_in_current_partner(driver: webdriver.Chrome, driver_name: str) -> bool:
    query = (driver_name or "").strip()
    if not query:
        return False
    # Essai via barre de recherche locale
    used_search = try_datatable_search(driver, query)
    if used_search:
        if wait_for_table_data(driver, timeout=8, min_rows=1):
            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            qn = normalize_text(query)
            for row in rows:
                if row.is_displayed() and row_contains_query(row, qn):
                    return True
    # Fallback scan + pagination
    return find_row_by_cell_text_with_pagination(driver, query, max_pages=25) is not None


def load_csv_rows(csv_path: Path) -> List[Dict[str, str]]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV introuvable: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def build_groups(rows: List[Dict[str, str]]) -> Dict[Tuple[str, str], List[Dict[str, str]]]:
    groups: Dict[Tuple[str, str], List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        email = (row.get("email") or "").strip()
        partner = (row.get("nom_partenaire") or "").strip()
        groups[(email, partner)].append(row)
    return groups


def write_excel(output_path: Path, found: List[Dict[str, str]], not_found: List[Dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws_found = wb.active
    ws_found.title = "trouves"
    ws_missing = wb.create_sheet("non_trouves")

    headers = [
        "nom_partenaire",
        "email",
        "chauffeur_nom",
        "chauffeur_telephone",
        "vehicle_matricule",
        "found_on_admin",
        "reason",
        "checked_at",
    ]
    ws_found.append(headers)
    ws_missing.append(headers)

    for row in found:
        ws_found.append([row.get(h, "") for h in headers])
    for row in not_found:
        ws_missing.append([row.get(h, "") for h in headers])
    wb.save(output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=str(INPUT_CSV), help="Chemin du CSV source")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_XLSX), help="Chemin du fichier Excel de sortie")
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Limiter le nombre de chauffeurs (test)")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    output_path = Path(args.output)

    rows = load_csv_rows(csv_path)
    if args.limit > 0:
        rows = rows[: args.limit]
    groups = build_groups(rows)

    found_rows: List[Dict[str, str]] = []
    missing_rows: List[Dict[str, str]] = []

    driver = make_driver(headless=not args.no_headless)
    wait = WebDriverWait(driver, 25)
    checked_at = datetime.now().isoformat(timespec="seconds")

    try:
        log(f"[ADMIN] Connexion a {ADMIN_EMAIL} ...")
        driver.get(ADMIN_LOGIN_URL)
        email_el = wait.until(EC.presence_of_element_located((By.ID, "email-input")))
        email_el.send_keys(ADMIN_EMAIL)
        pwd_el = driver.find_element(By.ID, "password-input")
        pwd_el.send_keys(ADMIN_PASSWORD)
        submit = driver.find_element(By.XPATH, "//button[@type='submit']")
        submit.click()
        wait.until(lambda d: "login" not in d.current_url.lower())
        log("[OK] Connexion admin reussie.", "OK")

        log(f"[INFO] Groupes partenaires a traiter: {len(groups)}")

        for (partner_email, partner_name), partner_rows in groups.items():
            query_email = partner_email.strip()
            query_name = partner_name.strip()
            log(f"\n[PARTNER] {query_name or '(sans nom)'} | {query_email or '(sans email)'}")

            driver.get(MANAGE_OWNERS_URL)
            time.sleep(2)
            wait_for_table_data(driver, timeout=30, min_rows=1)
            set_page_size(driver, size=500)
            wait_for_table_data(driver, timeout=30, min_rows=1)

            # Recherche partenaire: methode sync_admin_fleet_status.py
            partner_row = None
            if query_email:
                try_datatable_search(driver, query_email)
                partner_row = find_row_by_cell_text_with_pagination(driver, query_email, max_pages=25)
            if partner_row is None and query_name:
                try_datatable_search(driver, query_name)
                partner_row = find_row_by_cell_text_with_pagination(driver, query_name, max_pages=25)

            if partner_row is None:
                reason = "partner_not_found"
                log(f"[WARN] Partenaire introuvable (email/nom): {query_email or query_name or '(vide)'}", "WARNING")
                for base_row in partner_rows:
                    missing_rows.append(
                        {
                            "nom_partenaire": base_row.get("nom_partenaire", ""),
                            "email": base_row.get("email", ""),
                            "chauffeur_nom": base_row.get("chauffeur_nom", ""),
                            "chauffeur_telephone": base_row.get("chauffeur_telephone", ""),
                            "vehicle_matricule": base_row.get("vehicle_matricule", ""),
                            "found_on_admin": "no",
                            "reason": reason,
                            "checked_at": checked_at,
                        },
                    )
                continue

            if not open_partner_profile(driver, wait, partner_row):
                log("[WARN] Impossible d'ouvrir le profil partenaire.", "WARNING")
                for base_row in partner_rows:
                    missing_rows.append(
                        {
                            "nom_partenaire": base_row.get("nom_partenaire", ""),
                            "email": base_row.get("email", ""),
                            "chauffeur_nom": base_row.get("chauffeur_nom", ""),
                            "chauffeur_telephone": base_row.get("chauffeur_telephone", ""),
                            "vehicle_matricule": base_row.get("vehicle_matricule", ""),
                            "found_on_admin": "no",
                            "reason": "partner_profile_unavailable",
                            "checked_at": checked_at,
                        },
                    )
                continue

            if not open_fleet_tab(driver, wait):
                log("[WARN] Onglet flotte introuvable pour ce partenaire.", "WARNING")
                for base_row in partner_rows:
                    missing_rows.append(
                        {
                            "nom_partenaire": base_row.get("nom_partenaire", ""),
                            "email": base_row.get("email", ""),
                            "chauffeur_nom": base_row.get("chauffeur_nom", ""),
                            "chauffeur_telephone": base_row.get("chauffeur_telephone", ""),
                            "vehicle_matricule": base_row.get("vehicle_matricule", ""),
                            "found_on_admin": "no",
                            "reason": "fleet_tab_unavailable",
                            "checked_at": checked_at,
                        },
                    )
                continue

            for base_row in partner_rows:
                name = (base_row.get("chauffeur_nom") or "").strip()
                if not name:
                    missing_rows.append(
                        {
                            "nom_partenaire": base_row.get("nom_partenaire", ""),
                            "email": base_row.get("email", ""),
                            "chauffeur_nom": "",
                            "chauffeur_telephone": base_row.get("chauffeur_telephone", ""),
                            "vehicle_matricule": base_row.get("vehicle_matricule", ""),
                            "found_on_admin": "no",
                            "reason": "empty_driver_name",
                            "checked_at": checked_at,
                        },
                    )
                    continue

                # Revenir au début pagination pour chaque recherche
                try_datatable_search(driver, "")
                time.sleep(1)
                found = search_driver_in_current_partner(driver, name)
                target = found_rows if found else missing_rows
                target.append(
                    {
                        "nom_partenaire": base_row.get("nom_partenaire", ""),
                        "email": base_row.get("email", ""),
                        "chauffeur_nom": name,
                        "chauffeur_telephone": base_row.get("chauffeur_telephone", ""),
                        "vehicle_matricule": base_row.get("vehicle_matricule", ""),
                        "found_on_admin": "yes" if found else "no",
                        "reason": "ok" if found else "driver_not_found",
                        "checked_at": checked_at,
                    },
                )

        write_excel(output_path, found_rows, missing_rows)
        total = len(found_rows) + len(missing_rows)
        log("\n" + "=" * 60)
        log("BILAN VERIFICATION CHAUFFEURS")
        log(f"Total lignes traitees : {total}")
        log(f"Trouves             : {len(found_rows)}", "OK")
        log(f"Non trouves         : {len(missing_rows)}", "WARNING")
        log(f"Fichier Excel       : {output_path}", "OK")
        log("=" * 60)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
