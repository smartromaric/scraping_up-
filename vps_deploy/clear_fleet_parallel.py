#!/usr/bin/env python3
"""
clear_fleet_parallel.py
=======================
Vide la flotte de tous les partenaires en parallèle (N workers).
Chaque worker prend un partenaire, vide sa flotte, passe au suivant.

Usage :
  python3 clear_fleet_parallel.py                  # 5 workers par défaut
  python3 clear_fleet_parallel.py --workers 10     # 10 workers
  python3 clear_fleet_parallel.py --dry-run        # simulation
  python3 clear_fleet_parallel.py --start 20       # reprendre depuis partenaire 20
"""

import argparse
import json
import os
import queue
import re
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    import requests
except ImportError:
    requests = None

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR           = Path(__file__).parent
REFERENCE_FILE     = BASE_DIR / "output" / "organized_by_partner" / "all_partners_enriched.json"
LOG_FILE           = BASE_DIR / "output" / "clear_parallel.log"
OWNER_LOGIN_URL    = "https://upjunoo-server-new.junooapps.com/login/owner-login"
MANAGE_FLEET_URL   = "https://upjunoo-server-new.junooapps.com/manage-fleet"
UNIVERSAL_PASSWORD = "123456789@"
WEBHOOK_URL        = os.getenv("WEBHOOK_URL", "")
PARTNER_NUM_RE     = re.compile(r'^\s*partenaires?[-_]?\s*(\d+)\s*$', re.I)
EMAIL_FROM_NOM_RE  = re.compile(r'^\s*(partenaires?)(\d+)\s*$', re.I)

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_lock = Lock()

# ─────────────────────────────────────────────────────────────────────────────
#  LOG
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str, worker_id: int = 0):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[W{worker_id}]" if worker_id else "    "
    line = f"[{ts}] {prefix} {msg}"
    with _log_lock:
        print(line, flush=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def send_slack(msg: str, color: str = "#439FE0"):
    if not WEBHOOK_URL or not requests:
        return
    try:
        requests.post(WEBHOOK_URL, json={
            "attachments": [{"color": color, "text": msg, "mrkdwn_in": ["text"]}]
        }, timeout=10)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  CHROME (port unique par worker)
# ─────────────────────────────────────────────────────────────────────────────

def setup_driver(worker_id: int = 0, partner_num: int = 0):
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--no-zygote")
    port = 9300 + (partner_num if partner_num else worker_id)
    chrome_options.add_argument(f"--remote-debugging-port={port}")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    for binary in [
        "chromium-browser", "/snap/bin/chromium", "chromium",
        "google-chrome-stable", "google-chrome",
    ]:
        path = binary if os.path.isfile(binary) else shutil.which(binary)
        if path:
            chrome_options.binary_location = path
            break

    chromedriver_path = None
    for cd in [
        "/usr/bin/chromedriver", "chromedriver",
        "/snap/bin/chromium.chromedriver", "/snap/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ]:
        if os.path.isfile(cd) or shutil.which(cd):
            chromedriver_path = cd if os.path.isfile(cd) else shutil.which(cd)
            break

    if not chromedriver_path:
        raise RuntimeError("Aucun chromedriver trouvé")

    service = Service(chromedriver_path)
    return webdriver.Chrome(service=service, options=chrome_options)


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────────────────────

def login(driver, email: str, wid: int) -> bool:
    for attempt in range(1, 4):
        try:
            driver.get(OWNER_LOGIN_URL)
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[type='email'],input[type='text']")
                )
            )
            em = driver.find_element(By.CSS_SELECTOR, "input[type='email'],input[type='text']")
            pw = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            em.clear(); em.send_keys(email)
            pw.clear(); pw.send_keys(UNIVERSAL_PASSWORD)
            btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            driver.execute_script("arguments[0].click();", btn)
            WebDriverWait(driver, 30).until(EC.url_contains("/owner-dashboard"))
            return True
        except Exception as e:
            log(f"Login tentative {attempt}/3 échouée: {e}", wid)
            time.sleep(3)
    return False


def logout(driver):
    try:
        driver.get("https://upjunoo-server-new.junooapps.com/logout")
        time.sleep(1)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  SUPPRESSION (même logique qu'audit_fleet_vps.py)
# ─────────────────────────────────────────────────────────────────────────────

def get_total(driver) -> int:
    try:
        text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r'de\s+(\d+)\s+entr', text, re.I)
        return int(m.group(1)) if m else -1
    except Exception:
        return -1


