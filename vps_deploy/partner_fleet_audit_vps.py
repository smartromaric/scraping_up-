# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Encodage UTF-8 Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException, TimeoutException, StaleElementReferenceException
)
from webdriver_manager.chrome import ChromeDriverManager

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_URL            = "https://upjunoo-server-new.junooapps.com"
OWNER_LOGIN_URL     = f"{BASE_URL}/login/owner-login"
MANAGE_FLEET_URL    = f"{BASE_URL}/manage-fleet"

_SCRIPT_DIR   = Path(__file__).parent
OUTPUT_DIR    = _SCRIPT_DIR / "output"
ORGANIZED_DIR = OUTPUT_DIR / "organized_by_partner"

UNIVERSAL_PASSWORD  = "123456789@"
WAIT                = 20

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ─── Copie EXACTE des fonctions Admin Scraper (Pagination) ────────────────────

def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--window-size=1920,1080")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    svc = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.set_page_load_timeout(60)
    return driver

def set_page_size(driver: webdriver.Chrome, size: int = 500):
    selectors = [
        "select.form-select.form-select-sm",
        ".form-select-sm",
        "select[class*='form-select']",
        "select[name$='_length']",
        ".dataTables_length select",
    ]
    try:
        WebDriverWait(driver, WAIT).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
    except: return

    for sel in selectors:
        try:
            element = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            s = Select(element)
            vals = [o.get_attribute("value") for o in s.options]
            target = None
            for v in vals:
                if v == str(size): target = v; break
            if not target:
                target = max(vals, key=lambda v: int(v) if v.isdigit() else 0)
            
            if s.first_selected_option.get_attribute("value") == target:
                return 

            s.select_by_value(target)
            # Forcer le déclenchement de l'événement de changement (JS)
            driver.execute_script("arguments[0].dispatchEvent(new Event('change'))", element)
            log(f"[PAGE] Sélection de {target} entrées forcée via JS.")
            
            # Attente renforcée du rechargement
            time.sleep(2)
            try:
                # Attendre loader
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".dataTables_processing, .loading-overlay, .spinner-border"))
                )
                WebDriverWait(driver, WAIT).until_not(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, ".dataTables_processing, .loading-overlay, .spinner-border"))
                )
            except:
                pass
            
            time.sleep(3)
            # Debug: Capture d'écran pour voir si le tableau a grandi
            driver.save_screenshot("debug_500_entries.png")
            log(f"[PAGE] Tableau rechargé. Debug saved: debug_500_entries.png")

            WebDriverWait(driver, WAIT).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
            return
        except: continue

def click_next(driver: webdriver.Chrome, first_id_before: str = "") -> bool:
    """Clique sur Suivant exactement comme le script admin."""
    def get_nav_btn():
        lis = driver.find_elements(By.CSS_SELECTOR, "ul.pagination li.page-item")
        for li in lis:
            txt = li.text.strip().lower()
            if "suivant" in txt or "next" in txt or "›" in txt:
                if "disabled" in (li.get_attribute("class") or ""): return None
                try: return li.find_element(By.TAG_NAME, "a")
                except: return li
        return None

    btn = get_nav_btn()
    if not btn: return False
    try:
        driver.execute_script("arguments[0].click();", btn)
    except: return False

    if first_id_before:
        # log(f"[NAV] Attente changement de page (ID précédent: {first_id_before})...")
        deadline = time.time() + 10
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                if not rows: continue
                cells = rows[0].find_elements(By.TAG_NAME, "td")
                if len(cells) > 3:
                    links = cells[3].find_elements(By.TAG_NAME, "a")
                    for a in links:
                        href = a.get_attribute("href") or ""
                        new_id = href.split("/document/")[-1].strip("/")
                        if new_id and new_id != first_id_before:
                            return True
            except: pass
    else:
        time.sleep(2)
    return True

# ─── Audit Specifique ─────────────────────────────────────────────────────────

def get_detailed_doc_status(driver, doc_url: str) -> str:
    original_handle = driver.current_window_handle
    driver.execute_script("window.open(arguments[0]);", doc_url)
    driver.switch_to.window(driver.window_handles[-1])
    status = "Inconnu"
    try:
        wait = WebDriverWait(driver, 10)
        table = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table")))
        row = table.find_element(By.CSS_SELECTOR, "tbody tr")
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) >= 4:
            status = cells[3].text.strip()
    except:
        status = "Non trouvé / Erreur"
    driver.close()
    driver.switch_to.window(original_handle)
    return status

