"""
Script VPS - Scraping conducteurs non assignés depuis compte Owner
==================================================================
Usage: python3 scrape_owner_drivers_vps.py

1. Connexion sur /login/owner-login
2. Ouverture de /fleet-drivers
3. Pagination à 500
4. Scraping toutes les pages
5. Export JSON au format :
   {"nom": "...", "telephone": "...", "document_url": "...", "view_profile": "..."}

Variables d'environnement:
  OWNER_EMAIL    - Email du compte partenaire
  OWNER_PASSWORD - Mot de passe
  WEBHOOK_URL    - URL webhook Slack (optionnel)
"""

import argparse
import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from webdriver_manager.chrome import ChromeDriverManager

# ─── Configuration ───────────────────────────────────────────────────────────
BASE_URL      = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL     = f"{BASE_URL}/login/owner-login"
DRIVERS_URL   = f"{BASE_URL}/fleet-drivers"
OUTPUT_DIR    = Path(__file__).parent / "output"
PAGE_LOAD_TIMEOUT = 30


# ═════════════════════════════════════════════════════════════════════════════
#  CHROME HEADLESS
# ═════════════════════════════════════════════════════════════════════════════

def setup_driver_headless():
    import shutil
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument("--remote-debugging-port=9222")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--single-process")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.0")

    for binary in ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable"]:
        path = shutil.which(binary)
        if path:
            chrome_options.binary_location = path
            print(f"   ✅ Binaire Chrome: {path}")
            break

    chromedriver_path = None
    for cd in ["chromedriver", "/usr/bin/chromedriver",
               "/usr/lib/chromium-browser/chromedriver", "/usr/lib/chromium/chromedriver"]:
        if os.path.isfile(cd) or shutil.which(cd):
            chromedriver_path = cd if os.path.isfile(cd) else shutil.which(cd)
            print(f"   ✅ ChromeDriver: {chromedriver_path}")
            break

    service = Service(chromedriver_path) if chromedriver_path else Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


# ═════════════════════════════════════════════════════════════════════════════
#  AUTH OWNER
# ═════════════════════════════════════════════════════════════════════════════

def auto_login(driver, email: str, password: str) -> bool:
    """Connexion automatique au panel owner (copie exacte de scrape_drivers_vps.py)."""
    print(f"🔐 Connexion à {LOGIN_URL}...")
    wait = WebDriverWait(driver, PAGE_LOAD_TIMEOUT)
    try:
        driver.get(LOGIN_URL)
        email_input = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
        )))
        pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password']")
        email_input.clear()
        email_input.send_keys(email)
        pwd_input.clear()
        pwd_input.send_keys(password)
        try:
            btn = driver.find_element(By.XPATH, "//button[@type='submit']")
            btn.click()
        except:
            pwd_input.submit()
        # Attendre que l'URL change depuis la page owner-login
        wait.until(lambda d: d.current_url != LOGIN_URL)
        print(f"✅ Connecté: {driver.current_url}")
        return True
    except Exception as e:
        print(f"❌ Erreur connexion: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  PAGINATION
# ═════════════════════════════════════════════════════════════════════════════

def set_page_size_500(driver) -> bool:
    for attempt in range(3):
        try:
            print(f"🔄 Tentative {attempt+1}/3 pour pagination 500...")
            try:
                sel_el = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "select.form-select.form-select-sm.w-auto, "
                    "select[name='DataTables_Table_0_length'], "
                    "select[data-dt-idx='0']"
                )))
            except:
                sel_el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR, ".dataTables_length select, select.form-select"
                )))

            select_obj = Select(sel_el)
            options = [o.text.strip() for o in select_obj.options]
            target  = next((o for o in options if "500" in o), None)
            if not target:
                print(f"   ⚠️ Option 500 non trouvée dans: {options}")
                time.sleep(2); continue

            select_obj.select_by_visible_text(target)
            print(f"   ✅ Pagination '{target}' sélectionnée")
            time.sleep(3)

            start, last, stable = time.time(), 0, 0
            while time.time() - start < 30:
                time.sleep(1)
                count = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
                if count == last and count > 0:
                    stable += 1
                    if stable >= 3 and count >= 50:
                        print(f"   ✅ Tableau stable: {count} lignes")
                        return True
                else:
                    stable, last = 0, count
            if last > 0:
                print(f"   ✅ {last} lignes chargées")
                return True

        except Exception as e:
            print(f"   ❌ Tentative {attempt+1}: {e}")
            time.sleep(3)
            if attempt < 2:
                driver.refresh(); time.sleep(2)
    return False


def _get_first_row_sig(driver):
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        return rows[0].text[:200] if rows else None
    except:
        return None


