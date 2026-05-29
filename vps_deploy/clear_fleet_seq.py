#!/usr/bin/env python3
"""
clear_fleet_seq.py
==================
Vide la flotte de tous les partenaires SÉQUENTIELLEMENT.
1 seul Chrome, login → vide → logout → partenaire suivant.

Usage:
  python3 clear_fleet_seq.py
  python3 clear_fleet_seq.py --start 8 --end 20
  python3 clear_fleet_seq.py --only 8
  python3 clear_fleet_seq.py --dry-run
"""

import argparse
import json
import os
import re
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    import requests
except ImportError:
    requests = None

BASE_DIR         = Path(__file__).parent
REFERENCE_FILE   = BASE_DIR / "output" / "organized_by_partner" / "all_partners_enriched.json"
LOG_FILE         = "/tmp/clear_seq.log"
OWNER_LOGIN_URL  = "https://upjunoo-server-new.junooapps.com/login/owner-login"
MANAGE_FLEET_URL = "https://upjunoo-server-new.junooapps.com/manage-fleet"
UNIVERSAL_PASS   = "123456789@"
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "")
PARTNER_RE       = re.compile(r'^\s*(partenaires?)[-_]?(\d+)\s*$', re.I)


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def send_slack(msg, color="#439FE0"):
    if not WEBHOOK_URL or not requests:
        return
    try:
        requests.post(WEBHOOK_URL, json={
            "attachments": [{"color": color, "text": msg, "mrkdwn_in": ["text"]}]
        }, timeout=10)
    except Exception:
        pass


def setup_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-zygote")
    opts.add_argument("--remote-debugging-port=9600")
    opts.add_argument("--window-size=1280,800")

    for binary in ["chromium-browser", "/snap/bin/chromium", "chromium", "google-chrome"]:
        path = binary if os.path.isfile(binary) else shutil.which(binary)
        if path:
            opts.binary_location = path
            break

    for cd in ["/usr/bin/chromedriver", "/snap/bin/chromium.chromedriver", "chromedriver"]:
        if os.path.isfile(cd) or shutil.which(cd):
            cdpath = cd if os.path.isfile(cd) else shutil.which(cd)
            return webdriver.Chrome(service=Service(cdpath), options=opts)

    raise RuntimeError("chromedriver introuvable")


def login(driver, email):
    for attempt in range(1, 4):
        try:
            driver.get(OWNER_LOGIN_URL)
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[type='email'],input[type='text']")
                )
            )
            em = driver.find_element(By.CSS_SELECTOR, "input[type='email'],input[type='text']")
            pw = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            em.clear(); em.send_keys(email)
            pw.clear(); pw.send_keys(UNIVERSAL_PASS)
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            )
            WebDriverWait(driver, 25).until(EC.url_contains("/owner-dashboard"))
            log(f"✅ Connecté : {email}")
            return True
        except Exception as e:
            log(f"  Login tentative {attempt}/3 : {str(e).splitlines()[0]}")
            time.sleep(3)
    return False


def logout(driver):
    try:
        driver.get("https://upjunoo-server-new.junooapps.com/logout")
        time.sleep(1)
    except Exception:
        pass


def get_total(driver):
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


def delete_first_row(driver) -> str:
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