def scrape_full_audit(driver, limit: int = 0):
    set_page_size(driver, 500)
    all_final_data = []
    page_num = 1
    
    while True:
        log(f"[SCRAPE] Page {page_num}...")
        try:
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        except: break

        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if len(rows) == 1 and ("Aucune donnée" in rows[0].text or "No data" in rows[0].text):
            break

        current_batch = []
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6: continue
                
                doc_link = ""
                doc_id = ""
                try: 
                    a = cells[3].find_element(By.TAG_NAME, "a")
                    doc_link = a.get_attribute("href")
                    doc_id = doc_link.split("/document/")[-1].strip("/")
                except: pass

                current_batch.append({
                    "type": cells[0].text.strip(),
                    "marque": cells[1].text.strip(),
                    "modele": cells[2].text.strip(),
                    "immat": cells[4].text.strip(),
                    "status_tab": cells[5].text.strip(),
                    "doc_link": doc_link,
                    "doc_id": doc_id
                })
            except StaleElementReferenceException: continue

        log(f"   [INFO] {len(current_batch)} véhicules trouvés sur cette page.")
        # Audit profond
        for info in current_batch:
            if limit and len(all_final_data) >= limit: break
            if info["doc_link"]:
                log(f"   [AUDIT] {info['immat']}")
                info["detailed_status"] = get_detailed_doc_status(driver, info["doc_link"])
            else:
                info["detailed_status"] = "Lien absent"
            all_final_data.append(info)

        if limit and len(all_final_data) >= limit: break

        first_id_before = current_batch[0]["doc_id"] if current_batch else ""
        if not click_next(driver, first_id_before):
            break
        page_num += 1
        
    return all_final_data

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Nom du dossier partenaire")
    parser.add_argument("--start", type=int, default=1, help="Numéro de partenaire de départ (ex: 2)")
    parser.add_argument("--limit", type=int, default=0, help="Max véhicules par partenaire")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    partners = [d for d in ORGANIZED_DIR.iterdir() if d.is_dir() and "unassigned" not in d.name.lower()]
    def get_num(d):
        m = re.search(r'\d+', d.name)
        return int(m.group()) if m else 0
    
    partners.sort(key=get_num)
    
    # Filtrer par numéro de départ
    partners = [p for p in partners if get_num(p) >= args.start]
    
    # Limiter le nombre total de dossiers traités (ex: 120)
    partners = partners[:120] 

    if args.only:
        partners = [p for p in partners if p.name.lower() == args.only.lower()]

    log(f"[INFO] Audit de {len(partners)} partenaires lancé (Départ: Partenaire {args.start}).")
    driver = make_driver(headless=not args.no_headless)
    
    try:
        for p_dir in partners:
            try:
                m = re.search(r'\d+', p_dir.name)
                if not m: continue
                email = f"partenaire{m.group()}@upjunoo.com"
                
                log(f"[INFO] >>> PARTENAIRE SUIVANT : {p_dir.name} ({email})")
                
                # Nettoyage total pour éviter les conflits de session
                driver.delete_all_cookies()
                driver.get(OWNER_LOGIN_URL)
                time.sleep(2)

                # Si on est redirigé (déjà connecté), on force le logout
                if "login" not in driver.current_url.lower() and "logout" not in driver.current_url.lower():
                    log("[INFO] Session résiduelle détectée, nettoyage...")
                    driver.get(f"{BASE_URL}/logout")
                    driver.delete_all_cookies()
                    time.sleep(2)
                    driver.get(OWNER_LOGIN_URL)

                try:
                    wait = WebDriverWait(driver, 15)
                    email_f = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email'], input[name='email']")))
                    email_f.clear()
                    email_f.send_keys(email)
                    
                    pass_f = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password']")
                    pass_f.clear()
                    pass_f.send_keys(UNIVERSAL_PASSWORD)
                    
                    driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']").click()
                    wait.until(lambda d: "/login" not in d.current_url.lower())
                except:
                    log(f"[ERROR] Échec login pour {email}")
                    # Debug screenshot
                    driver.save_screenshot(f"login_fail_{p_dir.name}.png")
                    continue

                driver.get(MANAGE_FLEET_URL)
                time.sleep(2)
                results = scrape_full_audit(driver, limit=args.limit)
                
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"fleet_approval_report_final_{ts}.json"
                with open(p_dir / fname, "w", encoding="utf-8") as f:
                    json.dump({"partner": p_dir.name, "count": len(results), "vehicles": results}, f, indent=2, ensure_ascii=False)
                
                log(f"[OK] {p_dir.name} terminé : {len(results)} véhicules.")
            
            except Exception as e:
                log(f"[CRITICAL] Erreur lors du traitement de {p_dir.name} : {e}")
            
            finally:
                # Nettoyage systématique pour le partenaire suivant
                try:
                    driver.get(f"{BASE_URL}/logout")
                    driver.delete_all_cookies()
                    time.sleep(1)
                except: pass

    except Exception:
        traceback.print_exc()
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
