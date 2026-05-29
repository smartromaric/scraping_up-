"""
Script de scraping des Partenaires et leurs Conducteurs - UpJunoo Admin Panel
==========================================================================
Workflow :
1. Connexion manuelle et configuration (Filtre 500).
2. Phase 1 : Extraction de la liste globale des partenaires.
3. Phase 2 : Navigation dans chaque profil pour extraire les conducteurs.
"""

import csv
import json
import os
import re
import time
import traceback
from getpass import getpass
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
)
from webdriver_manager.chrome import ChromeDriverManager

# ─── Configuration ────────────────────────────────────────────────────────────────────────────────────
BASE_URL = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL = f"{BASE_URL}/login/admin"
OWNERS_URL = f"{BASE_URL}/manage-owners"
OUTPUT_DIR = Path(__file__).parent / "output"
JSON_OUT = OUTPUT_DIR / "partenaires.json"
CSV_OUT = OUTPUT_DIR / "partenaires.csv"

# Filtre noms de partenaires : Partenaire[s]?-?N avec N >= 1 (sans limite haute)
PARTNER_NAME_RE = re.compile(r'^\s*partenaires?-?\s*(\d+)\s*$', re.I)
PARTNER_MIN = 1


def should_keep_partner(nom: str) -> bool:
    m = PARTNER_NAME_RE.match(nom or "")
    if not m:
        return False
    n = int(m.group(1))
    return n >= PARTNER_MIN

# ═══════════════════════════════════════════════════════════════════════════════
#  EXPORTS
# ═══════════════════════════════════════════════════════════════════════════════

