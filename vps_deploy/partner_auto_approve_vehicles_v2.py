import argparse
import json
import os
import re
import sys
import time
import shutil
from pathlib import Path
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

# Encodage UTF-8 Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_URL             = "https://upjunoo-server-new.junooapps.com"
OWNER_LOGIN_URL      = f"{BASE_URL}/login/owner-login"
FLEET_URL            = f"{BASE_URL}/manage-fleet"

OUTPUT_DIR           = Path("output")
ORGANIZED_DIR        = OUTPUT_DIR / "organized_by_partner"

PARTNER_NAME_RE = re.compile(r'^(partenaire|partenaires)[-_\s]*(\d+)', re.IGNORECASE)

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

# ─── Utils ────────────────────────────────────────────────────────────────────
def derive_owner_email(partner_name: str):
    m = PARTNER_NAME_RE.match(partner_name or "")
    if not m: return None
    prefix = m.group(1).lower()
    num = m.group(2)
    return f"{prefix}{num}@upjunoo.com"

def normalize_partner_name(name: str) -> str:
    clean = re.sub(r'[-_\s]', '', name.lower())
    if clean.startswith("partenaires"): clean = "partenaire" + clean[11:]
    return clean

def normalize_immat(text: str) -> str:
    if not text: return ""
    return re.sub(r'[^a-zA-Z0-9]', '', str(text)).upper()

def extract_partner_number(name: str) -> int:
    m = PARTNER_NAME_RE.match(name or "")
    return int(m.group(2)) if m else 0

def find_partner_folders(organized_dir: Path) -> list:
    if not organized_dir.exists(): return []
    partners = [item for item in organized_dir.iterdir() if item.is_dir() and PARTNER_NAME_RE.match(item.name)]
    partners.sort(key=lambda p: extract_partner_number(p.name))
    return partners

# ─── Selenium ─────────────────────────────────────────────────────────────────
def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless: opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    try:
        return webdriver.Chrome(options=opts)
    except:
        svc = Service(shutil.which("chromedriver") or shutil.which("chromedriver.exe") or "chromedriver")
        return webdriver.Chrome(service=svc, options=opts)

def login_partner(driver, email, password) -> bool:
    log(f"[LOGIN] Connexion : {email}")
    try:
        driver.delete_all_cookies()
        driver.get(OWNER_LOGIN_URL)
        wait = WebDriverWait(driver, 20)
        e = wait.until(EC.presence_of_element_located((By.ID, "email-input")))
        e.clear(); e.send_keys(email)
        p = driver.find_element(By.ID, "password-input")
        p.clear(); p.send_keys(password)
        
        btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[@type='submit'] | //button[contains(., 'Connexion')]")))
        driver.execute_script("arguments[0].click();", btn)
        
        wait.until(lambda d: "login" not in d.current_url.lower())
        return True
    except Exception as e:
        log(f"❌ Login Erreur : {e}", "ERROR"); return False

def set_page_size(driver, size=500):
    try:
        wait = WebDriverWait(driver, 10)
        # On attend d'abord que le sélecteur existe
        selectors = ["select.form-select-sm", "select[name*='_length']", ".dataTables_length select", "select.form-select"]
        element = None
        for sel in selectors:
            try:
                element = driver.find_element(By.CSS_SELECTOR, sel)
                if element: break
            except: continue
        
        if element:
            s = Select(element); target = str(size)
            current = str(s.first_selected_option.get_attribute("value"))
            if current != target:
                s.select_by_value(target)
                log(f"   [PAGE] Taille réglée : {current} -> {target}")
                time.sleep(4)
        return True
    except Exception as e:
        log(f"   [PAGE] Erreur réglage : {e}", "WARNING")
        return False

