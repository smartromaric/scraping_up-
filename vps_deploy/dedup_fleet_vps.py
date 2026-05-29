#!/usr/bin/env python3
"""
dedup_fleet_vps.py
==================
Supprime les doublons dans la flotte de chaque partenaire.

Doublon = même (type_véhicule, marque, modèle, matricule).
On garde 1 exemplaire, on supprime les N-1 autres.

Usage:
  python3 dedup_fleet_vps.py                      # tous les partenaires
  python3 dedup_fleet_vps.py --only 8              # partenaire 8 uniquement
  python3 dedup_fleet_vps.py --start 1 --end 20    # partenaires 1 à 20
  python3 dedup_fleet_vps.py --dry-run             # simulation (aucune suppression)
"""

import argparse
import json
import os
import re
import shutil
import time
import traceback
from collections import Counter
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

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR         = Path(__file__).parent
REFERENCE_FILE   = BASE_DIR / "output" / "organized_by_partner" / "all_partners_enriched.json"
LOG_FILE         = "/tmp/dedup_fleet.log"
OWNER_LOGIN_URL  = "https://upjunoo-server-new.junooapps.com/login/owner-login"
MANAGE_FLEET_URL = "https://upjunoo-server-new.junooapps.com/manage-fleet"
UNIVERSAL_PASS   = "123456789@"
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "")
PARTNER_RE       = re.compile(r'^\s*(partenaires?)[-_]?(\d+)\s*$', re.I)

# Colonnes du tableau /manage-fleet :
# [0] Type de véhicule
# [1] Marque de voiture
# [2] Modèle de voiture
# [3] Affichage du document
# [4] Numéro de plaque d'immatriculation
# [5] Statut
# [6] Raison
# [7] Action
COL_TYPE   = 0
COL_MARQUE = 1
COL_MODELE = 2
COL_PLAQUE = 4


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


# ─────────────────────────────────────────────────────────────────────────────
#  CHROME
# ─────────────────────────────────────────────────────────────────────────────

def setup_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-zygote")
    opts.add_argument("--remote-debugging-port=9700")
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


# ─────────────────────────────────────────────────────────────────────────────
#  SCRAPE TABLE
# ─────────────────────────────────────────────────────────────────────────────

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
        opts_vals = driver.execute_script(
            "return Array.from(arguments[0].options).map(o=>o.value)", sel
        )
        best = max((v for v in opts_vals if v.isdigit()), key=int, default=None)
        if best:
            driver.execute_script(
                "arguments[0].value=arguments[1];"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}))",
                sel, best
            )
            time.sleep(1)
    except Exception:
        pass


def scrape_all_rows(driver):
    """
    Scrape toutes les lignes du tableau.
    Retourne une liste de dicts avec les infos de chaque ligne + index dans le tableau.
    """
    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    results = []
    for idx, row in enumerate(rows):
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) <= COL_PLAQUE:
                continue
            type_v  = cells[COL_TYPE].text.strip()
            marque  = cells[COL_MARQUE].text.strip()
            modele  = cells[COL_MODELE].text.strip()
            plaque  = cells[COL_PLAQUE].text.strip()
            if not plaque:
                continue
            key = (type_v.lower(), marque.lower(), modele.lower(), plaque.upper())
            results.append({"idx": idx, "key": key, "display": f"{type_v}|{marque}|{modele}|{plaque}"})
        except Exception:
            continue
    return results


def find_duplicate_indices(rows_data):
    """
    Identifie les indices des lignes à supprimer (doublons).
    Pour chaque clé (type, marque, modèle, matricule) qui apparaît N>1 fois,
    on garde la première occurrence et on marque les N-1 suivantes pour suppression.
    Retourne les indices à supprimer en ordre DÉCROISSANT (pour supprimer de bas en haut).
    """
    seen = {}
    to_delete = []
    for row in rows_data:
        key = row["key"]
        if key in seen:
            to_delete.append(row)
        else:
            seen[key] = row
    # Trier par index décroissant pour supprimer de bas en haut sans décaler les indices
    to_delete.sort(key=lambda r: r["idx"], reverse=True)
    return to_delete


# ─────────────────────────────────────────────────────────────────────────────
#  SUPPRESSION D'UNE LIGNE PAR INDEX
# ─────────────────────────────────────────────────────────────────────────────

def delete_row_at_index(driver, row_index: int) -> str:
    """Supprime la ligne à l'index donné dans le tableau visible."""
    try:
        result = driver.execute_script("""
            var rows = document.querySelectorAll('table tbody tr');
            if (!rows || arguments[0] >= rows.length) return 'no_row';
            var row = rows[arguments[0]];
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
        """, row_index)

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
#  DEDUP D'UN PARTENAIRE
# ─────────────────────────────────────────────────────────────────────────────

