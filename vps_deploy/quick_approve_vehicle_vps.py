#!/usr/bin/env python3
"""
quick_approve_vehicle_vps.py
==========================

Script admin rapide pour approbation complète d'un véhicule (document + flotte).

Workflow:
1. Connexion admin
2. Filtre par matricule + statut "En attente"
3. Télécharge la carte grise si pas encore faite
4. Approuve le document (Carte grise)
5. Ouvre le menu (3 points) → clique "Approuver" pour changer statut_flotte

Usage:
  python3 quick_approve_vehicle_vps.py <MATRICULE> [--headed]
  
Exemple:
  python3 quick_approve_vehicle_vps.py AA325AD01
  python3 quick_approve_vehicle_vps.py AA-325-AD-01 --headed  # Navigateur visible
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import urljoin

from webdriver_manager.chrome import ChromeDriverManager
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    InvalidSessionIdException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)

# ───────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ───────────────────────────────────────────────────────────────────────────────

# Charger les variables d'environnement
load_dotenv()

BASE_URL = "https://upjunoo-server-new.junooapps.com"
ADMIN_LOGIN_URL = f"{BASE_URL}/login/admin"
MANAGE_FLEET_URL = f"{BASE_URL}/manage-fleet"

ADMIN_EMAIL = os.getenv("UPJUNOO_EMAIL", "admin@upjunoo.com")
ADMIN_PASSWORD = os.getenv("UPJUNOO_PASSWORD", "123456789")

# Slack Configuration
SLACK_WEBHOOK = os.getenv("WEBHOOK_URL", "")
SLACK_BOT_NAME = os.getenv("SLACK_BOT_NAME", "UpJunoo Bot")
SLACK_ICON = os.getenv("SLACK_ICON_EMOJI", ":car:")

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"
DEBUG_DIR = OUTPUT_DIR / "debug"
IMAGES_OCR_DIR = PROJECT_ROOT / "images_ocr"

LOG_DIR.mkdir(exist_ok=True)
DEBUG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / f"quick_approve_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

# ───────────────────────────────────────────────────────────────────────────────
# LOGGING
# ───────────────────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}][{level}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass

def send_slack_message(text: str, mention_channel: bool = False):
    """Envoie un message à Slack via webhook."""
    if not SLACK_WEBHOOK:
        return
    
    try:
        prefix = "<!channel> " if mention_channel else ""
        payload = {
            "text": prefix + text,
            "username": SLACK_BOT_NAME,
            "icon_emoji": SLACK_ICON
        }
        
        req = urllib.request.Request(
            SLACK_WEBHOOK,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                log(f"   📨 Message Slack envoyé")
    except Exception as e:
        log(f"   ⚠️ Erreur Slack: {e}", "WARNING")

def save_screenshot(driver, name: str):
    """Sauvegarde un screenshot pour debug."""
    try:
        ts = datetime.now().strftime("%H%M%S")
        path = DEBUG_DIR / f"{name}_{ts}.png"
        driver.save_screenshot(str(path))
        log(f"📸 Screenshot: {path}")
        return path
    except Exception as e:
        log(f"⚠️ Screenshot échoué: {e}", "WARNING")
        return None

# ───────────────────────────────────────────────────────────────────────────────
# INDEX IMAGES OCR
# ───────────────────────────────────────────────────────────────────────────────

def build_image_index(images_dir: Path) -> dict:
    """Indexe les images OCR + racine projet: {nom_fichier_lower: Path}"""
    index = {}
    
    # Dossier images_ocr
    if images_dir.exists():
        for f in images_dir.iterdir():
            if f.suffix.lower() in (".jpeg", ".jpg", ".png"):
                index[f.name.lower()] = f
    
    # Racine du projet (pour carte_grise_upjunoo.jpg)
    for f in PROJECT_ROOT.iterdir():
        if f.is_file() and f.suffix.lower() in (".jpeg", ".jpg", ".png"):
            index[f.name.lower()] = f
    
    log(f"📷 {len(index)} images indexées")
    return index

def normalize_plate(plate: str) -> str:
    """Normalise un matricule pour la recherche."""
    if not plate:
        return ""
    raw = str(plate).strip().upper()
    for ch in ("-", " ", ".", "/", "_"):
        raw = raw.replace(ch, "")
    return raw

# ═════════════════════════════════════════════════════════════════════════════
# RECHERCHE VÉHICULE DANS JSON PARTENAIRE
# ═════════════════════════════════════════════════════════════════════════════

PARTENAIRE_DRIVERS_DIR = OUTPUT_DIR / "partenaire_drivers_scrape"
PARTNER_JSON_FILE_RE = re.compile(r"^\s*partenaires?[-_]?\s*(\d+)_drivers\.json\s*$", re.I)

def find_vehicle_in_json(matricule: str) -> dict:
    """
    Cherche un véhicule par matricule dans tous les fichiers Partenaire-N_drivers.json
    Retourne: {doc_link, edit_link, statut_document, statut_flotte, type_transport, partner_file, ...} ou None
    """
    mat_norm = normalize_plate(matricule)
    if not mat_norm:
        return None
    
    if not PARTENAIRE_DRIVERS_DIR.exists():
        log(f"⚠️ Dossier {PARTENAIRE_DRIVERS_DIR} introuvable")
        return None
    
    log(f"🔍 Recherche {matricule} dans les fichiers JSON partenaires...")
    
    for json_file in PARTENAIRE_DRIVERS_DIR.iterdir():
        if not json_file.is_file() or not PARTNER_JSON_FILE_RE.match(json_file.name):
            continue
        
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            conducteurs = data.get("conducteurs", [])
            for driver in conducteurs:
                vehicle = driver.get("vehicle", {})
                vehicle_mat = vehicle.get("matricule", "")
                
                if normalize_plate(vehicle_mat) == mat_norm:
                    # Véhicule trouvé
                    result = {
                        "matricule": vehicle_mat,
                        "doc_link": vehicle.get("doc_link"),
                        "edit_link": vehicle.get("edit_link"),
                        "statut_document": vehicle.get("statut_document", ""),
                        "statut_flotte": vehicle.get("statut_flotte", ""),
                        "type_transport": driver.get("Type de transport", ""),
                        "vehicle_type": vehicle.get("type", ""),
                        "driver_name": driver.get("Nom", ""),
                        "phone": driver.get("Numéro de portable", ""),
                        "partner_file": json_file.name,
                    }
                    log(f"   ✅ Trouvé dans {json_file.name}: {driver.get('Nom', 'N/A')} - Tél: {result['phone']}")
                    return result
                    
        except Exception as e:
            log(f"   ⚠️ Erreur lecture {json_file.name}: {e}", "WARNING")
            continue
    
    log(f"   ❌ Véhicule {matricule} non trouvé dans les JSON")
    return None

def get_all_vehicles_from_partners(partner_ids: list) -> tuple:
    """
    Charge tous les véhicules des partenaires spécifiés.
    Retourne: (vehicles_to_process, skipped_count)
    - vehicles_to_process: liste des véhicules sans statut_attribution
    - skipped_count: nombre de véhicules ignorés (déjà attribués)
    """
    vehicles_to_process = []
    skipped_count = 0
    
    if not PARTENAIRE_DRIVERS_DIR.exists():
        log(f"⚠️ Dossier {PARTENAIRE_DRIVERS_DIR} introuvable")
        return [], 0
    
    log(f"\n{'='*60}")
    log(f"📁 CHARGEMENT PARTENAIRES: {partner_ids}")
    log(f"{'='*60}")
    
    for partner_id in partner_ids:
        json_file = PARTENAIRE_DRIVERS_DIR / f"Partenaire-{partner_id}_drivers.json"
        
        if not json_file.exists():
            log(f"   ⚠️ Fichier introuvable: {json_file.name}")
            continue
        
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            conducteurs = data.get("conducteurs", [])
            partner_vehicles = 0
            partner_skipped = 0
            
            for driver in conducteurs:
                vehicle = driver.get("vehicle", {})
                vehicle_mat = vehicle.get("matricule", "")
                
                if not vehicle_mat:
                    continue
                
                # Vérifier si déjà attribué
                statut_attribution = vehicle.get("statut_attribution", "")
                if statut_attribution and "attribué" in statut_attribution.lower():
                    skipped_count += 1
                    partner_skipped += 1
                    continue  # SKIP ce véhicule
                
                # Vérifier si déjà approuvé (optionnel: skip aussi)
                statut_flotte = vehicle.get("statut_flotte", "")
                if statut_flotte and "approuvé" in statut_flotte.lower() and not statut_attribution:
                    # Approuvé mais pas attribué → on garde pour attribution
                    vehicles_to_process.append({
                        "matricule": vehicle_mat,
                        "partner_id": partner_id,
                        "driver_name": driver.get("Nom", ""),
                        "phone": driver.get("Numéro de portable", ""),
                        "statut_flotte": statut_flotte,
                        "statut_document": vehicle.get("statut_document", ""),
                        "already_approved": True
                    })
                    partner_vehicles += 1
                elif statut_flotte and "en attente" in statut_flotte.lower():
                    # En attente → à traiter
                    vehicles_to_process.append({
                        "matricule": vehicle_mat,
                        "partner_id": partner_id,
                        "driver_name": driver.get("Nom", ""),
                        "phone": driver.get("Numéro de portable", ""),
                        "statut_flotte": statut_flotte,
                        "statut_document": vehicle.get("statut_document", ""),
                        "already_approved": False
                    })
                    partner_vehicles += 1
            
            log(f"   ✅ Partenaire-{partner_id}: {partner_vehicles} à traiter, {partner_skipped} ignorés (déjà attribués)")
            
        except Exception as e:
            log(f"   ❌ Erreur lecture {json_file.name}: {e}", "ERROR")
            continue
    
    log(f"\n📊 TOTAL: {len(vehicles_to_process)} véhicules à traiter, {skipped_count} ignorés")
    return vehicles_to_process, skipped_count

def update_vehicle_status_in_json(matricule: str, new_doc_status: str = None, new_fleet_status: str = None, attribution_name: str = None) -> bool:
    """
    Met à jour le statut d'un véhicule dans le fichier JSON partenaire.
    """
    mat_norm = normalize_plate(matricule)
    if not mat_norm:
        return False
    
    if not PARTENAIRE_DRIVERS_DIR.exists():
        return False
    
    for json_file in PARTENAIRE_DRIVERS_DIR.iterdir():
        if not json_file.is_file() or not PARTNER_JSON_FILE_RE.match(json_file.name):
            continue
        
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            conducteurs = data.get("conducteurs", [])
            updated = False
            
            for driver in conducteurs:
                vehicle = driver.get("vehicle", {})
                vehicle_mat = vehicle.get("matricule", "")
                
                if normalize_plate(vehicle_mat) == mat_norm:
                    # Mettre à jour les statuts
                    if new_doc_status:
                        vehicle["statut_document"] = new_doc_status
                    if new_fleet_status:
                        vehicle["statut_flotte"] = new_fleet_status
                    if attribution_name:
                        vehicle["statut_attribution"] = f"Attribué à {attribution_name}"
                    updated = True
                    break
            
            if updated:
                # Sauvegarder le fichier
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                log(f"   📝 JSON mis à jour: {json_file.name}")
                return True
                    
        except Exception as e:
            log(f"   ⚠️ Erreur mise à jour {json_file.name}: {e}", "WARNING")
            continue
    
    return False

DEFAULT_CARTE_GRISE_FILE = "carte_grise_upjunoo.jpg"

# Statuts document qui indiquent "déjà uploadé" → skip
SKIP_DOC_STATUSES = ["en attente d'approbation", "approuvé", "approved", "approval pending"]

# Sous-types qui n'existent QUE en Livraison (jamais en Taxi) → pas de carte grise par défaut
LIVRAISON_ONLY_SUBTYPES = {"MOTO", "CAMIONNETTE", "CAMION", "DELIVERY"}

def vehicle_is_livraison(transport_type: str, subtype: str) -> bool:
    """
    Détermine si un véhicule est catégorie Livraison (pas de carte grise par défaut).
    
    Classification réelle UpJunoo:
      TAXI (carte grise): Taxi - CONFORT, Taxi - CONFORT+, Taxi - ECO, Taxi - PREMIUM,
                          Taxi -, Taxi, Livraison - CONFORT, Livraison - ECO, Livraison - PREMIUM
      LIVRAISON (skip):   Livraison - MOTO, Livraison - CARGO, Livraison - Camionnette,
                          Livraison - Camion, Livraison -, Taxi - MOTO, Taxi - CARGO
    """
    tt = (transport_type or "").strip().lower()
    # Cas explicites Livraison
    LIVRAISON_EXACT = {
        "livraison - moto", "livraison - cargo", "livraison - camionnette",
        "livraison - camion", "livraison -", "taxi - moto", "taxi - cargo",
    }
    # Cas explicites Taxi
    TAXI_EXACT = {
        "taxi - confort", "taxi - confort+", "taxi - eco", "taxi - premium",
        "taxi -", "taxi", "livraison - confort", "livraison - eco",
        "livraison - premium",
    }
    if tt in LIVRAISON_EXACT:
        return True
    if tt in TAXI_EXACT:
        return False
    # Fallback par sous-type si type de transport inconnu/vide
    return (subtype or "").strip().upper() in LIVRAISON_ONLY_SUBTYPES


def find_image_for_matricule(matricule: str, image_index: dict) -> Path:
    """
    Trouve l'image correspondant à un matricule.
    Si aucune image spécifique trouvée, retourne l'image par défaut carte_grise_upjunoo.jpg
    """
    if not image_index:
        return None
    
    # Essayer de trouver par nom de fichier contenant le matricule
    if matricule:
        mat_norm = normalize_plate(matricule)
        
        for filename, path in image_index.items():
            # Vérifier si le matricule est dans le nom de fichier
            if mat_norm in normalize_plate(filename):
                return path
            # Ou si le nom de fichier sans extension contient le matricule
            stem = Path(filename).stem.upper()
            if mat_norm in stem or stem in mat_norm:
                return path
    
    # Fallback: utiliser l'image par défaut
    default_key = DEFAULT_CARTE_GRISE_FILE.lower()
    if default_key in image_index:
        log(f"   📷 Image spécifique non trouvée, utilisation image par défaut: {DEFAULT_CARTE_GRISE_FILE}")
        return image_index[default_key]
    
    return None

# ───────────────────────────────────────────────────────────────────────────────
# UPLOAD CARTE GRISE (logique complète de upload_carte_grise_vps.py)
# ───────────────────────────────────────────────────────────────────────────────

def upload_carte_grise_full(driver, image_path: Path, immatriculation: str) -> tuple:
    """
    Upload une carte grise sur la page document actuelle.
    On est déjà sur la page document (ouverture via icône).
    
    Retourne: (success: bool, statut_document: str, skipped: bool)
    """
    # 1. Vérifier statut actuel sur la page Documents actuelle
    statut_document = ""
    try:
        doc_status_el = driver.find_element(By.CSS_SELECTOR, "table tbody td .badge, table tbody td span")
        statut_document = (doc_status_el.text or "").strip()
        doc_status_lower = statut_document.lower()
        
        is_already_uploaded = any(kw in doc_status_lower for kw in SKIP_DOC_STATUSES)
        if is_already_uploaded:
            log(f"   ⏭️ Déjà uploadé (statut: {statut_document!r}) — SKIP")
            return True, statut_document, True
    except Exception:
        pass
    
    # 2. Extraire l'UUID de l'URL actuelle pour construire l'URL upload
    current_url = driver.current_url
    if "/manage-fleet/document/" not in current_url:
        log(f"   ❌ Pas sur une page document — URL: {current_url}")
        return False, statut_document, False
    
    doc_uuid = current_url.rstrip("/").split("/")[-1]
    
    # 3. Naviguer vers la page upload
    upload_url = f"{BASE_URL}/manage-fleet/document-upload/1/{doc_uuid}"
    log(f"   🔗 Upload URL: {upload_url}")
    driver.get(upload_url)
    time.sleep(3)
    
    current_url = driver.current_url
    if "document-upload" not in current_url:
        log(f"   ❌ Pas sur document-upload — URL: {current_url}")
        # save_screenshot(driver, f"not_on_upload_{doc_uuid[:8]}")
        return False, statut_document, False
    
    # 3. Remplir le formulaire
    try:
        # Champ numéro d'immatriculation
        id_fields = driver.find_elements(
            By.CSS_SELECTOR,
            "input[placeholder*='numéro' i], input[placeholder*='numero' i], "
            "input[placeholder*='identifier' i], input[name*='number' i], "
            "input[name*='numero' i], input[id*='number' i], input[name*='plate' i]"
        )
        if not id_fields:
            id_fields = driver.find_elements(By.CSS_SELECTOR, "input[type='text']")
        
        id_field = None
        for f in id_fields:
            if not (f.is_displayed() and f.is_enabled()):
                continue
            label_text = ""
            try:
                fid = f.get_attribute("id") or ""
                label_els = driver.find_elements(By.CSS_SELECTOR, f"label[for='{fid}']")
                if label_els:
                    label_text = label_els[0].text.lower()
            except Exception:
                pass
            if "nom" not in label_text:
                id_field = f
                break
        
        if id_field:
            id_field.clear()
            id_field.send_keys(immatriculation)
            log(f"   ✏️ Numéro renseigné: {immatriculation!r}")
        
        # Upload image
        file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
        if not file_inputs:
            log("   ❌ Aucun input[type=file] sur la page")
            return False, statut_document, False
        
        file_input = None
        for fi in file_inputs:
            try:
                driver.execute_script(
                    "arguments[0].style.display='block'; arguments[0].style.visibility='visible';", fi
                )
                file_input = fi
                break
            except Exception:
                continue
        
        if not file_input:
            log("   ❌ Aucun input file accessible")
            return False, statut_document, False
        
        abs_path = str(image_path.absolute())
        log(f"   📎 Envoi image: {abs_path}")
        file_input.send_keys(abs_path)
        time.sleep(1.5)
        
        # Cliquer "Mise à jour"
        submit_btn = None
        submit_kws = ["mise à jour", "mise a jour", "update", "save", "enregistrer", "valider", "soumettre", "submit"]
        for sel in ["button[type='submit']", "button.btn-primary", "button.btn-success", "input[type='submit']"]:
            candidates = driver.find_elements(By.CSS_SELECTOR, sel)
            for c in candidates:
                if c.is_displayed() and c.is_enabled():
                    btn_text = (c.text or c.get_attribute("value") or "").lower()
                    if any(kw in btn_text for kw in submit_kws):
                        submit_btn = c
                        break
                    elif not submit_btn:
                        submit_btn = c
            if submit_btn and any(kw in (submit_btn.text or "").lower() for kw in submit_kws):
                break
        
        if not submit_btn:
            log("   ❌ Bouton 'Mise à jour' introuvable")
            # save_screenshot(driver, f"no_submit_{doc_uuid[:8]}")
            return False, statut_document, False
        
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_btn)
        time.sleep(0.3)
        try:
            submit_btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", submit_btn)
        
        time.sleep(3)
        url_after = driver.current_url
        log(f"   🌐 URL après submit: {url_after}")
        
        if "document-upload" not in url_after:
            log(f"   ✅ Redirection réussie → upload OK")
            return True, "Uploadé", False
        
        success_els = driver.find_elements(
            By.CSS_SELECTOR, ".alert-success, .swal2-success, .toast-success, [class*='success']"
        )
        if success_els:
            log(f"   ✅ Indicateur succès détecté")
            return True, "Uploadé", False
        
        log("   ⚠️ Toujours sur document-upload — succès supposé")
        return True, "Uploadé", False
        
    except Exception as e:
        log(f"   ❌ Exception upload: {e}")
        # save_screenshot(driver, f"upload_exception_{doc_uuid[:8]}")
        return False, statut_document, False


# Fonction legacy pour compatibilité
def upload_carte_grise(driver, image_path: Path, matricule: str) -> bool:
    """Wrapper simplifié pour upload_carte_grise_full."""
    success, _, _ = upload_carte_grise_full(driver, image_path, matricule)
    return success

# ───────────────────────────────────────────────────────────────────────────────
# SELENIUM SETUP
# ───────────────────────────────────────────────────────────────────────────────

def setup_driver(headed: bool = False, debug_port: int = None):
    opts = Options()
    if not headed:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # Flags pour VPS avec RAM limitée
    opts.add_argument("--disable-features=VizDisplayCompositor")
    opts.add_argument("--js-flags=--max-old-space-size=512")
    opts.add_argument("--renderer-process-limit=1")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-translate")
    opts.add_argument("--hide-scrollbars")
    opts.add_argument("--mute-audio")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option('useAutomationExtension', False)
    
    # Port de debugging pour instances parallèles
    if debug_port:
        opts.add_argument(f"--remote-debugging-port={debug_port}")
        opts.add_argument(f"--user-data-dir=/tmp/chrome_profile_{debug_port}")
        log(f"   🔌 Port debugging: {debug_port}")
    
    # Auto-download ChromeDriver correspondant à la version de Chrome
    chromedriver = ChromeDriverManager().install()
    log(f"🖥️  ChromeDriver: {chromedriver}")
    service = Service(chromedriver)
    return webdriver.Chrome(service=service, options=opts)

# ───────────────────────────────────────────────────────────────────────────────
# ADMIN AUTH
# ───────────────────────────────────────────────────────────────────────────────

def admin_login(driver) -> bool:
    log("🔐 Connexion admin...")
    
    for attempt in range(3):
        driver.get(ADMIN_LOGIN_URL)
        time.sleep(3)
        
        try:
            wait = WebDriverWait(driver, 30)
            
            # Email
            email_input = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']"))
            )
            email_input.clear()
            email_input.send_keys(ADMIN_EMAIL)
            log(f"   ✓ Email: {ADMIN_EMAIL}")
            
            # Password
            pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            pwd_input.clear()
            pwd_input.send_keys(ADMIN_PASSWORD)
            log("   ✓ Password saisi")
            
            # Submit
            btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            btn.click()
            
            # Attendre redirection vers dashboard (max 15s)
            for _ in range(15):
                time.sleep(1)
                current = driver.current_url
                if "dashboard" in current or "manage" in current:
                    log(f"   ✓ Connecté: {current}")
                    return True
            
            # Vérifier si toujours sur login
            if "login" in driver.current_url:
                log(f"   ⚠️ Tentative {attempt+1}/3 échouée - toujours sur login", "WARNING")
                time.sleep(2)
                continue
            
            log(f"   ✓ Connecté: {driver.current_url}")
            return True
            
        except Exception as e:
            log(f"   ⚠️ Tentative {attempt+1}/3 échouée: {e}", "WARNING")
            time.sleep(2)
    
    log(f"❌ Échec connexion après 3 tentatives", "ERROR")
    return False

# ───────────────────────────────────────────────────────────────────────────────
# FILTRES
# ───────────────────────────────────────────────────────────────────────────────

def open_filters(driver):
    """Ouvre le panneau des filtres."""
    log("🔍 Ouverture des filtres...")
    try:
        # Vérifier qu'on est sur manage-fleet (pas redirigé vers login)
        if "login" in driver.current_url:
            log("   ⚠️ Session expirée, re-login...", "WARNING")
            if not admin_login(driver):
                return False
            driver.get(MANAGE_FLEET_URL)
            time.sleep(2)
        
        # Attendre que la page soit chargée
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table, button, .btn"))
            )
        except:
            pass
        
        # Fermer tout menu ouvert d'abord (clic sur body)
        try:
            driver.find_element(By.TAG_NAME, "body").click()
            time.sleep(0.2)
        except:
            pass
        
        # ORDRE INVERSÉ: CSS d'abord (celui qui marche réellement), puis texte
        selectors_ordered = [
            (By.CSS_SELECTOR, "button.btn-danger, button[class*='danger']"),
            (By.XPATH, "//button[contains(text(), 'Filtres')]"),
            (By.XPATH, "//button[contains(., 'Filtres')]"),
        ]
        
        for by, sel in selectors_ordered:
            try:
                btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((by, sel))
                )
                driver.execute_script("arguments[0].click();", btn)
                log("   ✓ Bouton Filtres cliqué")
                time.sleep(0.8)
                return True
            except:
                continue
        
        log("❌ Impossible d'ouvrir les filtres", "ERROR")
        return False
        
    except Exception as e:
        log(f"❌ Erreur open_filters: {e}", "ERROR")
        return False

def set_filter_statut_en_attente(driver):
    """Met le filtre Statut = 'En attente'."""
    log("⏳ Filtre: Statut = En attente...")
    try:
        # Chercher le dropdown "Statut"
        # Le dropdown a un label "Statut" puis un select ou un input
        wait = WebDriverWait(driver, 10)
        
        # Chercher par label
        statut_label = driver.find_element(By.XPATH, "//*[contains(text(), 'STATUT') or contains(text(), 'Statut')]")
        log(f"   ✓ Label Statut trouvé")
        
        # Le select est souvent juste après le label ou dans le même conteneur
        # Essayer de trouver un select ou un dropdown
        try:
            # Chercher un select avec options
            statut_select = driver.find_element(By.XPATH, 
                "//label[contains(text(), 'Statut') or contains(text(), 'STATUT')]/following::select[1]")
            
            # Sélectionner "En attente"
            options = statut_select.find_elements(By.TAG_NAME, "option")
            for opt in options:
                if "en attente" in opt.text.lower():
                    opt.click()
                    log(f"   ✓ Option sélectionnée: {opt.text}")
                    break
        except NoSuchElementException:
            # Essayer avec un dropdown custom (div/ul/li)
            try:
                # Cliquer sur le dropdown pour l'ouvrir
                dropdown = driver.find_element(By.XPATH,
                    "//label[contains(text(), 'Statut') or contains(text(), 'STATUT')]/following::div[contains(@class, 'dropdown') or contains(@class, 'select')][1]")
                dropdown.click()
                time.sleep(0.3)
                
                # Chercher l'option "En attente"
                option = driver.find_element(By.XPATH, "//li[contains(text(), 'En attente') or contains(text(), 'en attente')]")
                option.click()
                log("   ✓ Dropdown: En attente sélectionné")
            except Exception as e:
                log(f"⚠️ Dropdown custom échoué: {e}", "WARNING")
                return False
        
        return True
        
    except Exception as e:
        log(f"❌ Erreur filtre statut: {e}", "ERROR")
        return False

def set_filter_matricule(driver, matricule: str):
    """Met le filtre Numéro de plaque d'immatriculation."""
    log(f"🔢 Filtre: Matricule = {matricule}...")
    try:
        # Utiliser le matricule tel quel (avec tirets si présents)
        matricule_typed = matricule.upper().strip()
        
        # Chercher le champ par placeholder ou label
        wait = WebDriverWait(driver, 10)
        
        selectors = [
            (By.XPATH, "//input[contains(@placeholder, 'plaque')]"),
            (By.XPATH, "//label[contains(text(), 'plaque') or contains(text(), 'PLAQUE')]/following::input[1]"),
            (By.CSS_SELECTOR, "input[placeholder*='immatriculation' i]"),
        ]
        
        plaque_input = None
        for by, val in selectors:
            try:
                plaque_input = driver.find_element(by, val)
                log(f"   ✓ Champ plaque trouvé: {by}={val}")
                break
            except:
                continue
        
        if not plaque_input:
            log("❌ Champ plaque introuvable", "ERROR")
            return False
        
        plaque_input.clear()
        plaque_input.send_keys(matricule_typed)
        log(f"   ✓ Matricule saisi: {matricule_typed}")
        return True
        
    except Exception as e:
        log(f"❌ Erreur filtre matricule: {e}", "ERROR")
        return False

