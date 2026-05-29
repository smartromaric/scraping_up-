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
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import UnexpectedAlertPresentException, NoAlertPresentException

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

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def handle_alert(driver):
    try:
        alert = driver.switch_to.alert
        txt = alert.text
        alert.accept()
        log(f"      [ALERT] Popup accepté : {txt}")
        return True
    except NoAlertPresentException: return False
    except: return False

def normalize_phone(phone: str) -> str:
    if not phone: return ""
    digits = re.sub(r'\D', '', str(phone))
    return digits[-10:] if len(digits) >= 10 else digits

def normalize_immat(text: str) -> str:
    if not text: return ""
    return re.sub(r'[^a-zA-Z0-9]', '', text).upper()

# ─── Driver ───────────────────────────────────────────────────────────────
def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless: opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    try:
        return webdriver.Chrome(options=opts)
    except:
        chromedriver_path = shutil.which("chromedriver.exe") or "chromedriver.exe"
        svc = Service(chromedriver_path)
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
        btn = driver.find_element(By.XPATH, "//button[@type='submit'] | //button[contains(@class, 'btn-success')]")
        driver.execute_script("arguments[0].click();", btn)
        wait.until(lambda d: "login" not in d.current_url.lower())
        return True
    except Exception as e:
        log(f"      ❌ Login Erreur : {e}")
        return False

def set_page_size(driver, size=500):
    try:
        time.sleep(3)
        selectors = ["select.form-select-sm", "select[name*='_length']", ".dataTables_length select"]
        for sel in selectors:
            try:
                element = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                s = Select(element)
                vals = [o.get_attribute("value") for o in s.options]
                target = str(size) if str(size) in vals else max(vals, key=lambda v: int(v) if v.isdigit() else 0)
                if s.first_selected_option.get_attribute("value") != target:
                    s.select_by_value(target)
                    log(f"   [PAGE] Taille mise à {target}")
                    time.sleep(5)
                return
            except: continue
    except: pass

