import asyncio
import json
import os
import sys
import re
from pathlib import Path
import aiohttp
from playwright.async_api import async_playwright

# --- Configuration ---
BASE_URL = "https://upjunoo-server-new.junooapps.com"
INPUT_JSON = Path("output/conducteurs.json")
OUTPUT_JSON = Path("output/conducteurs_enriched.json")
OUTPUT_HTML = Path("output/conducteurs_enriched.html")
CONCURRENCY = 10  # On réduit un peu pour éviter les déconnexions serveur
BATCH_SIZE = 50   # On sauvegarde toutes les 50 réponses RÉELLES

class DriverEnricher:
    def __init__(self):
        self.session_cookies = None
        self.drivers_data = []
        self.processed_count = 0

    async def get_session_cookies(self):
        """Ouvre un navigateur pour l'authentification."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(f"{BASE_URL}/login/admin")
            print("\n🔑 CONNECTEZ-VOUS ET APPUYEZ SUR [ENTRÉE]...")
            input()
            cookies = await context.cookies()
            self.session_cookies = {c['name']: c['value'] for c in cookies}
            await browser.close()
        if not self.session_cookies:
            print("❌ Erreur session.")
            sys.exit(1)

    def map_status(self, doc):
        status_id = doc.get('document_status')
        uploaded = doc.get('uploaded', False)
        if status_id == 1: return "Approuvé"
        if status_id is None and not uploaded: return "Non téléchargé"
        if status_id == 0: return "En attente"
        if status_id == 2: return "Décliné"
        return f"Statut {status_id}"

    async def fetch_one(self, session, driver_entry):
        """Récupère et met à jour UN conducteur."""
        doc_url = driver_entry.get('document_url', "")
        match = re.search(r'/document/(\d+)', doc_url)
        if not match: return
        
        driver_id = match.group(1)
        api_url = f"{BASE_URL}/fleet-drivers/document/list/{driver_id}"
        
        try:
            async with session.get(api_url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    raw_docs = data.get('results', [])
                    driver_entry['documents'] = [
                        {"type": d.get('name'), "numero": d.get('identify_number') or "N/A", "statut": self.map_status(d)}
                        for d in raw_docs
                    ]
                    self.processed_count += 1
                else:
                    print(f"  ⚠️ Error {response.status} for {driver_id}")
        except Exception as e:
            print(f"  ⚠️ Request error for {driver_id}: {e}")

    async def run(self):
        # 1. Chargement
        target_file = OUTPUT_JSON if OUTPUT_JSON.exists() else INPUT_JSON
        with open(target_file, 'r', encoding='utf-8') as f:
            self.drivers_data = json.load(f)

        await self.get_session_cookies()

        # 2. Traitement par lots (Batches)
        print(f"🚀 Scraping de {len(self.drivers_data)} conducteurs...")
        
        # Filtrer ceux déjà faits pour ne pas perdre de temps
        to_process = [d for d in self.drivers_data if 'documents' not in d]
        print(f"📊 {len(to_process)} conducteurs restant à traiter.")

        async with aiohttp.ClientSession(cookies=self.session_cookies) as session:
            # On traite par petits groupes pour forcer la sauvegarde
            for i in range(0, len(to_process), BATCH_SIZE):
                batch = to_process[i:i+BATCH_SIZE]
                tasks = [self.fetch_one(session, d) for d in batch]
                await asyncio.gather(*tasks)
                
                # Sauvegarde réelle du fichier entier
                self.save_data()
                print(f"⏳ Progression : {self.processed_count} conducteurs enregistrés.")

        self.generate_report()
        print("✅ Terminé !")

    def save_data(self):
        with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
            json.dump(self.drivers_data, f, ensure_ascii=False, indent=2)

    def generate_report(self):
        # (La méthode reste la même qu'avant)
        pass 

if __name__ == "__main__":
    enricher = DriverEnricher()
    asyncio.run(enricher.run())