def apply_filters(driver):
    """Clique sur le bouton Appliquer (bouton vert)."""
    log("▶️  Application des filtres...")
    try:
        wait = WebDriverWait(driver, 10)
        
        # Essayer plusieurs sélecteurs pour le bouton Appliquer (vert)
        selectors = [
            # Par texte exact
            (By.XPATH, "//button[text()='Appliquer']"),
            # Par texte contenu
            (By.XPATH, "//button[contains(text(), 'Appliquer')]"),
            # Par classe btn-success (bouton vert)
            (By.CSS_SELECTOR, "button.btn-success"),
            # Par style couleur verte approximative
            (By.CSS_SELECTOR, "button[style*='background-color: rgb(13, 148, 136)']"),
            (By.CSS_SELECTOR, "button[style*='background-color: rgb(20, 184, 166)']"),
            # Par classe contenant 'success' ou 'green'
            (By.CSS_SELECTOR, "button[class*='success']"),
            # Dernier bouton dans le panneau de filtres (souvent Appliquer)
            (By.XPATH, "//div[contains(@class, 'filter') or contains(@class, 'drawer') or contains(@class, 'modal')]//button[last()]"),
            (By.XPATH, "//aside//button[last()]"),
            (By.XPATH, "//div[contains(@class, 'offcanvas')]//button[last()]"),
        ]
        
        btn = None
        for by, val in selectors:
            try:
                btn = wait.until(EC.element_to_be_clickable((by, val)))
                log(f"   ✓ Bouton Appliquer trouvé: {by}={val}")
                break
            except:
                continue
        
        if not btn:
            # Dernier recours: chercher tous les boutons et prendre celui qui contient "Appliquer"
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for b in buttons:
                if "appliquer" in b.text.lower():
                    btn = b
                    log(f"   ✓ Bouton Appliquer trouvé par scan: text='{b.text}'")
                    break
        
        if not btn:
            log("❌ Bouton Appliquer introuvable", "ERROR")
            # save_screenshot(driver, "apply_button_not_found")
            return False
        
        # Clic avec scroll si nécessaire
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.2)
            btn.click()
        except ElementClickInterceptedException:
            # Clic JavaScript en fallback
            driver.execute_script("arguments[0].click();", btn)
        
        log("   ✓ Bouton Appliquer cliqué")
        time.sleep(1)  # Attendre le rechargement du tableau
        return True
        
    except Exception as e:
        log(f"❌ Erreur clic Appliquer: {e}", "ERROR")
        # save_screenshot(driver, "apply_error")
        return False

