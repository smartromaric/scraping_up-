import argparse
import json
import os
import re
import sys
import time
import traceback
import shutil
from pathlib import Path
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Encodage UTF-8 Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_URL      = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL     = f"{BASE_URL}/login/admin"
WAIT          = 30

_SCRIPT_DIR   = Path(__file__).parent
OUTPUT_DIR    = _SCRIPT_DIR / "output"
ORGANIZED_DIR = OUTPUT_DIR / "organized_by_partner"

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ─── Driver (Stable sans webdriver_manager) ──────────────────────────────────
def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    
    chromedriver_path = None
    for cd in ["chromedriver.exe", "chromedriver", "/usr/bin/chromedriver", "C:\\bin\\chromedriver.exe"]:
        if shutil.which(cd):
            chromedriver_path = shutil.which(cd)
            break
            
    if chromedriver_path:
        svc = Service(chromedriver_path)
        return webdriver.Chrome(service=svc, options=opts)
    else:
        return webdriver.Chrome(options=opts)

# ─── Actions Admin ────────────────────────────────────────────────────────────
def login_admin(driver, email, password):
    log(f"[LOGIN] Admin : {email}")
    driver.get(LOGIN_URL)
    w = WebDriverWait(driver, WAIT)
    try:
        ef = w.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']")))
        ef.clear(); ef.send_keys(email)
        pf = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        pf.clear(); pf.send_keys(password)
        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        w.until(lambda d: "login" not in d.current_url.lower())
        log("   [OK] Connecté.")
        return True
    except Exception as e:
        log(f"   [ERR] Login : {e}")
        return False

def approve_document(driver, doc_link, immat):
    log(f"   [APPROVE] Traitement de {immat}...")
    try:
        driver.get(doc_link)
        w = WebDriverWait(driver, 10)
        
        # Vérifier si déjà approuvé
        try:
            status_badge = driver.find_element(By.CSS_SELECTOR, ".badge-success, .text-success")
            if "approuv" in status_badge.text.lower():
                log(f"   [INFO] {immat} est déjà marqué comme approuvé sur le site.")
                return True
        except: pass

        # Cliquer Approuver
        btn = w.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Approuver')]")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.5)
        btn.click()
        
        # Confirmation Swal
        confirm = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-confirm")))
        confirm.click()
        log(f"   [OK] {immat} approuvé avec succès.")
        time.sleep(1)
        return True
    except Exception as e:
        log(f"   [FAIL] {immat} : {e}")
        return False

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Nom du partenaire")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    partners = [d for d in ORGANIZED_DIR.iterdir() if d.is_dir() and "unassigned" not in d.name.lower()]
    def get_num(d):
        m = re.search(r'\d+', d.name)
        return int(m.group()) if m else 0
    partners.sort(key=get_num)
    partners = [p for p in partners if get_num(p) >= args.start]
    
    if args.only:
        partners = [p for p in partners if p.name.lower() == args.only.lower()]

    if not partners:
        log("Aucun partenaire à traiter.")
        return

    log(f"[START] Automate d'approbation lancé sur {len(partners)} partenaires.")
    driver = make_driver(headless=not args.no_headless)

    try:
        if not login_admin(driver, "admin@upjunoo.com", "123456789"):
            return

        for p_dir in partners:
            log(f"[INFO] Analyse des rapports pour {p_dir.name}...")
            
            # Trouver le fichier fleet_approval_report_final_*.json le plus récent
            reports = list(p_dir.glob("fleet_approval_report_final_*.json"))
            if not reports:
                log("   [SKIP] Aucun rapport JSON trouvé.")
                continue
            
            latest_report = max(reports, key=lambda f: f.stat().st_mtime)
            log(f"   [FILE] Lecture de {latest_report.name}")
            
            with open(latest_report, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            vehicles = data.get("vehicles", [])
            to_approve = [v for v in vehicles if v.get("detailed_status") == "En attente d'approbation"]
            
            if not to_approve:
                log("   [SKIP] Aucun véhicule en attente d'approbation trouvé dans ce rapport.")
                continue

            log(f"   [WORK] {len(to_approve)} approbations à effectuer...")
            
            file_modified = False
            for veh in vehicles:
                immat = veh.get("immat") or veh.get("immatriculation") or "Sans Immat"
                
                if veh.get("detailed_status") == "En attente d'approbation":
                    if approve_document(driver, veh["doc_link"], immat):
                        veh["status_tab"] = "APPROUVÉ"
                        veh["detailed_status"] = "Approuvé par automate"
                        file_modified = True
                
                # Optionnel : si le rapport disait autre chose mais qu'il est déjà approuvé, on synchronise
                # (déjà géré dans approve_document qui retourne True si déjà approuvé)

            if file_modified:
                with open(latest_report, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                log(f"   [SAVE] Rapport {latest_report.name} mis à jour.")

            log(f"[DONE] Partenaire {p_dir.name} terminé.")

    except Exception:
        traceback.print_exc()
    finally:
        if driver: driver.quit()
        log("[END] Script terminé.")

if __name__ == "__main__":
    main()