def assign_in_modal(driver, target_phone, target_name):
    norm_phone_10 = normalize_phone(target_phone)
    wait = WebDriverWait(driver, 20)
    try:
        log("      [MODAL] Scan par numéro (10 chiffres)...")
        # Attendre que le modal soit VISIBLE (important car Junoo laisse des modals cachés dans le DOM)
        modal = wait.until(EC.visibility_of_element_located((By.CLASS_NAME, "modal-content")))
        time.sleep(4)
        
        def get_rows():
            # Chercher tous les éléments qui pourraient être des lignes de conducteurs
            return modal.find_elements(By.XPATH, ".//tr[not(contains(@class, 'header'))] | .//div[contains(@class, 'row')] | .//li | .//div[contains(@class, 'd-flex')]")

        target_el = None
        for attempt in range(4):
            try:
                modal_elements = get_rows()
                if not modal_elements:
                    time.sleep(2); continue
                
                for el in modal_elements:
                    try:
                        # Utiliser innerText si .text est vide (cas fréquent en headless ou si l'élément est considéré caché par Selenium)
                        txt = el.get_attribute("innerText") or el.text
                        if not txt or len(txt) < 8: continue
                        
                        # Nettoyage robuste : on ne garde que les chiffres
                        digits_in_txt = re.sub(r'\D', '', txt)
                        if norm_phone_10 in digits_in_txt:
                            # Vérifier qu'il y a bien un bouton Attribuer dans cet élément
                            if el.find_elements(By.XPATH, ".//button[contains(., 'Attribuer')]"):
                                target_el = el; break
                    except: continue
                
                if target_el: break
                
                # Scroll petit à petit si pas trouvé
                driver.execute_script("arguments[0].scrollTop += 200;", modal)
                time.sleep(1)
            except:
                time.sleep(1); continue

        # Fallback par nom si le numéro n'est pas trouvé
        if not target_el:
            modal_elements = get_rows()
            for el in modal_elements:
                try:
                    txt = el.get_attribute("innerText") or el.text
                    name_parts = [p for p in target_name.upper().split() if len(p) > 2]
                    if not name_parts: continue
                    if sum(1 for p in name_parts if p in txt.upper()) >= 2:
                        if el.find_elements(By.XPATH, ".//button[contains(., 'Attribuer')]"):
                            target_el = el; break
                except: continue

        if not target_el:
            # Debug: logge un peu de texte du modal pour voir ce qu'il se passe
            full_txt = modal.get_attribute("innerText") or modal.text
            sample_txt = full_txt[:200].replace('\n', ' ').strip()
            raise Exception(f"Chauffeur {norm_phone_10} introuvable. (Aperçu modal: {sample_txt}...)")

        # Scroll pour être sûr que le bouton est cliquable
        btn = target_el.find_element(By.XPATH, ".//button[contains(., 'Attribuer')]")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", btn)
        
        try:
            WebDriverWait(driver, 8).until(EC.alert_is_present())
            handle_alert(driver)
        except: pass
        try:
            swal = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-confirm")))
            swal.click()
        except: pass
        time.sleep(2)
        return True
    except Exception as e:
        log(f"      [MODAL FAIL] {target_name} : {e}")
        try: 
            close_btn = driver.find_elements(By.XPATH, "//button[@data-bs-dismiss='modal'] | //button[contains(., 'Close')] | //button[contains(@class, 'btn-close')]")
            if close_btn: driver.execute_script("arguments[0].click();", close_btn[0])
        except: pass
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Partner folder name")
    parser.add_argument("--start", help="Start processing from this partner (name)")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    # Liste des partenaires triée par numéro
    partners = [d for d in ORGANIZED_DIR.iterdir() if d.is_dir() and "unassigned" not in d.name.lower()]
    partners.sort(key=lambda d: int(re.search(r'\d+', d.name).group()) if re.search(r'\d+', d.name) else 0)
    
    # Filtre --only
    if args.only:
        partners = [p for p in partners if p.name.lower() == args.only.lower()]
    
    # Filtre --start
    if args.start:
        found_start = False
        new_list = []
        for p in partners:
            if p.name.lower() == args.start.lower():
                found_start = True
            if found_start:
                new_list.append(p)
        if found_start:
            partners = new_list
            log(f"[INFO] Démarrage à partir de {args.start}")
        else:
            log(f"[WARNING] Partenaire '{args.start}' non trouvé. On commence au début.")

    driver = make_driver(headless=not args.no_headless)

    try:
        for p_dir in partners:
            data_final_path = p_dir / "data_final.json"
            if not data_final_path.exists(): continue
            with open(data_final_path, "r", encoding="utf-8") as f: p_data = json.load(f)
            
            lookup = {}
            for i, entry in enumerate(p_data.get("data", [])):
                immat = normalize_immat(entry.get("vehicle_matricule", ""))
                if immat: lookup[immat] = {
                    "index": i,
                    "tel": entry.get("chauffeur_tel", ""),
                    "nom": entry.get("chauffeur_nom", ""),
                    "assigned": entry.get("assignment_status") == "DONE"
                }

            m = re.search(r'\d+', p_dir.name)
            if not m: continue
            prefix = "partenaires" if "partenaires" in p_dir.name.lower() else "partenaire"
            email = f"{prefix}{m.group()}@upjunoo.com"

            if not login_partner(driver, email, "123456789@"): continue

            driver.get(FLEET_URL); time.sleep(4); set_page_size(driver, 500)
            
            def apply_filter():
                try:
                    s = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='search']")))
                    s.clear(); s.send_keys("APPROUVÉ"); time.sleep(4)
                except: pass

            apply_filter()
            log(f"[START] Processing pour {p_dir.name}")
            processed_on_page = {k for k, v in lookup.items() if v["assigned"]}
            
            retry_count = 0
            while True:
                try:
                    handle_alert(driver)
                    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                    target_row, target_immat, target_phone, target_name = None, None, None, None
                    
                    for r in rows:
                        cells = r.find_elements(By.TAG_NAME, "td")
                        if len(cells) < 6: continue
                        if "APPROUVÉ" in cells[5].text.upper():
                            norm_i = normalize_immat(cells[4].text)
                            if norm_i in lookup and norm_i not in processed_on_page:
                                target_row = r; target_immat = norm_i
                                target_phone, target_name = lookup[norm_i]["tel"], lookup[norm_i]["nom"]
                                break
                    
                    if not target_row:
                        try:
                            next_btn = driver.find_element(By.XPATH, "//li[contains(@class, 'next') and not(contains(@class, 'disabled'))]/a")
                            driver.execute_script("arguments[0].click();", next_btn); time.sleep(5); apply_filter()
                            retry_count = 0; continue
                        except: break

                    log(f"   [FOUND] {target_immat} -> {target_name}")
                    last_td = target_row.find_elements(By.TAG_NAME, "td")[-1]
                    pts = last_td.find_element(By.TAG_NAME, "i")
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pts); time.sleep(1)
                    
                    try:
                        actions = ActionChains(driver)
                        actions.move_to_element(pts).click().perform()
                    except:
                        driver.execute_script("arguments[0].click();", pts)
                    
                    time.sleep(3)
                    all_links = driver.find_elements(By.XPATH, "//a[contains(., 'Attribuer')] | //span[contains(., 'Attribuer')]")
                    target_attrib = next((l for l in all_links if l.is_displayed()), None)
                    
                    if target_attrib:
                        driver.execute_script("arguments[0].click();", target_attrib)
                        if assign_in_modal(driver, target_phone, target_name):
                            log(f"      ✅ Affecté avec succès.")
                            idx = lookup[target_immat]["index"]
                            p_data["data"][idx]["assignment_status"] = "DONE"
                            with open(data_final_path, "w", encoding="utf-8") as fw:
                                json.dump(p_data, fw, indent=2, ensure_ascii=False)
                            processed_on_page.add(target_immat); retry_count = 0
                        else: processed_on_page.add(target_immat)
                    else:
                        log(f"      ⚠️ Menu invisible. Refresh ({retry_count}/2)...")
                        if retry_count < 2:
                            driver.refresh(); time.sleep(5); set_page_size(driver, 500); apply_filter()
                            retry_count += 1
                        else: processed_on_page.add(target_immat)
                        
                except UnexpectedAlertPresentException:
                    handle_alert(driver)
                    continue
                except Exception as e:
                    log(f"      ❌ Erreur : {e}"); processed_on_page.add(target_immat)

            log(f"[END] {p_dir.name} terminé."); driver.get(f"{BASE_URL}/logout"); time.sleep(2)
    finally:
        if driver: driver.quit()

if __name__ == "__main__": main()