# ───────────────────────────────────────────────────────────────────────────────
# VÉHICULES - ACTIONS
# ───────────────────────────────────────────────────────────────────────────────

def find_vehicle_row(driver, matricule: str):
    """Trouve la ligne du véhicule dans le tableau."""
    log(f"🔎 Recherche véhicule: {matricule}...")
    try:
        # Attendre que le tableau se charge
        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        log(f"   {len(rows)} ligne(s) trouvée(s)")
        
        matricule_clean = re.sub(r'[\s\-]', '', matricule).upper()
        
        for i, row in enumerate(rows):
            row_text = row.text.upper().replace(" ", "").replace("-", "")
            if matricule_clean in row_text:
                log(f"   ✓ Véhicule trouvé à la ligne {i+1}")
                return row
        
        log("❌ Véhicule non trouvé", "ERROR")
        return None
        
    except Exception as e:
        log(f"❌ Erreur recherche véhicule: {e}", "ERROR")
        return None

def get_document_status_from_row(row) -> str:
    """Extrait le statut du document de la ligne tableau."""
    try:
        # Chercher un badge dans la ligne
        badge = row.find_element(By.CSS_SELECTOR, ".badge")
        return badge.text.strip()
    except:
        return "Inconnu"

def click_menu_three_dots(driver, row):
    """Clique sur le menu 3 points (⋮) de la ligne."""
    log("⋮ Ouverture du menu...")
    
    # Méthode primaire: dernier bouton de la ligne (c'est ce qui marche réellement)
    try:
        buttons = row.find_elements(By.CSS_SELECTOR, "button")
        if buttons:
            driver.execute_script("arguments[0].click();", buttons[-1])
            log("   ✓ Menu ouvert")
            time.sleep(0.5)
            return True
    except Exception:
        pass
    
    # Fallbacks SVG
    fallback_selectors = [
        "button svg[data-icon='ellipsis-v']",
        "button svg.ellipsis",
        ".action-btn",
    ]
    for sel in fallback_selectors:
        try:
            btn = row.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].click();", btn)
            log("   ✓ Menu ouvert (fallback)")
            time.sleep(0.5)
            return True
        except:
            continue
    
    log("❌ Impossible d'ouvrir le menu", "ERROR")
    return False