def load_page_max_pagination(driver):
    driver.get(MANAGE_FLEET_URL)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
        )
    except TimeoutException:
        pass
    time.sleep(0.5)
    try:
        sel = WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select"))
        )
        opts = driver.execute_script(
            "return Array.from(arguments[0].options).map(o=>o.value)", sel
        )
        best = max((v for v in opts if v.isdigit()), key=int, default=None)
        if best:
            driver.execute_script(
                "arguments[0].value=arguments[1];"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}))",
                sel, best
            )
            time.sleep(1)
    except Exception:
        pass


def delete_first_row(driver, wid: int) -> str:
    try:
        result = driver.execute_script("""
            var rows = document.querySelectorAll('table tbody tr');
            if (!rows || rows.length === 0) return 'no_rows';
            var row = rows[0];
            var btn = row.querySelector(
                '[data-bs-toggle="dropdown"], button.dropdown-toggle, button'
            );
            if (!btn) return 'no_btn';
            if (window.bootstrap && window.bootstrap.Dropdown) {
                var dd = window.bootstrap.Dropdown.getOrCreateInstance(btn);
                dd.show();
            } else { btn.click(); }
            return new Promise(function(resolve) {
                setTimeout(function() {
                    var menu = row.querySelector('.dropdown-menu.show, .dropdown-menu');
                    if (!menu) { resolve('no_menu'); return; }
                    var items = menu.querySelectorAll('a, button, li');
                    for (var i = 0; i < items.length; i++) {
                        var t = items[i].textContent.trim().toLowerCase();
                        if (t.includes('supprimer') || t.includes('delete')) {
                            items[i].click();
                            resolve('clicked');
                            return;
                        }
                    }
                    resolve('no_supprimer');
                }, 300);
            });
        """)

        if result != "clicked":
            return result

        confirm = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
        )
        time.sleep(0.15)
        driver.execute_script("arguments[0].click();", confirm)

        try:
            ok = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
            )
            driver.execute_script("arguments[0].click();", ok)
        except TimeoutException:
            pass

        end = time.time() + 4
        while time.time() < end:
            if not driver.find_elements(By.CSS_SELECTOR, ".swal2-container .swal2-popup"):
                break
            time.sleep(0.15)

        time.sleep(0.3)
        return "ok"

    except Exception as e:
        try:
            driver.execute_script(
                "var b=document.querySelector('.swal2-cancel'); if(b) b.click();"
            )
        except Exception:
            pass
        return f"error:{str(e).splitlines()[0]}"


# ─────────────────────────────────────────────────────────────────────────────
#  WORKER : traite un partenaire
# ─────────────────────────────────────────────────────────────────────────────

def process_partner(partner: dict, wid: int, dry_run: bool) -> dict:
    nom   = partner["nom"]
    email = partner["email"]
    result = {"nom": nom, "deleted": 0, "failed": 0, "error": None}

    driver = None
    try:
        time.sleep((wid - 1) * 3)
        driver = setup_driver(wid, partner.get("num", 0))
        log(f"🔐 {nom} ({email})", wid)

        if not login(driver, email, wid):
            result["error"] = "login échoué"
            log(f"❌ Login échoué: {nom}", wid)
            send_slack(f"❌ [W{wid}] {nom} — login échoué", "#ff0000")
            return result

        # Charger la page avec pagination max
        load_page_max_pagination(driver)
        total = get_total(driver)
        log(f"🗑️  {nom}: {total} véhicules", wid)

        if dry_run:
            log(f"🧪 [DRY-RUN] {nom}: {total} suppressions simulées", wid)
            result["deleted"] = total
            return result

        deleted = 0
        consecutive_fails = 0
        stale_count = 0
        start_time_partner = time.time()

        while True:
            res = delete_first_row(driver, wid)

            if res == "ok":
                deleted += 1
                consecutive_fails = 0
            elif res in ("no_rows", "empty") or "no_btn" in res:
                remaining = get_total(driver)
                if remaining == 0:
                    log(f"✅ {nom}: flotte vide — {deleted} supprimés", wid)
                    break
                log(f"🔄 {nom}: refresh batch ({remaining} restants)", wid)
                load_page_max_pagination(driver)
                consecutive_fails = 0
                stale_count = 0
            elif "stale" in res.lower():
                stale_count += 1
                log(f"⚠️ {nom}: stale #{stale_count}", wid)
                if stale_count >= 3:
                    log(f"🔄 {nom}: trop de stale, refresh forcé", wid)
                    load_page_max_pagination(driver)
                    stale_count = 0
                else:
                    time.sleep(0.5)
            else:
                consecutive_fails += 1
                log(f"⚠️ {nom}: échec #{consecutive_fails}: {res}", wid)
                if consecutive_fails >= 5:
                    log(f"❌ {nom}: 5 échecs consécutifs, arrêt", wid)
                    result["error"] = "5 échecs consécutifs"
                    break
                time.sleep(1)

        result["deleted"] = deleted
        result["failed"]  = consecutive_fails
        send_slack(
            f"✅ [W{wid}] {nom} — {deleted} supprimés",
            "#36a64f" if not result["error"] else "#ffaa00"
        )

    except Exception as e:
        log(f"💥 {nom}: {e}", wid)
        traceback.print_exc()
        result["error"] = str(e)
        send_slack(f"❌ [W{wid}] {nom} — crash: {e}", "#ff0000")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def derive_email(nom: str) -> str:
    m = re.compile(r'^\s*(partenaires?)[-_]?(\d+)\s*$', re.I).match(nom or "")
    return f"{m.group(1).lower()}{m.group(2)}@upjunoo.com" if m else ""


