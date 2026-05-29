# -*- coding: utf-8 -*-
"""
Script d'automatisation de mise à jour de flotte - Dashboard Partenaires
===================================================================
Boucle sur tous les partenaires présents dans output/organized_by_partner/ :
  - Login auto comme le partenaire (partenaire<N>@upjunoo.com / 123456789@)
  - /manage-fleet pagination 500 auto
  - Détecte les matricules déjà présents
  - Crée uniquement les véhicules manquants via /manage-fleet/create
"""

import argparse
import json
import re
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
    InvalidSessionIdException,
    WebDriverException,
)
from webdriver_manager.chrome import ChromeDriverManager

# Configuration
BASE_URL = "https://upjunoo-server-new.junooapps.com"
OWNER_LOGIN_URL = BASE_URL + "/login/owner-login"
MANAGE_FLEET_URL = BASE_URL + "/manage-fleet"
CREATE_FLEET_URL = BASE_URL + "/manage-fleet/create"
PARTNERS_BASE_DIR = Path(__file__).parent / "output" / "organized_by_partner"
UNIVERSAL_PASSWORD = "123456789@"

# Filtre noms de partenaires : Partenaire[s]?-?N (meme regle que scrape_partners)
PARTNER_NAME_RE = re.compile(r'^\s*partenaires?-?\s*(\d+)\s*$', re.I)


def derive_owner_email(folder_name):
    """Partenaires-79 -> partenaires79@upjunoo.com, partenaire-101 -> partenaire101@upjunoo.com"""
    if not PARTNER_NAME_RE.match(folder_name):
        return None
    prefix = folder_name.replace("-", "").lower()
    return prefix + "@upjunoo.com"

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

def owner_login(driver, email: str, password: str) -> bool:
    """Login automatique sur /login/owner-login. Retourne True si connecté."""
    print(f"\n🔐 Login owner : {email}")
    driver.get(OWNER_LOGIN_URL)
    wait = WebDriverWait(driver, 30)
    try:
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
        return True
    except Exception as e:
        print(f"  ❌ Login échoué pour {email} : {e}")
        return False


def owner_logout(driver):
    """Déconnexion : on vide les cookies et on revient sur la page de login."""
    try:
        driver.delete_all_cookies()
    except Exception:
        pass


def _count_fleet_rows(driver) -> int:
    """Compte les lignes de données du tableau flotte (tbody tr)."""
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        # Filtrer les lignes vides / "no data"
        real = 0
        for r in rows:
            txt = r.text.strip()
            if txt and "no data" not in txt.lower() and "aucun" not in txt.lower():
                real += 1
        return real
    except Exception:
        return 0


def wait_for_fleet_table_ready(driver, timeout: int = 30) -> int:
    """Attend que le tableau de la flotte soit stable (plus de chargement).
    Renvoie le nombre final de lignes.
    Stratégie : on lit le compteur toutes les 0.5s ; quand il ne bouge plus
    pendant ~2s, on considère que c'est chargé.
    """
    deadline = time.time() + timeout
    last = -1
    stable_since = None
    while time.time() < deadline:
        current = _count_fleet_rows(driver)
        if current == last and current > 0:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= 2.0:
                return current
        else:
            stable_since = None
            last = current
        time.sleep(0.5)
    return last if last > 0 else 0


