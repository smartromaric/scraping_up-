"""
Script d'automatisation de création de flotte - UpJunoo Admin Panel
===================================================================
Workflow :
1. L'utilisateur fournit le chemin du fichier JSON.
2. Connexion manuelle à l'Admin Panel.
3. Navigation vers la page de création de flotte.
4. Remplissage automatique des champs pour chaque véhicule du JSON.

Éléments du formulaire (issus du diagnostic inspect_form.py) :
  - <select id="select_type" class="form-select"> : CONFORT, MOTO, CARGO, etc.
  - <input id="car_brand">   : marque
  - <input id="car_model">   : modèle
  - <input id="license_plate_number"> : plaque d'immatriculation
  - <input id="car_color">   : couleur
  - <button type="submit" class="btn btn-primary"> : Sauvegarder
"""

import json
import time
import traceback
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
)
from webdriver_manager.chrome import ChromeDriverManager

# ─── Configuration ──────────────────────────────────────────────────────────────
BASE_URL = "https://upjunoo-server-new.junooapps.com"
CREATE_FLEET_URL = f"{BASE_URL}/manage-fleet/create"
DEFAULT_PARTNER_FOLDER = "partenaire49"
PARTNERS_BASE_DIR = Path(__file__).parent / "output" / "partenaires"

# ═══════════════════════════════════════════════════════════════════════════════
#  FONCTIONS UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

def setup_driver():
    """Initialise le driver Selenium avec des options optimisées."""
    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-notifications")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def load_json_data(file_path):
    """Charge les données du fichier JSON spécifié."""
    path = Path(file_path)
    if not path.exists():
        print(f"❌ Erreur : Le fichier {file_path} n'existe pas.")
        return None
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Erreur lors de la lecture du JSON : {e}")
        return None

def wait_for_login(driver):
    """Attend que l'utilisateur se connecte et soit prêt sur la page de création."""
    print("\n" + "🚀" + "="*60)
    print(" ÉTAPE 1 : CONNEXION ET PRÉPARATION")
    print("="*60)
    print(f"1. Connectez-vous sur : `{BASE_URL}/login/owner-login` ")
    print(f"2. Allez sur la page de création de flotte : `{CREATE_FLEET_URL}` ")
    print("="*60)
    
    driver.get(f"{BASE_URL}/login/owner-login")
    
    input("\n👉 UNE FOIS QUE VOUS ÊTES SUR LA PAGE 'CRÉER' DE LA FLOTTE, APPUYEZ SUR [ENTRÉE] ICI...")

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTOMATISATION DU FORMULAIRE
# ═══════════════════════════════════════════════════════════════════════════════