def export_all(data):
    """Exporte les données dans plusieurs formats."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 1. JSON
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✅ JSON exporté: {JSON_OUT}")

    # 2. CSV
    with open(CSV_OUT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Nom Entreprise", "Email", "Portable", "URL Document", "URL Profil", "Nombre Conducteurs"])
        for row in data:
            writer.writerow([
                row.get("nom", ""), 
                row.get("email", ""), 
                row.get("telephone", ""), 
                row.get("document_url", ""),
                row.get("profile_url", ""),
                len(row.get("drivers", []))
            ])
    print(f"  ✅ CSV exporté: {CSV_OUT}")

# ═══════════════════════════════════════════════════════════════════════════════
#  SCRAPING PHASE 1 : LISTE GLOBALE
# ═══════════════════════════════════════════════════════════════════════════════

def get_credentials():
    email = os.getenv("UPJUNOO_EMAIL") or input("📧 Email admin UpJunoo : ").strip()
    password = os.getenv("UPJUNOO_PASSWORD") or getpass("🔑 Mot de passe : ")
    return email, password


def auto_login(driver, email, password):
    print(f"\n🔐 Connexion automatique à {LOGIN_URL}...")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 30)
    email_input = wait.until(EC.presence_of_element_located((
        By.CSS_SELECTOR, "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
    )))
    pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password']")
    email_input.clear(); email_input.send_keys(email)
    pwd_input.clear(); pwd_input.send_keys(password)
    try:
        btn = driver.find_element(
            By.XPATH,
            "//button[@type='submit'] | //button[contains(translate(.,'LOGIN','login'),'login')]"
            " | //button[contains(translate(.,'CONNEXION','connexion'),'connexion')]",
        )
        btn.click()
    except NoSuchElementException:
        pwd_input.submit()
    wait.until(lambda d: "/login" not in d.current_url)
    print(f"  ✅ Connecté. URL : {driver.current_url}")


def set_page_size_500(driver):
    try:
        from selenium.webdriver.support.ui import Select
        sel_el = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select.form-select.form-select-sm.w-auto"))
        )
        Select(sel_el).select_by_visible_text("500")
        print("  ✅ Pagination réglée sur 500.")
        time.sleep(1.5)
        WebDriverWait(driver, 30).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "table tbody tr")) > 50
        )
        return True
    except Exception as e:
        print(f"  ⚠️  Impossible de régler 500 auto : {e}")
        return False


def open_owners_and_setup(driver):
    print(f"\n📍 Ouverture de {OWNERS_URL}...")
    driver.get(OWNERS_URL)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
    except TimeoutException:
        print("  ⚠️  Tableau non détecté.")
    ok = set_page_size_500(driver)
    if not ok:
        print("\n👉 Règle manuellement la pagination sur 500, puis [ENTRÉE]...")
        input()

def scrape_global_list(driver):
    """Extrait les partenaires visibles dans le tableau actuel."""
    partners = []
    try:
        print("\n🔍 Extraction de la liste des partenaires...")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        
        for i, row in enumerate(rows):
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6: continue
                
                nom = cells[0].text.strip()
                email = cells[1].text.strip()
                telephone = cells[2].text.strip()
                
                # URL Document (Colonne 4 - index 3)
                document_url = "N/A"
                profile_url = "N/A"
                owner_id = "N/A"
                
                try:
                    # Lien document
                    link_doc = cells[3].find_element(By.TAG_NAME, "a")
                    document_url = link_doc.get_attribute("href")
                    
                    # ID Partenaire pour l'URL de profil (depuis le bouton View Profile)
                    # On cherche le bouton avec l'icône de profil (Image 1)
                    btn_profile = row.find_element(By.CSS_SELECTOR, "button[data-original-title='View Profile'], .btn-info, .btn-primary")
                    # Souvent l'ID est dans un attribut ou on peut le déduire de document_url si c'est le même ID
                    if document_url and "/document/" in document_url:
                        owner_id = document_url.split("/document/")[-1]
                        profile_url = f"{BASE_URL}/manage-owners/view-profile/{owner_id}"
                except:
                    pass
                
                if not should_keep_partner(nom):
                    continue

                partners.append({
                    "nom": nom,
                    "email": email,
                    "telephone": telephone,
                    "document_url": document_url,
                    "profile_url": profile_url,
                    "owner_id": owner_id,
                    "drivers": []
                })
            except Exception as e:
                print(f"  ⚠️ Erreur ligne {i}: {e}")
                
        print(f"✅ {len(partners)} partenaires filtrés (Partenaire[s]?-?N, N>={PARTNER_MIN}).")
    except Exception as e:
        print(f"❌ Erreur lors de l'extraction de la liste : {e}")
        
    return partners

# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE 2 : DÉTAILS CONDUCTEURS
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_drivers_for_partner(driver, partner):
    """Navigue vers le profil du partenaire et extrait ses conducteurs."""
    drivers = []
    if not partner["profile_url"] or partner["profile_url"] == "N/A":
        return []
        
    try:
        driver.get(partner["profile_url"])
        # Attendre le chargement de la page profil (Image 2)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//a[contains(text(), 'Détails du conducteur')] | //a[contains(text(), 'Driver Details')]")))
        
        # Cliquer sur l'onglet "Détails du conducteur"
        tab_drivers = driver.find_element(By.XPATH, "//a[contains(text(), 'Détails du conducteur')] | //a[contains(text(), 'Driver Details')]")
        driver.execute_script("arguments[0].click();", tab_drivers)
        
        # Attendre le tableau des conducteurs
        time.sleep(1.5) # Temps de chargement AJAX
        
        try:
            # Chercher le tableau dans la zone active
            driver_rows = driver.find_elements(By.CSS_SELECTOR, ".tab-pane.active table tbody tr")
            if not driver_rows:
                # Essayer un sélecteur plus large si le tableau est unique
                driver_rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

            for dr in driver_rows:
                d_cells = dr.find_elements(By.TAG_NAME, "td")
                # On attend : Nom, Emplacement, Mobile, Transport, Véhicule
                if len(d_cells) >= 4:
                    drivers.append({
                        "nom": d_cells[0].text.strip(),
                        "emplacement": d_cells[1].text.strip() if len(d_cells) > 1 else "N/A",
                        "telephone": d_cells[2].text.strip() if len(d_cells) > 2 else "N/A",
                        "type_transport": d_cells[3].text.strip() if len(d_cells) > 3 else "N/A",
                        "type_vehicule": d_cells[4].text.strip() if len(d_cells) > 4 else "N/A"
                    })
        except Exception as e:
            print(f"      ⚠️ Aucun conducteur trouvé ou erreur tableau : {e}")
            
    except Exception as e:
        print(f"      ❌ Erreur accès profil {partner['nom']} : {e}")
        
    return drivers

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    opts = Options()
    opts.add_argument("--window-size=1600,1000")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    
    try:
        email, password = get_credentials()
        auto_login(driver, email, password)

        # --- PHASE 1 : liste des partenaires (filtrée) ---
        open_owners_and_setup(driver)
        partners = scrape_global_list(driver)
        
        if not partners:
            print("❌ Aucun partenaire trouvé. Fin du script.")
            return

        export_all(partners)
        
        # --- PHASE 2 ---
        print("\n" + "🔄" + "="*60)
        print(f" LISTE GÉNÉRÉE ({len(partners)} partenaires)")
        print("="*60)
        print("Vérifiez le fichier output/partenaires.json.")
        input("\n👉 APPUYEZ SUR [ENTRÉE] POUR COMMENCER L'ENRICHISSEMENT DES CONDUCTEURS (Phase 2)...")
        
        print("\n🚀 Début de l'extraction des conducteurs (Crawl profond)...")
        for i, partner in enumerate(partners):
            print(f"  [{i+1}/{len(partners)}] Scraping : {partner['nom']}...")
            partner["drivers"] = scrape_drivers_for_partner(driver, partner)
            print(f"    ✅ {len(partner['drivers'])} conducteurs récupérés.")
            
            # Sauvegarde régulière
            if (i + 1) % 5 == 0 or (i + 1) == len(partners):
                export_all(partners)
                
        print("\n" + "✨" + "="*60)
        print(" TOUT EST TERMINÉ !")
        print(f" Données finales disponibles dans : {JSON_OUT}")
        print("="*60)
            
    except Exception as e:
        print(f"\n❌ ERREUR CRITIQUE: {e}")
        traceback.print_exc()
    finally:
        print("\n👋 Fermeture du navigateur dans 10 secondes...")
        time.sleep(10)
        driver.quit()

if __name__ == "__main__":
    run()
