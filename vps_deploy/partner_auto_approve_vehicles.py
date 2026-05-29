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
from selenium.webdriver.common.keys import Keys
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

_SCRIPT_DIR          = Path(__file__).parent
OUTPUT_DIR           = _SCRIPT_DIR / "output"
ORGANIZED_DIR        = OUTPUT_DIR / "organized_by_partner"

PARTNER_NAME_RE = re.compile(r'^(partenaire|partenaires)[-_\s]*(\d+)', re.IGNORECASE)

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
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
    opts.add_argument("--window-size=1920,1080")
    try:
        return webdriver.Chrome(options=opts)
    except:
        svc = Service(shutil.which("chromedriver.exe") or "chromedriver.exe")
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
        log(f"   [PAGE] Tentative de réglage à {size}...")
        wait = WebDriverWait(driver, 15)
        # On attend que le tableau soit chargé
        try: wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        except: pass
        
        selectors = ["select.form-select-sm", "select[name*='_length']", ".dataTables_length select", "select.form-select"]
        for sel in selectors:
            try:
                element = driver.find_element(By.CSS_SELECTOR, sel)
                s = Select(element); target = str(size)
                current = s.first_selected_option.get_attribute("value")
                if current != target:
                    try:
                        s.select_by_value(target)
                        log(f"   [PAGE] Taille changée : {current} -> {target}")
                        time.sleep(5)
                    except:
                        # Si la valeur n'existe pas, on prend la plus grande
                        vals = [o.get_attribute("value") for o in s.options]
                        best = max(vals, key=lambda v: int(v) if v.isdigit() else 0)
                        if current != best:
                            s.select_by_value(best)
                            log(f"   [PAGE] Valeur {target} absente, repli sur {best}")
                            time.sleep(5)
                else:
                    log(f"   [PAGE] Déjà à {target}")
                return
            except: continue
        log("   [PAGE] ⚠️ Aucun sélecteur de taille trouvé.", "WARNING")
    except Exception as e:
        log(f"   [PAGE] ❌ Erreur : {e}", "WARNING")

def apply_search_filter(driver, query):
    try:
        wait = WebDriverWait(driver, 10)
        search_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='search']")))
        
        # Click et clear agressif
        search_input.click()
        search_input.send_keys(Keys.CONTROL + "a")
        search_input.send_keys(Keys.BACKSPACE)
        time.sleep(1)
        
        search_input.send_keys(query)
        search_input.send_keys(Keys.ENTER)
        
        # Attendre un peu que le 'Processing' disparaisse si présent
        time.sleep(5)
        return True
    except:
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
            # Format detection
            items = p_data.get("drivers") or p_data.get("data") or []
            lk = "data" if "data" in p_data else "drivers"

            for i, d in enumerate(items):
                immat = ""
                is_to_approve = False
                if lk == "data":
                    immat = normalize_immat(d.get("vehicle_matricule", ""))
                    status = str(d.get("audit_status_tab", "")).upper()
                    is_to_approve = "APPROUV" in status
                else:
                    immat = normalize_immat(d.get("vehicle", {}).get("matricule", ""))
                    is_to_approve = d.get("type_vehicule") == "APPROUVÉ"

                if is_to_approve and d.get("owner_approval_status") != "DONE":
                    if immat and len(immat) > 1: targets[immat] = i

            if not targets:
                log(f"   ✅ {p_dir.name} : Déjà à jour."); continue

            if not driver: driver = make_driver(headless=not args.no_headless)

            if not login_partner(driver, email, "123456789@"): continue
            driver.get(FLEET_URL); time.sleep(4)
            log(f"[START] Approbation véhicules pour {p_dir.name} ({len(targets)} cibles)")

            while targets:
                set_page_size(driver, 500)
                target_immat = list(targets.keys())[0]
                idx_in_data = targets[target_immat]
                
                try:
                    log(f"   [PROCESS] Recherche de {target_immat}...")
                    
                    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                    target_row = None
                    
                    # Log de debug pour voir les immat présentes sur la page (pour le premier target seulement)
                    if rows and len(rows) > 0 and "data-debugged" not in driver.current_url:
                        sample = []
                        for r in rows[:5]:
                            try:
                                c = r.find_elements(By.TAG_NAME, "td")
                                if len(c) > 4: sample.append(c[4].text.strip())
                            except: pass
                        log(f"      [DEBUG] Immat sur page : {', '.join(sample)}...")
                        # On marque la session pour ne pas loggués à chaque target
                        # driver.execute_script("window.debugged = true;") # On verra
                    
                    for r in rows:
                        try:
                            cells = r.find_elements(By.TAG_NAME, "td")
                            if not cells: continue
                            
                            # On normalise le contenu de toutes les cellules
                            row_contents = [normalize_immat(c.text) for c in cells]
                            
                            if target_immat in row_contents:
                                target_row = r; break
                        except: continue
                    
                    if not target_row:
                        # On tente la page suivante
                        try:
                            next_btn = driver.find_element(By.XPATH, "//li[contains(@class, 'next') and not(contains(@class, 'disabled'))]/a")
                            log(f"      [PAGE] Non trouvé ici, passage à la page suivante...")
                            driver.execute_script("arguments[0].click();", next_btn); time.sleep(5)
                            continue
                        except:
                            log(f"      ⚠️ {target_immat} introuvable sur aucune page.", "WARNING")
                            del targets[target_immat]; continue

                    # Vérifier le statut (on cherche APPROUV dans toute la ligne)
                    full_text = target_row.text.upper()
                    
                    if "APPROUV" in full_text:
                        log(f"      ✅ Déjà approuvé sur le web (statut détecté dans la ligne).")
                        p_data[lk][idx_in_data]["owner_approval_status"] = "DONE"
                        del targets[target_immat]; continue

                    # On traite l'action
                    log(f"      [ACTION] Ouverture menu...")
                    pts = target_row.find_element(By.XPATH, ".//td[last()]//i")
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pts); time.sleep(1)
                    driver.execute_script("arguments[0].click();", pts); time.sleep(2)
                    
                    # On cherche le bouton 'Approuver' qui est REELLEMENT visible
                    all_approve_btns = driver.find_elements(By.XPATH, "//a[contains(., 'Approuver')] | //span[contains(., 'Approuver')]")
                    btn_app = next((b for b in all_approve_btns if b.is_displayed()), None)
                    
                    if btn_app:
                        driver.execute_script("arguments[0].click();", btn_app); time.sleep(2)
                        
                        # Validation alertes
                        try:
                            WebDriverWait(driver, 5).until(EC.alert_is_present())
                            driver.switch_to.alert.accept()
                        except: pass
                        try:
                            swal = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-confirm")))
                            swal.click()
                        except: pass
                        
                        log(f"      ✅ {target_immat} approuvé avec succès.")
                        p_data[lk][idx_in_data]["owner_approval_status"] = "DONE"
                        with open(data_path, "w", encoding="utf-8") as fw: json.dump(p_data, fw, indent=2, ensure_ascii=False)
                    else:
                        log(f"      ❌ Bouton 'Approuver' non trouvé ou invisible.", "WARNING")
                    
                    del targets[target_immat]
                    
                except Exception as e:
                    log(f"      ❌ Échec pour {target_immat} : {e}", "WARNING")
                    del targets[target_immat]
                    
                time.sleep(1)

            log(f"[END] {p_dir.name} terminé."); driver.get(f"{BASE_URL}/logout"); time.sleep(1)
    finally:
        if driver: driver.quit()

if __name__ == "__main__": main()
