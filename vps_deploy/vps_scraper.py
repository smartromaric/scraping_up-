"""
Script VPS pour scraping UpJunoo - Mode headless avec notifications Slack
"""

import json
import os
import re
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ─── Configuration ─────────────────────────────────────────────────────────
BASE_URL = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL = f"{BASE_URL}/login/admin"
OWNERS_URL = f"{BASE_URL}/manage-owners"
OUTPUT_DIR = Path(__file__).parent / "output"
JSON_OUT = OUTPUT_DIR / "partenaires.json"
LOG_FILE = OUTPUT_DIR / "scraper.log"

# Filtre partenaires (accepte partenaire-1 jusqu'à partenaire-XXXX sans limite)
PARTNER_NAME_RE = re.compile(r'^\s*partenaires?-?\s*(\d+)\s*$', re.I)
PARTNER_MIN = 1

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

def log(message):
    """Log avec timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {message}"
    print(full_msg)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(full_msg + "\n")


def send_slack(message, color="#36a64f"):
    """Envoie une notification Slack via webhook."""
    if not WEBHOOK_URL:
        return
    try:
        payload = json.dumps({
            "username": os.getenv("SLACK_BOT_NAME", "UpJunoo Bot"),
            "icon_emoji": os.getenv("SLACK_ICON_EMOJI", ":car:"),
            "attachments": [{"color": color, "text": message}]
        }).encode("utf-8")
        req = urllib.request.Request(WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"⚠️ Slack erreur: {e}")


def should_keep_partner(nom: str) -> bool:
    m = PARTNER_NAME_RE.match(nom or "")
    if not m:
        return False
    n = int(m.group(1))
    return n >= PARTNER_MIN


def get_chrome_driver():
    """Initialise Chrome en mode headless pour VPS"""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.0")
    
    # Désactiver les notifications
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2
    })
    
    try:
        driver = webdriver.Chrome(options=opts)
        return driver
    except Exception as e:
        log(f"❌ Erreur Chrome: {e}")
        log("Assure-toi que Chrome est installé sur le VPS:")
        log("  sudo apt update && sudo apt install -y chromium-browser chromium-chromedriver")
        raise


def auto_login(driver, email, password):
    log(f"🔐 Connexion à {LOGIN_URL}...")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 30)
    
    email_input = wait.until(EC.presence_of_element_located((
        By.CSS_SELECTOR, "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
    )))
    pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password']")
    
    email_input.clear()
    email_input.send_keys(email)
    pwd_input.clear()
    pwd_input.send_keys(password)
    
    try:
        btn = driver.find_element(
            By.XPATH,
            "//button[@type='submit'] | //button[contains(translate(.,'LOGIN','login'),'login')]"
            " | //button[contains(translate(.,'CONNEXION','connexion'),'connexion')]",
        )
        btn.click()
    except NoSuchElementException:
        pwd_input.submit()
    
    wait.until(lambda d: "/login" not in d.current_url)
    log(f"✅ Connecté: {driver.current_url}")


def set_page_size_500(driver):
    try:
        from selenium.webdriver.support.ui import Select
        sel_el = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select.form-select.form-select-sm.w-auto"))
        )
        Select(sel_el).select_by_visible_text("500")
        log("✅ Pagination réglée sur 500")
        time.sleep(1.5)
        WebDriverWait(driver, 30).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "table tbody tr")) > 50
        )
        return True
    except Exception as e:
        log(f"⚠️  Impossible de régler 500 auto: {e}")
        return False


def scrape_global_list(driver):
    """Extrait la liste des partenaires"""
    partners = []
    try:
        log("🔍 Extraction liste partenaires...")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        
        for i, row in enumerate(rows):
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6:
                    continue
                
                nom = cells[0].text.strip()
                email = cells[1].text.strip()
                telephone = cells[2].text.strip()
                
                document_url = "N/A"
                profile_url = "N/A"
                owner_id = "N/A"
                
                try:
                    link_doc = cells[3].find_element(By.TAG_NAME, "a")
                    document_url = link_doc.get_attribute("href")
                    if document_url and "/document/" in document_url:
                        owner_id = document_url.split("/document/")[-1]
                        profile_url = f"{BASE_URL}/manage-owners/view-profile/{owner_id}"
                except:
                    pass
                
                if not should_keep_partner(nom):
                    continue
                
                partners.append({
                    "nom": nom,
                    "email": email,
                    "telephone": telephone,
                    "document_url": document_url,
                    "profile_url": profile_url,
                    "owner_id": owner_id,
                    "drivers": []
                })
            except Exception as e:
                log(f"⚠️ Erreur ligne {i}: {e}")
        
        log(f"✅ {len(partners)} partenaires filtrés")
    except Exception as e:
        log(f"❌ Erreur extraction liste: {e}")
    
    return partners


def scrape_drivers_for_partner(driver, partner):
    """Extrait les conducteurs d'un partenaire"""
    drivers = []
    if not partner["profile_url"] or partner["profile_url"] == "N/A":
        return []
    
    try:
        driver.get(partner["profile_url"])
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((
            By.XPATH, "//a[contains(text(), 'Détails du conducteur')] | //a[contains(text(), 'Driver Details')]"
        )))
        
        tab_drivers = driver.find_element(
            By.XPATH, 
            "//a[contains(text(), 'Détails du conducteur')] | //a[contains(text(), 'Driver Details')]"
        )
        driver.execute_script("arguments[0].click();", tab_drivers)
        time.sleep(1.5)
        
        try:
            driver_rows = driver.find_elements(By.CSS_SELECTOR, ".tab-pane.active table tbody tr")
            if not driver_rows:
                driver_rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            
            for dr in driver_rows:
                d_cells = dr.find_elements(By.TAG_NAME, "td")
                if len(d_cells) >= 4:
                    drivers.append({
                        "nom": d_cells[0].text.strip(),
                        "emplacement": d_cells[1].text.strip() if len(d_cells) > 1 else "N/A",
                        "telephone": d_cells[2].text.strip() if len(d_cells) > 2 else "N/A",
                        "type_transport": d_cells[3].text.strip() if len(d_cells) > 3 else "N/A",
                        "type_vehicule": d_cells[4].text.strip() if len(d_cells) > 4 else "N/A"
                    })
        except Exception as e:
            log(f"⚠️ Pas de conducteurs pour {partner['nom']}: {e}")
    except Exception as e:
        log(f"❌ Erreur profil {partner['nom']}: {e}")
    
    return drivers