def fill_fleet_form(driver, vehicle_data):
    """Remplit le formulaire de création de flotte pour un véhicule donné."""
    wait = WebDriverWait(driver, 10)
    vehicle = vehicle_data.get("vehicle", {})
    vehicle_type = vehicle.get("type", "CONFORT")  # CONFORT, MOTO, CARGO, etc.
    nom = vehicle_data.get("nom", "Véhicule inconnu")
    
    print(f"📝 Remplissage pour : {nom}")
    
    try:
        # ── 1. Sélectionner le type (select natif id="select_type") ──
        # Mapping global des types de véhicules → UUIDs (issus du diagnostic)
        TYPE_UUID_MAP = {
            "CONFORT": "0d1802c4-3d32-4a96-b3ca-73e650802c62",
            "Camionnette": "15f90aaa-aa92-40ed-b34e-ce7e51541b7e",
            "MOTO": "35a673c3-aafe-48b4-8ae8-205e238b043b",
            "Taxi France": "4644788a-1065-4eb9-bbf6-01a6e394aeed",
            "ECO": "58eb223b-5ac7-4ed5-9a12-87d24f901dda",
            "moto livraison": "5f4ef87b-1be7-468d-8140-7379fefbaedf",
            "Camion 14T": "64d5d311-1f7c-42eb-b0ad-510d9af8cd54",
            "PREMIUM": "91ccc713-b07f-4971-b5c4-1d1c755c9d3a",
            "Camion": "95ad84fc-df36-48f7-8c69-e8bb51ad5f8d",
            "CONFORT+": "990a6e02-ac3d-4354-bccc-eedafb77de71",
            "CARGO": "c9a337de-fc81-4626-a5f9-2ac7ac1b5e03",
            "CONFORT Lyon": "dce302c1-c109-4023-9d9d-17b9da8c424c",
            "Semi-remorque": "e17983aa-af38-4ffc-b88c-37adc3f77dcd",
        }
        try:
            select_el = wait.until(EC.presence_of_element_located((By.ID, "select_type")))
            select_obj = Select(select_el)
            
            # Vérifier si le dropdown a des options (hors placeholder vide)
            real_options = [o for o in select_obj.options if o.get_attribute("value")]
            target_type = str(vehicle_type).strip().upper()
            
            if real_options:
                # ✅ Recherche d'une correspondance insensible à la casse dans les options visibles
                matched_text = None
                for opt in select_obj.options:
                    if opt.text.strip().upper() == target_type:
                        matched_text = opt.text
                        break
                
                if matched_text:
                    select_obj.select_by_visible_text(matched_text)
                    print(f"  ✅ Type sélectionné : {matched_text} (via '{vehicle_type}')")
                else:
                    # Fallback : Si non trouvé dans les options mais présent dans notre mapping UUID
                    normalized_map = {k.upper(): v for k, v in TYPE_UUID_MAP.items()}
                    uuid = normalized_map.get(target_type)
                    if uuid:
                        driver.execute_script(
                            """
                            var select = arguments[0];
                            var opt = document.createElement('option');
                            opt.value = arguments[1];
                            opt.text = arguments[2];
                            select.appendChild(opt);
                            select.value = arguments[1];
                            select.dispatchEvent(new Event('change', { bubbles: true }));
                            """,
                            select_el, uuid, vehicle_type
                        )
                        print(f"  ✅ Type injecté (car non listé) : {vehicle_type} → {uuid}")
                    else:
                        print(f"  ❌ Type '{vehicle_type}' non trouvé dans les options et aucun UUID connu.")
            else:
                # ⚠️ Cas dropdown vide : on injecte via mapping insensible à la casse
                normalized_map = {k.upper(): v for k, v in TYPE_UUID_MAP.items()}
                uuid = normalized_map.get(target_type)
                if uuid:
                    driver.execute_script(
                        """
                        var select = arguments[0];
                        var opt = document.createElement('option');
                        opt.value = arguments[1];
                        opt.text = arguments[2];
                        select.appendChild(opt);
                        select.value = arguments[1];
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                        """,
                        select_el, uuid, vehicle_type
                    )
                    print(f"  ✅ Type injecté (dropdown vide) : {vehicle_type} → {uuid}")
                else:
                    print(f"  ❌ Type '{vehicle_type}' inconnu — UUID non trouvé dans le mapping")
        except Exception as e:
            print(f"  ⚠️ Erreur sélection type '{vehicle_type}': {e}")

        # ── 2. Marque (input id="car_brand") ──
        brand_input = wait.until(EC.presence_of_element_located((By.ID, "car_brand")))
        brand_input.clear()
        brand_input.send_keys(vehicle.get("marque", "N/A"))
        print(f"  ✅ Marque : {vehicle.get('marque', 'N/A')}")

        # ── 3. Modèle (input id="car_model") ──
        model_input = wait.until(EC.presence_of_element_located((By.ID, "car_model")))
        model_input.clear()
        model_input.send_keys(vehicle.get("modele", "N/A"))
        print(f"  ✅ Modèle : {vehicle.get('modele', 'N/A')}")

        # ── 4. Plaque (input id="license_plate_number") ──
        plate_input = wait.until(EC.presence_of_element_located((By.ID, "license_plate_number")))
        plate_input.clear()
        plate_input.send_keys(vehicle.get("matricule", "N/A"))
        print(f"  ✅ Plaque : {vehicle.get('matricule', 'N/A')}")

        # ── 5. Couleur (input id="car_color") — toujours "Noir" ──
        color_input = wait.until(EC.presence_of_element_located((By.ID, "car_color")))
        color_input.clear()
        color_input.send_keys("Noir")
        print(f"  ✅ Couleur : Noir")

        time.sleep(0.5)

        # ── 6. Sauvegarder (button type="submit" class="btn btn-primary") ──
        save_btn = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.btn.btn-primary[type='submit']")
        ))
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", save_btn)
        time.sleep(0.3)
        save_btn.click()
        
        # Attendre le résultat (redirection ou message de succès)
        time.sleep(3)
        
        if "create" in driver.current_url:
            print("  ⚠️ Le formulaire n'a pas redirigé — possible erreur de soumission.")
        else:
            print("  ✅ Formulaire soumis avec succès !")

    except Exception as e:
        print(f"  ❌ Erreur lors du remplissage : {e}")
        traceback.print_exc()