def _try_set_pagination_auto(driver, target: str = "500") -> bool:
    """Règle la pagination automatiquement.
    Attend que le <select> soit peuplé par Vue/JS, puis force le changement
    via JS natif (nativeInputValueSetter) + events input/change pour que
    Vue.js détecte la modification. Vérifie ensuite que le tableau a bien
    rechargé avec plus de lignes.
    """
    selector = "select.form-select.form-select-sm.w-auto"

    # ── Étape 1 : attendre que le <select> ait des <option> ──
    def _select_populated(d):
        try:
            el = d.find_element(By.CSS_SELECTOR, selector)
            opts = el.find_elements(By.TAG_NAME, "option")
            return el if len(opts) >= 2 else False
        except Exception:
            return False

    try:
        sel_el = WebDriverWait(driver, 20).until(_select_populated)
    except TimeoutException:
        print("  ⚠️  Pagination <select> introuvable ou vide après 20 s.")
        return False

    rows_before = _count_fleet_rows(driver)
    print(f"  ℹ️  Avant pagination : {rows_before} lignes visibles.")

    # ── Étape 2 : forcer la valeur via JS de manière à ce que Vue détecte ──
    driver.execute_script("""
        var select = arguments[0];
        var target = arguments[1];

        // Chercher l'option correspondante
        var found = false;
        for (var i = 0; i < select.options.length; i++) {
            if (select.options[i].value === target || select.options[i].text === target) {
                select.selectedIndex = i;
                found = true;
                break;
            }
        }
        if (!found) {
            // Fallback : set la dernière option (le max)
            select.selectedIndex = select.options.length - 1;
        }

        // Forcer Vue.js à détecter le changement
        // Méthode 1 : nativeInputValueSetter (React/Vue)
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
            window.HTMLSelectElement.prototype, 'value'
        ).set;
        nativeInputValueSetter.call(select, select.options[select.selectedIndex].value);

        // Méthode 2 : fire tous les events
        select.dispatchEvent(new Event('input', { bubbles: true }));
        select.dispatchEvent(new Event('change', { bubbles: true }));

        // Méthode 3 : trigger via Vue instance si dispo
        if (select.__vue__) {
            try { select.__vue__.$emit('input', select.value); } catch(e) {}
            try { select.__vue__.$emit('change', select.value); } catch(e) {}
        }
    """, sel_el, target)

    print(f"  ✅ Pagination réglée sur {target} (JS forcé).")

    # ── Étape 3 : attendre que le tableau recharge réellement ──
    time.sleep(1)  # laisser le temps au framework de réagir

    # Vérifier que le nombre de lignes a augmenté (si on avait 10 et
    # qu'on passe à 500, on doit voir plus de lignes — sauf si le
    # partenaire a ≤ 10 véhicules).
    rows_after = wait_for_fleet_table_ready(driver, timeout=20)
    print(f"  📊 Après pagination : {rows_after} lignes visibles.")

    if rows_after > rows_before:
        print(f"  ✅ Rechargement confirmé ({rows_before} → {rows_after}).")
        return True
    elif rows_after == rows_before and rows_before <= int(target):
        print(f"  ✅ Total ≤ {target}, pas de changement attendu.")
        return True
    else:
        print(f"  ⚠️  Nombre de lignes inchangé ({rows_after}). Le changement n'a peut-être pas pris.")
        return False


def open_fleet_and_set_pagination(driver) -> bool:
    """Ouvre /manage-fleet et tente l'auto-pagination ; fallback manuel sinon."""
    print(f"📍 Ouverture {MANAGE_FLEET_URL}...")
    driver.get(MANAGE_FLEET_URL)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
        )
    except TimeoutException:
        print("  ⚠️  Tableau non détecté (peut-être flotte vide).")

    if _try_set_pagination_auto(driver, "500"):
        return True

    # Auto KO → fallback : dump HTML + intervention manuelle
    try:
        dump = Path(__file__).parent / "output" / "fleet_pagination_debug.html"
        dump.write_text(driver.page_source, encoding="utf-8")
        print(f"  📝 HTML dumpé pour debug : {dump}")
    except Exception:
        pass
    print("\n  👉 Auto KO — règle la pagination sur 500 manuellement, puis [ENTRÉE]...")
    input()
    n = wait_for_fleet_table_ready(driver, timeout=30)
    print(f"  📊 Tableau stable (manuel) : {n} lignes.")
    return True

def _get_first_row_signature_fleet(driver):
    """Renvoie une signature de la 1ère ligne du tableau flotte pour détecter rechargement."""
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            return None
        return rows[0].text[:200]
    except Exception:
        return None


def _find_next_button_fleet(driver):
    """Cherche le bouton 'Suivant' dans la pagination de la flotte."""
    try:
        # Sélecteurs Bootstrap pour le bouton Next
        a = driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item:not(.disabled) a.page-link[aria-label='Next'], "
            "ul.pagination li:not(.disabled) a[aria-label='Next'], "
            ".pagination li:not(.disabled) a:contains('Next'), "
            ".pagination li:not(.disabled) a:contains('Suivant')"
        )
        if a.is_displayed():
            return a
    except NoSuchElementException:
        # Essayer avec XPath si CSS selector échoue
        try:
            a = driver.find_element(
                By.XPATH,
                "//ul[@class='pagination']//li[not(contains(@class, 'disabled'))]//a[@aria-label='Next'] | "
                "//ul[@class='pagination']//li[not(contains(@class, 'disabled'))]//a[contains(text(), 'Next')] | "
                "//ul[@class='pagination']//li[not(contains(@class, 'disabled'))]//a[contains(text(), 'Suivant')]"
            )
            return a
        except NoSuchElementException:
            pass
    return None