def dedup_partner(driver, nom, dry_run=False):
    """
    Stratégie :
    1. Charger la page avec pagination max
    2. Scraper toutes les lignes
    3. Identifier les doublons
    4. Supprimer de bas en haut (index décroissant) sans refresh
    5. Si stale/no_row → refresh et recommencer
    """
    load_page_max_pagination(driver)
    time.sleep(1)

    rows_data = scrape_all_rows(driver)
    total = len(rows_data)
    log(f"📊 {nom} : {total} lignes scrapées")

    # Compter les doublons
    key_counts = Counter(r["key"] for r in rows_data)
    duplicates_count = sum(c - 1 for c in key_counts.values() if c > 1)
    unique_duplicated_keys = sum(1 for c in key_counts.values() if c > 1)

    if duplicates_count == 0:
        log(f"✅ {nom} : aucun doublon")
        return 0

    log(f"🔍 {nom} : {duplicates_count} doublons ({unique_duplicated_keys} véhicules dupliqués)")

    # Afficher quelques exemples de doublons
    for key, count in key_counts.most_common(5):
        if count > 1:
            log(f"   🔁 x{count} : {key[0]}|{key[1]}|{key[2]}|{key[3]}")

    if dry_run:
        log(f"🧪 [DRY-RUN] {nom} : {duplicates_count} doublons à supprimer")
        return duplicates_count

    deleted = 0
    start = time.time()
    max_passes = 10  # Sécurité : max 10 passes de refresh

    for pass_num in range(1, max_passes + 1):
        # Re-scraper à chaque passe
        if pass_num > 1:
            load_page_max_pagination(driver)
            time.sleep(1)
            rows_data = scrape_all_rows(driver)

        to_delete = find_duplicate_indices(rows_data)
        if not to_delete:
            log(f"✅ {nom} : plus de doublons (pass {pass_num})")
            break

        log(f"🗑️  Pass {pass_num} : {len(to_delete)} doublons à supprimer")

        pass_deleted = 0
        consecutive_fails = 0

        for item in to_delete:
            res = delete_row_at_index(driver, item["idx"])
            if res == "ok":
                pass_deleted += 1
                deleted += 1
                consecutive_fails = 0
            elif "stale" in res.lower() or res in ("no_row", "no_btn"):
                # DOM a changé après suppression, les index sont décalés → refresh
                log(f"🔄 DOM décalé après {pass_deleted} suppressions, refresh")
                break
            else:
                consecutive_fails += 1
                log(f"⚠️ Échec suppression idx {item['idx']} : {res}")
                if consecutive_fails >= 5:
                    log(f"❌ 5 échecs consécutifs, arrêt passe {pass_num}")
                    break
                time.sleep(0.5)

        log(f"   Pass {pass_num} : {pass_deleted} supprimés (total: {deleted})")

    elapsed = time.time() - start
    log(f"📊 {nom} : {deleted} doublons supprimés en {elapsed:.0f}s")
    return deleted


def derive_email(nom):
    m = PARTNER_RE.match(nom or "")
    return f"{m.group(1).lower()}{m.group(2)}@upjunoo.com" if m else ""


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

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
    log(f"🔍 DEDUP FLOTTE — {len(partners)} partenaires")
    if args.dry_run:
        log(f"🧪 MODE DRY-RUN (aucune suppression)")
    log(f"{'='*60}")
    send_slack(
        f"🔍 Dédoublonnage démarré — {len(partners)} partenaires"
        + (" [DRY-RUN]" if args.dry_run else ""),
        "#439FE0"
    )

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

            deleted = dedup_partner(driver, nom, args.dry_run)
            total_deleted += deleted

            if deleted > 0:
                send_slack(f"✅ {nom} — {deleted} doublons supprimés", "#36a64f")
            else:
                send_slack(f"✅ {nom} — aucun doublon", "#36a64f")

            logout(driver)
            time.sleep(1)

    finally:
        driver.quit()
        log("🏁 Chrome fermé")

    duration = time.time() - start_time
    log(f"\n{'='*60}")
    log(f"✅ TERMINÉ en {duration/60:.1f} min")
    log(f"   🗑️  {total_deleted} doublons supprimés")
    log(f"   ❌ {len(errors)} erreurs : {errors}")
    log(f"{'='*60}")

    send_slack(
        f"✅ Dédoublonnage terminé en {duration/60:.1f} min\n"
        f"• 🗑️ {total_deleted} doublons supprimés\n"
        f"• ❌ {len(errors)} erreurs",
        "#36a64f" if not errors else "#ffaa00"
    )


if __name__ == "__main__":
    main()