def click_approve_in_menu(driver):
    """Clique sur 'Approuver' dans le menu dropdown."""
    log("👆 Clic sur 'Approuver' dans le menu...")
    try:
        # Petit délai pour laisser le menu s'ouvrir
        time.sleep(0.4)
        
        approve_option = None
        
        # Scan d'abord (instantané, marche toujours)
        menu_items = driver.find_elements(
            By.XPATH,
            "//div[contains(@class, 'dropdown-menu')]//a | //div[contains(@class, 'dropdown-menu')]//li | //div[contains(@class, 'dropdown-menu')]//button | //ul[contains(@class, 'menu')]//a"
        )
        for item in menu_items:
            text = item.text or item.get_attribute("textContent") or ""
            if "approuver" in text.lower() or "approuv" in text.strip().lower():
                approve_option = item
                log(f"   ✓ Trouvé: '{text.strip()}'")
                break
        
        # Fallback: sélecteurs XPath rapides (1s timeout)
        if not approve_option:
            wait = WebDriverWait(driver, 1)
            for sel in [
                "//a[contains(., 'Approuver')]",
                "//button[contains(., 'Approuver')]",
                "//li[contains(., 'Approuver')]",
            ]:
                try:
                    approve_option = wait.until(EC.element_to_be_clickable((By.XPATH, sel)))
                    log(f"   ✓ Option Approuver trouvée (fallback)")
                    break
                except:
                    continue
        
        if approve_option:
            try:
                approve_option.click()
                log("   ✓ Approuver cliqué (clic normal)")
            except Exception:
                log("   ⚠️ Clic intercepté, essai avec JS...")
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", approve_option)
                time.sleep(0.3)
                driver.execute_script("arguments[0].click();", approve_option)
                log("   ✓ Approuver cliqué (JS)")
            time.sleep(1.5)
            return True
        
        log("   ❌ Option Approuver non trouvée dans le menu")
        return False
        
    except Exception as e:
        log(f"❌ Option Approuver non trouvée: {e}", "ERROR")
        return False

