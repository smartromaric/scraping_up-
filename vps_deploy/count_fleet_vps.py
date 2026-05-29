#!/usr/bin/env python3
"""
count_fleet_vps.py
==============
Compte le nombre de véhicules de chaque partenaire et envoie le rapport sur Slack.

Usage:
  python3 count_fleet_vps.py                    # tous les partenaires
  python3 count_fleet_vps.py --start 1 --end 20 # partenaires 1 à 20
  WEBHOOK_URL=https://hooks.slack.com/... python3 count_fleet_vps.py
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock

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

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BASE_DIR           = Path(__file__).parent
REFERENCE_FILE     = BASE_DIR / "output" / "organized_by_partner" / "all_partners_enriched.json"
OWNER_LOGIN_URL    = "https://upjunoo-server-new.junooapps.com/login/owner-login"
MANAGE_FLEET_URL   = "https://upjunoo-server-new.junooapps.com/manage-fleet"
UNIVERSAL_PASSWORD = "123456789@"
WEBHOOK_URL        = os.getenv("WEBHOOK_URL", "")
PARTNER_NUM_RE     = re.compile(r'^\s*partenaires?[-_]?\s*(\d+)\s*$', re.I)
EMAIL_FROM_NOM_RE  = re.compile(r'^\s*(partenaires?)[-_]?(\d+)\s*$', re.I)

_log_lock = Lock()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def email_from_nom(nom: str) -> str:
    m = EMAIL_FROM_NOM_RE.match(nom or "")
    if not m:
        return ""
    return f"{m.group(1).lower()}{m.group(2)}@upjunoo.com"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        print(line, flush=True)


def send_slack(msg: str, color: str = "#439FE0"):
    if not WEBHOOK_URL or not requests:
        return
    try:
        requests.post(WEBHOOK_URL, json={
            "attachments": [{"color": color, "text": msg, "mrkdwn_in": ["text"]}]
        }, timeout=10)
    except Exception:
        pass


def setup_driver(port: int):
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


def login(driver, email: str) -> bool:
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
            btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            driver.execute_script("arguments[0].click();", btn)
            WebDriverWait(driver, 25).until(EC.url_contains("/owner-dashboard"))
            return True
        except Exception as e:
            if attempt < 3:
                time.sleep(3)
    return False


def count_vehicles(driver) -> int:
    try:
        driver.get(MANAGE_FLEET_URL)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
        )
        time.sleep(0.5)
        text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r'de\s+(\d+)\s+entr', text, re.I)
        if m:
            return int(m.group(1))
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        return len(rows)
    except Exception:
        return -1


# ─── WORKER ───────────────────────────────────────────────────────────────────

def check_partner(partner: dict) -> dict:
    nom = partner["nom"]
    email = partner["email"]
    num = partner["num"]
    driver = None
    try:
        driver = setup_driver(9400 + num)
        if not login(driver, email):
            log(f"❌ {nom}: login échoué")
            return {"nom": nom, "num": num, "count": -1, "error": "login"}
        count = count_vehicles(driver)
        status = "✅ vide" if count == 0 else f"🚗 {count} véhicules"
        log(f"{nom}: {status}")
        return {"nom": nom, "num": num, "count": count, "error": None}
    except Exception as e:
        log(f"💥 {nom}: {e}")
        return {"nom": nom, "num": num, "count": -1, "error": str(e)}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start",   type=int, default=1)
    parser.add_argument("--end",     type=int, default=0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--input",   default=str(REFERENCE_FILE))
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        raw = json.load(f)

    partners = []
    for p in raw:
        nom = p.get("nom", "")
        email = email_from_nom(nom)
        m = PARTNER_NUM_RE.match(nom)
        if not m or not email:
            continue
        num = int(m.group(1))
        partners.append({"nom": nom, "email": email, "num": num})

    partners.sort(key=lambda x: x["num"])
    partners = [p for p in partners if p["num"] >= args.start]
    if args.end:
        partners = [p for p in partners if p["num"] <= args.end]

    log(f"🔍 Vérification de {len(partners)} partenaires ({args.workers} workers)...")
    send_slack(f"🔍 Début vérification flotte — {len(partners)} partenaires", "#439FE0")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(check_partner, p): p for p in partners}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                log(f"💥 future: {e}")

    results.sort(key=lambda x: x["num"])

    # Rapport
    vides    = [r for r in results if r["count"] == 0]
    non_vides = [r for r in results if r["count"] > 0]
    erreurs  = [r for r in results if r["count"] < 0]

    log(f"\n{'='*50}")
    log(f"✅ Vides       : {len(vides)}/{len(results)}")
    log(f"🚗 Non vides   : {len(non_vides)}")
    log(f"❌ Erreurs     : {len(erreurs)}")

    # Détail non vides
    if non_vides:
        log("\n🚗 Partenaires avec véhicules restants :")
        for r in non_vides:
            log(f"   {r['nom']}: {r['count']} véhicules")

    # Slack — résumé
    slack_lines = [
        f"*📊 Rapport flotte — {datetime.now().strftime('%H:%M')}*",
        f"✅ Vides: *{len(vides)}* | 🚗 Non vides: *{len(non_vides)}* | ❌ Erreurs: *{len(erreurs)}*",
        "",
    ]
    if non_vides:
        slack_lines.append("*🚗 À vider encore :*")
        for r in non_vides:
            slack_lines.append(f"  • {r['nom']}: {r['count']} véhicules")
    if erreurs:
        slack_lines.append("\n*❌ Erreurs login :*")
        for r in erreurs:
            slack_lines.append(f"  • {r['nom']}: {r['error']}")

    send_slack("\n".join(slack_lines), "#36a64f" if not non_vides else "#ffaa00")

    # Sauvegarde JSON
    out = BASE_DIR / "output" / "fleet_count_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"\n📄 Rapport sauvegardé : {out}")


if __name__ == "__main__":
    main()