def _is_next_disabled_fleet(driver):
    """True si le bouton 'Next' est désactivé (fin de pagination)."""
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item.disabled a.page-link[aria-label='Next'], "
            "ul.pagination li.disabled a[aria-label='Next']"
        )
        return True
    except NoSuchElementException:
        # Vérifier aussi avec XPath
        try:
            driver.find_element(
                By.XPATH,
                "//ul[@class='pagination']//li[contains(@class, 'disabled')]//a[@aria-label='Next']"
            )
            return True
        except NoSuchElementException:
            pass
    return False


def _go_to_next_page_fleet(driver, timeout=30):
    """Clique sur 'suivant' et attend le rechargement du tableau. Retourne True si nouvelle page."""
    if _is_next_disabled_fleet(driver):
        return False

    btn = _find_next_button_fleet(driver)
    if btn is None:
        print("      ℹ️ Bouton 'suivant' introuvable → fin de pagination.")
        return False

    prev_sig = _get_first_row_signature_fleet(driver)

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
    except Exception as e:
        print(f"      ⚠️ Clic 'suivant' impossible : {e}")
        return False

    # Attendre le rechargement : 1ère ligne différente
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(0.5)
        new_sig = _get_first_row_signature_fleet(driver)
        if new_sig and new_sig != prev_sig:
            time.sleep(1.0)  # Attente supplémentaire pour stabilisation
            return True
        if _is_next_disabled_fleet(driver):
            return False
    print("      ⏱️ Timeout en attendant la nouvelle page.")
    return False


def _extract_plates_from_current_page(driver):
    """Extrait les plaques du tableau visible sur la page actuelle."""
    plates = set()
    try:
        # Attendre que le tableau soit présent
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 4:  # La flotte a généralement: Type, Marque, Modèle, Plaque, Statut
                    # La plaque est souvent dans la 4ème colonne (index 3)
                    plaque_text = cells[3].text.strip().upper() if len(cells) > 3 else ""
                    # Ou chercher dans toutes les cellules un pattern de plaque
                    for cell in cells:
                        text = cell.text.strip().upper()
                        # Pattern de plaque: AB-123-CD ou AB123CD ou similaire
                        if text and len(text) >= 5 and text not in ["N/A", "", "-"]:
                            # Vérifier si ça ressemble à une plaque (contient des chiffres et des lettres)
                            if any(c.isdigit() for c in text) and any(c.isalpha() for c in text):
                                plates.add(text)
                                break
            except Exception:
                continue
    except Exception as e:
        print(f"      ⚠️ Erreur extraction plaques page : {e}")
    
    return plates


