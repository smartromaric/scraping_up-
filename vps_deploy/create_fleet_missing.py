#!/usr/bin/env python3
"""
Création automatique de flotte pour véhicules manquants
====================================================
Travaille dans organized_by_partner et exclut UNASSIGNED_DRIVERS
"""

import json
import time
import traceback
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# Configuration
BASE_URL = "https://upjunoo-server-new.junooapps.com"
OWNER_LOGIN_URL = f"{BASE_URL}/login/owner-login"
MANAGE_FLEET_URL = f"{BASE_URL}/manage-fleet"
CREATE_FLEET_URL = f"{BASE_URL}/manage-fleet/create"

def get_chrome_driver():
    """Initialise Chrome en mode headless"""
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def login_owner(driver, email, password):
    """Connexion en tant que propriétaire"""
    print(f"🔐 Connexion owner: {email}")
    driver.get(OWNER_LOGIN_URL)
    
    try:
        # Attendre le formulaire
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )
        
        # Remplir le formulaire
        driver.find_element(By.NAME, "email").send_keys(email)
        driver.find_element(By.NAME, "password").send_keys(password)
        
        # Soumettre
        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        
        # Attendre la redirection
        WebDriverWait(driver, 10).until(
            lambda d: "/login" not in d.current_url
        )
        
        print("✅ Connexion réussie")
        return True
        
    except Exception as e:
        print(f"❌ Erreur connexion: {e}")
        return False

def create_vehicle(driver, vehicle_data):
    """Crée un véhicule dans le formulaire"""
    try:
        driver.get(CREATE_FLEET_URL)
        time.sleep(2)
        
        # Type de véhicule
        type_select = Select(WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "select_type"))
        ))
        vehicle_type = vehicle_data.get('transport_type', 'Taxi').upper()
        if vehicle_type in ['TAXI', 'LIVRAISON']:
            type_select.select_by_visible_text("CONFORT")
        elif vehicle_type == 'MOTO':
            type_select.select_by_visible_text("MOTO")
        else:
            type_select.select_by_visible_text("CONFORT")
        
        # Marque (générer une par défaut)
        driver.find_element(By.ID, "car_brand").send_keys("TOYOTA")
        
        # Modèle (générer un par défaut)
        driver.find_element(By.ID, "car_model").send_keys("YARIS")
        
        # Plaque d'immatriculation (générer unique)
        import random
        plaque = f"{random.randint(100, 999)}-{random.randint(1000, 9999)}"
        driver.find_element(By.ID, "license_plate_number").send_keys(plaque)
        
        # Couleur
        driver.find_element(By.ID, "car_color").send_keys("BLANC")
        
        # Soumettre
        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        
        # Attendre la confirmation
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "alert-success"))
        )
        
        print(f"✅ Véhicule créé: {vehicle_data['driver_name']} -> {plaque}")
        return True
        
    except Exception as e:
        print(f"❌ Erreur création véhicule {vehicle_data['driver_name']}: {e}")
        return False

def process_partner_vehicles(driver, missing_vehicles):
    """Traite les véhicules manquants pour un partenaire"""
    success_count = 0
    error_count = 0
    
    for vehicle in missing_vehicles:
        if create_vehicle(driver, vehicle):
            success_count += 1
        else:
            error_count += 1
        
        # Pause entre créations
        time.sleep(3)
    
    return success_count, error_count

def main():
    """Fonction principale"""
    # Charger les données
    input_file = Path("output/organized_by_partner/missing_vehicles.json")
    
    if not input_file.exists():
        print("❌ Fichier missing_vehicles.json introuvable")
        print("   Lancez d'abord analyze_missing_fleet.py")
        return
    
    with open(input_file, 'r', encoding='utf-8') as f:
        missing_vehicles = json.load(f)
    
    print(f"📊 {len(missing_vehicles)} véhicules à créer")
    
    # Grouper par partenaire
    partners = {}
    for vehicle in missing_vehicles:
        partner = vehicle['partner']
        if partner not in partners:
            partners[partner] = []
        partners[partner].append(vehicle)
    
    print(f"🚗 {len(partners)} partenaires à traiter")
    
    # Demander les identifiants
    import getpass
    email = input("📧 Email owner: ").strip()
    password = getpass.getpass("🔑 Mot de passe: ")
    
    driver = None
    total_success = 0
    total_error = 0
    
    try:
        driver = get_chrome_driver()
        
        if not login_owner(driver, email, password):
            return
        
        for partner_name, vehicles in partners.items():
            print(f"\n🏢 Traitement de {partner_name} ({len(vehicles)} véhicules)")
            
            success, error = process_partner_vehicles(driver, vehicles)
            total_success += success
            total_error += error
            
            print(f"   ✅ {success} créés, ❌ {error} erreurs")
    
    except KeyboardInterrupt:
        print("\n⏹️ Interruption utilisateur")
    
    except Exception as e:
        print(f"❌ Erreur critique: {e}")
        traceback.print_exc()
    
    finally:
        if driver:
            driver.quit()
        
        print(f"\n📈 RÉSULTAT FINAL:")
        print(f"   • Véhicules créés: {total_success}")
        print(f"   • Erreurs: {total_error}")
        print(f"   • Total traités: {total_success + total_error}")

if __name__ == "__main__":
    main()