def clear_partner(driver, nom, dry_run=False):
    load_page_max_pagination(driver)
    total = get_total(driver)
    log(f"📊 {nom} : {total} véhicules")

    if total == 0:
        log(f"✅ {nom} déjà vide")
        return 0

    if dry_run:
        log(f"🧪 [DRY-RUN] {nom} : {total} suppressions simulées")
        return total

    deleted = 0
    stale_count = 0
    consecutive_fails = 0
    start = time.time()

    while True:
        res = delete_first_row(driver)

        if res == "ok":
            deleted += 1
            stale_count = 0
            consecutive_fails = 0
            elapsed = time.time() - start
            log(f"  [{deleted}/{total}] supprimé ({elapsed:.1f}s)")
        elif res in ("no_rows", "empty") or "no_btn" in res:
            remaining = get_total(driver)
            if remaining == 0:
                log(f"✅ {nom} vide — {deleted} supprimés")
                break
            log(f"🔄 Refresh batch ({remaining} restants)")
            load_page_max_pagination(driver)
            stale_count = 0
            consecutive_fails = 0
        elif "stale" in res.lower():
            stale_count += 1
            if stale_count >= 3:
                log(f"🔄 Stale répété, refresh forcé")
                load_page_max_pagination(driver)
                stale_count = 0
            else:
                time.sleep(0.5)
        else:
            consecutive_fails += 1
            log(f"⚠️ Échec #{consecutive_fails} : {res}")
            if consecutive_fails >= 5:
                log(f"❌ 5 échecs consécutifs, arrêt")
                break
            time.sleep(1)

    # Refresh final
    driver.get(MANAGE_FLEET_URL)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table,body"))
        )
    except TimeoutException:
        pass
    time.sleep(0.5)
    final = get_total(driver)
    elapsed = time.time() - start
    log(f"📊 Après refresh : {final} restants | ⏱️ {elapsed:.0f}s total")
    return deleted


def derive_email(nom):
    m = PARTNER_RE.match(nom or "")
    return f"{m.group(1).lower()}{m.group(2)}@upjunoo.com" if m else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start",   type=int, default=1)
    parser.add_argument("--end",     type=int, default=0)
    parser.add_argument("--only",    type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--input",   default=str(REFERENCE_FILE))
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        raw = json.load(f)

    partners = []
    for p in raw:
        nom = p.get("nom", "")
        email = p.get("email", "") or derive_email(nom)
        if not email:
            continue
        m = PARTNER_RE.match(nom)
        num = int(m.group(2)) if m else 9999
        partners.append({"nom": nom, "email": email, "num": num})

    partners.sort(key=lambda x: x["num"])

    if args.only:
        partners = [p for p in partners if p["num"] == args.only]
    else:
        partners = [p for p in partners if p["num"] >= args.start]
        if args.end:
            partners = [p for p in partners if p["num"] <= args.end]

    log(f"\n{'='*60}")
    log(f"🗑️  VIDAGE SÉQUENTIEL — {len(partners)} partenaires")
    log(f"{'='*60}")
    send_slack(f"🚀 Vidage séquentiel démarré — {len(partners)} partenaires", "#439FE0")

    log("🚀 Démarrage Chrome unique...")
    driver = setup_driver()

    total_deleted = 0
    errors = []
    start_time = time.time()

    try:
        for i, p in enumerate(partners):
            nom   = p["nom"]
            email = p["email"]
            log(f"\n[{i+1}/{len(partners)}] 🔐 {nom} ({email})")

            if not login(driver, email):
                log(f"❌ Login échoué : {nom}")
                send_slack(f"❌ {nom} — login échoué", "#ff0000")
                errors.append(nom)
                continue

            deleted = clear_partner(driver, nom, args.dry_run)
            total_deleted += deleted
            send_slack(f"✅ {nom} — {deleted} supprimés", "#36a64f")

            logout(driver)
            time.sleep(1)

    finally:
        driver.quit()
        log("🏁 Chrome fermé")

    duration = time.time() - start_time
    log(f"\n{'='*60}")
    log(f"✅ TERMINÉ en {duration/60:.1f} min")
    log(f"   🗑️  {total_deleted} véhicules supprimés")
    log(f"   ❌ {len(errors)} erreurs : {errors}")
    log(f"{'='*60}")

    send_slack(
        f"✅ Vidage séquentiel terminé en {duration/60:.1f} min\n"
        f"• 🗑️ {total_deleted} véhicules supprimés\n"
        f"• ❌ {len(errors)} erreurs : {', '.join(errors) if errors else 'aucune'}",
        "#36a64f" if not errors else "#ffaa00"
    )


if __name__ == "__main__":
    main()
