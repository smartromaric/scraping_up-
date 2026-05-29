"""
Script VPS pour enrichissement véhicules des conducteurs - UpJunoo (Mode headless)
===================================================================================
1. Lit output/conducteurs.json
2. Se connecte à UpJunoo via Playwright (headless)
3. Visite chaque profil conducteur pour extraire les infos véhicule
4. Exporte conducteurs_vehicles.json + conducteurs_sans_vehicles.json
5. Notification Slack à la fin
"""

import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# ─── Configuration ──────────────────────────────────────────────────────────────
BASE_URL = "https://upjunoo-server-new.junooapps.com"
OUTPUT_DIR = Path(__file__).parent / "output"
INPUT_JSON = OUTPUT_DIR / "conducteurs.json"
OUTPUT_JSON = OUTPUT_DIR / "conducteurs_vehicles.json"
NO_VEHICLE_JSON = OUTPUT_DIR / "conducteurs_sans_vehicles.json"
LOG_FILE = OUTPUT_DIR / "enrich_vehicles.log"

CONCURRENCY = 10
BATCH_SIZE = 50
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


class VehicleEnricher:
    def __init__(self):
        self.session_cookies = None
        self.drivers_data = []
        self.processed_count = 0

    async def get_session_cookies(self):
        """Login automatique + récupération des cookies de session."""
        email = os.getenv("UPJUNOO_EMAIL")
        password = os.getenv("UPJUNOO_PASSWORD")

        if not email or not password:
            log("❌ Variables UPJUNOO_EMAIL et UPJUNOO_PASSWORD requises")
            send_slack("❌ enrich_vehicles: Variables d'environnement manquantes", "#ff0000")
            sys.exit(1)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            log(f"🔐 Connexion à {BASE_URL}/login/admin...")
            try:
                await page.goto(f"{BASE_URL}/login/admin", timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                log(f"⚠️ Page lente ({e}), on continue...")

            try:
                await page.fill("input[type='email'], input[name='email']", email, timeout=15000)
                await page.fill("input[type='password'], input[name='password']", password, timeout=15000)
                try:
                    await page.click("button[type='submit']", timeout=5000)
                except Exception:
                    await page.keyboard.press("Enter")

                await page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
                log(f"✅ Connecté: {page.url}")
            except Exception as e:
                log(f"❌ Échec login: {e}")
                send_slack(f"❌ enrich_vehicles: Échec login - {e}", "#ff0000")
                await browser.close()
                sys.exit(1)

            cookies = await context.cookies()
            self.session_cookies = {c['name']: c['value'] for c in cookies}
            await browser.close()

        if not self.session_cookies:
            log("❌ Erreur session.")
            send_slack("❌ enrich_vehicles: Erreur session cookies", "#ff0000")
            sys.exit(1)

    async def fetch_one(self, session, driver_entry):
        """Récupère les infos véhicule d'un conducteur."""
        profile_url = driver_entry.get('view_profile', "")
        if not profile_url or profile_url == "N/A":
            return

        try:
            async with session.get(profile_url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    html_content = await response.text()
                    soup = BeautifulSoup(html_content, 'html.parser')

                    vehicle_info = {
                        "type": "N/A",
                        "marque": "N/A",
                        "modele": "N/A",
                        "matricule": "N/A"
                    }

                    app_div = soup.find('div', id='app')
                    if app_div and app_div.get('data-page'):
                        try:
                            raw_json = app_div.get('data-page')
                            data_page = json.loads(raw_json)

                            def find_key(obj, key):
                                if isinstance(obj, dict):
                                    if key in obj and obj[key] not in [None, "", "null"]:
                                        return obj[key]
                                    for v in obj.values():
                                        res = find_key(v, key)
                                        if res: return res
                                elif isinstance(obj, list):
                                    for item in obj:
                                        res = find_key(item, key)
                                        if res: return res
                                return None

                            v_type = find_key(data_page, 'vehicle_type_name')
                            v_marque = find_key(data_page, 'car_make_name')
                            v_modele = find_key(data_page, 'car_model_name')
                            v_matricule = find_key(data_page, 'car_number')

                            if v_type: vehicle_info["type"] = str(v_type).upper()
                            if v_marque: vehicle_info["marque"] = str(v_marque).upper()
                            if v_modele: vehicle_info["modele"] = str(v_modele).upper()
                            if v_matricule: vehicle_info["matricule"] = str(v_matricule).upper()

                        except Exception as e:
                            log(f"  ⚠️ Erreur parsing JSON: {e}")

                    driver_entry['vehicle'] = vehicle_info
                    self.processed_count += 1

                    if self.processed_count % 50 == 0:
                        log(f"  ⏳ {self.processed_count} conducteurs traités...")
                else:
                    log(f"  ⚠️ HTTP {response.status} pour {driver_entry.get('nom')}")
        except Exception as e:
            log(f"  ⚠️ Erreur requête {driver_entry.get('nom')}: {e}")

    async def run(self):
        log("=" * 60)
        log("🚀 Démarrage enrichissement véhicules (VPS Mode)")
        log("=" * 60)

        # 1. Chargement
        if not INPUT_JSON.exists():
            msg = f"❌ Fichier source introuvable: {INPUT_JSON}"
            log(msg)
            send_slack(msg, "#ff0000")
            return

        with open(INPUT_JSON, 'r', encoding='utf-8') as f:
            self.drivers_data = json.load(f)

        log(f"📊 {len(self.drivers_data)} conducteurs chargés")

        await self.get_session_cookies()

        # 2. Traitement par lots
        to_process = [d for d in self.drivers_data if 'vehicle' not in d]
        log(f"📊 {len(to_process)} conducteurs à enrichir")

        async with aiohttp.ClientSession(cookies=self.session_cookies) as session:
            for i in range(0, len(to_process), BATCH_SIZE):
                batch = to_process[i:i + BATCH_SIZE]
                tasks = [self.fetch_one(session, d) for d in batch]
                await asyncio.gather(*tasks)
                self.save_data()

        # 3. Générer la liste des conducteurs sans véhicule
        sans_vehicule = []
        avec_vehicule = 0
        for d in self.drivers_data:
            v = d.get('vehicle', {})
            if all(v.get(k, "N/A") == "N/A" for k in ["type", "marque", "modele", "matricule"]):
                sans_vehicule.append(d)
            else:
                avec_vehicule += 1

        with open(NO_VEHICLE_JSON, 'w', encoding='utf-8') as f:
            json.dump(sans_vehicule, f, ensure_ascii=False, indent=2)
        log(f"✅ conducteurs_sans_vehicles.json: {len(sans_vehicule)} conducteurs sans véhicule")

        # 4. Résultat final
        log("=" * 60)
        log("✨ TERMINÉ!")
        log(f"📊 {self.processed_count} profils traités")
        log(f"🚗 {avec_vehicule} avec véhicule")
        log(f"❌ {len(sans_vehicule)} sans véhicule")
        log(f"📁 {OUTPUT_JSON}")
        log(f"📁 {NO_VEHICLE_JSON}")
        log("=" * 60)

        msg = (
            f"✅ Enrichissement véhicules terminé!\n"
            f"{self.processed_count} profils traités\n"
            f"🚗 {avec_vehicule} avec véhicule\n"
            f"❌ {len(sans_vehicule)} sans véhicule"
        )
        send_slack(msg, "#36a64f")

    def save_data(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
            json.dump(self.drivers_data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    try:
        enricher = VehicleEnricher()
        asyncio.run(enricher.run())
    except Exception as e:
        log(f"❌ ERREUR CRITIQUE: {e}")
        send_slack(f"❌ Erreur enrichissement véhicules: {str(e)}", "#ff0000")
