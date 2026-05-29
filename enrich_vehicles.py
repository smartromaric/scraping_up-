import asyncio
import json
import os
import sys
import re
from pathlib import Path
import aiohttp
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# --- Configuration ---
BASE_URL = "https://upjunoo-server-new.junooapps.com"
INPUT_JSON = Path("output/conducteurs.json")
OUTPUT_JSON = Path("output/conducteurs_vehicles.json")
CONCURRENCY = 10
BATCH_SIZE = 50

class VehicleEnricher:
    def __init__(self):
        self.session_cookies = None
        self.drivers_data = []
        self.processed_count = 0

    async def get_session_cookies(self):
        """Login automatique + récupération des cookies de session."""
        from getpass import getpass

        email = os.getenv("UPJUNOO_EMAIL")
        password = os.getenv("UPJUNOO_PASSWORD")
        if not email:
            email = input("📧 Email admin UpJunoo : ").strip()
        if not password:
            password = getpass("🔑 Mot de passe : ")

        headless_env = os.getenv("UPJUNOO_HEADLESS", "1")
        headless = headless_env not in ("0", "false", "False", "no")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context()
            page = await context.new_page()

            print(f"🌐 Navigation vers {BASE_URL}/login/admin...")
            try:
                await page.goto(f"{BASE_URL}/login/admin", timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"⚠️ Note: La page met du temps à charger ({e}), mais on continue...")

            # Remplissage du formulaire
            print("� Connexion automatique...")
            try:
                await page.fill("input[type='email'], input[name='email']", email, timeout=15000)
                await page.fill("input[type='password'], input[name='password']", password, timeout=15000)
                # Submit
                try:
                    await page.click("button[type='submit']", timeout=5000)
                except Exception:
                    await page.keyboard.press("Enter")

                # Attendre la redirection hors de /login
                await page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
                print(f"  ✅ Connecté. URL : {page.url}")
            except Exception as e:
                print(f"❌ Échec du login automatique : {e}")
                await browser.close()
                sys.exit(1)

            cookies = await context.cookies()
            self.session_cookies = {c['name']: c['value'] for c in cookies}
            await browser.close()

        if not self.session_cookies:
            print("❌ Erreur session.")
            sys.exit(1)

    async def fetch_one(self, session, driver_entry):
        """Récupère et met à jour les infos VEHICULE d'UN conducteur."""
        profile_url = driver_entry.get('view_profile', "")
        if not profile_url or profile_url == "N/A":
            return
        
        try:
            async with session.get(profile_url, timeout=15) as response:
                if response.status == 200:
                    html_content = await response.text()
                    soup = BeautifulSoup(html_content, 'html.parser')
                    
                    # --- Logique d'extraction (Analyse récursive du JSON source) ---
                    vehicle_info = {
                        "type": "N/A",
                        "marque": "N/A",
                        "modele": "N/A",
                        "matricule": "N/A"
                    }
                    
                    app_div = soup.find('div', id='app')
                    if app_div and app_div.get('data-page'):
                        try:
                            # On convertit tout le JSON en chaîne pour chercher les clés n'importe où
                            raw_json = app_div.get('data-page')
                            data_page = json.loads(raw_json)
                            
                            # Fonction pour chercher une clé récursivement dans un dictionnaire/liste
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

                            # Extraction directe par recherche récursive (plus robuste aux changements de structure)
                            v_type = find_key(data_page, 'vehicle_type_name')
                            v_marque = find_key(data_page, 'car_make_name')
                            v_modele = find_key(data_page, 'car_model_name')
                            v_matricule = find_key(data_page, 'car_number')

                            if v_type: vehicle_info["type"] = str(v_type).upper()
                            if v_marque: vehicle_info["marque"] = str(v_marque).upper()
                            if v_modele: vehicle_info["modele"] = str(v_modele).upper()
                            if v_matricule: vehicle_info["matricule"] = str(v_matricule).upper()
                            
                            # DEBUG: Si toujours N/A, on regarde si les clés existent dans le texte brut
                            if vehicle_info["matricule"] == "N/A":
                                if "car_number" in raw_json:
                                    print(f"  🔍 Clé 'car_number' trouvée dans le texte brut mais pas extraite pour {driver_entry.get('nom')}")
                            
                            print(f"  ✅ Extrait pour {driver_entry.get('nom')}: {vehicle_info['type']} | {vehicle_info['matricule']}")
                        except Exception as e:
                            print(f"  ⚠️ Erreur lors du parsing du JSON source : {e}")
                    
                    # Sauvegarder l'HTML si TOUT est N/A pour voir ce qui cloche
                    if all(v == "N/A" for v in vehicle_info.values()):
                        with open("last_failed_profile.html", "w", encoding="utf-8") as f:
                            f.write(html_content)
                        print(f"  ❌ ÉCHEC TOTAL pour {driver_entry.get('nom')}. Source sauvegardée dans last_failed_profile.html")

                    driver_entry['vehicle'] = vehicle_info
                    self.processed_count += 1
                else:
                    print(f"  ⚠️ Error {response.status} for {profile_url}")
        except Exception as e:
            print(f"  ⚠️ Request error for {profile_url}: {e}")

    async def run(self):
        # 1. Chargement
        if not INPUT_JSON.exists():
            print(f"❌ Fichier source introuvable : {INPUT_JSON}")
            return

        with open(INPUT_JSON, 'r', encoding='utf-8') as f:
            self.drivers_data = json.load(f)

        await self.get_session_cookies()

        # 2. Traitement par lots
        print(f"🚀 Scraping des véhicules pour {len(self.drivers_data)} conducteurs...")
        
        # Filtrer ceux déjà faits
        to_process = [d for d in self.drivers_data if 'vehicle' not in d]
        print(f"📊 {len(to_process)} véhicules restant à traiter.")

        async with aiohttp.ClientSession(cookies=self.session_cookies) as session:
            for i in range(0, len(to_process), BATCH_SIZE):
                batch = to_process[i:i+BATCH_SIZE]
                tasks = [self.fetch_one(session, d) for d in batch]
                await asyncio.gather(*tasks)
                
                self.save_data()
                print(f"⏳ Progression : {self.processed_count} véhicules enregistrés.")

        print("✅ Terminé !")
        self.generate_report()

    def save_data(self):
        with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
            json.dump(self.drivers_data, f, ensure_ascii=False, indent=2)

    def generate_report(self):
        """Génère un fichier HTML pour visualiser les résultats."""
        html_path = Path("output/conducteurs_vehicles.html")
        rows = []
        for d in self.drivers_data:
            v = d.get('vehicle', {})
            rows.append(f"""
                <tr>
                    <td>{d.get('nom', 'N/A')}</td>
                    <td>{d.get('telephone', 'N/A')}</td>
                    <td>{v.get('type', 'N/A')}</td>
                    <td>{v.get('marque', 'N/A')}</td>
                    <td>{v.get('modele', 'N/A')}</td>
                    <td>{v.get('matricule', 'N/A')}</td>
                    <td><a href="{d.get('view_profile', '#')}" target="_blank">Profil</a></td>
                </tr>
            """)
        
        html_content = f"""
        <html>
        <head>
            <title>Enrichissement Véhicules</title>
            <style>
                table {{ width: 100%; border-collapse: collapse; font-family: sans-serif; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #f2f2f2; }}
                tr:nth-child(even) {{ background-color: #f9f9f9; }}
            </style>
        </head>
        <body>
            <h1>Rapport Véhicules ({len(self.drivers_data)} conducteurs)</h1>
            <table>
                <thead>
                    <tr>
                        <th>Nom</th>
                        <th>Téléphone</th>
                        <th>Type</th>
                        <th>Marque</th>
                        <th>Modèle</th>
                        <th>Matricule</th>
                        <th>Lien</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(rows)}
                </tbody>
            </table>
        </body>
        </html>
        """
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"📊 Rapport HTML généré : {html_path}")

if __name__ == "__main__":
    enricher = VehicleEnricher()
    asyncio.run(enricher.run())