def wait_for_table_data(driver, timeout=12):
    start = time.time()
    ignore_mots = ["chargement", "loading", "aucune", "no data", "na", "cartegrise"]
    while (time.time() - start) < timeout:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if len(rows) > 1: # On veut au moins 2 lignes pour être sûr
            txt = rows[0].text.lower()
            if not any(m in txt for m in ignore_mots):
                return True
        time.sleep(1)
    return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Partner folder name")
    parser.add_argument("--start", help="Start partner number")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    partners = find_partner_folders(ORGANIZED_DIR)
    if args.only:
        partners = [p for p in partners if normalize_partner_name(p.name) == normalize_partner_name(args.only)]
    if args.start:
        start_num = int(args.start)
        partners = [p for p in partners if extract_partner_number(p.name) >= start_num]

    driver = None
    try:
        for p_dir in partners:
            data_path = p_dir / "data_final.json"
            if not data_path.exists(): data_path = p_dir / "data.json"
            if not data_path.exists(): continue
            
            with open(data_path, "r", encoding="utf-8") as f: p_data = json.load(f)
            email = p_data.get("email") or p_data.get("partner_email") or derive_owner_email(p_dir.name)
            if not email: continue

            targets = {}
            items = p_data.get("drivers") or p_data.get("data") or []
            lk = "drivers" if "drivers" in p_data else "data"
            for i, d in enumerate(items):
                raw_mat = d.get("vehicle", {}).get("matricule", "") if lk=="drivers" else d.get("vehicle_matricule", "")
                norm_mat = normalize_immat(raw_mat)
                status = str(d.get("type_vehicule") or d.get("audit_status_tab") or "").upper()
                if "APPROUV" in status and d.get("owner_approval_status") != "DONE" and norm_mat and len(norm_mat) > 1:
                    targets[norm_mat] = {"idx": i, "raw": str(raw_mat).upper()}

            if not targets:
                log(f"   ✅ {p_dir.name} : Déjà à jour."); continue

            if not driver: driver = make_driver(headless=not args.no_headless)
            if not login_partner(driver, email, "123456789@"): continue
            driver.get(FLEET_URL); time.sleep(4)
            log(f"[START] Approbation {p_dir.name} ({len(targets)} cibles restantes)")

            empty_scans = 0
            while targets and empty_scans < 3:
                set_page_size(driver, 500)
                if not wait_for_table_data(driver, 10):
                    log(f"   ♻️ Tableau semble bloqué. Tentative de Hard Reload...")
                    driver.get(FLEET_URL); time.sleep(5)
                    set_page_size(driver, 500)
                    wait_for_table_data(driver, 8)

                rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                found_on_page = []
                for r in rows:
                    if not r.is_displayed(): continue
                    try:
                        row_txt = r.text.upper()
                        # Match hybride
                        for norm_mat, info in targets.items():
                            if norm_mat in normalize_immat(row_txt) or (len(info['raw']) > 2 and info['raw'] in row_txt):
                                found_on_page.append({"el": r, "immat": norm_mat, "raw": info['raw'], "txt": row_txt.replace("\n", " | ")})
                                break
                    except: continue
                
                if not found_on_page:
                    empty_scans += 1
                    log(f"   🔎 Scan {empty_scans}/3 : Aucune cible sur {len(rows)} lignes. Hard reload au prochain essai.")
                    driver.get(FLEET_URL); time.sleep(4); continue
                
                empty_scans = 0
                item = found_on_page[0]
                norm_immat = item["immat"]

                log(f"   [PROCESS] Cible : {item['raw']}")
                
                # Double vérification AVANT action
                try:
                    current_txt = item["el"].text.upper()
                    if norm_immat not in normalize_immat(current_txt) and item['raw'] not in current_txt:
                        log(f"      ⚠️ Décalage détecté, on annule ce clic pour sécurité.", "WARNING")
                        time.sleep(2); continue
                        
                    log(f"      [MATCH] Ligne confirmée : {current_txt.replace('\n',' ')[:80]}...")

                    # Action
                    pts = item["el"].find_element(By.XPATH, ".//td[last()]//i")
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pts); time.sleep(1)
                    driver.execute_script("arguments[0].click();", pts); time.sleep(2.5)
                    
                    btns = driver.find_elements(By.XPATH, "//a[contains(., 'Approuver')] | //span[contains(., 'Approuver')]")
                    btn = next((b for b in btns if b.is_displayed()), None)
                    
                    if btn:
                        driver.execute_script("arguments[0].click();", btn); time.sleep(2.5)
                        # Swals
                        for _ in range(2):
                            try:
                                s = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-confirm, .btn-primary")))
                                s.click(); time.sleep(2)
                            except: break
                        
                        log(f"       ✅ {norm_immat} approuvé avec succès.")
                        p_data[lk][targets[norm_immat]["idx"]]["owner_approval_status"] = "DONE"
                        with open(data_path, "w", encoding="utf-8") as fw: json.dump(p_data, fw, indent=2, ensure_ascii=False)
                    else:
                        log(f"      ❌ Bouton 'Approuver' non détecté.", "WARNING")
                except Exception as e:
                    log(f"      ❌ Erreur sur {norm_immat} : {e}", "WARNING")

                del targets[norm_immat]
                time.sleep(3) # On laisse Junoo souffler

            log(f"[END] {p_dir.name} terminé."); driver.get(f"{BASE_URL}/logout"); time.sleep(2)
    finally:
        if driver: driver.quit()

if __name__ == "__main__": main()
