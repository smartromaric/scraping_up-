#!/usr/bin/env python3
"""
clear_fleet_vps.py
==================
Vide entièrement la flotte de chaque partenaire (1 → 120).
Supprime TOUS les véhicules, un par un, en rechargeant après chaque suppression.

Usage :
  python3 clear_fleet_vps.py                      # tous les partenaires 1→120
  python3 clear_fleet_vps.py --only partenaire42  # un seul
  python3 clear_fleet_vps.py --start partenaire-5 # reprendre depuis
  python3 clear_fleet_vps.py --dry-run            # simulation
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
import shutil
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR           = Path(__file__).parent
REFERENCE_FILE     = BASE_DIR / "output" / "organized_by_partner" / "all_partners_enriched.json"
LOG_FILE           = BASE_DIR / "output" / "clear_fleet.log"

OWNER_LOGIN_URL    = "https://upjunoo-server-new.junooapps.com/login/owner-login"
MANAGE_FLEET_URL   = "https://upjunoo-server-new.junooapps.com/manage-fleet"
UNIVERSAL_PASSWORD = "123456789@"

PARTNER_NUM_RE = re.compile(r'^\s*partenaires?[-_]?\s*(\d+)\s*$', re.I)

# ─────────────────────────────────────────────────────────────────────────────
#  LOG
# ─────────────────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─────────────────────────────────────────────────────────────────────────────
#  CHROME
# ─────────────────────────────────────────────────────────────────────────────

def setup_driver(headed: bool = False):
    chrome_options = Options()
    if not headed:
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
    if not headed:
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    if not headed:
        for binary in [
            "chromium-browser",            # wrapper Ubuntu qui pointe vers snap
            "/snap/bin/chromium",
            "chromium",
            "google-chrome-stable",
            "google-chrome",
        ]:
            path = binary if os.path.isfile(binary) else shutil.which(binary)
            if path:
                chrome_options.binary_location = path
                log(f"   🌐 Chrome binary: {path}")
                break

    chromedriver_path = None
    for cd in [
        "/usr/bin/chromedriver",           # chromedriver réel installé sur ce VPS
        "chromedriver",
        "/snap/bin/chromium.chromedriver",
        "/snap/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/lib/chromium/chromedriver",
    ]:
        found = cd if os.path.isfile(cd) else shutil.which(cd)
        if found:
            chromedriver_path = found
            log(f"   🔧 Chromedriver: {chromedriver_path}")
            break

    if not chromedriver_path:
        log("   ❌ Aucun chromedriver trouvé — installe-le avec: sudo apt install chromium-chromedriver")
        sys.exit(1)

    service = Service(chromedriver_path)
    return webdriver.Chrome(service=service, options=chrome_options)


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────────────────────

def login(driver, email: str) -> bool:
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
            log(f"      ✅ Connecté : {email}")
            return True
        except Exception as e:
            log(f"      ⚠️ Login tentative {attempt}/3 échouée: {e}")
            time.sleep(3)
    log(f"      ❌ Login impossible : {email}")
    return False


def logout(driver):
    try:
        driver.get("https://upjunoo-server-new.junooapps.com/logout")
        time.sleep(1)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  PAGINATION
# ─────────────────────────────────────────────────────────────────────────────

def set_pagination_max(driver):
    try:
        sel = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select"))
        )
        options = driver.execute_script(
            "return Array.from(arguments[0].options).map(o=>o.value)", sel
        )
        best = max((v for v in options if v.isdigit()), key=int, default=None)
        if best:
            driver.execute_script(
                "arguments[0].value=arguments[1];"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}))",
                sel, best
            )
            time.sleep(1.5)
    except Exception:
        pass


def count_rows(driver) -> int:
    try:
        return len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
#  SUPPRIMER UNE LIGNE (toujours la première)
# ─────────────────────────────────────────────────────────────────────────────

def delete_first_row(driver) -> bool:
    try:
        # Opération atomique : ouvre dropdown + clique Supprimer
        result = driver.execute_script("""
            var rows = document.querySelectorAll('table tbody tr');
            if (!rows || rows.length === 0) return 'no_rows';
            var row = rows[0];

            var btn = row.querySelector(
                'button.dropdown-toggle, button[data-bs-toggle="dropdown"], .btn-action, button'
            );
            if (!btn) return 'no_btn';

            if (window.bootstrap && window.bootstrap.Dropdown) {
                var dd = window.bootstrap.Dropdown.getOrCreateInstance(btn);
                dd.show();
            } else {
                btn.click();
            }

            return new Promise(function(resolve) {
                setTimeout(function() {
                    var menu = row.querySelector('.dropdown-menu.show, .dropdown-menu');
                    if (!menu) { resolve('no_menu'); return; }
                    var items = menu.querySelectorAll('a, button, li');
                    for (var i = 0; i < items.length; i++) {
                        var t = items[i].textContent.trim().toLowerCase();
                        if (t.includes('supprimer') || t.includes('delete') || t.includes('retirer')) {
                            items[i].click();
                            resolve('clicked');
                            return;
                        }
                    }
                    resolve('no_supprimer');
                }, 400);
            });
        """)

        if result != "clicked":
            log(f"      ⚠️ Résultat dropdown: {result}")
            return False

        # Confirmer "Yes, delete it!"
        confirm = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
        )
        time.sleep(0.2)
        driver.execute_script("arguments[0].click();", confirm)

        # Cliquer "OK" sur le modal succès
        time.sleep(0.5)
        try:
            ok = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
            )
            driver.execute_script("arguments[0].click();", ok)
        except TimeoutException:
            pass

        # Attendre disparition modale
        end = time.time() + 3
        while time.time() < end:
            if not driver.find_elements(By.CSS_SELECTOR, ".swal2-container .swal2-popup"):
                break
            time.sleep(0.2)

        time.sleep(0.5)
        return True

    except Exception as e:
        log(f"      ❌ Erreur suppression: {e}")
        try:
            driver.execute_script(
                "var b=document.querySelector('.swal2-cancel'); if(b) b.click();"
            )
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  VIDER UN PARTENAIRE
# ─────────────────────────────────────────────────────────────────────────────

def clear_partner(driver, email: str, dry_run: bool = False) -> dict:
    stats = {"deleted": 0, "failed": 0, "total": 0}

    if not login(driver, email):
        stats["login_failed"] = True
        return stats

    # Compter le total initial
    driver.get(MANAGE_FLEET_URL)
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
        )
    except TimeoutException:
        pass
    time.sleep(1)
    set_pagination_max(driver)
    total = count_rows(driver)
    stats["total"] = total

    if total == 0:
        log(f"      ℹ️ Flotte déjà vide")
        logout(driver)
        return stats

    log(f"      🗑️  {total} véhicule(s) à supprimer")

    if dry_run:
        log(f"      🧪 [DRY-RUN] Suppression simulée")
        stats["deleted"] = total
        logout(driver)
        return stats

    consecutive_failures = 0
    while True:
        # Recharger la page fraîche
        driver.get(MANAGE_FLEET_URL)
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
            )
        except TimeoutException:
            pass
        time.sleep(1)
        set_pagination_max(driver)
        time.sleep(0.5)

        remaining = count_rows(driver)
        if remaining == 0:
            log(f"      ✅ Flotte vidée ({stats['deleted']} supprimés)")
            break

        log(f"      → {remaining} restant(s) | suppression en cours...")

        ok = delete_first_row(driver)
        if ok:
            stats["deleted"] += 1
            consecutive_failures = 0
        else:
            stats["failed"] += 1
            consecutive_failures += 1
            if consecutive_failures >= 5:
                log(f"      ❌ 5 échecs consécutifs — arrêt pour ce partenaire")
                break

    logout(driver)
    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def derive_email(nom: str) -> str:
    m = PARTNER_NUM_RE.match(nom or "")
    if not m:
        return ""
    return f"partenaire{m.group(1)}@upjunoo.com"


def main():
    parser = argparse.ArgumentParser(description="Vide la flotte de chaque partenaire")
    parser.add_argument("--only",    help="Un seul partenaire (ex: partenaire42)")
    parser.add_argument("--start",   help="Reprendre depuis ce partenaire")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--headed",  action="store_true", help="Chrome visible (Mac local)")
    parser.add_argument("--input",   default=str(REFERENCE_FILE),
                        help="JSON de référence (all_partners_enriched.json)")
    args = parser.parse_args()

    log(f"\n{'='*60}")
    log("🗑️  VIDAGE FLOTTE — VPS")
    log(f"{'='*60}")

    input_path = Path(args.input)
    if not input_path.exists():
        log(f"❌ Fichier introuvable: {input_path}")
        sys.exit(1)

    with open(input_path, encoding="utf-8") as f:
        raw_partners = json.load(f)

    # Construire la liste triée par numéro
    partners = []
    for p in raw_partners:
        nom = p.get("nom", "")
        email = derive_email(nom)
        if not email:
            continue
        m = PARTNER_NUM_RE.match(nom)
        num = int(m.group(1)) if m else 9999
        partners.append({"nom": nom, "email": email, "num": num})

    partners.sort(key=lambda x: x["num"])
    log(f"   📂 {len(partners)} partenaires chargés (triés 1→120)")

    # Filtres
    def _norm(s):
        return re.sub(r'[\s\-_]', '', (s or "")).lower()

    if args.only:
        partners = [p for p in partners if _norm(p["nom"]) == _norm(args.only)]
        if not partners:
            log(f"❌ '{args.only}' introuvable"); sys.exit(1)

    if args.start and not args.only:
        names = [_norm(p["nom"]) for p in partners]
        target = _norm(args.start)
        if target not in names:
            log(f"❌ '{args.start}' introuvable"); sys.exit(1)
        partners = partners[names.index(target):]
        log(f"   ▶️ Reprise depuis {partners[0]['nom']}")

    log(f"   📋 {len(partners)} partenaires à traiter")
    if args.dry_run:
        log(f"   🧪 MODE DRY-RUN")

    driver = setup_driver(headed=args.headed)
    total_deleted = 0
    total_failed  = 0

    try:
        for i, p in enumerate(partners, 1):
            log(f"\n   ▶️ [{i}/{len(partners)}] {p['nom']} ({p['email']})")
            st = clear_partner(driver, p["email"], dry_run=args.dry_run)
            total_deleted += st.get("deleted", 0)
            total_failed  += st.get("failed", 0)
            log(f"      📊 Supprimés={st.get('deleted',0)} | Échecs={st.get('failed',0)}")

        log(f"\n{'='*60}")
        log(f"✅ VIDAGE TERMINÉ")
        log(f"   🗑️  Total supprimés : {total_deleted}")
        log(f"   ❌ Total échecs    : {total_failed}")
        log(f"{'='*60}")

    except KeyboardInterrupt:
        log("\n🛑 Interrompu.")
    except Exception as e:
        log(f"\n💥 Erreur fatale: {e}")
        traceback.print_exc()
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
