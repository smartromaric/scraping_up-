#!/usr/bin/env python3
"""
test_delete_vps.py
==================
Test de suppression SANS refresh entre chaque véhicule.
Charge la page une fois, supprime ligne par ligne jusqu'à 0.
Refresh uniquement à la fin pour confirmer.

Usage:
  python3 test_delete_vps.py --partner 7
  python3 test_delete_vps.py --partner 7 --dry-run
"""

import argparse
import os
import re
import shutil
import time
from datetime import datetime

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

OWNER_LOGIN_URL    = "https://upjunoo-server-new.junooapps.com/login/owner-login"
MANAGE_FLEET_URL   = "https://upjunoo-server-new.junooapps.com/manage-fleet"
UNIVERSAL_PASSWORD = "123456789@"
WEBHOOK_URL        = os.getenv("WEBHOOK_URL", "")
EMAIL_RE           = re.compile(r'^\s*(partenaires?)[-_]?(\d+)\s*$', re.I)


LOG_FILE = "/tmp/test_delete.log"

def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def send_slack(msg, color="#439FE0"):
    if not WEBHOOK_URL:
        return
    try:
        import requests
        requests.post(WEBHOOK_URL, json={
            "attachments": [{"color": color, "text": msg, "mrkdwn_in": ["text"]}]
        }, timeout=10)
    except Exception:
        pass


def setup_driver(port=9500):
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-zygote")
    opts.add_argument(f"--remote-debugging-port={port}")
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
            pw.clear(); pw.send_keys(UNIVERSAL_PASSWORD)
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            )
            WebDriverWait(driver, 25).until(EC.url_contains("/owner-dashboard"))
            log(f"✅ Connecté : {email}")
            return True
        except Exception as e:
            log(f"Login tentative {attempt}/3 : {e}")
            time.sleep(3)
    return False


def get_total(driver):
    """Lit 'de XX entrées' dans la page."""
    try:
        text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r'de\s+(\d+)\s+entr', text, re.I)
        return int(m.group(1)) if m else -1
    except Exception:
        return -1


def delete_first_row(driver) -> str:
    """
    Supprime la première ligne du tableau SANS recharger la page.
    Retourne : 'ok' | 'empty' | 'error:<msg>'
    """
    try:
        # Vérifie qu'il y a des lignes
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            return "empty"

        # Ouvre le dropdown de la ligne 0 via JS
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
            return f"error:{result}"

        # 1ère confirmation SweetAlert2
        confirm = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
        )
        time.sleep(0.15)
        driver.execute_script("arguments[0].click();", confirm)

        # 2ème popup éventuelle (succès)
        try:
            ok = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
            )
            driver.execute_script("arguments[0].click();", ok)
        except TimeoutException:
            pass

        # Attend que la popup disparaisse
        end = time.time() + 4
        while time.time() < end:
            if not driver.find_elements(By.CSS_SELECTOR, ".swal2-container .swal2-popup"):
                break
            time.sleep(0.15)

        # Attend que le DOM se mette à jour (ligne disparaît)
        time.sleep(0.3)
        return "ok"

    except Exception as e:
        # Ferme popup si ouverte
        try:
            driver.execute_script(
                "var b=document.querySelector('.swal2-cancel'); if(b) b.click();"
            )
        except Exception:
            pass
        return f"error:{str(e).splitlines()[0]}"


def clear_partner_no_refresh(driver, nom, dry_run=False):
    """Vide la flotte sans refresh intermédiaire."""
    def load_page_with_max_pagination():
        driver.get(MANAGE_FLEET_URL)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
            )
        except TimeoutException:
            pass
        time.sleep(0.5)
        # Met pagination à 500
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

    load_page_with_max_pagination()
    total = get_total(driver)
    log(f"📊 {nom} : {total} véhicules au total (pagination 500 visibles)")

    if total == 0:
        log(f"✅ {nom} déjà vide")
        return 0

    if dry_run:
        log(f"🧪 [DRY-RUN] {nom} : {total} suppressions simulées")
        return total

    deleted = 0
    consecutive_fails = 0
    start = time.time()

    while True:
        result = delete_first_row(driver)

        if result == "empty" or result == "error:no_rows":
            # Plus de lignes visibles → refresh pour charger le batch suivant
            remaining = get_total(driver)
            if remaining == 0:
                log(f"✅ Flotte vide après {deleted} suppressions")
                break
            log(f"  🔄 Batch terminé ({deleted} supprimés), refresh — {remaining} restants")
            load_page_with_max_pagination()
            consecutive_fails = 0
        elif result == "ok":
            deleted += 1
            consecutive_fails = 0
            elapsed = time.time() - start
            log(f"  [{deleted}/{total}] supprimé ({elapsed:.1f}s écoulées)")
        elif "no_btn" in result:
            remaining = get_total(driver)
            if remaining == 0:
                log(f"✅ Flotte vide après {deleted} suppressions")
                break
            log(f"  🔄 no_btn — refresh ({remaining} restants)")
            load_page_with_max_pagination()
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            log(f"  ⚠️ Échec #{consecutive_fails} : {result}")
            if consecutive_fails >= 5:
                log(f"❌ 5 échecs consécutifs, arrêt")
                break
            time.sleep(1)

    # Refresh final pour confirmer
    log("🔄 Refresh final...")
    driver.get(MANAGE_FLEET_URL)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table,body"))
        )
    except TimeoutException:
        pass
    time.sleep(0.5)
    final = get_total(driver)
    log(f"📊 Après refresh : {final} véhicules restants")

    elapsed = time.time() - start
    log(f"⏱️  Durée totale : {elapsed:.1f}s pour {deleted} suppressions "
        f"({elapsed/deleted:.1f}s/véhicule)" if deleted else "")

    return deleted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--partner", type=int, required=True, help="Numéro du partenaire à tester")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Cherche le nom dans le JSON pour construire l'email
    from pathlib import Path
    import json
    ref = Path(__file__).parent / "output" / "organized_by_partner" / "all_partners_enriched.json"
    nom = f"partenaire{args.partner}"
    email = None
    if ref.exists():
        with open(ref) as f:
            raw = json.load(f)
        for p in raw:
            n = p.get("nom", "")
            m = EMAIL_RE.match(n)
            if m and int(m.group(2)) == args.partner:
                nom = n
                email = f"{m.group(1).lower()}{m.group(2)}@upjunoo.com"
                break
    if not email:
        email = f"partenaire{args.partner}@upjunoo.com"

    log(f"🚀 Test suppression sans refresh : {nom} ({email})")
    send_slack(f"🚀 Test suppression démarré : *{nom}*", "#439FE0")

    driver = setup_driver(9500 + args.partner)
    try:
        if not login(driver, email):
            log("❌ Login échoué")
            send_slack(f"❌ Login échoué : *{nom}*", "#ff0000")
            return
        deleted = clear_partner_no_refresh(driver, nom, args.dry_run)
        send_slack(
            f"✅ Test terminé : *{nom}*\n• {deleted} véhicules supprimés",
            "#36a64f"
        )
    finally:
        driver.quit()
        log("🏁 Chrome fermé")


if __name__ == "__main__":
    main()