def extract_existing_plates(driver, drivers_json_data):
    """
    Parcoure TOUTES les pages de la flotte et récupère TOUS les matricules existants.
    Empêche la création de doublons même si la pagination est à 10 par page.
    """
    print("\n🔍 Extraction et vérification des véhicules déjà présents sur le site...")
    print("   (Scan de toutes les pages de la flotte)")
    
    all_plates_found = set()
    page_num = 1
    
    # 1. D'abord, essayer de régler la pagination à 500 pour tout voir en une page
    pagination_ok = _try_set_pagination_auto(driver, "500")
    if pagination_ok:
        print("   ✅ Pagination réglée sur 500 - extraction en une seule page")
        time.sleep(3)  # Attendre le rechargement
    else:
        print("   ⚠️ Pagination à 500 impossible - scan page par page")
    
    # 2. Parcourir toutes les pages pour extraire tous les matricules
    while True:
        print(f"   📄 Page {page_num}: extraction des plaques...")
        
        # Extraire les plaques de la page actuelle
        page_plates = _extract_plates_from_current_page(driver)
        all_plates_found.update(page_plates)
        print(f"      ✅ {len(page_plates)} plaques trouvées sur cette page")
        
        # Passer à la page suivante si possible
        if pagination_ok:
            # Si on a réussi à mettre à 500, on a tout sur une page
            print("   ✅ Toutes les données sur une page (pagination 500)")
            break
        
        # Sinon, essayer d'aller à la page suivante
        has_next = _go_to_next_page_fleet(driver)
        if not has_next:
            print(f"   🏁 Fin de la pagination (page {page_num} traitée)")
            break
        
        page_num += 1
        if page_num > 100:  # Sécurité: max 100 pages
            print("   ⚠️ Limite de 100 pages atteinte, arrêt")
            break
    
    # 3. Afficher les plaques trouvées pour debug
    if all_plates_found:
        print(f"   📊 Total plaques trouvées sur le site: {len(all_plates_found)}")
        # Afficher quelques exemples (max 10)
        sample = list(all_plates_found)[:10]
        print(f"   📝 Exemples: {', '.join(sample)}{'...' if len(all_plates_found) > 10 else ''}")
    
    # 4. Croiser avec les données JSON pour ne garder que ceux qui existent vraiment
    plates_found_on_website = set()
    for vd in drivers_json_data:
        vehicle = vd.get("vehicle")
        if vehicle and isinstance(vehicle, dict):
            matricule = str(vehicle.get("matricule", "")).strip().upper()
            if matricule and matricule != "N/A":
                # Vérifier si cette plaque existe dans notre liste complète
                # Comparaison exacte ET partielle (sans tirets/espaces)
                matricule_clean = matricule.replace("-", "").replace(" ", "")
                for plate in all_plates_found:
                    plate_clean = plate.replace("-", "").replace(" ", "")
                    if matricule == plate or matricule_clean == plate_clean:
                        plates_found_on_website.add(matricule)
                        break
    
    print(f"\n✅ {len(plates_found_on_website)}/{len(all_plates_found)} véhicules du JSON sont DÉJÀ EXISTANTS sur la plateforme.")
    print(f"   🚗 Nombre total de véhicules sur le site: {len(all_plates_found)}")
    
    return plates_found_on_website

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTOMATISATION DU FORMULAIRE (Identique à Create Fleet)
# ═══════════════════════════════════════════════════════════════════════════════

def fill_fleet_form(driver, vehicle_data):
    """Remplit le formulaire de création de flotte pour un véhicule donné."""
    wait = WebDriverWait(driver, 10)
    vehicle = vehicle_data.get("vehicle", {})
    vehicle_type = vehicle.get("type", "CONFORT")  # CONFORT, MOTO, CARGO, etc.
    nom = vehicle_data.get("nom", "Véhicule inconnu")
    
    print(f"📝 Enregistrement du NOUVEAU véhicule pour : {nom}")
    
    try:
        # ── 1. Sélectionner le type (select natif id="select_type") ──
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
        
        # Attendre le résultat
        time.sleep(3)
        
        if "create" in driver.current_url:
            print("  ⚠️ Le formulaire n'a pas redirigé — possible erreur de soumission.")
        else:
            print("  ✅ Formulaire soumis avec succès ! Véhicule ajouté.")

    except Exception as e:
        print(f"  ❌ Erreur lors du remplissage : {e}")
        traceback.print_exc()

