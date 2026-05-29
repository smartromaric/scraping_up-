# -*- coding: utf-8 -*-
"""
Réassigne des conducteurs d'un partenaire à un autre.
Usage: python3 reassign_drivers.py --from "Partenaires-57" --to "Nouveau-Partenaire"

Note: Nécessite d'être connecté en ADMIN sur /fleet-drivers
"""

import argparse
import json
import time
import sys
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "https://upjunoo-server-new.junooapps.com"
DRIVERS_URL = f"{BASE_URL}/fleet-drivers"


def setup_driver():
    """Initialise le driver."""
    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-notifications")
    
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )
    return driver


def load_drivers_data():
    """Charge les données des conducteurs par partenaire."""
    data_file = Path(__file__).parent / "output" / "drivers_by_partner.json"
    if not data_file.exists():
        print(f"❌ Fichier introuvable: {data_file}")
        print("   Lance d'abord: python3 list_drivers_by_partner.py")
        return None
    return json.loads(data_file.read_text(encoding="utf-8"))


def get_partner_options(driver):
    """Récupère la liste des partenaires disponibles dans le dropdown."""
    try:
        # Attendre que le select des partenaires soit présent
        partner_select = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "select_partner"))
        )
        select_obj = Select(partner_select)
        
        options = []
        for opt in select_obj.options:
            val = opt.get_attribute("value")
            text = opt.text.strip()
            if val and val != "":
                options.append({"value": val, "text": text})
        
        return options
    except Exception as e:
        print(f"⚠️ Impossible de récupérer la liste des partenaires: {e}")
        return []


