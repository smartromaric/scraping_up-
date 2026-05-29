"""
Admin Approved Fleet Scraper — VPS (headless, full auto)
=========================================================
Mission : Login admin → /owner-dashboard → clic "Approuvé Flottes"
→ /manage-fleet → scraper toutes les pages → export JSON + Excel.

Usage:
  python3 admin_approved_fleet_scraper_vps.py
  python3 admin_approved_fleet_scraper_vps.py --start 10 --limit 500
  python3 admin_approved_fleet_scraper_vps.py --search "Partenaire50"
"""

import argparse
import json
import os
import re
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException, TimeoutException,
    StaleElementReferenceException,
)
from webdriver_manager.chrome import ChromeDriverManager

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_URL             = "https://upjunoo-server-new.junooapps.com"
ADMIN_LOGIN_URL      = f"{BASE_URL}/login/admin"
OWNER_DASHBOARD_URL  = f"{BASE_URL}/owner-dashboard"
MANAGE_FLEET_URL     = f"{BASE_URL}/manage-fleet"
ADMIN_EMAIL          = os.getenv("UPJUNOO_EMAIL",    "admin@upjunoo.com")
ADMIN_PASSWORD       = os.getenv("UPJUNOO_PASSWORD", "123456789")
WEBHOOK_URL          = os.getenv("WEBHOOK_URL",      "")
OUTPUT_DIR           = Path(__file__).parent / "output"
WAIT                 = 20


# ─── Slack ────────────────────────────────────────────────────────────────────
def send_slack(text: str, color: str = "#36a64f", fields: Optional[List] = None):
    url = WEBHOOK_URL
    if not url:
        return
    att = {"color": color, "text": text, "footer": "UpJunoo Bot :car:",
           "ts": int(datetime.now().timestamp())}
    if fields:
        att["fields"] = fields
    try:
        requests.post(url, json={"username": "UpJunoo Bot", "icon_emoji": ":car:",
                                  "attachments": [att]}, timeout=10)
    except Exception:
        pass


# ─── Driver ───────────────────────────────────────────────────────────────────
def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--remote-debugging-port=0")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-sync")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--mute-audio")
    opts.add_argument("--no-first-run")
    opts.add_argument("--safebrowsing-disable-auto-update")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--allow-running-insecure-content")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    svc = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.set_page_load_timeout(60)
    return driver