def main():
    print("\n" + "🚗 UPJUNOO FLEET CREATOR 🚗")
    
    # Demander le nom du dossier partenaire
    print("\n--- CONFIGURATION DU PARTENAIRE ---")
    partner_folder = input(f"Entrez le nom du dossier dans partenaires (Défaut: {DEFAULT_PARTNER_FOLDER}) : ").strip()
    if not partner_folder:
        partner_folder = DEFAULT_PARTNER_FOLDER
    
    # Construction du chemin vers data.json
    json_path = PARTNERS_BASE_DIR / partner_folder / "data.json"
    
    print(f"📂 Recherche du fichier : {json_path}")
    data = load_json_data(json_path)
    if not data:
        return

    # Vérification de la structure du JSON
    drivers = data.get("drivers", [])
    if not drivers:
        # Essayer de voir si le JSON est une liste directe
        if isinstance(data, list):
            drivers = data
        else:
            print("❌ Aucune donnée de conducteur ('drivers') trouvée dans le JSON.")
            return

    print(f"📊 {len(drivers)} véhicules à enregistrer.")

    driver = setup_driver()
    try:
        # Étape 1 : Connexion
        wait_for_login(driver)
        
        # S'assurer qu'on est sur la bonne URL avant de commencer
        if not driver.current_url.startswith(CREATE_FLEET_URL):
            print(f"🔄 Navigation vers {CREATE_FLEET_URL}...")
            driver.get(CREATE_FLEET_URL)
            time.sleep(2)

        for i, vehicle_data in enumerate(drivers):
            print(f"\n🚀 Traitement {i+1}/{len(drivers)}...")
            
            # On vérifie que les données du véhicule existent et ne sont pas "N/A"
            vehicle = vehicle_data.get("vehicle", {})
            
            # Liste des champs à vérifier
            fields_to_check = {
                "Modèle": vehicle.get("modele"),
                "Marque": vehicle.get("marque"),
                "Matricule": vehicle.get("matricule"),
                "Type véhicule": vehicle.get("type")
            }
            
            # Vérification si un champ obligatoire est "N/A" ou vide
            invalid_fields = [k for k, v in fields_to_check.items() if not v or v == "N/A"]
            
            if invalid_fields:
                print(f"  ⏩ Saut : Données incomplètes ({', '.join(invalid_fields)}) pour {vehicle_data.get('nom', '?')}")
                continue

            fill_fleet_form(driver, vehicle_data)
            
            # Rafraîchir systématiquement la page pour le prochain véhicule
            print(f"  🔄 Rafraîchissement de la page...")
            driver.get(CREATE_FLEET_URL)
            time.sleep(2)
            
        print("\n✨ Terminé ! Tous les véhicules valides ont été traités.")
        
    except KeyboardInterrupt:
        print("\n🛑 Interrompu par l'utilisateur.")
    except Exception as e:
        print(f"\n💥 Erreur critique : {e}")
    finally:
        input("\nAppuyez sur [ENTRÉE] pour fermer le navigateur...")
        driver.quit()

if __name__ == "__main__":
    main()
