"""
Script LOCAL - Scraping conducteurs non assignés depuis compte Owner
====================================================================
Usage: python3 scrape_owner_drivers.py

Lit les credentials depuis .env automatiquement.
Chrome non-headless pour debug visuel.
"""

import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────
BASE_URL          = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL         = f"{BASE_URL}/login/owner-login"
DRIVERS_URL       = f"{BASE_URL}/fleet-drivers"
OUTPUT_DIR        = Path(__file__).parent / "output"
PAGE_LOAD_TIMEOUT = 30


# ═════════════════════════════════════════════════════════════════════════════
#  CHROME LOCAL (non-headless)
# ═════════════════════════════════════════════════════════════════════════════

def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--window-size=1400,900")
    chrome_options.add_argument("--disable-notifications")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


# ═════════════════════════════════════════════════════════════════════════════
#  AUTH OWNER
# ═════════════════════════════════════════════════════════════════════════════

def auto_login(driver, email: str, password: str) -> bool:
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
        wait.until(lambda d: d.current_url != LOGIN_URL)
        print(f"✅ Connecté: {driver.current_url}")
        return True
    except Exception as e:
        print(f"❌ Erreur connexion: {e}")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / "login_debug.html").write_text(driver.page_source, encoding="utf-8")
        print(f"   💾 HTML sauvegardé: output/login_debug.html")
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
#  SCRAPING
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
                edit_url = "N/A"
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
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    email    = os.getenv("OWNER_EMAIL")
    password = os.getenv("OWNER_PASSWORD")

    if not email or not password:
        print("❌ OWNER_EMAIL et OWNER_PASSWORD manquants dans .env")
        sys.exit(1)

    print(f"📧 Email: {email}")
    driver = setup_driver()
    try:
        if not auto_login(driver, email, password):
            sys.exit(1)

        print(f"\n📍 Ouverture de {DRIVERS_URL}...")
        driver.get(DRIVERS_URL)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )

        ok = set_page_size_500(driver)
        if not ok:
            print("⚠️ Pagination 500 impossible – scraping avec pagination actuelle")
        time.sleep(2)

        rows_detected = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
        print(f"📊 {rows_detected} lignes détectées sur page 1")

        start = time.time()
        results = scrape_all_pages(driver)
        elapsed = time.time() - start

        print(f"\n{'='*50}")
        print(f"🎉 TERMINÉ en {elapsed/60:.2f} minutes")
        print(f"👥 {len(results)} conducteurs non assignés")
        print(f"{'='*50}")

        export_results(results)

    except Exception as e:
        print(f"❌ Erreur critique: {traceback.format_exc()}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