def click_attribuer_in_menu(driver):
    """Clique sur 'Attribuer' dans le menu dropdown et gère l'attribution."""
    log("👆 Clic sur 'Attribuer' dans le menu...")
    try:
        # Petit délai pour laisser le menu s'ouvrir
        time.sleep(0.4)
        
        attribuer_option = None
        
        # Scan d'abord (instantané, marche toujours)
        menu_items = driver.find_elements(
            By.XPATH,
            "//div[contains(@class, 'dropdown-menu')]//a | //div[contains(@class, 'dropdown-menu')]//li | //div[contains(@class, 'dropdown-menu')]//button | //ul[contains(@class, 'menu')]//a"
        )
        for item in menu_items:
            text = item.text or item.get_attribute("textContent") or ""
            if "attribuer" in text.lower() or "attrib" in text.strip().lower():
                attribuer_option = item
                log(f"   ✓ Trouvé: '{text.strip()}'")
                break
        
        # Fallback: sélecteurs XPath rapides (1s timeout)
        if not attribuer_option:
            wait = WebDriverWait(driver, 1)
            for sel in [
                "//a[contains(., 'Attribuer')]",
                "//button[contains(., 'Attribuer')]",
                "//li[contains(., 'Attribuer')]",
            ]:
                try:
                    attribuer_option = wait.until(EC.element_to_be_clickable((By.XPATH, sel)))
                    log(f"   ✓ Option Attribuer trouvée (fallback)")
                    break
                except:
                    continue
        
        if attribuer_option:
            try:
                # Essayer le clic normal d'abord
                attribuer_option.click()
                log("   ✓ Attribuer cliqué (clic normal)")
            except Exception as e:
                # Si ça échoue, essayer avec JS
                log(f"   ⚠️ Clic intercepté, essai avec JS...")
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", attribuer_option)
                time.sleep(0.3)
                driver.execute_script("arguments[0].click();", attribuer_option)
                log("   ✓ Attribuer cliqué (JS)")
            time.sleep(1.5)
            return True
        else:
            log("   ❌ Option Attribuer non trouvée dans le menu")
            return False
        
    except Exception as e:
        log(f"❌ Option Attribuer non trouvée: {e}", "ERROR")
        return False

def attribuer_vehicule_au_conducteur(driver, conducteur_info: dict) -> bool:
    """
    Attribue le véhicule au conducteur dans le popup d'attribution.
    Recherche par téléphone et clique sur Attribuer.
    """
    if not conducteur_info:
        log("   ⚠️ Pas d'info conducteur pour l'attribution")
        return False
    
    phone = conducteur_info.get("phone", "")
    name = conducteur_info.get("name", "")
    
    log(f"   🔍 Recherche conducteur: {name} (Tél: {phone})")
    
    try:
        wait = WebDriverWait(driver, 10)
        
        # Chercher le champ de recherche par téléphone
        search_input = None
        selectors = [
            "//input[contains(@placeholder, 'téléphone') or contains(@placeholder, 'telephone')]",
            "//input[contains(@placeholder, 'phone')]",
            "//label[contains(text(), 'Téléphone')]/following::input[1]",
            "//input[@type='tel']",
            "//input[contains(@class, 'search')]",
        ]
        
        for sel in selectors:
            try:
                search_input = driver.find_element(By.XPATH, sel)
                log(f"   ✓ Champ recherche trouvé: {sel}")
                break
            except:
                continue
        
        if not search_input:
            log("   ⚠️ Champ recherche non trouvé, essai boutons Attribuer direct...")
            # Essayer de trouver directement un bouton Attribuer
            buttons = driver.find_elements(By.XPATH, "//button[contains(text(), 'Attribuer')]")
            if buttons:
                try:
                    buttons[0].click()
                except:
                    driver.execute_script("arguments[0].click();", buttons[0])
                log("   ✓ Attribuer cliqué (bouton direct)")
                time.sleep(1.5)
                return True
            return False
        
        # Entrer le numéro de téléphone (sans +225 pour le popup)
        phone_clean = phone.replace("+225", "") if phone.startswith("+225") else phone
        log(f"   📞 Téléphone formaté pour recherche: {phone_clean}")
        
        try:
            # Scroller jusqu'au champ pour s'assurer qu'il est visible
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_input)
            time.sleep(0.3)
            
            # Essayer d'abord la méthode normale
            search_input.clear()
            search_input.send_keys(phone_clean)
            log(f"   ✓ Téléphone saisi (normal): {phone_clean}")
        except Exception as e:
            # Si ça échoue, utiliser JS
            log(f"   ⚠️ Saisie normale échouée, essai JS...")
            driver.execute_script("arguments[0].value = arguments[1];", search_input, phone_clean)
            # Déclencher un événement input pour que l'UI réagisse
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", search_input)
            log(f"   ✓ Téléphone saisi (JS): {phone_clean}")
        time.sleep(0.5)
        
        # Chercher et cliquer sur le bouton rechercher ou Attribuer
        try:
            search_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Rechercher') or contains(text(), 'Search')]")
            search_btn.click()
            log("   ✓ Rechercher cliqué")
            time.sleep(1)
        except:
            pass
        
        # Chercher le conducteur dans les résultats et vérifier nom + téléphone
        try:
            conducteur_row = wait.until(
                EC.presence_of_element_located((By.XPATH, 
                    f"//tr[contains(., '{phone_clean}')] | //div[contains(@class, 'result')]//div[contains(., '{phone_clean}')]"))
            )
            log("   ✓ Conducteur trouvé dans les résultats")
            
            # Vérifier que le nom correspond aussi
            row_text = conducteur_row.text or ""
            if name and name.lower() not in row_text.lower():
                log(f"   ⚠️ Nom '{name}' non trouvé dans la ligne: {row_text[:100]}")
                # Continuer quand même si le téléphone correspond
            
            # Chercher le bouton Attribuer dans cette ligne
            attrib_btn = conducteur_row.find_element(By.XPATH, ".//button[contains(text(), 'Attribuer')]")
            attrib_btn.click()
            log("   ✅ Attribuer cliqué pour ce conducteur!")
            time.sleep(1)
            
            # Gérer l'alerte de confirmation
            try:
                alert = driver.switch_to.alert
                log(f"   📋 Alert: {alert.text}")
                alert.accept()
                log("   ✓ Alert acceptée")
            except:
                pass
            return True
            
        except Exception as e:
            log(f"   ⚠️ Conducteur avec téléphone {phone_clean} non trouvé dans les résultats")
            # NE PAS cliquer sur le premier bouton Attribuer trouvé!
            # Cela attribuerait au mauvais conducteur
            log("   ❌ Attribution annulée: conducteur non trouvé")
            return False
        
    except Exception as e:
        log(f"❌ Erreur attribution: {e}", "ERROR")
        return False