def go_to_next_page(driver) -> bool:
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item.disabled a.page-link[aria-label='Next']"
        )
        return False
    except NoSuchElementException:
        pass
    try:
        btn = driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item:not(.disabled) a.page-link[aria-label='Next']"
        )
        if not btn.is_displayed():
            return False
        prev = _get_first_row_sig(driver)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            btn.click()
        except:
            driver.execute_script("arguments[0].click();", btn)
        start = time.time()
        while time.time() - start < PAGE_LOAD_TIMEOUT:
            time.sleep(0.5)
            new_sig = _get_first_row_sig(driver)
            if new_sig and new_sig != prev:
                time.sleep(1.0)
                return True
        return False
    except NoSuchElementException:
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  SCRAPING — même logique que scrape_drivers_vps.py
# ═════════════════════════════════════════════════════════════════════════════

def scrape_page(driver) -> list:
    page_data = []
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6:
                    continue

                nom       = cells[0].text.strip()
                telephone = cells[3].text.strip()

                document_url = "N/A"
                view_profile = "N/A"
                edit_url     = "N/A"
                try:
                    link_el = cells[5].find_element(By.TAG_NAME, "a")
                    document_url = link_el.get_attribute("href")
                    if document_url and "/document/" in document_url:
                        view_profile = document_url.replace("/document/", "/view-profile/")
                        edit_url     = document_url.replace("/document/", "/edit/")
                except:
                    pass

                page_data.append({
                    "nom":          nom,
                    "telephone":    telephone,
                    "document_url": document_url,
                    "view_profile": view_profile,
                    "edit":         edit_url,
                })
            except StaleElementReferenceException:
                continue
            except Exception as e:
                print(f"⚠️ Erreur ligne: {e}")
    except Exception as e:
        print(f"⚠️ Erreur page: {e}")
    return page_data


def scrape_all_pages(driver) -> list:
    all_data = []
    page_num = 1
    while True:
        print(f"\n📄 Page {page_num}: scraping...")
        data = scrape_page(driver)
        all_data.extend(data)
        print(f"   ✅ {len(data)} conducteurs (Total: {len(all_data)})")
        if not data:
            break
        if not go_to_next_page(driver):
            print("🏁 Fin de pagination")
            break
        page_num += 1
    return all_data


# ═════════════════════════════════════════════════════════════════════════════
#  EXPORT
# ═════════════════════════════════════════════════════════════════════════════

def export_results(data: list):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = OUTPUT_DIR / f"conducteurs_non_assignes_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ JSON: {json_path}")

    csv_path = OUTPUT_DIR / f"conducteurs_non_assignes_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Nom", "Téléphone", "URL Document", "Lien Profil"])
        for row in data:
            writer.writerow([row["nom"], row["telephone"], row["document_url"], row["view_profile"]])
    print(f"✅ CSV: {csv_path}")


# ═════════════════════════════════════════════════════════════════════════════
#  WEBHOOK
# ═════════════════════════════════════════════════════════════════════════════

def send_webhook(total: int, webhook_url: str, status: str = "success"):
    if not webhook_url:
        return
    try:
        color = "#36a64f" if status == "success" else "#ff0000"
        payload = {
            "text": f"Scraping conducteurs non assignés: {total} conducteurs",
            "attachments": [{
                "color": color,
                "fields": [{"title": "👥 Total", "value": str(total), "short": True}],
                "footer": "UpJunoo VPS Owner",
                "ts": int(datetime.now().timestamp()),
            }],
        }
        requests.post(webhook_url, json=payload, timeout=30).raise_for_status()
        print(f"✅ Webhook envoyé")
    except Exception as e:
        print(f"❌ Webhook échoué: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Scraping conducteurs non assignés (Owner login)")
    parser.add_argument("--email",    default=os.getenv("OWNER_EMAIL"),    help="Email owner")
    parser.add_argument("--password", default=os.getenv("OWNER_PASSWORD"), help="Mot de passe owner")
    parser.add_argument("--webhook",  default=os.getenv("WEBHOOK_URL"),    help="URL webhook")
    args = parser.parse_args()

    if not args.email or not args.password:
        print("❌ OWNER_EMAIL et OWNER_PASSWORD requis")
        sys.exit(1)

    driver = setup_driver_headless()
    try:
        # 1. Connexion owner
        if not auto_login(driver, args.email, args.password):
            sys.exit(1)

        # 2. Ouvrir /fleet-drivers
        print(f"\n📍 Ouverture de {DRIVERS_URL}...")
        driver.get(DRIVERS_URL)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )

        # 3. Pagination 500
        ok = set_page_size_500(driver)
        if not ok:
            print("⚠️ Pagination 500 impossible – scraping avec pagination actuelle")
        time.sleep(2)

        rows_detected = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
        print(f"📊 {rows_detected} lignes détectées sur page 1")

        # 4. Scraping toutes les pages
        start = time.time()
        results = scrape_all_pages(driver)
        elapsed = time.time() - start

        print(f"\n{'='*50}")
        print(f"🎉 TERMINÉ en {elapsed/60:.2f} minutes")
        print(f"👥 {len(results)} conducteurs non assignés")
        print(f"{'='*50}")

        # 5. Export
        export_results(results)

        if args.webhook:
            send_webhook(len(results), args.webhook)

    except Exception as e:
        print(f"❌ Erreur critique: {traceback.format_exc()}")
        if args.webhook:
            send_webhook(0, args.webhook, status="failed")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
