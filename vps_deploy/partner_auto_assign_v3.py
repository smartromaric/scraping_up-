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

PARTNER_NAME_RE = re.compile(r'^(partenaire|partenaires)[-_\s]*(\d+)', re.IGNORECASE)

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

# ─── Utils Partenaires ────────────────────────────────────────────────────────
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

def extract_partner_number(name: str) -> int:
    m = PARTNER_NAME_RE.match(name or "")
    return int(m.group(2)) if m else 0

def find_partner_folders(organized_dir: Path) -> list:
    if not organized_dir.exists(): return []
    partners = [item for item in organized_dir.iterdir() if item.is_dir() and PARTNER_NAME_RE.match(item.name)]
    partners.sort(key=lambda p: extract_partner_number(p.name))
    log(f"   📁 {len(partners)} partenaires trouvés dans {organized_dir.name}")
    return partners

def normalize_phone(phone: str) -> str:
    if not phone: return ""
    digits = re.sub(r'\D', '', str(phone))
    return digits[-10:] if len(digits) >= 10 else digits

def normalize_immat(text: str) -> str:
    if not text: return ""
    return re.sub(r'[^a-zA-Z0-9]', '', text).upper()

# ─── Selenium Logic ───────────────────────────────────────────────────────────
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
        btn = driver.find_element(By.XPATH, "//button[@type='submit'] | //button[contains(@class, 'btn-success')]")
        driver.execute_script("arguments[0].click();", btn)
        wait.until(lambda d: "login" not in d.current_url.lower())
        return True
    except Exception as e:
        log(f"      ❌ Login Erreur : {e}", "ERROR"); return False

def set_page_size(driver, size=500):
    try:
        time.sleep(3)
        selectors = ["select.form-select-sm", "select[name*='_length']", ".dataTables_length select"]
        for sel in selectors:
            try:
                element = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                s = Select(element); target = str(size)
                if s.first_selected_option.get_attribute("value") != target:
                    s.select_by_value(target); log(f"   [PAGE] Taille mise à {target}"); time.sleep(5)
                return
            except: continue
    except: pass

def wait_for_table_data(driver, timeout=12):
    start = time.time()
    ignore_mots = ["chargement", "loading", "aucune", "no data", "na", "vide"]
    while (time.time() - start) < timeout:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if len(rows) >= 1:
            txt = rows[0].text.lower()
            if not any(m in txt for m in ignore_mots):
                return True
        time.sleep(1)
    return False