def open_document_page(driver, row):
    """Ouvre la page document du véhicule pour approuver la carte grise."""
    log("📄 Ouverture page document...")
    try:
        # Chercher l'icône document dans la ligne
        doc_icon = row.find_element(By.CSS_SELECTOR, 
            "svg[data-icon='file-alt'], svg.file-icon, .document-icon, a[href*='document']")
        
        # Cliquer ou obtenir le href
        try:
            href = doc_icon.get_attribute("href")
            if href:
                full_url = urljoin(BASE_URL, href)
                driver.get(full_url)
                log(f"   ✓ Navigation: {full_url}")
                time.sleep(1)
                return True
        except:
            pass
        
        # Sinon cliquer sur l'icône
        doc_icon.click()
        log("   ✓ Icône document cliquée")
        time.sleep(1)
        return True
        
    except Exception as e:
        log(f"❌ Icône document non trouvée: {e}", "ERROR")
        return False

def approve_document_on_page(driver, image_index: dict = None, matricule: str = None) -> tuple:
    """
    Approuve la carte grise sur la page document.
    Si non téléchargée et image disponible, fait l'upload d'abord.
    
    Returns:
        tuple: (success: bool, uploaded: bool, already_approved: bool)
    """
    log("📋 Approbation document...")
    uploaded = False
    already_approved = False
    
    try:
        # save_screenshot(driver, "document_page")
        
        # Chercher la ligne "Carte grise" dans le tableau
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        
        for row in rows:
            if "carte grise" in row.text.lower():
                log("   ✓ Ligne Carte grise trouvée")
                
                # Chercher le badge de statut
                status = "Inconnu"
                try:
                    badge = row.find_element(By.CSS_SELECTOR, ".badge")
                    status = badge.text.strip()
                    log(f"   📛 Statut document: {status}")
                    
                    if status.lower() in ["approuvé", "approved"]:
                        log("   ⏩ Document déjà approuvé")
                        return True, False, True
                except:
                    pass
                
                # Si non téléchargé, essayer d'uploader l'image
                if status.lower() in ["non téléchargé", "not downloaded", "non telecharge", "non télécharge"]:
                    log("   ⚠️ Carte grise non téléchargée")
                    
                    if image_index and matricule:
                        image_path = find_image_for_matricule(matricule, image_index)
                        if image_path:
                            log(f"   📷 Image trouvée: {image_path.name}")
                            # Sauvegarder l'UUID du document avant upload
                            doc_uuid = driver.current_url.rstrip("/").split("/")[-1]
                            # Utiliser la fonction complète avec vérification de statut
                            success, new_statut, skipped = upload_carte_grise_full(driver, image_path, matricule)
                            if success:
                                uploaded = True
                                if skipped:
                                    log("   ⏭️ Déjà uploadé (skip)")
                                else:
                                    log("   ✅ Carte grise uploadée!")
                                # Retourner sur la page document pour voir le nouveau statut
                                doc_url = f"{BASE_URL}/manage-fleet/document/{doc_uuid}"
                                log(f"   🔄 Retour page document: {doc_url}")
                                driver.get(doc_url)
                                time.sleep(2)
                                # Re-vérifier le statut après upload
                                rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                                for r in rows:
                                    if "carte grise" in r.text.lower():
                                        try:
                                            new_badge = r.find_element(By.CSS_SELECTOR, ".badge")
                                            new_status = new_badge.text.strip()
                                            log(f"   📛 Nouveau statut: {new_status}")
                                            if new_status.lower() in ["approuvé", "approved", "en attente d'approbation", "en attente"]:
                                                status = new_status
                                                break
                                        except:
                                            pass
                                break
                            else:
                                log("   ❌ Échec upload carte grise")
                        else:
                            log(f"   ⚠️ Aucune image trouvée pour {matricule} dans images_ocr/ (ni carte_grise_upjunoo.jpg)")
                    else:
                        log("   ⚠️ Pas d'index d'images ou de matricule fourni")
                
                # Chercher le bouton Approuver
                rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                for r in rows:
                    if "carte grise" in r.text.lower():
                        buttons = r.find_elements(By.TAG_NAME, "button")
                        for btn in buttons:
                            if "approuver" in btn.text.lower():
                                log("   👆 Clic Approuver...")
                                
                                # Essayer clic normal, sinon JS
                                try:
                                    btn.click()
                                except ElementNotInteractableException:
                                    driver.execute_script("arguments[0].click();", btn)
                                
                                log("   ✅ Document approuvé!")
                                time.sleep(1)
                                return True, uploaded, False
                        
                        log("   ⚠️ Bouton Approuver non trouvé dans la ligne")
                        return False, uploaded, False
                
                return False, uploaded, False
        
        log("   ❌ Ligne Carte grise non trouvée", "ERROR")
        return False, uploaded, False
        
    except Exception as e:
        log(f"❌ Erreur approbation document: {e}", "ERROR")
        return False, uploaded, False

# ───────────────────────────────────────────────────────────────────────────────
# WORKFLOW PRINCIPAL
# ───────────────────────────────────────────────────────────────────────────────