def safe_get(driver: webdriver.Chrome, url: str, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            driver.get(url)
            return
        except TimeoutException:
            if attempt == retries:
                raise
            print(f"[GET] Timeout ({attempt}/{retries}), retry dans 3s...")
            time.sleep(3)


# ─── Login Admin ──────────────────────────────────────────────────────────────
def login_admin(driver: webdriver.Chrome) -> bool:
    print(f"[LOGIN] {ADMIN_LOGIN_URL}")
    safe_get(driver, ADMIN_LOGIN_URL)
    w = WebDriverWait(driver, WAIT)
    try:
        ef = w.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input[type='email'],input[name='email']")))
        ef.clear(); ef.send_keys(ADMIN_EMAIL)
        pf = driver.find_element(By.CSS_SELECTOR, "input[type='password'],input[name='password']")
        pf.clear(); pf.send_keys(ADMIN_PASSWORD)
        driver.find_element(By.CSS_SELECTOR, "button[type='submit'],input[type='submit']").click()
        w.until(lambda d: "login" not in d.current_url.lower())
        print(f"[LOGIN] OK → {driver.current_url}")
        return True
    except Exception as e:
        print(f"[LOGIN] ERREUR: {e}")
        return False


# ─── Navigation → manage-fleet ────────────────────────────────────────────────
def go_to_manage_fleet(driver: webdriver.Chrome) -> bool:
    print(f"[NAV] Navigation directe → {MANAGE_FLEET_URL}")
    safe_get(driver, MANAGE_FLEET_URL)
    time.sleep(2)
    if "manage-fleet" in driver.current_url:
        print(f"[NAV] OK → {driver.current_url}")
        return True
    print(f"[NAV] URL inattendue ({driver.current_url}), passage par owner-dashboard...")
    safe_get(driver, OWNER_DASHBOARD_URL)
    time.sleep(2)
    safe_get(driver, MANAGE_FLEET_URL)
    time.sleep(2)
    if "manage-fleet" in driver.current_url:
        print(f"[NAV] OK → {driver.current_url}")
        return True
    print(f"[NAV] ÉCHEC — URL: {driver.current_url}")
    return False


# ─── Taille de page ───────────────────────────────────────────────────────────
def set_page_size(driver: webdriver.Chrome, size: int = 100):
    selectors = [
        "select.form-select.form-select-sm",
        ".form-select-sm",
        "select[class*='form-select']",
        "select[name$='_length']",
        ".dataTables_length select",
    ]
    try:
        WebDriverWait(driver, WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
    except TimeoutException:
        print("[PAGE] Tableau non détecté.")
        return

    for sel in selectors:
        try:
            element = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            s = Select(element)
            vals = [o.get_attribute("value") for o in s.options]
            target = str(size) if str(size) in vals else max(vals, key=lambda v: int(v) if v.isdigit() else 0)
            if s.first_selected_option.get_attribute("value") != target:
                s.select_by_value(target)
                time.sleep(3)
                WebDriverWait(driver, WAIT).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
            return
        except: continue

def apply_filter(driver: webdriver.Chrome, query: str):
    try:
        s = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='search']")))
        s.clear(); s.send_keys(query)
        print(f"[FILTER] Recherche appliquée: {query}")
        time.sleep(4)
    except:
        print("[FILTER] Impossible d'appliquer le filtre.")

# ─── Scrape page courante ─────────────────────────────────────────────────────
def scrape_page(driver: webdriver.Chrome) -> List[Dict]:
    records = []
    try:
        WebDriverWait(driver, WAIT).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
    except: return records

    for row in driver.find_elements(By.CSS_SELECTOR, "table tbody tr"):
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            v = [c.text.strip() for c in cells]
            if not v or all(x == "" for x in v): continue
            
            id_doc, url_doc, url_edit = "", "", ""
            try:
                links = cells[3].find_elements(By.TAG_NAME, "a")
                for a in links:
                    href = a.get_attribute("href") or ""
                    if "/manage-fleet/document/" in href:
                        url_doc = href
                        id_doc = href.split("/manage-fleet/document/")[-1].strip("/")
                        url_edit = href.replace("/manage-fleet/document/", "/manage-fleet/edit/")
                        break
            except: pass

            records.append({
                "id_document": id_doc,
                "type_vehicule": v[0] if len(v) > 0 else "",
                "marque": v[1] if len(v) > 1 else "",
                "modele": v[2] if len(v) > 2 else "",
                "immatriculation": v[4] if len(v) > 4 else "",
                "statut": v[5] if len(v) > 5 else "",
                "raison": v[6] if len(v) > 6 else "",
                "url_document": url_doc,
                "url_edit": url_edit,
                "scraped_at": datetime.now().isoformat(),
            })
        except StaleElementReferenceException: continue
    return records

def click_next(driver: webdriver.Chrome, first_id_before: str = "") -> bool:
    def get_btn():
        lis = driver.find_elements(By.CSS_SELECTOR, "ul.pagination li.page-item")
        for li in lis:
            t = li.text.strip().lower()
            if any(x in t for x in ["suivant", "next", "›"]):
                if "disabled" in (li.get_attribute("class") or ""): return None
                try: return li.find_element(By.TAG_NAME, "a")
                except: return li
        return None

    btn = get_btn()
    if not btn: return False
    try:
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(3)
        return True
    except: return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", type=int, default=1, help="Numéro de page de départ")
    parser.add_argument("--search", type=str, help="Texte à chercher (ex: nom du partenaire)")
    parser.add_argument("--output-dir", type=str, default=str(OUTPUT_DIR))
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"admin_approved_fleet_{ts}.json"

    all_records = []
    driver = None
    try:
        driver = make_driver(headless=not args.no_headless)
        if not login_admin(driver): raise RuntimeError("Login Failed")
        if not go_to_manage_fleet(driver): raise RuntimeError("Nav Failed")

        if args.search:
            apply_filter(driver, args.search)

        set_page_size(driver, 500)
        
        # Sauter des pages si --start > 1
        if args.start > 1:
            print(f"[NAV] Saut vers la page {args.start}...")
            for p in range(1, args.start):
                if not click_next(driver): break
                print(f"   Page {p} dépassée...")

        page_num = args.start
        while True:
            print(f"[SCRAPE] Page {page_num}...")
            recs = scrape_page(driver)
            if not recs: break
            
            first_id = recs[0].get("id_document", "")
            all_records.extend(recs)
            print(f"   Total: {len(all_records)}")

            if args.limit and len(all_records) >= args.limit: break
            if not click_next(driver, first_id): break
            page_num += 1

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"exported_at": datetime.now().isoformat(), "total": len(all_records), "fleet": all_records}, f, ensure_ascii=False, indent=2)
        print(f"✅ Terminé : {len(all_records)} véhicules.")

    finally:
        if driver: driver.quit()

if __name__ == "__main__": main()
