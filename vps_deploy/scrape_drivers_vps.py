"""
Script VPS pour scraping des conducteurs - UpJunoo Admin Panel (Mode headless)
===============================================================================
1. Connexion automatique (env vars UPJUNOO_EMAIL / UPJUNOO_PASSWORD)
2. Ouvre /fleet-drivers, règle la pagination au max
3. Scrape toutes les pages automatiquement
4. Export JSON + Excel
"""

import json
import os
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
)

# ─── Configuration ──────────────────────────────────────────────────────────────
BASE_URL = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL = f"{BASE_URL}/login/admin"
DRIVERS_URL = f"{BASE_URL}/fleet-drivers"
OUTPUT_DIR = Path(__file__).parent / "output"
JSON_OUT = OUTPUT_DIR / "conducteurs.json"
EXCEL_OUT = OUTPUT_DIR / "conducteurs.xlsx"
LOG_FILE = OUTPUT_DIR / "scrape_drivers.log"

PAGE_LOAD_TIMEOUT = 30
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


# ═══════════════════════════════════════════════════════════════════════════════
#  EXPORTS
# ═══════════════════════════════════════════════════════════════════════════════

def export_all(data):
    """Exporte les données en JSON + Excel."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"✅ JSON: {JSON_OUT}")

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Conducteurs"
    ws.append(["Nom", "Téléphone", "URL Document", "Lien Profil"])
    for row in data:
        ws.append([
            row.get("nom", ""),
            row.get("telephone", ""),
            row.get("document_url", ""),
            row.get("view_profile", "")
        ])
    wb.save(EXCEL_OUT)
    log(f"✅ Excel: {EXCEL_OUT}")


# ═══════════════════════════════════════════════════════════════════════════════
#  CHROME + LOGIN
# ═══════════════════════════════════════════════════════════════════════════════

def get_chrome_driver():
    """Initialise Chrome en mode headless pour VPS"""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.0")
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2
    })

    try:
        driver = webdriver.Chrome(options=opts)
        return driver
    except Exception as e:
        log(f"❌ Erreur Chrome: {e}")
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


# ═══════════════════════════════════════════════════════════════════════════════
#  PAGINATION
# ═══════════════════════════════════════════════════════════════════════════════

def set_page_size_max(driver):
    try:
        from selenium.webdriver.support.ui import Select
        sel_el = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select.form-select.form-select-sm.w-auto, select[name*='length'], select.custom-select"))
        )
        sel = Select(sel_el)
        options = [o.text.strip() for o in sel.options]
        log(f"📋 Options pagination disponibles: {options}")

        for target in ["500", "All", "Tout", "100", "50"]:
            if target in options:
                sel.select_by_visible_text(target)
                log(f"✅ Pagination réglée sur {target}")
                time.sleep(2)
                WebDriverWait(driver, 30).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "table tbody tr")) > 5
                )
                return True

        if options:
            sel.select_by_visible_text(options[-1])
            log(f"✅ Pagination réglée sur {options[-1]} (dernière option)")
            time.sleep(2)
            return True

    except Exception as e:
        log(f"⚠️  Impossible de régler pagination: {e}")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  SCRAPING
# ═══════════════════════════════════════════════════════════════════════════════

def fast_scrape_page(driver):
    """Extrait les données de la page actuelle."""
    page_data = []
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6:
                    continue

                nom = cells[0].text.strip()
                telephone = cells[3].text.strip()

                document_url = "N/A"
                view_profile = "N/A"
                try:
                    link_el = cells[5].find_element(By.TAG_NAME, "a")
                    document_url = link_el.get_attribute("href")
                    if document_url and "/document/" in document_url:
                        view_profile = document_url.replace("/document/", "/view-profile/")
                except:
                    pass

                page_data.append({
                    "nom": nom,
                    "telephone": telephone,
                    "document_url": document_url,
                    "view_profile": view_profile
                })
            except StaleElementReferenceException:
                continue
            except Exception as e:
                log(f"  ⚠️ Erreur ligne: {e}")

    except Exception as e:
        log(f"  ⚠️ Erreur page: {e}")

    return page_data


def _get_first_row_signature(driver):
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            return None
        return rows[0].text[:200]
    except Exception:
        return None


def _is_next_disabled(driver):
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item.disabled a.page-link[aria-label='Next']",
        )
        return True
    except NoSuchElementException:
        return False


def _find_next_button(driver):
    try:
        a = driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item:not(.disabled) a.page-link[aria-label='Next']",
        )
        if a.is_displayed():
            return a
    except NoSuchElementException:
        pass
    return None


def go_to_next_page(driver):
    """Clique sur 'suivant' et attend le rechargement."""
    if _is_next_disabled(driver):
        return False

    btn = _find_next_button(driver)
    if btn is None:
        log("  ℹ️  Bouton 'suivant' introuvable → fin de pagination.")
        return False

    prev_sig = _get_first_row_signature(driver)

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
    except Exception as e:
        log(f"  ⚠️  Clic 'suivant' impossible: {e}")
        return False

    start = time.time()
    while time.time() - start < PAGE_LOAD_TIMEOUT:
        time.sleep(0.5)
        new_sig = _get_first_row_signature(driver)
        if new_sig and new_sig != prev_sig:
            time.sleep(1.0)
            return True
        if _is_next_disabled(driver):
            return False
    log("  ⏱️  Timeout en attendant la nouvelle page.")
    return False


def scrape_all(driver):
    """Boucle de scraping avec pagination AUTO."""
    all_data = []
    page_num = 1

    while True:
        log(f"📄 Page {page_num} : scraping en cours...")
        start_time = time.time()

        data = fast_scrape_page(driver)
        all_data.extend(data)

        elapsed = time.time() - start_time
        log(f"  ✅ {len(data)} conducteurs récupérés en {elapsed:.2f}s")
        log(f"  📊 Total accumulé: {len(all_data)}")

        export_all(all_data)

        if len(data) == 0:
            log("  ⚠️  Aucune ligne → arrêt.")
            break

        log("  ➡️  Passage à la page suivante...")
        if not go_to_next_page(driver):
            log("🏁 Fin de pagination.")
            break

        page_num += 1

    return all_data


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    log("=" * 60)
    log("🚀 Démarrage scraping conducteurs (VPS Mode)")
    log("=" * 60)

    email = os.getenv("UPJUNOO_EMAIL")
    password = os.getenv("UPJUNOO_PASSWORD")

    if not email or not password:
        log("❌ Variables UPJUNOO_EMAIL et UPJUNOO_PASSWORD requises")
        return

    driver = None

    try:
        driver = get_chrome_driver()
        auto_login(driver, email, password)

        # Ouvrir page conducteurs
        log(f"📍 Ouverture {DRIVERS_URL}...")
        driver.get(DRIVERS_URL)
        time.sleep(5)  # Attendre le chargement complet de la page
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
        
        # Régler pagination au max
        if not set_page_size_max(driver):
            log("⚠️  Pagination non réglée, tentative 2...")
            time.sleep(3)
            set_page_size_max(driver)

        time.sleep(5)  # Attendre que le tableau se recharge complètement
        row_count = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
        log(f"📊 {row_count} lignes dans le tableau")
        
        if row_count <= 10:
            log("⚠️  Seulement 10 lignes, retry pagination...")
            set_page_size_max(driver)
            time.sleep(5)
            row_count = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
            log(f"📊 Après retry: {row_count} lignes")

        # Scraping
        start_time = time.time()
        results = scrape_all(driver)
        total_time = time.time() - start_time

        msg = f"✅ Scraping conducteurs terminé!\n{len(results)} conducteurs en {total_time/60:.2f} minutes."
        log("=" * 60)
        log(f"✨ TERMINÉ en {total_time/60:.2f} minutes")
        log(f"📊 TOTAL: {len(results)} conducteurs")
        log(f"📁 Données: {JSON_OUT}")
        log("=" * 60)
        send_slack(msg, "#36a64f")

    except Exception as e:
        err_msg = f"❌ Erreur scraping conducteurs: {str(e)}"
        log(f"❌ ERREUR CRITIQUE: {e}")
        traceback.print_exc()
        send_slack(err_msg, "#ff0000")
    finally:
        if driver:
            driver.quit()
            log("👋 Navigateur fermé")


if __name__ == "__main__":
    run()