def reassign_driver(driver, driver_id, target_partner_name, target_partner_value=None):
    """
    Réassigne un conducteur à un nouveau partenaire.
    Retourne True si succès.
    """
    edit_url = f"{BASE_URL}/fleet-drivers/edit/{driver_id}"
    
    try:
        print(f"   🌐 Ouverture {edit_url}...")
        driver.get(edit_url)
        time.sleep(2)
        
        # Attendre le chargement du formulaire
        wait = WebDriverWait(driver, 15)
        
        # Chercher le select des partenaires
        try:
            partner_select = wait.until(
                EC.presence_of_element_located((By.ID, "select_partner"))
            )
        except TimeoutException:
            print(f"   ❌ Select partenaire introuvable")
            return False
        
        select_obj = Select(partner_select)
        
        # Récupérer le partenaire actuel
        current_value = partner_select.get_attribute("value")
        current_text = select_obj.first_selected_option.text if select_obj.first_selected_option else "N/A"
        
        print(f"   ℹ️ Partenaire actuel: {current_text}")
        
        # Si on a déjà la valeur cible, l'utiliser directement
        if target_partner_value:
            try:
                select_obj.select_by_value(target_partner_value)
                print(f"   ✅ Partenaire changé (value={target_partner_value})")
            except Exception as e:
                print(f"   ⚠️ Erreur sélection par value: {e}")
                return False
        else:
            # Chercher le partenaire par nom
            found = False
            for opt in select_obj.options:
                if target_partner_name.lower() in opt.text.lower():
                    select_obj.select_by_visible_text(opt.text)
                    print(f"   ✅ Partenaire changé vers: {opt.text}")
                    found = True
                    break
            
            if not found:
                print(f"   ❌ Partenaire '{target_partner_name}' non trouvé dans la liste")
                # Afficher les options disponibles
                print(f"   📋 Options disponibles:")
                for opt in select_obj.options[:10]:
                    print(f"      - {opt.text}")
                return False
        
        # Sauvegarder
        time.sleep(1)
        
        # Chercher le bouton de sauvegarde
        try:
            save_btn = driver.find_element(By.CSS_SELECTOR, "button.btn-primary[type='submit'], button[type='submit']")
            save_btn.click()
            print(f"   💾 Sauvegarde...")
            time.sleep(2)
            
            # Vérifier si on reste sur la page ou si redirection = succès
            if "edit" in driver.current_url:
                print(f"   ⚠️ Toujours sur la page d'édition - vérifier manuellement")
            else:
                print(f"   ✅ Sauvegarde réussie !")
            
            return True
            
        except NoSuchElementException:
            print(f"   ❌ Bouton sauvegarde introuvable")
            return False
            
    except Exception as e:
        print(f"   ❌ Erreur: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Réassigne des conducteurs à un autre partenaire")
    parser.add_argument("--from", dest="source", required=True, help="Nom du partenaire source (ex: 'Partenaires-57')")
    parser.add_argument("--to", dest="target", required=True, help="Nom du partenaire cible (ex: 'UPJUNOO CI')")
    parser.add_argument("--dry-run", action="store_true", help="Simulation sans modification")
    args = parser.parse_args()
    
    # Charger les données
    data = load_drivers_data()
    if not data:
        sys.exit(1)
    
    # Trouver le partenaire source
    source_partner = None
    for name, info in data.items():
        if args.source.lower() in name.lower() or args.source.lower() in info["nom"].lower():
            source_partner = info
            source_partner["key"] = name
            break
    
    if not source_partner:
        print(f"❌ Partenaire source '{args.source}' non trouvé")
        print("   Partenaires disponibles:")
        for name in data.keys():
            print(f"      - {name}")
        sys.exit(1)
    
    drivers_to_move = source_partner["drivers"]
    
    print(f"\n{'='*60}")
    print(f"🔄 RÉASSIGNATION DE CONDUCTEURS")
    print(f"{'='*60}")
    print(f"Source: {source_partner['key']} ({source_partner['nom']})")
    print(f"Cible:  {args.target}")
    print(f"Nombre de conducteurs: {len(drivers_to_move)}")
    
    if args.dry_run:
        print("\n⚠️ MODE SIMULATION - Aucune modification ne sera faite")
        print("\n📋 Conducteurs à réassigner:")
        for d in drivers_to_move:
            print(f"   - {d['nom']} (ID: {d['driver_id']})")
            print(f"     ↳ {d['edit_url']}")
        return
    
    # Confirmation
    print(f"\n⚠️  Voulez-vous réassigner {len(drivers_to_move)} conducteurs de '{source_partner['key']}' vers '{args.target}'?")
    confirm = input("   [o/N]: ").strip().lower()
    if confirm not in ['o', 'oui', 'y', 'yes']:
        print("❌ Annulation")
        return
    
    # Lancer le navigateur
    print("\n🌐 Démarrage du navigateur...")
    driver = setup_driver()
    
    try:
        # Connexion manuelle (comme update_partner.py)
        driver.get(f"{BASE_URL}/login/admin")
        
        print("\n" + "="*60)
        print("🔐 CONNEXION REQUISE")
        print("="*60)
        print("1. Connecte-toi manuellement sur la page qui s'est ouverte")
        print(f"   URL: {BASE_URL}/login/admin")
        print("2. Une fois connecté, appuie sur [ENTRÉE] ici...")
        print("="*60)
        input("\n👉 [ENTRÉE] pour continuer...")
        
        # Aller sur la page des conducteurs pour voir les options disponibles
        print("\n📍 Chargement de la liste des partenaires disponibles...")
        driver.get(DRIVERS_URL)
        time.sleep(3)
        
        # Récupérer la liste des partenaires
        partner_options = get_partner_options(driver)
        target_value = None
        
        for opt in partner_options:
            if args.target.lower() in opt["text"].lower():
                target_value = opt["value"]
                print(f"✅ Partenaire cible trouvé: {opt['text']} (value={target_value[:20]}...)")
                break
        
        if not target_value:
            print(f"⚠️ Partenaire cible '{args.target}' non trouvé dans la liste")
            print("   Partenaires disponibles:")
            for opt in partner_options[:20]:
                print(f"      - {opt['text']}")
            return
        
        # Réassigner chaque conducteur
        print(f"\n🚀 Début de la réassignation...\n")
        success_count = 0
        failed_drivers = []
        
        for i, driver_info in enumerate(drivers_to_move, 1):
            print(f"[{i}/{len(drivers_to_move)}] {driver_info['nom']}")
            
            if driver_info["driver_id"] == "N/A":
                print(f"   ⚠️ ID inconnu - ignoré")
                failed_drivers.append(driver_info)
                continue
            
            success = reassign_driver(driver, driver_info["driver_id"], args.target, target_value)
            if success:
                success_count += 1
            else:
                failed_drivers.append(driver_info)
            
            time.sleep(1)  # Pause entre chaque
        
        # Bilan
        print(f"\n{'='*60}")
        print(f"📊 BILAN")
        print(f"{'='*60}")
        print(f"✅ Succès: {success_count}/{len(drivers_to_move)}")
        print(f"❌ Échecs: {len(failed_drivers)}")
        
        if failed_drivers:
            print(f"\n⚠️  Conducteurs en échec:")
            for d in failed_drivers:
                print(f"   - {d['nom']}: {d['edit_url']}")
        
        print(f"{'='*60}")
        
    except Exception as e:
        print(f"\n❌ Erreur critique: {e}")
    finally:
        print("\n👋 Fermeture dans 5 secondes...")
        time.sleep(5)
        driver.quit()


if __name__ == "__main__":
    main()