def main():
    parser = argparse.ArgumentParser(description="Vide les flottes en parallèle")
    parser.add_argument("--workers",  type=int, default=5,  help="Nombre de workers (défaut: 5)")
    parser.add_argument("--dry-run",  action="store_true",  help="Simulation")
    parser.add_argument("--start",    type=int, default=1,  help="Numéro de partenaire de départ")
    parser.add_argument("--end",      type=int, default=0,  help="Numéro de partenaire de fin (0=tous)")
    parser.add_argument("--only",     type=int, default=0,  help="Un seul partenaire (numéro)")
    parser.add_argument("--input",    default=str(REFERENCE_FILE))
    args = parser.parse_args()

    log(f"\n{'='*60}")
    log(f"🗑️  VIDAGE PARALLÈLE — {args.workers} workers")
    log(f"{'='*60}")

    with open(args.input, encoding="utf-8") as f:
        raw = json.load(f)

    partners = []
    for p in raw:
        nom = p.get("nom", "")
        email = p.get("email", "") or derive_email(nom)
        if not email:
            continue
        m = PARTNER_NUM_RE.match(nom)
        num = int(m.group(1)) if m else 9999
        partners.append({"nom": nom, "email": email, "num": num})

    partners.sort(key=lambda x: x["num"])

    if args.only:
        partners = [p for p in partners if p["num"] == args.only]
    else:
        partners = [p for p in partners if p["num"] >= args.start]
        if args.end:
            partners = [p for p in partners if p["num"] <= args.end]

    log(f"   📋 {len(partners)} partenaires | {args.workers} workers")
    if args.dry_run:
        log(f"   🧪 MODE DRY-RUN")

    send_slack(
        f"🚀 Vidage parallèle démarré — {len(partners)} partenaires, {args.workers} workers",
        "#439FE0"
    )

    start_time = time.time()
    all_results = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_partner, p, (i % args.workers) + 1, args.dry_run): p
            for i, p in enumerate(partners)
        }
        for future in as_completed(futures):
            try:
                r = future.result()
                all_results.append(r)
            except Exception as e:
                log(f"💥 Future error: {e}")

    duration = time.time() - start_time
    total_del = sum(r["deleted"] for r in all_results)
    total_err = sum(1 for r in all_results if r.get("error"))

    log(f"\n{'='*60}")
    log(f"✅ TERMINÉ en {duration/60:.1f} min")
    log(f"   Partenaires : {len(all_results)}")
    log(f"   🗑️  Supprimés : {total_del}")
    log(f"   ❌ Erreurs   : {total_err}")
    log(f"{'='*60}")

    send_slack(
        f"✅ Vidage terminé en {duration/60:.1f} min\n"
        f"• {len(all_results)} partenaires\n"
        f"• 🗑️ {total_del} véhicules supprimés\n"
        f"• ❌ {total_err} erreurs",
        "#36a64f" if not total_err else "#ffaa00"
    )


if __name__ == "__main__":
    main()