def quick_approve_vehicle(driver, matricule: str, image_index: dict = None) -> dict:
    """
    Workflow complet d'approbation rapide d'un véhicule avec attribution au conducteur.
    
    Args:
        driver: Instance Selenium
        matricule: Matricule du véhicule
        image_index: Index des images OCR (optionnel)
    
    Returns:
        dict avec les résultats de chaque étape
    """
    results = {
        "matricule": matricule,
        "success": False,
        "document_approved": False,
        "fleet_approved": False,
        "carte_grise_uploaded": False,
        "attribution_done": False,
        "attributed_to": None,
        "errors": []
    }
    
    try:
        # 0. Chercher le véhicule dans les JSON pour obtenir les infos du conducteur
        vehicle_info = find_vehicle_in_json(matricule)
        conducteur_info = None
        if vehicle_info:
            conducteur_info = {
                "name": vehicle_info.get("driver_name", ""),
                "phone": vehicle_info.get("phone", ""),
                "type_transport": vehicle_info.get("type_transport", "")
            }
            log(f"   👤 Conducteur trouvé: {conducteur_info['name']}")
        
        # Méthode originale: filtre sur manage-fleet d'abord
        log(f"\n{'='*60}")
        log(f"🚗 APPROBATION RAPIDE: {matricule}")
        log(f"{'='*60}")
        
        # 1. Aller sur manage-fleet
        driver.get(MANAGE_FLEET_URL)
        time.sleep(1.5)
        # save_screenshot(driver, "manage_fleet_initial")
        
        # 2. Ouvrir les filtres
        if not open_filters(driver):
            results["errors"].append("Échec ouverture filtres")
            return results
        # save_screenshot(driver, "filters_open")
        
        # 3. Mettre Statut = En attente
        if not set_filter_statut_en_attente(driver):
            results["errors"].append("Échec filtre statut")
        
        # 4. Mettre le matricule
        if not set_filter_matricule(driver, matricule):
            results["errors"].append("Échec filtre matricule")
            return results
        
        # 5. Appliquer les filtres
        if not apply_filters(driver):
            results["errors"].append("Échec application filtres")
            return results
        # save_screenshot(driver, "filters_applied")
        
        # 6. Trouver le véhicule
        row = find_vehicle_row(driver, matricule)
        
        # 6b. Si non trouvé, essayer sans filtre statut
        if not row:
            log("   ⚠️ Non trouvé avec 'En attente', essai sans filtre statut...")
            # Réouvrir filtres et chercher sans statut
            if open_filters(driver):
                # Réinitialiser le filtre statut (sélectionner option vide ou Tous)
                try:
                    statut_select = driver.find_element(By.XPATH, "//label[contains(text(), 'Statut')]/following::select[1]")
                    # Essayer de sélectionner la première option (généralement vide ou "Tous")
                    options = statut_select.find_elements(By.TAG_NAME, "option")
                    if options:
                        driver.execute_script("arguments[0].selectedIndex = 0; arguments[0].dispatchEvent(new Event('change'));", statut_select)
                        time.sleep(0.5)
                except:
                    pass
                # Remettre le matricule et appliquer
                set_filter_matricule(driver, matricule)
                apply_filters(driver)
                time.sleep(2)
                row = find_vehicle_row(driver, matricule)
        
        if not row:
            results["errors"].append("Véhicule non trouvé")
            return results
        
        # Lire le statut actuel
        statut_flotte = get_document_status_from_row(row)
        log(f"   📛 Statut flotte actuel: {statut_flotte}")
        
        # 6b. Si déjà APPROUVÉ → passer directement à l'attribution
        if "approuvé" in statut_flotte.lower():
            log("   ✅ Flotte déjà APPROUVÉE! Passage direct à l'attribution...")
            results["fleet_approved"] = True
            
            # Aller directement à l'attribution
            if conducteur_info:
                if click_menu_three_dots(driver, row):
                    if click_attribuer_in_menu(driver):
                        log("   📋 Popup attribution ouvert")
                        if attribuer_vehicule_au_conducteur(driver, conducteur_info):
                            log("   ✅ Véhicule attribué au conducteur!")
                            results["success"] = True
                            results["attribution_done"] = True
                            results["attributed_to"] = conducteur_info.get("name", "Inconnu")
                        else:
                            results["errors"].append("Échec attribution au conducteur")
                    else:
                        results["errors"].append("Échec ouverture popup Attribuer")
                else:
                    results["errors"].append("Échec ouverture menu")
            else:
                log("   ⚠️ Pas d'info conducteur pour l'attribution")
                results["errors"].append("Pas d'info conducteur")
            
            return results
        
        # Vérifier si c'est un véhicule Livraison (MOTO, CARGO, etc.) → pas besoin de carte grise
        is_livraison = False
        if vehicle_info:
            type_transport = vehicle_info.get("type_transport", "")
            vehicle_type = vehicle_info.get("vehicle_type", "")
            is_livraison = vehicle_is_livraison(type_transport, vehicle_type)
            if is_livraison:
                log(f"   🏍️ Véhicule Livraison détecté ({type_transport} / {vehicle_type}) → Skip carte grise")
                results["document_approved"] = True  # Pas besoin de carte grise pour Livraison
        
        # 7. Si EN ATTENTE et pas Livraison → aller sur la page document pour approuver
        if not is_livraison:
            if not open_document_page(driver, row):
                results["errors"].append("Échec ouverture page document")
                return results
            
            # 8. Approuver le document (avec upload si nécessaire)
            doc_ok, uploaded, already_approved = approve_document_on_page(driver, image_index, matricule)
            results["carte_grise_uploaded"] = uploaded
        else:
            # Livraison: on marque le document comme approuvé (pas besoin)
            doc_ok = True
            uploaded = False
            already_approved = True
        
        if doc_ok:
            results["document_approved"] = True
            if uploaded:
                log("   ✅ Étape 1: Carte grise uploadée et document approuvé!")
            elif already_approved:
                log("   ✅ Étape 1: Document déjà approuvé")
            else:
                log("   ✅ Étape 1: Document approuvé")
        else:
            results["errors"].append("Échec approbation document")
        
        # 9. Retourner sur manage-fleet pour approver la flotte
        log("🔄 Retour sur manage-fleet (back)...")
        driver.back()
        time.sleep(1.5)
        # save_screenshot(driver, "manage_fleet_return")
        
        # 10. Si pas sur manage-fleet, aller directement
        if "manage-fleet" not in driver.current_url or "document" in driver.current_url:
            log("   ⚠️ Back a échoué, navigation directe...")
            driver.get(MANAGE_FLEET_URL)
            time.sleep(1.5)
            
            # Ouvrir les filtres et mettre le matricule
            if not open_filters(driver):
                results["errors"].append("Échec ouverture filtres")
                return results
            if not set_filter_matricule(driver, matricule):
                results["errors"].append("Échec filtre matricule")
                return results
            if not apply_filters(driver):
                results["errors"].append("Échec application filtres")
                return results
        
        # 13. Trouver le véhicule, approuver la flotte puis attribuer au conducteur
        row = find_vehicle_row(driver, matricule)
        if not row:
            results["errors"].append("Véhicule non retrouvé après approbation doc")
        else:
            # Vérifier le statut de la flotte
            statut_flotte_final = get_document_status_from_row(row)
            log(f"   📛 Statut flotte: {statut_flotte_final}")
            
            if "approuvé" in statut_flotte_final.lower():
                log("   ✅ Flotte déjà approuvée sur le site!")
                results["fleet_approved"] = True
                # Passer directement à l'attribution
                need_attribution = True
            elif "en attente" in statut_flotte_final.lower():
                # Étape A : Approuver la flotte d'abord
                log("   🔄 Approbation flotte requise avant attribution...")
                if click_menu_three_dots(driver, row):
                    if click_approve_in_menu(driver):
                        log("   ✅ Flotte approuvée!")
                        time.sleep(1)
                        results["fleet_approved"] = True
                        # Maintenant on peut attribuer
                        need_attribution = True
                    else:
                        results["errors"].append("Échec approbation flotte")
                        need_attribution = False
                else:
                    results["errors"].append("Échec ouverture menu")
                    need_attribution = False
            else:
                log(f"   ⚠️ Statut flotte inattendu: {statut_flotte_final}")
                results["errors"].append(f"Statut flotte inattendu: {statut_flotte_final}")
                need_attribution = False
            
            # Étape B : Attribuer au conducteur (si flotte approuvée)
            if need_attribution and results["fleet_approved"]:
                log("   📋 Étape attribution au conducteur...")
                # Rafraîchir pour voir le nouveau statut
                driver.refresh()
                time.sleep(1.5)
                
                # Retrouver la ligne
                row = find_vehicle_row(driver, matricule)
                if row:
                    if click_menu_three_dots(driver, row):
                        if click_attribuer_in_menu(driver):
                            log("   📋 Popup attribution ouvert")
                            # Attribuer au conducteur avec son téléphone
                            if conducteur_info and conducteur_info.get("phone"):
                                if attribuer_vehicule_au_conducteur(driver, conducteur_info):
                                    log("   ✅ Véhicule attribué au conducteur!")
                                    results["attribution_done"] = True
                                    results["attributed_to"] = conducteur_info.get("name", "Inconnu")
                                else:
                                    log("   ⚠️ Attribution manuelle nécessaire")
                            else:
                                log("   ⚠️ Pas d'info conducteur pour attribution auto")
                        else:
                            log("   ⚠️ Option Attribuer non trouvée (peut-être déjà attribué)")
                else:
                    log("   ⚠️ Véhicule non retrouvé pour attribution")
        
        # Vérifier le résultat final
        if results["document_approved"] and results["fleet_approved"]:
            results["success"] = True
            log("\n🎉 APPROBATION COMPLÈTE RÉUSSIE!")
        elif results["document_approved"]:
            log("\n⚠️  Approbation document OK, mais flotte en échec")
        else:
            log("\n❌ ÉCHEC APPROBATION")
        
        # save_screenshot(driver, "final_result")
        
        # 14. Mettre à jour le fichier JSON si succès
        if results["success"]:
            log("📝 Mise à jour du fichier JSON partenaire...")
            attribution_name = results.get("attributed_to") if results.get("attribution_done") else None
            update_vehicle_status_in_json(
                matricule, 
                new_doc_status="Approuvé",
                new_fleet_status="Approuvé",
                attribution_name=attribution_name
            )
        
    except Exception as e:
        log(f"❌ Erreur inattendue: {e}", "ERROR")
        log(traceback.format_exc(), "ERROR")
        results["errors"].append(str(e))
        # save_screenshot(driver, "error")
    
    return results