def process_partner(driver, folder: Path) -> dict:
    """Traite un partenaire : login, vérifie flotte, ajoute les véhicules manquants.
    Retourne un dict avec les stats."""
    name = folder.name
    stats = {"name": name, "added": 0, "skipped": 0, "invalid": 0, "login_ok": False, "error": None}

    email = derive_owner_email(name)
    if not email:
        stats["error"] = "nom hors filtre Partenaire[s]?-?N"
        return stats

    data = load_json_data(folder / "data.json")
    if not data:
        stats["error"] = "data.json introuvable/illisible"
        return stats

    drivers_list = data.get("drivers", []) if isinstance(data, dict) else data
    if not drivers_list:
        stats["error"] = "aucun driver"
        return stats

    print(f"\n{'='*60}")
    print(f"🏱 PARTENAIRE : {name}  ({len(drivers_list)} conducteurs)")
    print(f"{'='*60}")

    if not owner_login(driver, email, UNIVERSAL_PASSWORD):
        stats["error"] = "login échoué"
        return stats
    stats["login_ok"] = True

    open_fleet_and_set_pagination(driver)
    existing_plates = extract_existing_plates(driver, drivers_list)

    # --- NOUVEAU: Calculer et afficher ce qui va être créé avant de commencer ---
    to_create = []
    for vehicle_data in drivers_list:
        vehicle = vehicle_data.get("vehicle")
        if not vehicle or not isinstance(vehicle, dict):
            continue
        matricule = str(vehicle.get("matricule", "")).strip().upper()
        if (not matricule or matricule == "N/A"
                or not vehicle.get("modele") or vehicle.get("modele") == "N/A"
                or not vehicle.get("marque") or vehicle.get("marque") == "N/A"):
            continue
        if matricule not in existing_plates:
            to_create.append({
                "nom": vehicle_data.get("nom", "Inconnu"),
                "matricule": matricule,
                "marque": vehicle.get("marque", "N/A"),
                "modele": vehicle.get("modele", "N/A"),
                "type": vehicle.get("type", "N/A")
            })

    # Afficher le résumé
    print(f"\n{'='*60}")
    print("📋 RÉSUMÉ AVANT CRÉATION")
    print(f"{'='*60}")
    print(f"   • Total conducteurs dans JSON: {len(drivers_list)}")
    print(f"   • Véhicules déjà existants: {len(existing_plates)}")
    print(f"   • Véhicules à créer: {len(to_create)}")
    print(f"   • Véhicules invalides: {len(drivers_list) - len(to_create) - len([p for p in existing_plates if any(d.get('vehicle', {}).get('matricule', '').upper() == p for d in drivers_list)])}")
    
    if to_create:
        print(f"\n   🚗 Véhicules qui vont être créés ({len(to_create)}):")
        for i, v in enumerate(to_create[:10], 1):  # Afficher max 10
            print(f"      {i}. {v['nom']} → {v['matricule']} ({v['marque']} {v['modele']})")
        if len(to_create) > 10:
            print(f"      ... et {len(to_create) - 10} autres")
        
        # Si beaucoup de véhicules à créer, demander confirmation
        if len(to_create) > 5:
            print(f"\n{'='*60}")
            response = input(f"⚠️  Voulez-vous créer ces {len(to_create)} véhicules ? [o/N]: ").strip().lower()
            if response not in ['o', 'oui', 'y', 'yes']:
                print("   ❌ Annulation - aucun véhicule ne sera créé.")
                stats["skipped"] = len(to_create)
                owner_logout(driver)
                return stats
            print(f"{'='*60}")
    else:
        print("\n   ✅ Tous les véhicules existent déjà - rien à créer.")
        owner_logout(driver)
        return stats

    print(f"\n🚀 Lancement de la création de {len(to_create)} véhicules...\n")

    for i, vehicle_data in enumerate(drivers_list):
        nom = vehicle_data.get("nom", "Inconnu")
        vehicle = vehicle_data.get("vehicle")
        if not vehicle or not isinstance(vehicle, dict):
            stats["invalid"] += 1
            continue

        matricule = str(vehicle.get("matricule", "")).strip().upper()
        if (not matricule or matricule == "N/A"
                or not vehicle.get("modele") or vehicle.get("modele") == "N/A"
                or not vehicle.get("marque") or vehicle.get("marque") == "N/A"):
            stats["invalid"] += 1
            continue

        if matricule in existing_plates:
            stats["skipped"] += 1
            continue

        print(f"  ➕ [{i+1}/{len(drivers_list)}] {nom} → {matricule}")
        if not driver.current_url.startswith(CREATE_FLEET_URL):
            driver.get(CREATE_FLEET_URL)
            time.sleep(2)
        fill_fleet_form(driver, vehicle_data)
        stats["added"] += 1
        existing_plates.add(matricule)
        driver.get(CREATE_FLEET_URL)
        time.sleep(2)

    print(f"\n  📊 {name} → ajoutés: {stats['added']}, ignorés(déjà présents): {stats['skipped']}, invalides: {stats['invalid']}")

    owner_logout(driver)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Update fleet pour tous les partenaires.")
    parser.add_argument("--start", help="Nom du partenaire à partir duquel reprendre (ex: partenaire24).")
    parser.add_argument("--only", help="Traiter UNIQUEMENT ce partenaire (ex: partenaire24).")
    args = parser.parse_args()

    print("\n🔄 UPJUNOO UPDATE FLEET — boucle auto sur tous les partenaires 🔄")
    if not PARTNERS_BASE_DIR.exists():
        print(f"❌ Dossier introuvable : {PARTNERS_BASE_DIR}")
        return

    all_folders = sorted(
        [p for p in PARTNERS_BASE_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.name.lower(),
    )
    targets = [p for p in all_folders if derive_owner_email(p.name)]
    skipped_folders = [p.name for p in all_folders if not derive_owner_email(p.name)]

    # ── Filtre CLI --only / --start ──
    if args.only:
        targets = [p for p in targets if p.name.lower() == args.only.lower()]
        if not targets:
            print(f"❌ Partenaire '{args.only}' introuvable.")
            return
        print(f"🎯 Mode --only : uniquement {targets[0].name}")
    elif args.start:
        names_lower = [p.name.lower() for p in targets]
        start_lower = args.start.lower()
        if start_lower not in names_lower:
            print(f"❌ Partenaire de départ '{args.start}' introuvable.")
            return
        idx_start = names_lower.index(start_lower)
        skipped_before = [p.name for p in targets[:idx_start]]
        targets = targets[idx_start:]
        print(f"▶️  Reprise depuis {targets[0].name} (saut de {len(skipped_before)} partenaires précédents).")

    print(f"📂 {len(targets)} partenaires à traiter.")
    if skipped_folders:
        print(f"⏩ Ignorés (hors filtre) : {', '.join(skipped_folders)}")

    driver = setup_driver()
    all_stats = []

    def _session_is_alive(d):
        try:
            _ = d.current_url
            return True
        except Exception:
            return False

    try:
        for idx, folder in enumerate(targets, 1):
            print(f"\n▶️  [{idx}/{len(targets)}] {folder.name}")

            # Recrée le driver si la session est morte (Chrome fermé / crashé)
            if not _session_is_alive(driver):
                print("  ♻️  Session Chrome perdue, re-création du driver...")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = setup_driver()

            try:
                stats = process_partner(driver, folder)
            except (InvalidSessionIdException, WebDriverException) as e:
                print(f"💥 Session morte sur {folder.name} : {e}")
                print("  ♻️  Re-création du driver et on retente ce partenaire...")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = setup_driver()
                try:
                    stats = process_partner(driver, folder)
                except Exception as e2:
                    print(f"💥 Nouvel échec sur {folder.name} : {e2}")
                    stats = {"name": folder.name, "added": 0, "skipped": 0, "invalid": 0,
                             "login_ok": False, "error": str(e2)}
            except Exception as e:
                print(f"💥 Erreur inattendue sur {folder.name} : {e}")
                traceback.print_exc()
                stats = {"name": folder.name, "added": 0, "skipped": 0, "invalid": 0,
                         "login_ok": False, "error": str(e)}
            all_stats.append(stats)

            # Sauvegarde progressive du rapport
            report_path = Path(__file__).parent / "output" / "update_fleet_report.json"
            report_path.write_text(json.dumps(all_stats, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n" + "="*60)
        print("✨ TERMINÉ — RÉSUMÉ GLOBAL")
        print("="*60)
        total_added = sum(s["added"] for s in all_stats)
        total_skipped = sum(s["skipped"] for s in all_stats)
        total_invalid = sum(s["invalid"] for s in all_stats)
        failed = [s for s in all_stats if s.get("error") or not s.get("login_ok")]
        print(f"  Partenaires traités : {len(all_stats)}")
        print(f"  ➕ Véhicules ajoutés : {total_added}")
        print(f"  ⏩ Déjà présents : {total_skipped}")
        print(f"  ❌ Données invalides : {total_invalid}")
        if failed:
            print(f"\n⚠️  Partenaires en erreur ({len(failed)}) :")
            for s in failed:
                print(f"    - {s['name']} → {s.get('error') or 'login KO'}")
        # Sauvegarde récapitulative
        report_path = Path(__file__).parent / "output" / "update_fleet_report.json"
        report_path.write_text(json.dumps(all_stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n� Rapport : {report_path}")

    except KeyboardInterrupt:
        print("\n🛑 Interrompu par l'utilisateur.")
    finally:
        time.sleep(3)
        driver.quit()

if __name__ == "__main__":
    main()