def export_all(data):
    """Exporte les données"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"✅ JSON: {JSON_OUT}")
    


def run():
    log("=" * 60)
    log("🚀 Démarrage scraping UpJunoo (VPS Mode)")
    log("=" * 60)
    
    # Récupérer credentials depuis l'environnement
    email = os.getenv("UPJUNOO_EMAIL")
    password = os.getenv("UPJUNOO_PASSWORD")
    
    if not email or not password:
        error_msg = "❌ Variables UPJUNOO_EMAIL et UPJUNOO_PASSWORD requises"
        log(error_msg)
        return
    
    driver = None
    success = False
    details = ""
    
    try:
        driver = get_chrome_driver()
        auto_login(driver, email, password)
        
        # Ouvrir page des owners
        log(f"📍 Ouverture {OWNERS_URL}...")
        driver.get(OWNERS_URL)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
        set_page_size_500(driver)
        
        # Scraping
        partners = scrape_global_list(driver)
        
        if not partners:
            details = "Aucun partenaire trouvé"
            log(f"❌ {details}")
            return
        
        export_all(partners)
        
        # Phase 2 - Conducteurs
        log("=" * 60)
        log(f"🔄 Enrichissement conducteurs ({len(partners)} partenaires)")
        log("=" * 60)
        
        for i, partner in enumerate(partners):
            log(f"[{i+1}/{len(partners)}] {partner['nom']}...")
            partner["drivers"] = scrape_drivers_for_partner(driver, partner)
            log(f"  ✅ {len(partner['drivers'])} conducteurs")
            
            partner["total_drivers"] = len(partner["drivers"])
            
            if (i + 1) % 5 == 0 or (i + 1) == len(partners):
                export_all(partners)
        
        total_drivers = sum(len(p['drivers']) for p in partners)
        msg = f"✅ Scraping partenaires terminé!\n{len(partners)} partenaires, {total_drivers} conducteurs au total."
        log("=" * 60)
        log("✨ TERMINÉ!")
        log(msg)
        log(f"📁 Données: {JSON_OUT}")
        log("=" * 60)
        send_slack(msg, "#36a64f")
        success = True
        
    except Exception as e:
        err_msg = f"❌ Erreur scraping partenaires: {str(e)}"
        log(f"❌ ERREUR CRITIQUE: {e}")
        traceback.print_exc()
        send_slack(err_msg, "#ff0000")
    finally:
        if driver:
            driver.quit()
            log("👋 Navigateur fermé")
        


if __name__ == "__main__":
    run()