def assign_in_modal(driver, target_phone, target_name):
    norm_phone_10 = normalize_phone(target_phone)
    wait = WebDriverWait(driver, 20)
    try:
        log(f"      [MODAL] Recherche du chauffeur {target_name} ({norm_phone_10})...")
        modal = wait.until(EC.visibility_of_element_located((By.CLASS_NAME, "modal-content")))
        time.sleep(3)
        
        def get_rows():
            return modal.find_elements(By.XPATH, ".//tr[not(contains(@class, 'header'))] | .//div[contains(@class, 'row')] | .//li | .//div[contains(@class, 'd-flex')]")

        target_el = None
        btn = None
        for attempt in range(5):
            modal_elements = get_rows()
            for el in modal_elements:
                try:
                    txt = el.get_attribute("innerText") or el.text
                    if not txt or len(txt) < 10 or len(txt) > 600: continue
                    
                    digits = re.sub(r'\D', '', txt)
                    if norm_phone_10 in digits:
                        btns = el.find_elements(By.XPATH, ".//button[contains(., 'Attribuer')]")
                        if btns:
                            target_el = el; btn = btns[0]; break
                except: continue
            
            if target_el: break
            driver.execute_script("arguments[0].scrollTop += 300;", modal)
            time.sleep(1)

        if not target_el:
            log("      [MODAL] Non trouvé par numéro, essai par nom...")
            name_parts = [p for p in target_name.upper().split() if len(p) > 2]
            modal_elements = get_rows()
            for el in modal_elements:
                try:
                    txt = (el.get_attribute("innerText") or el.text).upper()
                    if all(p in txt for p in name_parts):
                        btns = el.find_elements(By.XPATH, ".//button[contains(., 'Attribuer')]")
                        if btns:
                            target_el = el; btn = btns[0]; break
                except: continue

        if not target_el: raise Exception(f"Chauffeur {target_name} introuvable.")
        
        log(f"      [MODAL] Ligne VALIDÉE : {target_el.text.replace('\n', ' ')[:100]}...")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        driver.execute_script("arguments[0].style.border='3px solid red';", btn)
        time.sleep(1)
        
        # Clic hybride sur le bouton final
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.5)
            ActionChains(driver).move_to_element(btn).click().perform()
        except:
            driver.execute_script("arguments[0].click();", btn)

        # Gestion confirmation (Alertes ou SweetAlert)
        try:
            WebDriverWait(driver, 5).until(EC.alert_is_present())
            alert = driver.switch_to.alert
            log(f"      [MODAL] Alerte acceptée : {alert.text}")
            alert.accept()
            time.sleep(2)
        except: pass
        
        try:
            swal = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-confirm")))
            swal.click()
            time.sleep(1)
        except: pass
        
        return True
    except Exception as e:
        log(f"      [MODAL FAIL] {target_name} : {e}", "WARNING")
        try:
            close_btn = driver.find_elements(By.XPATH, "//button[@data-bs-dismiss='modal'] | //button[contains(., 'Close')]")
            if close_btn: driver.execute_script("arguments[0].click();", close_btn[0])
        except: pass
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Partner folder name")
    parser.add_argument("--start", help="Start partner number")
    parser.add_argument("--range", help="Partner range (e.g. 1-50)")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    partners = find_partner_folders(ORGANIZED_DIR)
    
    if args.only: 
        partners = [p for p in partners if normalize_partner_name(p.name) == normalize_partner_name(args.only)]
    
    if args.range:
        try:
            s_range, e_range = map(int, args.range.split('-'))
            partners = [p for p in partners if s_range <= extract_partner_number(p.name) <= e_range]
            log(f"[INFO] Filtrage par range : {s_range} à {e_range} ({len(partners)} partenaires retenus)")
        except Exception as e:
            log(f"❌ Format de range invalide : {args.range}. Utilisez '1-50'.", "ERROR")
            return

    if args.start:
        start_num = int(args.start)
        partners = [p for p in partners if extract_partner_number(p.name) >= start_num]

    driver = make_driver(headless=not args.no_headless)
    try:
        for p_dir in partners:
            # Utiliser uniquement data.json (forcé)
            data_path = p_dir / "data.json"
            if not data_path.exists():
                log(f"   ⚠️ Aucun fichier data.json trouvé dans {p_dir.name}", "WARNING")
                continue
            
            log(f"   📂 Chargement de {data_path.name}...")
            with open(data_path, "r", encoding="utf-8") as f: p_data = json.load(f)
            email = p_data.get("email") or p_data.get("partner_email") or derive_owner_email(p_dir.name)
            if not email: continue

            # Détection du format (drivers vs data)
            items = p_data.get("drivers") or p_data.get("data") or []
            log(f"   📊 JSON : {len(items)} entrées trouvées.")
            
            lookup = {}
            for i, d in enumerate(items):
                # Gestion flexible des champs
                v = d.get("vehicle", {})
                raw_immat = v.get("matricule") or d.get("vehicle_matricule") or d.get("immatriculation")
                immat = normalize_immat(raw_immat)
                
                if immat:
                    tel = d.get("telephone") or d.get("chauffeur_tel") or ""
                    nom = d.get("nom") or d.get("chauffeur_nom") or ""
                    # Un vehicule est "deja traite" uniquement s'il est assigne DONE.
                    # owner_approval_status=done ne doit pas bloquer une nouvelle assignation.
                    is_assigned = (d.get("assignment_status") == "DONE")
                    
                    lookup[immat] = {
                        "index": i, 
                        "tel": tel, 
                        "nom": nom, 
                        "assigned": is_assigned,
                        "list_key": "drivers" if "drivers" in p_data else "data",
                        "marque": v.get("marque", ""),
                        "modele": v.get("modele", ""),
                    }

            if not login_partner(driver, email, "123456789@"): continue
            driver.get(FLEET_URL); time.sleep(4); set_page_size(driver, 500)
            
            processed = {k for k, v in lookup.items() if v["assigned"]}
            skipped_this_run = set()
            success_count = 0
            
            to_process_count = len(lookup) - len(processed)
            log(f"[START] Affectation pour {p_dir.name} ({to_process_count} cibles à traiter)")
            
            if to_process_count == 0:
                log(f"   ✅ Aucun véhicule à assigner.")
                continue

            while True:
                try:
                    if not wait_for_table_data(driver, 15):
                        log("   ⚠️ Tableau vide ou en chargement prolongé.", "WARNING")
                        break

                    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                    
                    target = None
                    for idx, r in enumerate(rows):
                        if not r.is_displayed(): continue
                        row_txt = r.text.upper().replace("\n", " | ")
                        
                        if "APPROUV" in row_txt:
                            norm_row = normalize_immat(row_txt)
                            for imm_key, info in lookup.items():
                                if imm_key in processed or imm_key in skipped_this_run: continue
                                if imm_key in norm_row:
                                    target = (r, imm_key)
                                    break
                        if target: break
                    
                    if not target:
                        # Si on ne trouve rien mais qu'il reste des gens non traités (et non sautés)
                        remaining = [k for k in lookup if k not in processed and k not in skipped_this_run]
                        if remaining:
                            try:
                                next_btn = driver.find_element(By.XPATH, "//li[contains(@class, 'next') and not(contains(@class, 'disabled'))]/a")
                                log(f"      ➡️ Page suivante (reste {len(remaining)} à voir)...")
                                driver.execute_script("arguments[0].click();", next_btn); time.sleep(5); continue
                            except:
                                log(f"      ⏹️ Fin de liste. {len(remaining)} véhicules n'ont pas été trouvés sur le site.")
                                break
                        else:
                            break

                    row, imm = target
                    log(f"   [FOUND] {imm} -> {lookup[imm]['nom']}")
                    driver.execute_script("arguments[0].style.border='2px solid blue';", row)
                    
                    menu_opened = False
                    try:
                        # 1. Stratégie Robuste pour trouver le bouton ⋮
                        action_btn = None
                        for sel in [
                            "td:last-child [data-bs-toggle='dropdown']",
                            "td:last-child button",
                            "td:last-child .bi-three-dots-vertical",
                            "td:last-child i",
                            "td:last-child a"
                        ]:
                            try:
                                found = row.find_element(By.CSS_SELECTOR, sel)
                                if found.is_displayed():
                                    action_btn = found; break
                            except: continue

                        if not action_btn:
                            log(f"      ⚠️ Bouton ⋮ introuvable pour {imm}", "WARNING")
                            skipped_this_run.add(imm); continue

                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", action_btn)
                        time.sleep(0.5)

                        # 2. Ouverture forcée du menu (JS Bootstrap) avec normalisation du vrai bouton
                        driver.execute_script("""
                            var btn = arguments[0];
                            if (!btn) return;
                            if (!btn.matches || !btn.matches('[data-bs-toggle="dropdown"]')) {
                                var nearest = btn.closest('[data-bs-toggle="dropdown"]');
                                if (nearest) { btn = nearest; }
                            }
                            if (window.bootstrap && window.bootstrap.Dropdown) {
                                var dd = window.bootstrap.Dropdown.getInstance(btn) || new window.bootstrap.Dropdown(btn);
                                dd.show();
                            } else { btn.click(); }
                        """, action_btn)
                        time.sleep(1.5)

                        # 3. Clic sur "Attribuer" limité au menu de la ligne courante
                        clicked = driver.execute_script("""
                            var row = arguments[0];
                            if (!row) return false;
                            var menu = row.querySelector('.dropdown-menu.show') || row.querySelector('.dropdown-menu');
                            if (!menu) return false;
                            var items = menu.querySelectorAll('.dropdown-item, a, button, li, span');
                            for (var i = 0; i < items.length; i++) {
                                var txt = (items[i].textContent || '').trim();
                                if (txt.indexOf('Attribuer') !== -1 || txt === 'Attribuer') {
                                    items[i].click();
                                    return true;
                                }
                            }
                            return false;
                        """, row)

                        if clicked:
                            time.sleep(1)
                            if assign_in_modal(driver, lookup[imm]["tel"], lookup[imm]["nom"]):
                                list_key = lookup[imm]["list_key"]
                                p_data[list_key][lookup[imm]["index"]]["assignment_status"] = "DONE"
                                with open(data_path, "w", encoding="utf-8") as fw: 
                                    json.dump(p_data, fw, indent=2, ensure_ascii=False)
                                processed.add(imm)
                                success_count += 1
                                menu_opened = True
                        else:
                            log(f"      ⚠️ Option 'Attribuer' non trouvée dans le menu pour {imm}.", "WARNING")
                    except Exception as e:
                        log(f"      [DEBUG] Erreur interaction ligne : {e}")
                    
                    if not menu_opened:
                        log(f"      ❌ Échec sur {imm}. On le saute pour cette session.")
                        skipped_this_run.add(imm)
                        # Optionnel : refresh si trop d'échecs
                        if len(skipped_this_run) % 5 == 0:
                            log("      🔄 Refresh de maintenance...")
                            driver.refresh(); time.sleep(5); set_page_size(driver, 500)

                except Exception as e:
                    log(f"❌ Erreur critique : {e}", "ERROR")
                    break

            log(f"[END] {p_dir.name} terminé. BILAN : {success_count} réussis, {len(skipped_this_run)} échoués.")
            driver.get(f"{BASE_URL}/logout"); time.sleep(1)

            log(f"[END] {p_dir.name} terminé."); driver.get(f"{BASE_URL}/logout"); time.sleep(1)
    finally:
        driver.quit()

if __name__ == "__main__": main()