# ───────────────────────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Approbation rapide d'un véhicule (document + flotte)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  # Mode matricule unique
  python3 quick_approve_vehicle_vps.py AA325AD01
  
  # Mode batch par partenaires (multi-instances)
  python3 quick_approve_vehicle_vps.py --partners 6,8,9,10 --port 9225
  
  # Mode fichier liste
  python3 quick_approve_vehicle_vps.py --list-file matricules.txt
        """
    )
    parser.add_argument("matricules", nargs="*", help="Matricule(s) du véhicule à approuver")
    parser.add_argument("--headed", action="store_true", help="Navigateur visible (pas headless)")
    parser.add_argument("--list-file", help="Fichier avec liste de matricules (un par ligne)")
    parser.add_argument("--partners", help="IDs partenaires séparés par virgule (ex: 6,8,9,10)")
    parser.add_argument("--port", type=int, help="Port debugging Chrome (pour instances parallèles)")
    
    args = parser.parse_args()
    
    # Collecter les matricules
    all_matricules = []
    
    # Mode batch par partenaires
    if args.partners:
        partner_ids = [int(p.strip()) for p in args.partners.split(",") if p.strip()]
        vehicles, skipped = get_all_vehicles_from_partners(partner_ids)
        all_matricules = [v["matricule"] for v in vehicles]
        log(f"\n🚀 MODE BATCH: {len(all_matricules)} véhicules à traiter ({skipped} ignorés)")
    
    # Mode fichier liste
    elif args.list_file:
        try:
            with open(args.list_file, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
                all_matricules.extend(lines)
            log(f"📁 {len(all_matricules)} matricules chargés depuis {args.list_file}")
        except Exception as e:
            log(f"❌ Erreur lecture fichier: {e}", "ERROR")
            sys.exit(1)
    
    # Mode matricules en argument
    if args.matricules:
        all_matricules.extend(args.matricules)
    
    if not all_matricules:
        log("❌ Aucun matricule fourni. Utilisez --partners, --list-file ou des matricules en arguments", "ERROR")
        sys.exit(1)
    
    # Notification Slack début
    port_info = f" (Port {args.port})" if args.port else ""
    send_slack_message(f"🚀 *Batch Approval Started*{port_info}\n📊 {len(all_matricules)} véhicules à traiter")
    
    log(f"\n{'='*60}")
    log(f"🚀 QUICK APPROVE - {len(all_matricules)} véhicule(s)")
    if args.port:
        log(f"   Instance port: {args.port}")
    log(f"{'='*60}")
    
    # Indexer les images OCR pour upload auto
    image_index = build_image_index(IMAGES_OCR_DIR)
    
    # Lancer le driver
    driver = None
    try:
        driver = setup_driver(headed=args.headed, debug_port=args.port)
        log(f"🖥️  Mode: {'visible (headed)' if args.headed else 'headless'}")
        
        # Connexion
        if not admin_login(driver):
            log("❌ Arrêt: connexion échouée", "ERROR")
            send_slack_message(f"❌ *Connexion échouée*{port_info}", mention_channel=True)
            sys.exit(1)
        
        # Traiter chaque véhicule avec gestion robuste des erreurs
        all_results = []
        slack_progress_interval = 10  # Notifier tous les 10 véhicules
        DRIVER_RESTART_INTERVAL = 50  # Redémarrer le driver tous les N véhicules (memory leak Chrome)
        
        for i, matricule in enumerate(all_matricules, 1):
            log(f"\n{'─'*60}")
            log(f"📌 Véhicule {i}/{len(all_matricules)}: {matricule}")
            log(f"{'─'*60}")
            
            # Progression Slack
            if i % slack_progress_interval == 0:
                progress_pct = int((i / len(all_matricules)) * 100)
                send_slack_message(f"⏳ *Progression*{port_info}: {i}/{len(all_matricules)} ({progress_pct}%)")
            
            # Redémarrage périodique du driver pour éviter memory leak Chrome
            if i > 1 and (i - 1) % DRIVER_RESTART_INTERVAL == 0:
                log(f"\n🔄 REDÉMARRAGE DRIVER (après {i-1} véhicules pour éviter memory leak)...")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(3)
                # Nettoyer le profil Chrome pour repartir propre
                try:
                    import shutil as _shutil
                    if args.port:
                        profile_dir = f"/tmp/chrome_profile_{args.port}"
                        if os.path.exists(profile_dir):
                            _shutil.rmtree(profile_dir, ignore_errors=True)
                except Exception:
                    pass
                # Relancer le driver + login
                restart_success = False
                for attempt in range(1, 4):
                    try:
                        driver = setup_driver(headed=args.headed, debug_port=args.port)
                        if admin_login(driver):
                            log(f"   ✅ Driver redémarré et reconnecté")
                            restart_success = True
                            break
                        else:
                            log(f"   ⚠️ Login échoué tentative {attempt}/3", "WARNING")
                            try:
                                driver.quit()
                            except:
                                pass
                            time.sleep(5)
                    except Exception as e:
                        log(f"   ⚠️ Redémarrage tentative {attempt}/3 échouée: {e}", "WARNING")
                        time.sleep(5)
                if not restart_success:
                    log(f"❌ Impossible de redémarrer le driver, arrêt", "ERROR")
                    send_slack_message(f"💥 *Driver mort*{port_info} après {i-1} véhicules")
                    break
            
            # Gestion robuste: try/except par véhicule pour continuer malgré les erreurs
            try:
                result = quick_approve_vehicle(driver, matricule, image_index)
                all_results.append(result)
            except Exception as e:
                error_msg = f"❌ CRASH sur {matricule}: {e}"
                log(error_msg, "ERROR")
                log(traceback.format_exc(), "ERROR")
                # Alert Slack sur crash
                send_slack_message(f"💥 *CRASH*{port_info}: `{matricule}`\n```{str(e)[:100]}```")
                # Ajouter un résultat d'échec pour ce véhicule
                all_results.append({
                    "matricule": matricule,
                    "success": False,
                    "document_approved": False,
                    "fleet_approved": False,
                    "carte_grise_uploaded": False,
                    "attribution_done": False,
                    "attributed_to": None,
                    "errors": [f"CRASH: {str(e)}"]
                })
                # Sauvegarder screenshot d'erreur si possible (désactivé)
                # try:
                #     save_screenshot(driver, f"crash_{matricule}")
                # except:
                #     pass
                # Continuer avec le véhicule suivant
                log(f"⏭️  Passage au véhicule suivant...")
            
            # Pause entre véhicules
            if i < len(all_matricules):
                time.sleep(3)
        
        # Résumé final
        log(f"\n{'='*60}")
        log("📊 RÉSUMÉ FINAL")
        log(f"{'='*60}")
        
        success_count = sum(1 for r in all_results if r["success"])
        doc_count = sum(1 for r in all_results if r["document_approved"])
        fleet_count = sum(1 for r in all_results if r["fleet_approved"])
        upload_count = sum(1 for r in all_results if r["carte_grise_uploaded"])
        attr_count = sum(1 for r in all_results if r.get("attribution_done"))
        crash_count = sum(1 for r in all_results if any("CRASH" in err for err in r.get("errors", [])))
        
        log(f"✅ Approbations complètes: {success_count}/{len(all_results)}")
        log(f"📄 Documents approuvés: {doc_count}/{len(all_results)}")
        log(f"📷 Cartes grises uploadées: {upload_count}/{len(all_results)}")
        log(f"🚗 Flottes approuvées: {fleet_count}/{len(all_results)}")
        log(f"👤 Attributions: {attr_count}/{len(all_results)}")
        if crash_count > 0:
            log(f"💥 Crashs (mais continué): {crash_count}")
        
        # Détails avec attribution
        for r in all_results:
            status = "✅" if r["success"] else "❌"
            crash_mark = " 💥" if any("CRASH" in err for err in r.get("errors", [])) else ""
            cg_info = " 📷CG" if r["carte_grise_uploaded"] else ""
            attr_info = f" 👤→{r.get('attributed_to', '?')}" if r.get("attribution_done") else ""
            errors = f" (Erreurs: {', '.join(r['errors'][:2])})" if r["errors"] else ""
            log(f"   {status} {r['matricule']}{cg_info}{attr_info}{crash_mark}{errors}")
        
        log(f"\n📝 Log complet: {LOG_FILE}")
        
        # Notification Slack résumé final
        slack_summary = f"""✅ *Batch Terminé*{port_info}

📊 *Résultats*:
• Approbations complètes: {success_count}/{len(all_results)}
• Documents approuvés: {doc_count}
• Flottes approuvées: {fleet_count}
• Attributions: {attr_count}
{f"• 💥 Crashs (mais continué): {crash_count}" if crash_count > 0 else ""}

📁 Log: `{LOG_FILE.name}`"""
        send_slack_message(slack_summary, mention_channel=(crash_count > 0 or success_count < len(all_results) * 0.8))
        
        # Code de sortie
        sys.exit(0 if success_count == len(all_results) else 1)
        
    except KeyboardInterrupt:
        log("\n⛔ Interrompu par l'utilisateur")
        sys.exit(130)
    except Exception as e:
        log(f"❌ Erreur fatale: {e}", "ERROR")
        log(traceback.format_exc(), "ERROR")
        sys.exit(1)
    finally:
        if driver:
            try:
                driver.quit()
                log("🔒 Driver fermé")
            except:
                pass
        # Nettoyage du profil Chrome temporaire
        try:
            if args.port:
                profile_dir = f"/tmp/chrome_profile_{args.port}"
                if os.path.exists(profile_dir):
                    shutil.rmtree(profile_dir, ignore_errors=True)
                    log(f"🧹 Profil Chrome nettoyé: {profile_dir}")
        except Exception:
            pass

if __name__ == "__main__":
    main()
