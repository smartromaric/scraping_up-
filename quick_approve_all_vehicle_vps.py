#!/usr/bin/env python3
"""
quick_approve_all_vehicle_vps.py
================================

Script admin rapide pour approbation complète d'un véhicule (document + flotte).
Upload + approbation carte grise pour TOUS les types (voitures, motos, cargos, camions...).
PAS d'attribution au conducteur.

Supporte le traitement PARALLÈLE via --workers N (chaque worker a sa propre instance Chrome).

Optimisations perf: attentes WebDriverWait, lien document direct (JSON), un seul filtre
manage-fleet par véhicule, cache ChromeDriver + index métadonnées.

Workflow par véhicule:
1. Connexion admin
2. Filtre par matricule puis parcours du tableau (statut lu ligne par ligne)
3. Upload la carte grise (image par défaut si pas d'image spécifique)
4. Approuve le document (Carte grise)
5. Ouvre le menu (3 points) → clique "Approuver" pour changer statut_flotte

Usage:
  python3 quick_approve_all_vehicle_vps.py <MATRICULE> [--headed]
  python3 quick_approve_all_vehicle_vps.py --partners 6,8,9,10 --workers 4
  python3 quick_approve_all_vehicle_vps.py --list-file matricules.txt --workers 3
  python3 quick_approve_all_vehicle_vps.py --list-file matricules.txt --workers 20 --fast
  python3 quick_approve_all_vehicle_vps.py --list-file matricules-blocked.txt --reverse --workers 20
"""

import argparse
import json
import os
import re
import shutil
import sys


def _configure_stdout_utf8() -> None:
    """Évite UnicodeEncodeError sous Windows (console cp1252 + emojis dans les logs)."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdout_utf8()
import tempfile
import time
import traceback
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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

load_dotenv()

BASE_URL = "https://upjunoo-server-new.junooapps.com"
ADMIN_LOGIN_URL = f"{BASE_URL}/login/admin"
MANAGE_FLEET_URL = f"{BASE_URL}/manage-fleet"

ADMIN_EMAIL = os.getenv("UPJUNOO_EMAIL", "admin@upjunoo.com")
ADMIN_PASSWORD = os.getenv("UPJUNOO_PASSWORD", "Upjunoo@Admin")

SLACK_WEBHOOK = os.getenv("WEBHOOK_URL", "")
SLACK_BOT_NAME = os.getenv("SLACK_BOT_NAME", "UpJunoo Bot")
SLACK_ICON = os.getenv("SLACK_ICON_EMOJI", ":car:")

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR

# Détection auto de la structure : VPS (tout dans le même dossier) ou local (sous-dossier vps_deploy/)
if (PROJECT_ROOT / "vps_deploy" / "output").exists():
    OUTPUT_DIR = PROJECT_ROOT / "vps_deploy" / "output"
    LOG_DIR = PROJECT_ROOT / "vps_deploy" / "logs"
elif (PROJECT_ROOT / "output").exists():
    OUTPUT_DIR = PROJECT_ROOT / "output"
    LOG_DIR = PROJECT_ROOT / "logs"
else:
    OUTPUT_DIR = PROJECT_ROOT / "output"
    LOG_DIR = PROJECT_ROOT / "logs"

DEBUG_DIR = OUTPUT_DIR / "debug"

if (PROJECT_ROOT / "images_ocr").exists():
    IMAGES_OCR_DIR = PROJECT_ROOT / "images_ocr"
elif (PROJECT_ROOT / "vps_deploy" / "images_ocr").exists():
    IMAGES_OCR_DIR = PROJECT_ROOT / "vps_deploy" / "images_ocr"
else:
    IMAGES_OCR_DIR = PROJECT_ROOT / "images_ocr"

LOG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

RUN_TIMESTAMP = datetime.now().strftime('%Y%m%d_%H%M%S')
LOG_FILE = LOG_DIR / f"quick_approve_all_{RUN_TIMESTAMP}.log"

BASE_DEBUG_PORT = 9222

_log_lock = threading.Lock()
_chromedriver_lock = threading.Lock()
_chromedriver_path = None

# Mode --fast : délais réduits entre matricules et lors des attentes tableau / pagination
_FAST_MODE = False


def _pause(seconds: float, min_sec: float = 0.0) -> None:
    """time.sleep avec réduction en mode --fast."""
    if seconds <= 0:
        return
    if _FAST_MODE:
        seconds = max(min_sec, seconds * 0.2)
    time.sleep(seconds)


def _pagination_timing() -> dict:
    if _FAST_MODE:
        return {"min_stable_s": 1.0, "poll_s": 0.2, "initial_sleep": 1.0, "default_max_wait": 22}
    return {"min_stable_s": 2.8, "poll_s": 0.45, "initial_sleep": 3.0, "default_max_wait": 35}


def _filter_wait_timeout(default: float) -> float:
    return default * 0.55 if _FAST_MODE else default


# ───────────────────────────────────────────────────────────────────────────────
# ATTENTES RAPIDES (WebDriverWait au lieu de sleep fixes)
# ───────────────────────────────────────────────────────────────────────────────

def _get_chromedriver_path() -> str:
    global _chromedriver_path
    with _chromedriver_lock:
        if _chromedriver_path is None:
            _chromedriver_path = ChromeDriverManager().install()
        return _chromedriver_path


def _wait(driver, timeout: float, condition, wid: int = None):
    try:
        return WebDriverWait(driver, timeout).until(condition)
    except TimeoutException:
        return None


def _wait_table(driver, timeout: float = 5, wid: int = None) -> bool:
    el = _wait(
        driver, timeout,
        EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr, table, button.btn")),
        wid,
    )
    return el is not None


def _wait_url_change(driver, url_fragment: str, timeout: float = 10, must_leave: bool = True) -> bool:
    """Attend que l'URL change (quitte ou entre un fragment)."""
    try:
        if must_leave:
            WebDriverWait(driver, timeout).until(lambda d: url_fragment not in d.current_url)
        else:
            WebDriverWait(driver, timeout).until(lambda d: url_fragment in d.current_url)
        return True
    except TimeoutException:
        return False


def _safe_get(driver, url: str, wid: int = None, retries: int = 3) -> bool:
    """
    Navigation tolérante : le site UpJunoo est lent.
    Si le timeout page_load arrive, on arrête le chargement et on continue si le DOM est utilisable.
    """
    for attempt in range(1, retries + 1):
        try:
            driver.get(url)
            return True
        except TimeoutException:
            log(
                f"   ⚠️ Timeout chargement ({attempt}/{retries}) — poursuite: {url[:80]}",
                "WARNING",
                wid,
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            time.sleep(0.5)
            cur = driver.current_url.lower()
            if "manage-fleet" in url and ("manage-fleet" in cur or _wait_table(driver, 4, wid)):
                return True
            if "login" in url and _is_on_login_page(driver):
                return True
            if _is_admin_logged_in(driver):
                return True
            try:
                if driver.find_elements(By.CSS_SELECTOR, "body"):
                    return True
            except Exception:
                pass
        except Exception as e:
            err = str(e).split("\n")[0][:120]
            log(f"   ⚠️ Erreur navigation ({attempt}/{retries}): {err}", "WARNING", wid)
            time.sleep(1)
    try:
        return "manage-fleet" in driver.current_url or _wait_table(driver, 3, wid)
    except Exception:
        return False

# ───────────────────────────────────────────────────────────────────────────────
# LOGGING (thread-safe)
# ───────────────────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO", worker_id: int = None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[W{worker_id}]" if worker_id is not None else ""
    line = f"[{ts}][{level}]{prefix} {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(line.encode(enc, errors="replace").decode(enc, errors="replace"), flush=True)
    with _log_lock:
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

# ───────────────────────────────────────────────────────────────────────────────
# INDEX IMAGES OCR
# ───────────────────────────────────────────────────────────────────────────────

def build_image_index(images_dir: Path) -> dict:
    """Indexe les images OCR + racine projet: {nom_fichier_lower: Path}"""
    index = {}
    
    if images_dir.exists():
        for f in images_dir.iterdir():
            if f.suffix.lower() in (".jpeg", ".jpg", ".png"):
                index[f.name.lower()] = f
    
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

# Détection auto du dossier partenaire_drivers_scrape
_candidates = [
    OUTPUT_DIR / "partenaire_drivers_scrape",
    PROJECT_ROOT / "partenaire_drivers_scrape",
    PROJECT_ROOT / "vps_deploy" / "output" / "partenaire_drivers_scrape",
]
PARTENAIRE_DRIVERS_DIR = next((p for p in _candidates if p.exists()), _candidates[0])
PARTNER_JSON_FILE_RE = re.compile(r"^\s*partenaires?[-_]?\s*(\d+)_drivers\.json\s*$", re.I)

def find_vehicle_in_json(matricule: str) -> dict:
    """Cherche un véhicule par matricule dans tous les fichiers Partenaire-N_drivers.json"""
    mat_norm = normalize_plate(matricule)
    if not mat_norm:
        return None
    
    if not PARTENAIRE_DRIVERS_DIR.exists():
        return None
    
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
                    return {
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
                    
        except Exception:
            continue
    
    return None


def count_json_en_attente_for_matricule(matricule: str) -> int:
    """Compte les entrées JSON encore EN ATTENTE pour ce matricule (doublons multi-partenaires)."""
    mat_norm = normalize_plate(matricule)
    if not mat_norm or not PARTENAIRE_DRIVERS_DIR.exists():
        return 0
    count = 0
    for json_file in PARTENAIRE_DRIVERS_DIR.iterdir():
        if not json_file.is_file() or not PARTNER_JSON_FILE_RE.match(json_file.name):
            continue
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for driver_row in data.get("conducteurs", []):
                vehicle = driver_row.get("vehicle", {})
                if normalize_plate(vehicle.get("matricule", "")) != mat_norm:
                    continue
                sf = (vehicle.get("statut_flotte") or "").lower()
                if "attente" in sf and "approuv" not in sf:
                    count += 1
        except Exception:
            continue
    return count


def build_matricule_meta_index() -> dict:
    """Index global matricule → doc_link / statuts (évite une relecture JSON par véhicule)."""
    index = {}
    if not PARTENAIRE_DRIVERS_DIR.exists():
        return index
    for json_file in PARTENAIRE_DRIVERS_DIR.iterdir():
        if not json_file.is_file() or not PARTNER_JSON_FILE_RE.match(json_file.name):
            continue
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for driver in data.get("conducteurs", []):
                vehicle = driver.get("vehicle", {})
                mat = vehicle.get("matricule", "")
                if not mat:
                    continue
                index[normalize_plate(mat)] = {
                    "matricule": mat,
                    "doc_link": vehicle.get("doc_link"),
                    "edit_link": vehicle.get("edit_link"),
                    "statut_document": vehicle.get("statut_document", ""),
                    "statut_flotte": vehicle.get("statut_flotte", ""),
                }
        except Exception:
            continue
    if index:
        log(f"📇 Index métadonnées: {len(index)} matricule(s)")
    return index


def discover_all_partner_ids() -> list:
    """Scanne le dossier partenaire_drivers_scrape et retourne tous les IDs trouvés."""
    if not PARTENAIRE_DRIVERS_DIR.exists():
        log(f"⚠️ Dossier {PARTENAIRE_DRIVERS_DIR} introuvable")
        return []
    
    ids = []
    for json_file in PARTENAIRE_DRIVERS_DIR.iterdir():
        if not json_file.is_file():
            continue
        match = PARTNER_JSON_FILE_RE.match(json_file.name)
        if match:
            ids.append(int(match.group(1)))
    
    ids.sort()
    log(f"🔍 {len(ids)} partenaires détectés automatiquement: {ids}")
    return ids


def get_all_vehicles_from_partners(partner_ids: list, reverse_within_partner: bool = False) -> tuple:
    """
    Charge tous les véhicules des partenaires spécifiés (dans l'ordre de partner_ids).
    Si reverse_within_partner: derniers conducteurs du JSON en premier pour chaque partenaire.
    Retourne: (vehicles_to_process, skipped_count)
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
            if reverse_within_partner:
                conducteurs = list(reversed(conducteurs))
            partner_vehicles = 0
            partner_skipped = 0
            
            for driver in conducteurs:
                vehicle = driver.get("vehicle", {})
                vehicle_mat = vehicle.get("matricule", "")
                
                if not vehicle_mat:
                    continue
                
                statut_attribution = vehicle.get("statut_attribution", "")
                if statut_attribution and "attribué" in statut_attribution.lower():
                    skipped_count += 1
                    partner_skipped += 1
                    continue
                
                statut_flotte = vehicle.get("statut_flotte", "")
                if statut_flotte and "approuvé" in statut_flotte.lower() and not statut_attribution:
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

_json_lock = threading.Lock()

def update_vehicle_status_in_json(matricule: str, new_doc_status: str = None, new_fleet_status: str = None) -> bool:
    """Met à jour le statut d'un véhicule dans le fichier JSON partenaire (thread-safe)."""
    mat_norm = normalize_plate(matricule)
    if not mat_norm:
        return False
    
    if not PARTENAIRE_DRIVERS_DIR.exists():
        return False
    
    with _json_lock:
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
                        if new_doc_status:
                            vehicle["statut_document"] = new_doc_status
                        if new_fleet_status:
                            vehicle["statut_flotte"] = new_fleet_status
                        updated = True
                        break
                
                if updated:
                    with open(json_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    return True
                        
            except Exception:
                continue
    
    return False

DEFAULT_CARTE_GRISE_FILE = "carte_grise_upjunoo.jpg"

SKIP_DOC_STATUSES = ["en attente d'approbation", "approuvé", "approved", "approval pending"]


def find_image_for_matricule(matricule: str, image_index: dict) -> Path:
    """Trouve l'image correspondant à un matricule, fallback sur image par défaut."""
    if not image_index:
        return None
    
    if matricule:
        mat_norm = normalize_plate(matricule)
        
        for filename, path in image_index.items():
            if mat_norm in normalize_plate(filename):
                return path
            stem = Path(filename).stem.upper()
            if mat_norm in stem or stem in mat_norm:
                return path
    
    default_key = DEFAULT_CARTE_GRISE_FILE.lower()
    if default_key in image_index:
        return image_index[default_key]
    
    return None

# ───────────────────────────────────────────────────────────────────────────────
# UPLOAD CARTE GRISE
# ───────────────────────────────────────────────────────────────────────────────

def upload_carte_grise_full(driver, image_path: Path, immatriculation: str, wid: int = None) -> tuple:
    """
    Upload une carte grise sur la page document actuelle.
    Retourne: (success: bool, statut_document: str, skipped: bool)
    """
    statut_document = ""
    try:
        doc_status_el = driver.find_element(By.CSS_SELECTOR, "table tbody td .badge, table tbody td span")
        statut_document = (doc_status_el.text or "").strip()
        doc_status_lower = statut_document.lower()
        
        is_already_uploaded = any(kw in doc_status_lower for kw in SKIP_DOC_STATUSES)
        if is_already_uploaded:
            log(f"   ⏭️ Déjà uploadé (statut: {statut_document!r}) — SKIP", worker_id=wid)
            return True, statut_document, True
    except Exception:
        pass
    
    current_url = driver.current_url
    if "/manage-fleet/document/" not in current_url:
        log(f"   ❌ Pas sur une page document — URL: {current_url}", "ERROR", wid)
        return False, statut_document, False
    
    doc_uuid = current_url.rstrip("/").split("/")[-1]
    
    upload_url = f"{BASE_URL}/manage-fleet/document-upload/1/{doc_uuid}"
    log(f"   🔗 Upload URL: {upload_url}", worker_id=wid)
    _safe_get(driver, upload_url, wid)
    if not _wait(driver, 6, EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']")), wid):
        time.sleep(0.5)
    
    current_url = driver.current_url
    if "document-upload" not in current_url:
        log(f"   ❌ Pas sur document-upload — URL: {current_url}", "ERROR", wid)
        return False, statut_document, False
    
    try:
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
            log(f"   ✏️ Numéro renseigné: {immatriculation!r}", worker_id=wid)
        
        file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
        if not file_inputs:
            log("   ❌ Aucun input[type=file] sur la page", "ERROR", wid)
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
            log("   ❌ Aucun input file accessible", "ERROR", wid)
            return False, statut_document, False
        
        abs_path = str(image_path.absolute())
        log(f"   📎 Envoi image: {abs_path}", worker_id=wid)
        file_input.send_keys(abs_path)
        _wait(driver, 4, EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'], button.btn-primary")), wid)
        
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
            log("   ❌ Bouton 'Mise à jour' introuvable", "ERROR", wid)
            return False, statut_document, False
        
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_btn)
        try:
            submit_btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", submit_btn)
        
        _wait_url_change(driver, "document-upload", timeout=8, must_leave=True)
        url_after = driver.current_url
        
        if "document-upload" not in url_after:
            log(f"   ✅ Redirection réussie → upload OK", worker_id=wid)
            return True, "Uploadé", False
        
        success_els = driver.find_elements(
            By.CSS_SELECTOR, ".alert-success, .swal2-success, .toast-success, [class*='success']"
        )
        if success_els:
            log(f"   ✅ Indicateur succès détecté", worker_id=wid)
            return True, "Uploadé", False
        
        log("   ⚠️ Toujours sur document-upload — succès supposé", "WARNING", wid)
        return True, "Uploadé", False
        
    except Exception as e:
        log(f"   ❌ Exception upload: {e}", "ERROR", wid)
        return False, statut_document, False

# ───────────────────────────────────────────────────────────────────────────────
# SELENIUM SETUP
# ───────────────────────────────────────────────────────────────────────────────

def setup_driver(headed: bool = False, debug_port: int = None, wid: int = None):
    opts = Options()
    if headed:
        opts.add_argument("--start-maximized")
        log("   🪟 Mode navigateur visible (--headed)", worker_id=wid)
    else:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    if not headed:
        opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-features=VizDisplayCompositor")
    opts.add_argument("--js-flags=--max-old-space-size=512")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-translate")
    if not headed:
        opts.add_argument("--hide-scrollbars")
        opts.add_argument("--mute-audio")
        opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.page_load_strategy = "eager"
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option('useAutomationExtension', False)
    prefs = {"profile.default_content_setting_values.notifications": 2}
    if not headed:
        prefs["profile.managed_default_content_settings.images"] = 2
    opts.add_experimental_option("prefs", prefs)

    # Port debug + profil persistant : headless VPS uniquement (évite conflit 9222 sous Windows)
    if debug_port and not headed:
        profile_dir = Path(tempfile.gettempdir()) / "upjunoo_chrome_profiles" / f"port_{debug_port}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        opts.add_argument(f"--remote-debugging-port={debug_port}")
        opts.add_argument(f"--user-data-dir={profile_dir}")
        log(f"   🔌 Port debugging: {debug_port}", worker_id=wid)
    
    chromedriver = _get_chromedriver_path()
    log(f"🖥️  ChromeDriver: {chromedriver}", worker_id=wid)
    service = Service(chromedriver)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.implicitly_wait(0)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)
    return driver

# ───────────────────────────────────────────────────────────────────────────────
# ADMIN AUTH
# ───────────────────────────────────────────────────────────────────────────────

def _dismiss_alert(driver, wid: int = None) -> str | None:
    """Ferme une alerte JS si présente et retourne son texte, sinon None."""
    try:
        from selenium.webdriver.common.alert import Alert
        alert = Alert(driver)
        txt = alert.text
        alert.accept()
        return txt
    except Exception:
        return None


def _is_on_login_page(driver) -> bool:
    """True si le formulaire de connexion admin est affiché."""
    url = driver.current_url.lower()
    if any(p in url for p in ("/dashboard", "/manage-fleet", "/manage-owners", "/manage-drivers")):
        return False
    try:
        pwd = driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
        email = driver.find_elements(By.CSS_SELECTOR, "input[type='email']")
        if pwd and email and any(e.is_displayed() for e in email):
            return True
    except Exception:
        pass
    return "login" in url


def _is_admin_logged_in(driver) -> bool:
    """True si une session admin active est détectée (hors écran login)."""
    if _is_on_login_page(driver):
        return False
    url = driver.current_url.lower()
    if any(p in url for p in ("/dashboard", "/manage-fleet", "/manage-owners", "/manage-drivers")):
        return True
    try:
        if driver.find_elements(By.CSS_SELECTOR, "input[type='password']"):
            return False
        markers = driver.find_elements(
            By.CSS_SELECTOR,
            "table tbody tr, a[href*='manage-fleet'], nav, .sidebar, .navbar",
        )
        return len(markers) > 0
    except Exception:
        return False


def _wait_login_success(driver, timeout: float = 25, wid: int = None) -> bool:
    """Attend la fin du login (redirection lente ou SPA)."""
    end = time.time() + timeout
    last_url = ""
    while time.time() < end:
        _dismiss_alert(driver, wid)
        if _is_admin_logged_in(driver):
            return True
        cur = driver.current_url
        if cur != last_url:
            last_url = cur
            log(f"   ↪ URL: {cur}", worker_id=wid)
        time.sleep(0.5)
    return _is_admin_logged_in(driver)


def admin_login(driver, wid: int = None) -> bool:
    masked_email = ADMIN_EMAIL[:3] + "***" + ADMIN_EMAIL[ADMIN_EMAIL.index("@"):] if "@" in ADMIN_EMAIL else "???"
    masked_pwd = ADMIN_PASSWORD[:2] + "***" + ADMIN_PASSWORD[-2:] if len(ADMIN_PASSWORD) > 4 else "???"
    log(f"🔐 Connexion admin... (email={masked_email}, pwd={masked_pwd})", worker_id=wid)
    
    for attempt in range(3):
        _dismiss_alert(driver, wid)
        
        # Session déjà active (évite de recharger /login et de casser la session)
        try:
            _safe_get(driver, f"{BASE_URL}/dashboard", wid)
            _wait(driver, 8, EC.presence_of_element_located((By.CSS_SELECTOR, "body")), wid)
            _dismiss_alert(driver, wid)
            if _is_admin_logged_in(driver):
                log(f"   ✓ Déjà connecté: {driver.current_url}", worker_id=wid)
                return True
        except Exception:
            pass
        
        if not _safe_get(driver, ADMIN_LOGIN_URL, wid):
            log(f"   ⚠️ Page login lente (tentative {attempt+1}/3)", "WARNING", wid)
        _dismiss_alert(driver, wid)
        
        # Redirection auto si cookie de session valide
        time.sleep(0.8)
        if _is_admin_logged_in(driver):
            log(f"   ✓ Connecté (session existante): {driver.current_url}", worker_id=wid)
            return True
        
        if not _is_on_login_page(driver):
            if _is_admin_logged_in(driver):
                log(f"   ✓ Connecté: {driver.current_url}", worker_id=wid)
                return True
        
        try:
            wait = WebDriverWait(driver, 20)
            email_input = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='email']"))
            )
            email_input.clear()
            email_input.send_keys(ADMIN_EMAIL)
            
            pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            pwd_input.clear()
            pwd_input.send_keys(ADMIN_PASSWORD)
            
            entered_email = email_input.get_attribute("value")
            entered_pwd = pwd_input.get_attribute("value")
            log(f"   📧 Email saisi: {entered_email[:3]}***{entered_email[entered_email.index('@'):] if '@' in entered_email else '???'}", worker_id=wid)
            log(f"   🔑 Mdp saisi: {len(entered_pwd)} chars", worker_id=wid)
            
            btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            try:
                btn.click()
            except Exception:
                driver.execute_script("arguments[0].click();", btn)
            
            alert_txt = _dismiss_alert(driver, wid)
            if alert_txt:
                log(f"   ⚠️ Tentative {attempt+1}/3 — Alerte: {alert_txt}", "WARNING", wid)
                log(f"   💡 Vérifiez les identifiants dans le .env !", "WARNING", wid)
                time.sleep(1)
                continue
            
            if _wait_login_success(driver, timeout=25, wid=wid):
                log(f"   ✓ Connecté: {driver.current_url}", worker_id=wid)
                return True
            
            log(
                f"   ⚠️ Tentative {attempt+1}/3 — pas de session admin détectée (URL: {driver.current_url})",
                "WARNING",
                wid,
            )
            
        except TimeoutException as e:
            _dismiss_alert(driver, wid)
            if _is_admin_logged_in(driver):
                log(f"   ✓ Connecté (après timeout formulaire): {driver.current_url}", worker_id=wid)
                return True
            err_msg = str(e).split("\n")[0][:200] or "timeout"
            log(f"   ⚠️ Tentative {attempt+1}/3 échouée: {err_msg}", "WARNING", wid)
            time.sleep(1)
        except Exception as e:
            _dismiss_alert(driver, wid)
            if _is_admin_logged_in(driver):
                log(f"   ✓ Connecté: {driver.current_url}", worker_id=wid)
                return True
            err_msg = str(e).split("\n")[0][:200] or type(e).__name__
            log(f"   ⚠️ Tentative {attempt+1}/3 échouée: {err_msg}", "WARNING", wid)
            time.sleep(1)
    
    log(f"❌ Échec connexion après 3 tentatives", "ERROR", wid)
    log(f"   💡 VÉRIFIEZ que UPJUNOO_EMAIL et UPJUNOO_PASSWORD sont corrects dans .env", "ERROR", wid)
    return False

# ───────────────────────────────────────────────────────────────────────────────
# FILTRES
# ───────────────────────────────────────────────────────────────────────────────

def open_filters(driver, wid: int = None):
    """Ouvre le panneau des filtres."""
    try:
        if _is_on_login_page(driver) and not _is_admin_logged_in(driver):
            log("   ⚠️ Session expirée, re-login...", "WARNING", wid)
            if not admin_login(driver, wid):
                return False
            _safe_get(driver, MANAGE_FLEET_URL, wid)
            _wait_table(driver, 5, wid)
        
        try:
            driver.find_element(By.TAG_NAME, "body").click()
        except:
            pass
        
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
                log("   ✓ Bouton Filtres cliqué", worker_id=wid)
                return True
            except:
                continue
        
        log("❌ Impossible d'ouvrir les filtres", "ERROR", wid)
        return False
        
    except Exception as e:
        log(f"❌ Erreur open_filters: {e}", "ERROR", wid)
        return False


def set_pagination_max(driver, wid: int = None, max_wait: float = None) -> bool:
    """Force la pagination du tableau à 500 (ou max). Attend que toutes les lignes AJAX soient chargées."""
    pt = _pagination_timing()
    if max_wait is None:
        max_wait = pt["default_max_wait"]
    selector = "select.form-select.form-select-sm.w-auto"
    min_stable_s = pt["min_stable_s"]
    poll_s = pt["poll_s"]

    for attempt in range(1, 4):
        try:
            sel_el = WebDriverWait(driver, 8).until(
                lambda d: d.find_element(By.CSS_SELECTOR, selector)
                if len(d.find_element(By.CSS_SELECTOR, selector).find_elements(By.TAG_NAME, "option")) >= 2
                else False
            )
        except Exception:
            _pause(0.5, 0.1)
            continue
        try:
            page_size = driver.execute_script(
                """
                var select = arguments[0], found = false, chosen = '';
                for (var i = 0; i < select.options.length; i++) {
                    if (select.options[i].value === '500' || select.options[i].text.trim() === '500') {
                        select.selectedIndex = i; found = true; break;
                    }
                }
                if (!found) { select.selectedIndex = select.options.length - 1; }
                chosen = select.options[select.selectedIndex].text.trim() || select.options[select.selectedIndex].value;
                var setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
                setter.call(select, select.options[select.selectedIndex].value);
                select.dispatchEvent(new Event('change', { bubbles: true }));
                if (window.jQuery) { window.jQuery(select).trigger('change'); }
                return chosen;
                """,
                sel_el,
            )
            log(f"   📄 Pagination → {page_size or 'max'}", worker_id=wid)
        except Exception:
            continue

        log("   ⏳ Chargement du tableau après pagination...", worker_id=wid)
        _pause(pt["initial_sleep"], 0.8)

        start = time.time()
        last_count = -1
        stable_since = None
        peak_count = 0

        while time.time() - start < max_wait:
            count = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
            if count > peak_count:
                peak_count = count
                if count > last_count > 0:
                    log(f"   📄 … {count} ligne(s) chargée(s)", worker_id=wid)

            if count == last_count and count > 0:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= min_stable_s:
                    log(f"   📄 Tableau stabilisé: {count} ligne(s) visibles", worker_id=wid)
                    return True
            else:
                stable_since = None
                last_count = count

            _pause(poll_s, 0.08)

        final_count = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
        if final_count > 0:
            log(
                f"   📄 Fin attente pagination: {final_count} ligne(s) (pic {peak_count})",
                worker_id=wid,
            )
            return True

    log("   ⚠️ Pagination 500 non appliquée — risque de lignes tronquées", "WARNING", wid)
    return False


def set_filter_statut_en_attente(driver, wid: int = None):
    """Met le filtre Statut = 'En attente'."""
    try:
        driver.find_element(By.XPATH, "//*[contains(text(), 'STATUT') or contains(text(), 'Statut')]")
        
        try:
            statut_select = driver.find_element(By.XPATH, 
                "//label[contains(text(), 'Statut') or contains(text(), 'STATUT')]/following::select[1]")
            
            options = statut_select.find_elements(By.TAG_NAME, "option")
            for opt in options:
                if "en attente" in opt.text.lower():
                    opt.click()
                    log(f"   ✓ Filtre statut: {opt.text}", worker_id=wid)
                    break
        except NoSuchElementException:
            try:
                dropdown = driver.find_element(By.XPATH,
                    "//label[contains(text(), 'Statut') or contains(text(), 'STATUT')]/following::div[contains(@class, 'dropdown') or contains(@class, 'select')][1]")
                dropdown.click()
                time.sleep(0.3)
                
                option = driver.find_element(By.XPATH, "//li[contains(text(), 'En attente') or contains(text(), 'en attente')]")
                option.click()
                log("   ✓ Dropdown: En attente sélectionné", worker_id=wid)
            except Exception:
                return False
        
        return True
        
    except Exception:
        return False

def set_filter_matricule(driver, matricule: str, wid: int = None):
    """Met le filtre Numéro de plaque d'immatriculation."""
    try:
        matricule_typed = matricule.upper().strip()
        
        selectors = [
            (By.XPATH, "//input[contains(@placeholder, 'plaque')]"),
            (By.XPATH, "//label[contains(text(), 'plaque') or contains(text(), 'PLAQUE')]/following::input[1]"),
            (By.CSS_SELECTOR, "input[placeholder*='immatriculation' i]"),
        ]
        
        plaque_input = None
        for by, val in selectors:
            try:
                plaque_input = driver.find_element(by, val)
                break
            except:
                continue
        
        if not plaque_input:
            log("❌ Champ plaque introuvable", "ERROR", wid)
            return False
        
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();",
                plaque_input,
            )
            plaque_input.clear()
            plaque_input.send_keys(matricule_typed)
        except Exception:
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                plaque_input,
                matricule_typed,
            )
        log(f"   ✓ Matricule saisi: {matricule_typed}", worker_id=wid)
        return True
        
    except Exception as e:
        log(f"❌ Erreur filtre matricule: {e}", "ERROR", wid)
        return False

def _find_appliquer_button(driver):
    """Trouve le bouton Appliquer (recherche fraîche à chaque appel)."""
    wait = WebDriverWait(driver, 5)
    selectors = [
        (By.XPATH, "//button[normalize-space()='Appliquer']"),
        (By.XPATH, "//button[contains(normalize-space(), 'Appliquer')]"),
        (By.CSS_SELECTOR, "button.btn-success"),
        (By.CSS_SELECTOR, "button[style*='background-color: rgb(13, 148, 136)']"),
        (By.CSS_SELECTOR, "button[style*='background-color: rgb(20, 184, 166)']"),
        (By.CSS_SELECTOR, "button[class*='success']"),
        (By.XPATH, "//div[contains(@class, 'filter') or contains(@class, 'drawer') or contains(@class, 'modal')]//button[last()]"),
        (By.XPATH, "//aside//button[last()]"),
        (By.XPATH, "//div[contains(@class, 'offcanvas')]//button[last()]"),
    ]
    for by, val in selectors:
        try:
            return wait.until(EC.element_to_be_clickable((by, val)))
        except Exception:
            continue
    for b in driver.find_elements(By.TAG_NAME, "button"):
        try:
            if "appliquer" in (b.text or "").lower():
                return b
        except StaleElementReferenceException:
            continue
    return None


def apply_filters(driver, wid: int = None, wait_matricule: str = None):
    """Clique sur le bouton Appliquer (bouton vert). Attend le matricule si fourni."""
    _pause(0.4, 0.05)

    for attempt in range(1, 4):
        try:
            btn = _find_appliquer_button(driver)
            if not btn:
                if attempt < 3:
                    _pause(0.5, 0.1)
                    continue
                log("❌ Bouton Appliquer introuvable", "ERROR", wid)
                return False

            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            try:
                btn.click()
            except (ElementClickInterceptedException, StaleElementReferenceException):
                btn = _find_appliquer_button(driver)
                if not btn:
                    raise StaleElementReferenceException("Appliquer introuvable après rechargement")
                driver.execute_script("arguments[0].click();", btn)

            log("   ✓ Filtres appliqués", worker_id=wid)
            _wait_table(driver, 5, wid)
            # Le filtre recharge le tableau → la pagination repasse souvent à 10
            set_pagination_max(driver, wid)
            if wait_matricule:
                log(f"   ⏳ Attente des résultats pour {wait_matricule}...", worker_id=wid)
                _wait_for_matricule_rows(
                    driver, wait_matricule, timeout=_filter_wait_timeout(28), wid=wid
                )
            return True

        except StaleElementReferenceException:
            log(f"   ⚠️ Bouton Appliquer périmé (DOM rechargé) — retry {attempt}/3", "WARNING", wid)
            _pause(0.7, 0.12)
        except Exception as e:
            if attempt < 3 and "stale element" in str(e).lower():
                log(f"   ⚠️ Clic Appliquer — retry {attempt}/3", "WARNING", wid)
                _pause(0.7, 0.12)
                continue
            log(f"❌ Erreur clic Appliquer: {e}", "ERROR", wid)
            return False

    log("❌ Erreur clic Appliquer après 3 tentatives", "ERROR", wid)
    return False


# ───────────────────────────────────────────────────────────────────────────────
# UI VISUELLE (mode --headed)
# ───────────────────────────────────────────────────────────────────────────────

_UI_BANNER_ID = "quick-approve-ui-banner"


def _ui_enabled(driver) -> bool:
    return bool(getattr(driver, "_quick_approve_ui", False))


def _ui_enable(driver, enabled: bool = True):
    driver._quick_approve_ui = enabled


def _ui_clear_marks(driver):
    if not _ui_enabled(driver):
        return
    try:
        driver.execute_script(
            """
            document.querySelectorAll('tr[data-qa-highlight]').forEach(function(tr) {
                tr.style.outline = '';
                tr.style.background = '';
                tr.removeAttribute('data-qa-highlight');
            });
            """
        )
    except Exception:
        pass


def _ui_set_banner(driver, text: str, color: str = "#ff9800"):
    if not _ui_enabled(driver):
        return
    try:
        driver.execute_script(
            """
            var id = arguments[0], text = arguments[1], color = arguments[2];
            var b = document.getElementById(id);
            if (!b) {
                b = document.createElement('div');
                b.id = id;
                b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;padding:12px 16px;'
                    + 'font:bold 16px/1.3 sans-serif;color:#fff;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.35);';
                document.body.appendChild(b);
            }
            b.style.background = color;
            b.textContent = text;
            """,
            _UI_BANNER_ID,
            text,
            color,
        )
    except Exception:
        pass


def _ui_highlight_row(driver, row, mode: str = "active"):
    """Surligne une ligne du tableau (active / pending / done / dim)."""
    if not _ui_enabled(driver) or row is None:
        return
    styles = {
        "active": ("4px solid #ff9800", "#fff3e0"),
        "pending": ("2px solid #f44336", "#ffebee"),
        "done": ("2px solid #4caf50", "#e8f5e9"),
        "dim": ("1px solid #90caf9", "transparent"),
        "delete": ("4px solid #b71c1c", "#ffcdd2"),
        "warn": ("3px solid #fbc02d", "#fff9c4"),
        "dryrun": ("3px dashed #7b1fa2", "#f3e5f5"),
    }
    outline, bg = styles.get(mode, styles["active"])
    try:
        driver.execute_script(
            """
            arguments[0].setAttribute('data-qa-highlight', arguments[3]);
            arguments[0].style.outline = arguments[1];
            arguments[0].style.outlineOffset = '-2px';
            arguments[0].style.background = arguments[2];
            arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});
            """,
            row,
            outline,
            bg,
            mode,
        )
    except Exception:
        pass


def _ui_mark_all_rows(driver, matching: list):
    if not _ui_enabled(driver):
        return
    for _, row in matching:
        if _is_row_en_attente(row):
            _ui_highlight_row(driver, row, "pending")
        elif _is_row_approved(row):
            _ui_highlight_row(driver, row, "done")
        else:
            _ui_highlight_row(driver, row, "dim")


# ───────────────────────────────────────────────────────────────────────────────
# VÉHICULES - ACTIONS
# ───────────────────────────────────────────────────────────────────────────────

def _row_contains_matricule(row, matricule: str) -> bool:
    """Vérifie si une ligne contient le matricule (texte global ou colonne plaque)."""
    key = normalize_plate(matricule)
    if not key:
        return False
    try:
        if key in normalize_plate(row.text):
            return True
    except Exception:
        pass
    for cell in _row_table_cells(row):
        try:
            if key in normalize_plate(cell.text):
                return True
        except Exception:
            continue
    return False


def _scan_matching_rows(driver, matricule: str) -> list:
    """Scanne le tableau sans attendre (lecture instantanée). Tolère DOM AJAX / stale."""
    for attempt in range(1, 4):
        matching = []
        try:
            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            for i, row in enumerate(rows):
                try:
                    txt = (row.text or "").strip()
                except StaleElementReferenceException:
                    continue
                if not txt or ("aucun" in txt.lower() and "résultat" in txt.lower()):
                    continue
                try:
                    if _row_contains_matricule(row, matricule):
                        matching.append((i, row))
                except StaleElementReferenceException:
                    continue
            return matching
        except StaleElementReferenceException:
            time.sleep(0.35 * attempt)
    return []


def _wait_for_matricule_rows(driver, matricule: str, timeout: float = 18, wid: int = None) -> list:
    """
    Attend que le tableau AJAX affiche les lignes pour ce matricule.
    Ne s'arrête pas à la 1ère ligne : attend que le nombre de lignes se stabilise.
    """
    end = time.time() + timeout
    best: list = []
    last_count = -1
    stable_since = None
    min_stable_s = 2.8

    while time.time() < end:
        matching = _scan_matching_rows(driver, matricule)
        count = len(matching)
        if count > 0:
            best = matching
            if count == last_count:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= min_stable_s:
                    return best
            else:
                stable_since = time.time()
                last_count = count
        else:
            stable_since = None
            last_count = -1
        time.sleep(0.45)

    return best if best else _scan_matching_rows(driver, matricule)


def _log_matching_rows_summary(matching: list, matricule: str, wid: int = None, title: str = "Lignes"):
    if not matching:
        log(f"   📋 {title}: aucune ligne", worker_id=wid)
        return
    log(f"   📋 {title} ({len(matching)}):", worker_id=wid)
    for n, (_, row) in enumerate(matching, 1):
        partner = get_partner_from_row(row) or "?"
        statut = get_fleet_status_from_row(row)
        mark = "⏳" if _is_row_en_attente(row) else ("✅" if _is_row_approved(row) else "❔")
        log(f"      [{n}] {mark} Partenaire: {partner} | Statut: {statut}", worker_id=wid)


def find_vehicle_rows(driver, matricule: str, wid: int = None, wait_timeout: float = 10) -> list:
    """Trouve TOUTES les lignes du véhicule dans le tableau (avec attente AJAX)."""
    try:
        _wait_table(driver, 5, wid)
        if wait_timeout and wait_timeout > 0:
            matching = _wait_for_matricule_rows(driver, matricule, timeout=wait_timeout, wid=wid)
        else:
            matching = _scan_matching_rows(driver, matricule)
        
        if matching:
            total_visible = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
            log(
                f"   ✓ {len(matching)} ligne(s) trouvée(s) pour {matricule} "
                f"({total_visible} ligne(s) visibles dans le tableau)",
                worker_id=wid,
            )
            if total_visible > 0 and len(matching) == total_visible and total_visible <= 15:
                log(
                    "   ⚠️ Peu de lignes visibles — vérifiez que la pagination est bien à 500",
                    "WARNING",
                    wid,
                )
            _ui_mark_all_rows(driver, matching)
        else:
            total = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
            log(f"   ℹ️ {total} ligne(s) visible(s) dans le tableau, aucune ne correspond", worker_id=wid)
            log(f"❌ Véhicule {matricule} non trouvé", "ERROR", wid)
        
        return matching
        
    except Exception as e:
        log(f"❌ Erreur recherche véhicule: {e}", "ERROR", wid)
        return []

def find_vehicle_row(driver, matricule: str, wid: int = None):
    """Trouve la première ligne du véhicule (compatibilité)."""
    matching = find_vehicle_rows(driver, matricule, wid)
    return matching[0][1] if matching else None

def _row_table_cells(row) -> list:
    try:
        return row.find_elements(By.CSS_SELECTOR, "td")
    except Exception:
        return []


def get_partner_from_row(row) -> str:
    """Colonne Partenaire du tableau manage-fleet."""
    cells = _row_table_cells(row)
    if len(cells) >= 4:
        return (cells[3].text or "").strip()
    return ""


def get_fleet_status_from_row(row) -> str:
    """Statut flotte lu dans le tableau (badges + texte ligne) — pas de filtre UI."""
    candidates = []
    for cell in _row_table_cells(row)[-4:]:
        try:
            for el in cell.find_elements(By.CSS_SELECTOR, ".badge, span.badge, .label, span"):
                t = (el.text or "").strip()
                if t and len(t) < 40:
                    candidates.append(t)
            t = (cell.text or "").strip()
            if t and len(t) < 40:
                candidates.append(t)
        except Exception:
            continue
    try:
        row_low = (row.text or "").lower()
        if "en attente" in row_low or "en_attente" in row_low:
            return "EN ATTENTE"
        if "approuvé" in row_low or "approuve" in row_low or "approved" in row_low:
            return "Approuvé"
        if "rejet" in row_low:
            return "Rejeté"
    except Exception:
        pass
    for t in reversed(candidates):
        tl = t.lower()
        if "attente" in tl or "approuv" in tl or "rejet" in tl:
            return t
    if candidates:
        return candidates[-1]
    return "Inconnu"


def _is_row_en_attente(row) -> bool:
    s = get_fleet_status_from_row(row).lower()
    if "en attente" in s or "en_attente" in s or s.strip() == "attente":
        return True
    try:
        low = row.text.lower()
        return "en attente" in low and "approuv" not in low
    except Exception:
        return False


def _is_row_approved(row) -> bool:
    s = get_fleet_status_from_row(row).lower()
    return "approuvé" in s or "approuve" in s or "approved" in s


def _first_pending_in_table(matching: list, matricule: str, completed: set):
    """
    Parcourt le tableau dans l'ordre (ligne 1 → N).
    Retourne la première ligne EN ATTENTE pas encore traitée avec succès.
    """
    for pos, (tbl_idx, row) in enumerate(matching, 1):
        partner = get_partner_from_row(row)
        row_key = _row_identity_key(row, matricule)
        statut = get_fleet_status_from_row(row)
        if not _is_row_en_attente(row):
            continue
        if row_key in completed:
            continue
        return pos, tbl_idx, row, partner, row_key, statut
    return None


def get_document_status_from_row(row) -> str:
    """Alias — statut affiché sur la ligne (flotte dans manage-fleet)."""
    return get_fleet_status_from_row(row)


def _row_fingerprint(matricule: str, partner: str) -> str:
    """Compat — préférer _row_identity_key pour distinguer les doublons même partenaire."""
    return f"{normalize_plate(matricule)}|{(partner or '').strip().lower()}"


_DOC_UUID_RE = re.compile(r"/document/([0-9a-fA-F-]{36})", re.I)


def get_row_doc_href(row) -> str:
    """Lien document propre à cette ligne (évite d'ouvrir le mauvais doc en cas de doublon)."""
    try:
        for sel in (
            "a[href*='manage-fleet/document/']",
            "a[href*='/document/']",
            "[href*='manage-fleet/document/']",
        ):
            for anchor in row.find_elements(By.CSS_SELECTOR, sel):
                for attr in ("href", "data-href", "ng-reflect-router-link"):
                    href = (anchor.get_attribute(attr) or "").strip()
                    if href and "document" in href.lower():
                        if not href.startswith("http"):
                            href = urljoin(BASE_URL, href.lstrip("/"))
                        return href
        onclick = row.get_attribute("onclick") or ""
        m = _DOC_UUID_RE.search(onclick)
        if m:
            return f"{BASE_URL}/manage-fleet/document/{m.group(1)}"
    except Exception:
        pass
    return ""


def get_row_doc_uuid(row) -> str:
    """UUID document extrait du lien de la ligne (pas du HTML global — évite faux UUID partagés)."""
    href = get_row_doc_href(row)
    if href:
        m = _DOC_UUID_RE.search(href)
        if m:
            return m.group(1).lower()
    return ""


def _row_identity_key(row, matricule: str) -> str:
    """
    Identifiant unique d'une LIGNE du tableau.
    Priorité : UUID document (chaque doublon même partenaire a son propre doc).
    """
    doc_id = get_row_doc_uuid(row)
    if doc_id:
        return f"{normalize_plate(matricule)}|doc:{doc_id}"
    partner = (get_partner_from_row(row) or "").strip().lower()
    parts = [normalize_plate(matricule), partner]
    for cell in _row_table_cells(row):
        t = (cell.text or "").strip()
        if not t or len(t) >= 80:
            continue
        tl = t.lower()
        if "en attente" in tl or "approuvé" in tl or "approuve" in tl or "approved" in tl:
            continue
        parts.append(tl)
    return "||".join(parts)


def find_vehicle_row_by_key(
    driver,
    matricule: str,
    row_key: str,
    wid: int = None,
    wait_timeout: float = 12,
):
    """Retrouve la MÊME ligne après rechargement (clé doc UUID ou empreinte)."""
    if wait_timeout and wait_timeout > 0:
        matching = find_vehicle_rows(driver, matricule, wid, wait_timeout=wait_timeout)
    else:
        _wait_table(driver, 3, wid)
        matching = _scan_matching_rows(driver, matricule)
    for pos, (_, row) in enumerate(matching, 1):
        if _row_identity_key(row, matricule) == row_key:
            log(f"   ✓ Même ligne retrouvée (position {pos} dans le tableau)", worker_id=wid)
            return row
    log(
        f"   ⚠️ Ligne introuvable pour clé …{row_key[-48:]} ({len(matching)} lignes)",
        "WARNING",
        wid,
    )
    return None


def find_vehicle_row_by_partner(driver, matricule: str, partner_name: str, wid: int = None):
    """Fallback legacy — ne pas utiliser pour les doublons (prend la 1ère ligne du partenaire)."""
    if not partner_name:
        return find_vehicle_row(driver, matricule, wid)
    matching = find_vehicle_rows(driver, matricule, wid)
    partner_norm = partner_name.strip().lower()
    for _, row in matching:
        p = get_partner_from_row(row).strip().lower()
        if p == partner_norm:
            return row
    for _, row in matching:
        p = get_partner_from_row(row).strip().lower()
        if partner_norm in p or p in partner_norm:
            return row
    log(f"   ⚠️ Partenaire « {partner_name} » introuvable dans le tableau", "WARNING", wid)
    return None

def click_menu_three_dots(driver, row, wid: int = None):
    """Clique sur le menu 3 points (⋮) de la ligne."""
    try:
        buttons = row.find_elements(By.CSS_SELECTOR, "button")
        if buttons:
            driver.execute_script("arguments[0].click();", buttons[-1])
            log("   ✓ Menu ouvert", worker_id=wid)
            return True
    except Exception:
        pass
    
    for sel in ["button svg[data-icon='ellipsis-v']", "button svg.ellipsis", ".action-btn"]:
        try:
            btn = row.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].click();", btn)
            return True
        except:
            continue
    
    log("❌ Impossible d'ouvrir le menu", "ERROR", wid)
    return False

def click_approve_in_menu(driver, wid: int = None):
    """Clique sur 'Approuver' dans le menu dropdown."""
    try:
        approve_option = None
        
        menu_items = driver.find_elements(
            By.XPATH,
            "//div[contains(@class, 'dropdown-menu')]//a | //div[contains(@class, 'dropdown-menu')]//li | //div[contains(@class, 'dropdown-menu')]//button | //ul[contains(@class, 'menu')]//a"
        )
        for item in menu_items:
            text = item.text or item.get_attribute("textContent") or ""
            if "approuver" in text.lower() or "approuv" in text.strip().lower():
                approve_option = item
                break
        
        if not approve_option:
            wait = WebDriverWait(driver, 1)
            for sel in [
                "//a[contains(., 'Approuver')]",
                "//button[contains(., 'Approuver')]",
                "//li[contains(., 'Approuver')]",
            ]:
                try:
                    approve_option = wait.until(EC.element_to_be_clickable((By.XPATH, sel)))
                    break
                except:
                    continue
        
        if approve_option:
            try:
                approve_option.click()
            except Exception:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", approve_option)
                time.sleep(0.3)
                driver.execute_script("arguments[0].click();", approve_option)
            log("   ✓ Approuver cliqué", worker_id=wid)
            time.sleep(0.5)
            return True
        
        log("   ❌ Option Approuver non trouvée dans le menu", "ERROR", wid)
        return False
        
    except Exception as e:
        log(f"❌ Option Approuver non trouvée: {e}", "ERROR", wid)
        return False

def open_document_page(driver, row, wid: int = None, doc_link: str = None):
    """Ouvre la page document (lien JSON direct si dispo, sinon clic sur la ligne)."""
    try:
        if doc_link:
            full_url = doc_link if doc_link.startswith("http") else urljoin(BASE_URL, doc_link)
            _safe_get(driver, full_url, wid)
            log("   ✓ Page document (lien direct)", worker_id=wid)
            if _wait_table(driver, 5, wid):
                return True
            return True
        
        doc_icon = row.find_element(By.CSS_SELECTOR, 
            "svg[data-icon='file-alt'], svg.file-icon, .document-icon, a[href*='document']")
        
        try:
            href = doc_icon.get_attribute("href")
            if href:
                full_url = urljoin(BASE_URL, href)
                _safe_get(driver, full_url, wid)
                log(f"   ✓ Page document ouverte", worker_id=wid)
                _wait_table(driver, 5, wid)
                return True
        except:
            pass
        
        doc_icon.click()
        _wait_table(driver, 4, wid)
        return True
        
    except Exception as e:
        log(f"❌ Icône document non trouvée: {e}", "ERROR", wid)
        return False

def _doc_status_from_row(row) -> str:
    try:
        return row.find_element(By.CSS_SELECTOR, ".badge").text.strip()
    except Exception:
        return (row.text or "").strip()[:80]


def _find_carte_grise_row(driver):
    """Retrouve la ligne Carte grise (tolère changements DOM après upload)."""
    for row in driver.find_elements(By.CSS_SELECTOR, "table tbody tr"):
        low = (row.text or "").lower()
        if "carte grise" in low or "carte grise" in low.replace("é", "e"):
            return row
    for row in driver.find_elements(By.CSS_SELECTOR, "table tbody tr"):
        if "grise" in (row.text or "").lower():
            return row
    return None


def _click_approve_document_button(driver, row, wid: int = None) -> bool:
    """
    Clique sur Approuver dans la ligne document (button, lien, ou texte imbriqué).
    Petit délai + retries : le bouton apparaît souvent après le badge de statut.
    """
    log("   ⏳ Attente chargement bouton Approuver (document)...", worker_id=wid)
    _pause(2.0, 0.35)

    def _try_click(current_row) -> bool:
        selectors = [
            ".//button[contains(translate(., 'APPROUVER', 'approuver'), 'approuver')]",
            ".//a[contains(translate(., 'APPROUVER', 'approuver'), 'approuver')]",
            ".//*[self::button or self::a][contains(@class, 'btn')]",
        ]
        for xpath in selectors:
            try:
                for el in current_row.find_elements(By.XPATH, xpath):
                    label = (
                        el.text or el.get_attribute("aria-label") or el.get_attribute("title") or ""
                    ).lower()
                    if "approuver" in label or "approve" in label:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        try:
                            el.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", el)
                        log("   ✅ Document approuvé (clic Approuver)!", worker_id=wid)
                        _pause(0.5, 0.08)
                        return True
            except StaleElementReferenceException:
                return False
            except Exception:
                continue

        for btn in current_row.find_elements(By.CSS_SELECTOR, "button, a.btn, a[class*='btn']"):
            label = (btn.text or btn.get_attribute("aria-label") or "").lower()
            if "approuver" in label or "approve" in label:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    try:
                        btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btn)
                    log("   ✅ Document approuvé (clic Approuver)!", worker_id=wid)
                    _pause(0.5, 0.08)
                    return True
                except StaleElementReferenceException:
                    return False
                except Exception:
                    continue
        return False

    for attempt in range(1, 4):
        if attempt > 1:
            log(f"   🔘 Recherche Approuver — tentative {attempt}/3...", worker_id=wid)
            _pause(1.2, 0.2)
            row = _find_carte_grise_row(driver) or row
        else:
            log("   🔘 Recherche du bouton Approuver (document)...", worker_id=wid)

        if _try_click(row):
            return True

    log("   ❌ Bouton Approuver introuvable dans la ligne Carte grise", "ERROR", wid)
    return False


def _needs_document_upload(status: str) -> bool:
    s = status.lower()
    return any(k in s for k in ("non téléchargé", "non telecharge", "not downloaded", "non telecharge"))


def _needs_document_approve_click(status: str) -> bool:
    s = status.lower()
    if any(k in s for k in ("approuvé", "approved")):
        return False
    return any(
        k in s
        for k in (
            "en attente d'approbation",
            "en attente d approbation",
            "en attente",
            "attente",
            "approval pending",
            "pending",
        )
    )


def approve_document_on_page(driver, image_index: dict = None, matricule: str = None, wid: int = None) -> tuple:
    """
    Approuve la carte grise sur la page document (upload si nécessaire).
    Returns: (success, uploaded, already_approved)
    """
    uploaded = False

    try:
        _wait_table(driver, 6, wid)
        row = _find_carte_grise_row(driver)
        if not row:
            log("   ❌ Ligne Carte grise non trouvée", "ERROR", wid)
            return False, uploaded, False

        log("   ✓ Ligne Carte grise trouvée", worker_id=wid)
        status = _doc_status_from_row(row)
        log(f"   📛 Statut document: {status}", worker_id=wid)

        if status.lower() in ("approuvé", "approved"):
            log("   ⏩ Document déjà approuvé", worker_id=wid)
            return True, False, True

        # 1) Upload si non téléchargé
        if _needs_document_upload(status):
            log("   ⚠️ Carte grise non téléchargée — upload requis", "WARNING", wid)
            if image_index and matricule:
                image_path = find_image_for_matricule(matricule, image_index)
                if image_path:
                    log(f"   📷 Image: {image_path.name}", worker_id=wid)
                    doc_uuid = driver.current_url.rstrip("/").split("/")[-1]
                    success, _, skipped = upload_carte_grise_full(driver, image_path, matricule, wid)
                    if success:
                        uploaded = not skipped
                        if uploaded:
                            log("   ✅ Carte grise uploadée!", worker_id=wid)
                        doc_url = f"{BASE_URL}/manage-fleet/document/{doc_uuid}"
                        _safe_get(driver, doc_url, wid)
                        _wait_table(driver, 8, wid)
                        time.sleep(0.5)
                        row = _find_carte_grise_row(driver)
                        if not row:
                            log("   ⚠️ Ligne Carte grise absente après upload — rechargement", "WARNING", wid)
                            _safe_get(driver, doc_url, wid)
                            _wait_table(driver, 8, wid)
                            row = _find_carte_grise_row(driver)
                        if row:
                            status = _doc_status_from_row(row)
                            log(f"   📛 Statut après upload: {status}", worker_id=wid)
                    else:
                        log("   ❌ Échec upload carte grise", "ERROR", wid)
                        return False, uploaded, False
                else:
                    log(f"   ⚠️ Aucune image trouvée pour {matricule}", "WARNING", wid)
                    return False, uploaded, False

        # 2) Clic Approuver si « En attente d'approbation » (ou équivalent)
        row = _find_carte_grise_row(driver) or row
        status = _doc_status_from_row(row)
        if status.lower() in ("approuvé", "approved"):
            return True, uploaded, True

        if _needs_document_approve_click(status):
            log(f"   ▶️ Statut « {status} » → clic Approuver", worker_id=wid)
            if _click_approve_document_button(driver, row, wid):
                return True, uploaded, False

        log(f"   ❌ Document non approuvé (statut final: {status})", "ERROR", wid)
        return False, uploaded, False

    except Exception as e:
        log(f"❌ Erreur approbation document: {e}", "ERROR", wid)
        return False, uploaded, False

# ───────────────────────────────────────────────────────────────────────────────
# WORKFLOW POUR UN VÉHICULE
# ───────────────────────────────────────────────────────────────────────────────

def _fleet_row_document_approved(row) -> bool:
    """
    Sur manage-fleet : document déjà approuvé mais flotte encore EN ATTENTE.
    (badge Approuvé + badge En attente sur la même ligne)
    """
    if not _is_row_en_attente(row):
        return False
    try:
        badges = []
        for el in row.find_elements(By.CSS_SELECTOR, ".badge, span.badge, .label"):
            t = (el.text or "").strip()
            if t and len(t) < 50:
                badges.append(t.lower())
        has_approved = any("approuv" in t and "attente" not in t for t in badges)
        has_pending = any("attente" in t for t in badges)
        return has_approved and has_pending
    except Exception:
        return False


def _approve_fleet_for_row(driver, row, wid: int = None) -> bool:
    """Clic menu ⋮ → Approuver sur la ligne manage-fleet."""
    if _is_row_approved(row):
        log("   ✅ Flotte déjà approuvée!", worker_id=wid)
        return True
    if _is_row_en_attente(row):
        if click_menu_three_dots(driver, row, wid):
            if click_approve_in_menu(driver, wid):
                log("   ✅ Flotte approuvée!", worker_id=wid)
                return True
    return False


def _return_to_manage_fleet_table(driver, matricule: str, wid: int = None) -> bool:
    """
    Retour au tableau manage-fleet filtré.
    Pas de driver.back() (SPA + lien direct doc → stale element / mauvaise page).
    """
    url = driver.current_url or ""
    if "/document" in url:
        log("   🔄 Retour tableau via manage-fleet (lien direct doc — pas de back())", worker_id=wid)
        return _navigate_and_filter(driver, matricule, wid)

    if "manage-fleet" in url:
        _wait_table(driver, 4, wid)
        try:
            matching = _scan_matching_rows(driver, matricule)
            if matching:
                set_pagination_max(driver, wid, max_wait=_pagination_timing()["default_max_wait"] * 0.45)
                matching = _scan_matching_rows(driver, matricule)
                if matching:
                    log(
                        f"   ✓ Tableau déjà ouvert ({len(matching)} ligne(s)) — pas de refiltre",
                        worker_id=wid,
                    )
                    return True
        except StaleElementReferenceException:
            log("   ⚠️ Tableau stale — rechargement filtres", "WARNING", wid)

    log("   🔄 Rechargement manage-fleet + filtres...", worker_id=wid)
    return _navigate_and_filter(driver, matricule, wid)


def _navigate_and_filter(driver, matricule: str, wid: int = None, with_statut: bool = False) -> bool:
    """Navigue vers manage-fleet, filtre matricule uniquement (statut lu ligne par ligne)."""
    if with_statut:
        log("   ℹ️ Filtre statut UI ignoré — parcours tableau en Python", worker_id=wid)
    if not _safe_get(driver, MANAGE_FLEET_URL, wid):
        log("   ❌ Impossible d'ouvrir manage-fleet", "ERROR", wid)
        return False
    if not _wait_table(driver, 10, wid):
        time.sleep(0.5)

    set_pagination_max(driver, wid)

    if not open_filters(driver, wid):
        return False
    
    if not set_filter_matricule(driver, matricule, wid):
        return False
    
    return apply_filters(driver, wid, wait_matricule=matricule)


def _row_status_counts(matching: list) -> tuple:
    en_attente = already_ok = 0
    for _, row in matching:
        if _is_row_en_attente(row):
            en_attente += 1
        elif _is_row_approved(row):
            already_ok += 1
    return en_attente, already_ok


def _log_table_walk(matching: list, matricule: str, wid: int = None):
    """Affiche chaque ligne du tableau dans l'ordre (diagnostic)."""
    log(f"   📊 Parcours tableau — {len(matching)} ligne(s) pour {matricule}:", worker_id=wid)
    for pos, (_, row) in enumerate(matching, 1):
        partner = get_partner_from_row(row) or "?"
        statut = get_fleet_status_from_row(row)
        doc_tag = ""
        doc_id = get_row_doc_uuid(row)
        if doc_id:
            doc_tag = f" | doc …{doc_id[-8:]}"
        if _is_row_en_attente(row):
            mark, action = "⏳", "→ À TRAITER"
        elif _is_row_approved(row):
            mark, action = "✅", "→ ignoré (déjà approuvé)"
        else:
            mark, action = "❔", "→ ignoré"
        log(f"      Ligne {pos}: {mark} {partner} | {statut}{doc_tag} {action}", worker_id=wid)


def _approve_single_row(
    driver,
    row,
    matricule: str,
    image_index: dict,
    wid: int = None,
    vehicle_meta: dict = None,
    partner_name: str = None,
    row_key: str = None,
) -> dict:
    """
    Approuve UNE ligne (document + flotte). row_key verrouille la ligne exacte (UUID doc).
    """
    result = {
        "document_approved": False,
        "fleet_approved": False,
        "carte_grise_uploaded": False,
        "partner": partner_name or get_partner_from_row(row),
        "errors": [],
    }
    partner_name = result["partner"]
    if row_key is None:
        row_key = _row_identity_key(row, matricule)
    doc_uuid = get_row_doc_uuid(row)
    statut_row = get_fleet_status_from_row(row)
    if doc_uuid:
        log(
            f"   🔑 Ligne verrouillée — doc …{doc_uuid[-8:]} | {partner_name} | {statut_row}",
            worker_id=wid,
        )
    elif partner_name:
        log(f"   👤 Partenaire ciblé: {partner_name} | Statut: {statut_row}", worker_id=wid)
    _ui_clear_marks(driver)
    _ui_highlight_row(driver, row, "active")
    _ui_set_banner(
        driver,
        f"🎯 EN COURS — {matricule} — {partner_name or 'partenaire ?'} ({statut_row})",
        "#ff9800",
    )

    if _is_row_en_attente(row) and _fleet_row_document_approved(row):
        log(
            "   ⏩ Doc déjà approuvé (badges ligne) — flotte directe, sans page document",
            worker_id=wid,
        )
        result["document_approved"] = True
        if _approve_fleet_for_row(driver, row, wid):
            result["fleet_approved"] = True
        else:
            result["errors"].append("Échec approbation flotte (doc déjà OK, menu tableau)")
        return result

    doc_link = get_row_doc_href(row)
    if not doc_link and vehicle_meta and not doc_uuid:
        doc_link = vehicle_meta.get("doc_link")
    if not open_document_page(driver, row, wid, doc_link=doc_link):
        result["errors"].append("Échec ouverture page document")
        return result
    
    doc_ok, uploaded, already_approved = approve_document_on_page(driver, image_index, matricule, wid)
    result["carte_grise_uploaded"] = uploaded
    
    if doc_ok:
        result["document_approved"] = True
        if uploaded:
            log("   ✅ Étape 1: CG uploadée + doc approuvé", worker_id=wid)
        elif already_approved:
            log("   ✅ Étape 1: Doc déjà approuvé — passage à la flotte", worker_id=wid)
            page_uuid = ""
            try:
                m = _DOC_UUID_RE.search(driver.current_url or "")
                if m:
                    page_uuid = m.group(1).lower()
                    row_key = f"{normalize_plate(matricule)}|doc:{page_uuid}"
            except Exception:
                pass
        else:
            log("   ✅ Étape 1: Doc approuvé", worker_id=wid)
    else:
        result["errors"].append("Échec approbation document")

    # Étape 2 : retour tableau + menu ⋮ Approuver (obligatoire si flotte EN ATTENTE)
    log("   🔄 Retour manage-fleet pour approbation flotte...", worker_id=wid)
    if not _return_to_manage_fleet_table(driver, matricule, wid):
        result["errors"].append("Échec retour manage-fleet")
        return result

    row2 = find_vehicle_row_by_key(driver, matricule, row_key, wid, wait_timeout=0)
    if not row2:
        row2 = find_vehicle_row_by_key(driver, matricule, row_key, wid, wait_timeout=10)
    if not row2:
        result["errors"].append(
            f"Ligne non retrouvée après doc (clé …{row_key[-40:]}, partenaire: {partner_name or '?'})"
        )
        return result

    if _row_identity_key(row2, matricule) != row_key:
        result["errors"].append("Mismatch ligne après rechargement tableau")
        return result

    statut_final = get_fleet_status_from_row(row2)
    if _approve_fleet_for_row(driver, row2, wid):
        result["fleet_approved"] = True
    elif not _is_row_en_attente(row2) and not _is_row_approved(row2):
        result["errors"].append(f"Statut flotte inattendu: {statut_final}")
    elif _is_row_en_attente(row2):
        result["errors"].append("Échec approbation flotte (menu ⋮)")

    return result


def quick_approve_one(
    driver,
    matricule: str,
    image_index: dict,
    wid: int = None,
    vehicle_meta: dict = None,
    show_ui: bool = False,
) -> dict:
    """
    Workflow complet d'approbation rapide d'un véhicule SANS attribution.
    Carte grise pour TOUS les types.
    Gère les DOUBLONS : boucle tant qu'il reste des lignes EN ATTENTE.
    """
    results = {
        "matricule": matricule,
        "success": False,
        "document_approved": False,
        "fleet_approved": False,
        "carte_grise_uploaded": False,
        "duplicates_found": 0,
        "duplicates_approved": 0,
        "errors": []
    }
    
    try:
        _ui_enable(driver, show_ui)
        log(f"\n{'='*60}", worker_id=wid)
        log(f"🚗 APPROBATION: {matricule}", worker_id=wid)
        log(f"{'='*60}", worker_id=wid)
        _ui_set_banner(driver, f"🚗 Approbation — {matricule}", "#1976d2")
        
        if vehicle_meta is None:
            vehicle_meta = find_vehicle_in_json(matricule) or {}
        
        json_pending = count_json_en_attente_for_matricule(matricule)
        if json_pending:
            log(f"   📇 JSON: {json_pending} entrée(s) EN ATTENTE (référence)", worker_id=wid)

        if not _navigate_and_filter(driver, matricule, wid):
            log("   🔄 Nouvelle tentative navigation...", worker_id=wid)
            if not _navigate_and_filter(driver, matricule, wid):
                results["errors"].append("Échec filtres / timeout manage-fleet")
                return results

        matching = find_vehicle_rows(driver, matricule, wid, wait_timeout=_filter_wait_timeout(18))
        if not matching:
            results["errors"].append("Véhicule non trouvé")
            return results

        en_attente_count, already_ok_count = _row_status_counts(matching)
        total_rows = len(matching)
        results["duplicates_found"] = total_rows

        if total_rows > 1:
            log(
                f"   🔁 {total_rows} lignes dans le tableau "
                f"({en_attente_count} EN ATTENTE, {already_ok_count} approuvé)",
                worker_id=wid,
            )

        _log_table_walk(matching, matricule, wid)

        if en_attente_count == 0:
            if json_pending > 0:
                results["errors"].append(
                    f"{json_pending} EN ATTENTE dans le JSON mais aucune ligne EN ATTENTE dans le tableau"
                )
                log("   ❌ Lignes EN ATTENTE non détectées au parcours tableau", "ERROR", wid)
                return results
            log("   ✅ Toutes les lignes déjà APPROUVÉES!", worker_id=wid)
            _ui_set_banner(driver, f"✅ Déjà approuvé — {matricule}", "#4caf50")
            results["fleet_approved"] = True
            results["document_approved"] = True
            results["duplicates_approved"] = total_rows
            results["success"] = True
            return results

        # --- Parcours tableau : ligne 1 → N, traiter chaque EN ATTENTE puis recharger ---
        completed_row_keys = set()
        failed_row_keys = {}
        newly_approved = 0
        max_passes = max(total_rows * 4, 12)
        stuck_passes = 0

        for pass_num in range(1, max_passes + 1):
            if not _navigate_and_filter(driver, matricule, wid):
                results["errors"].append(f"Échec rechargement tableau (passe {pass_num})")
                break

            matching = find_vehicle_rows(driver, matricule, wid, wait_timeout=_filter_wait_timeout(18))
            if not matching:
                break

            total_rows = len(matching)
            _log_table_walk(matching, matricule, wid)

            target = _first_pending_in_table(matching, matricule, completed_row_keys)
            if not target:
                en_left, _ = _row_status_counts(matching)
                if en_left == 0:
                    log("   ✅ Parcours terminé — plus aucune ligne EN ATTENTE", worker_id=wid)
                    break
                stuck_passes += 1
                if stuck_passes >= 3:
                    log(
                        f"   ⚠️ {en_left} ligne(s) EN ATTENTE restante(s) mais non traitables",
                        "WARNING",
                        wid,
                    )
                    for pos, (_, row) in enumerate(matching, 1):
                        if _is_row_en_attente(row):
                            p = get_partner_from_row(row)
                            results["errors"].append(f"Bloqué: ligne {pos} {p}")
                    break
                continue

            stuck_passes = 0
            pos, tbl_idx, target_row, partner, row_key, statut = target
            en_left, _ = _row_status_counts(matching)

            doc_hint = ""
            _doc_id = get_row_doc_uuid(target_row)
            if _doc_id:
                doc_hint = f" — doc …{_doc_id[-8:]}"
            log(
                f"   🎯 Prochaine cible: ligne {pos}/{total_rows} — 1ère EN ATTENTE "
                f"({partner}){doc_hint}",
                worker_id=wid,
            )
            log(
                f"\n   ▶️ PASSE {pass_num} — TRAITEMENT ligne {pos}/{total_rows} "
                f"— {partner or '?'} — {statut} ({en_left} EN ATTENTE restante(s))",
                worker_id=wid,
            )
            _ui_mark_all_rows(driver, matching)
            _ui_highlight_row(driver, target_row, "active")
            _ui_set_banner(
                driver,
                f"🎯 {matricule} — ligne {pos}/{total_rows} — {partner or '?'}",
                "#ff9800",
            )

            try:
                row_result = _approve_single_row(
                    driver,
                    target_row,
                    matricule,
                    image_index,
                    wid,
                    vehicle_meta=vehicle_meta,
                    partner_name=partner,
                    row_key=row_key,
                )
            except StaleElementReferenceException as e:
                log(
                    f"   ⚠️ DOM périmé sur ligne {pos} ({partner}) — retry prochaine passe",
                    "WARNING",
                    wid,
                )
                results["errors"].append(f"Stale ligne {pos}: {partner}")
                failed_row_keys[row_key] = failed_row_keys.get(row_key, 0) + 1
                if not _navigate_and_filter(driver, matricule, wid):
                    results["errors"].append("Échec recovery après stale")
                continue

            if row_result["document_approved"]:
                results["document_approved"] = True
            if row_result["carte_grise_uploaded"]:
                results["carte_grise_uploaded"] = True

            full_ok = row_result["document_approved"] and row_result["fleet_approved"]
            if full_ok:
                if _navigate_and_filter(driver, matricule, wid):
                    verify = find_vehicle_row_by_key(driver, matricule, row_key, wid)
                    if verify and not _is_row_approved(verify):
                        full_ok = False
                        result_msg = "doc/flotte OK mais ligne encore EN ATTENTE au rechargement"
                        row_result["errors"].append(result_msg)
                        log(f"   ⚠️ {result_msg} (ligne {pos})", "WARNING", wid)
                if full_ok:
                    completed_row_keys.add(row_key)
                    newly_approved += 1
                    results["fleet_approved"] = True
                    log(
                        f"   ✅ Ligne {pos}/{total_rows} terminée — {partner} "
                        f"({already_ok_count + newly_approved} OK au total)",
                        worker_id=wid,
                    )
            if not full_ok:
                failed_row_keys[row_key] = failed_row_keys.get(row_key, 0) + 1
                log(
                    f"   ⚠️ Ligne {pos} incomplète ({partner}): "
                    f"{', '.join(row_result['errors'][:2])}",
                    "WARNING",
                    wid,
                )
                if failed_row_keys[row_key] >= 3:
                    log(
                        f"   ⏭️ Abandon temporaire clé …{row_key[-32:]} après 3 échecs",
                        "WARNING",
                        wid,
                    )
                    completed_row_keys.add(row_key)

            results["errors"].extend(row_result["errors"])
            # Recharger le tableau depuis la ligne 1 pour la prochaine EN ATTENTE

        # Vérification finale : re-parcourir tout le tableau
        final_en_attente = -1
        final_ok = already_ok_count
        if _navigate_and_filter(driver, matricule, wid):
            final_matching = find_vehicle_rows(driver, matricule, wid, wait_timeout=_filter_wait_timeout(18))
            _log_table_walk(final_matching, matricule, wid)
            final_en_attente, final_ok = _row_status_counts(final_matching)
            total_rows = len(final_matching)
        
        results["duplicates_approved"] = already_ok_count + newly_approved
        
        if final_en_attente == 0:
            results["success"] = True
            results["fleet_approved"] = True
            results["document_approved"] = True
            log(
                f"🎉 TOUTES LES LIGNES APPROUVÉES! ({results['duplicates_approved']}/{total_rows})",
                worker_id=wid,
            )
            _ui_set_banner(driver, f"🎉 Terminé — {matricule} ({total_rows} ligne(s))", "#4caf50")
            update_vehicle_status_in_json(matricule, new_doc_status="Approuvé", new_fleet_status="Approuvé")
        elif newly_approved > 0:
            results["success"] = False
            results["fleet_approved"] = newly_approved > 0
            log(
                f"⚠️ Partiel: {newly_approved} nouvelle(s) ligne(s), "
                f"{final_en_attente if final_en_attente >= 0 else '?'} encore EN ATTENTE",
                "WARNING",
                wid,
            )
        else:
            log("❌ ÉCHEC APPROBATION — aucune ligne entièrement validée", "ERROR", wid)
        
    except StaleElementReferenceException as e:
        log(f"⚠️ DOM périmé (stale) — partiel conservé, relancez si besoin: {e}", "WARNING", wid)
        results["errors"].append(f"Stale: {e}")
    except Exception as e:
        log(f"❌ Erreur inattendue: {e}", "ERROR", wid)
        log(traceback.format_exc(), "ERROR", wid)
        results["errors"].append(str(e))
    
    return results

# ───────────────────────────────────────────────────────────────────────────────
# WORKER (un thread = un Chrome = une part de matricules)
# ───────────────────────────────────────────────────────────────────────────────

def run_worker(
    worker_id: int,
    matricules: list,
    headed: bool,
    base_port: int,
    image_index: dict,
    matricule_meta: dict = None,
) -> list:
    """
    Un worker traite sa liste de matricules avec sa propre instance Chrome.
    Retourne la liste des résultats.
    """
    wid = worker_id
    debug_port = base_port + worker_id
    total = len(matricules)
    
    log(f"\n{'━'*60}", worker_id=wid)
    log(f"🚀 WORKER {wid} DÉMARRÉ — {total} véhicule(s) — Port {debug_port}", worker_id=wid)
    log(f"{'━'*60}", worker_id=wid)
    
    worker_results = []
    driver = None
    DRIVER_RESTART_INTERVAL = 120
    meta_index = matricule_meta or {}
    
    try:
        driver = setup_driver(headed=headed, debug_port=debug_port, wid=wid)
        
        if not admin_login(driver, wid):
            log(f"❌ Worker {wid}: connexion échouée, arrêt", "ERROR", wid)
            for mat in matricules:
                worker_results.append({
                    "matricule": mat, "success": False, "document_approved": False,
                    "fleet_approved": False, "carte_grise_uploaded": False,
                    "errors": ["Connexion échouée"]
                })
            return worker_results
        
        for i, matricule in enumerate(matricules, 1):
            log(f"\n{'─'*50}", worker_id=wid)
            log(f"📌 [{i}/{total}] {matricule}", worker_id=wid)
            log(f"{'─'*50}", worker_id=wid)
            
            # Redémarrage périodique Chrome
            if i > 1 and (i - 1) % DRIVER_RESTART_INTERVAL == 0:
                log(f"🔄 Redémarrage Chrome (après {i-1} véhicules)...", worker_id=wid)
                try:
                    driver.quit()
                except Exception:
                    pass
                _pause(3.0, 0.6)
                try:
                    profile_dir = f"/tmp/chrome_profile_{debug_port}"
                    if os.path.exists(profile_dir):
                        shutil.rmtree(profile_dir, ignore_errors=True)
                except Exception:
                    pass
                restart_ok = False
                for attempt in range(1, 4):
                    try:
                        driver = setup_driver(headed=headed, debug_port=debug_port, wid=wid)
                        if admin_login(driver, wid):
                            log(f"   ✅ Chrome redémarré", worker_id=wid)
                            restart_ok = True
                            break
                        else:
                            try: driver.quit()
                            except: pass
                            _pause(5.0, 1.0)
                    except Exception:
                        _pause(5.0, 1.0)
                if not restart_ok:
                    log(f"❌ Chrome mort, arrêt du worker", "ERROR", wid)
                    for mat in matricules[i-1:]:
                        worker_results.append({
                            "matricule": mat, "success": False, "document_approved": False,
                            "fleet_approved": False, "carte_grise_uploaded": False,
                            "errors": ["Driver crash"]
                        })
                    break
            
            try:
                vmeta = meta_index.get(normalize_plate(matricule))
                result = quick_approve_one(
                    driver, matricule, image_index, wid,
                    vehicle_meta=vmeta, show_ui=headed,
                )
                worker_results.append(result)
            except Exception as e:
                log(f"❌ CRASH: {matricule}: {e}", "ERROR", wid)
                worker_results.append({
                    "matricule": matricule, "success": False, "document_approved": False,
                    "fleet_approved": False, "carte_grise_uploaded": False,
                    "errors": [f"CRASH: {str(e)}"]
                })
            
            if i < total:
                _pause(0.12 if not _FAST_MODE else 0.0)
        
    except Exception as e:
        log(f"❌ Worker {wid} erreur fatale: {e}", "ERROR", wid)
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        try:
            profile_dir = f"/tmp/chrome_profile_{debug_port}"
            if os.path.exists(profile_dir):
                shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass
        log(f"🏁 Worker {wid} terminé — {sum(1 for r in worker_results if r['success'])}/{total} OK", worker_id=wid)
    
    return worker_results

# ───────────────────────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────────────────────

def split_list(lst: list, n: int) -> list:
    """Découpe une liste en N parts à peu près égales."""
    k, m = divmod(len(lst), n)
    return [lst[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]


def load_matricules_from_file(path: str) -> tuple[list, int]:
    """Charge un fichier matricules (1 par ligne), dédoublonne en conservant l'ordre."""
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    seen = set()
    matricules = []
    for plate in lines:
        key = normalize_plate(plate)
        if key and key not in seen:
            seen.add(key)
            matricules.append(plate)
    return matricules, len(lines) - len(matricules)


def apply_reverse_order(matricules: list, source: str = "liste") -> list:
    """Inverse l'ordre de traitement (dernière ligne du fichier en premier)."""
    if not matricules:
        return matricules
    reversed_list = list(reversed(matricules))
    log(
        f"↩️ {source} inversée — traitement: "
        f"{reversed_list[0]} → … → {reversed_list[-1]} ({len(reversed_list)} matricule(s))"
    )
    return reversed_list


def main():
    parser = argparse.ArgumentParser(
        description="Approbation rapide TOUS véhicules (document + flotte, SANS attribution) — multi-workers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  # Mode matricule unique
  python3 quick_approve_all_vehicle_vps.py AA325AD01
  
  # Mode batch 4 workers en parallèle
  python3 quick_approve_all_vehicle_vps.py --partners 6,8,9,10 --workers 4
  
  # Mode fichier liste avec 3 workers
  python3 quick_approve_all_vehicle_vps.py --list-file matricules.txt --workers 3
  
  # Fichier liste : commencer par la fin du fichier (dernière ligne en premier)
  python3 quick_approve_all_vehicle_vps.py --list-file matricules-blocked.txt --reverse --workers 20 --fast
  
  # Port de base personnalisé (défaut: 9222)
  python3 quick_approve_all_vehicle_vps.py --partners 6 --workers 2 --base-port 9300
  
  # TOUS les partenaires (détection automatique)
  python3 quick_approve_all_vehicle_vps.py --all-partners --workers 4
  python3 quick_approve_all_vehicle_vps.py --all-partners --workers 10 --reverse
        """
    )
    parser.add_argument("matricules", nargs="*", help="Matricule(s) du véhicule à approuver")
    parser.add_argument("--headed", action="store_true", help="Navigateur visible (pas headless)")
    parser.add_argument("--list-file", help="Fichier avec liste de matricules (un par ligne)")
    parser.add_argument("--partners", help="IDs partenaires séparés par virgule (ex: 6,8,9,10)")
    parser.add_argument("--all-partners", action="store_true", help="Détecte et traite TOUS les partenaires automatiquement")
    parser.add_argument("--workers", type=int, default=1, help="Nombre de workers parallèles (défaut: 1)")
    parser.add_argument("--base-port", type=int, default=BASE_DEBUG_PORT, help=f"Port de base Chrome debug (défaut: {BASE_DEBUG_PORT})")
    parser.add_argument(
        "--reverse",
        action="store_true",
        help=(
            "Traite du dernier au premier. Avec --list-file : inverse l'ordre du fichier "
            "(dernière ligne traitée en premier). Avec --partners : partenaires et véhicules inversés."
        ),
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Réduit les pauses entre matricules et les attentes pagination/filtres (~5x plus rapide)",
    )
    
    args = parser.parse_args()
    global _FAST_MODE
    _FAST_MODE = bool(args.fast)
    
    # Collecter les matricules
    all_matricules = []
    
    if args.all_partners:
        partner_ids = discover_all_partner_ids()
        if not partner_ids:
            log("❌ Aucun fichier Partenaire-N_drivers.json trouvé", "ERROR")
            sys.exit(1)
        if args.reverse:
            partner_ids = list(reversed(partner_ids))
            log(f"↩️ Ordre partenaires inversé: {partner_ids[0]} → … → {partner_ids[-1]}")
        vehicles, skipped = get_all_vehicles_from_partners(
            partner_ids, reverse_within_partner=args.reverse
        )
        already_ok = [v for v in vehicles if v.get("already_approved")]
        to_process = [v for v in vehicles if not v.get("already_approved")]
        all_matricules = [v["matricule"] for v in to_process]
        log(f"\n🚀 MODE ALL PARTNERS: {len(partner_ids)} partenaires, {len(all_matricules)} véhicules à traiter ({len(already_ok)} déjà approuvés skippés, {skipped} attribués ignorés)")
    
    elif args.partners:
        partner_ids = [int(p.strip()) for p in args.partners.split(",") if p.strip()]
        if args.reverse:
            partner_ids = list(reversed(partner_ids))
            log(f"↩️ Ordre partenaires inversé: {partner_ids}")
        vehicles, skipped = get_all_vehicles_from_partners(
            partner_ids, reverse_within_partner=args.reverse
        )
        already_ok = [v for v in vehicles if v.get("already_approved")]
        to_process = [v for v in vehicles if not v.get("already_approved")]
        all_matricules = [v["matricule"] for v in to_process]
        log(f"\n🚀 MODE BATCH: {len(all_matricules)} véhicules à traiter ({len(already_ok)} déjà approuvés skippés, {skipped} attribués ignorés)")
    
    elif args.list_file:
        try:
            loaded, dupes = load_matricules_from_file(args.list_file)
            all_matricules.extend(loaded)
            log(f"📁 {len(loaded)} matricules depuis {args.list_file}" + (
                f" ({dupes} doublon(s) ignoré(s))" if dupes else ""
            ))
            if loaded:
                log(f"   Ordre fichier: {loaded[0]} → … → {loaded[-1]}")
        except Exception as e:
            log(f"❌ Erreur lecture fichier: {e}", "ERROR")
            sys.exit(1)
    
    if args.matricules:
        all_matricules.extend(args.matricules)
    
    # --reverse sur fichier liste ou matricules CLI (hors modes partenaires déjà gérés)
    if args.reverse and all_matricules and not args.all_partners and not args.partners:
        all_matricules = apply_reverse_order(
            all_matricules,
            source=args.list_file or "matricules",
        )
    
    if not all_matricules:
        log("❌ Aucun matricule fourni. Utilisez --partners, --list-file ou des matricules en arguments", "ERROR")
        sys.exit(1)
    
    num_workers = min(args.workers, len(all_matricules))
    
    log(f"\n{'='*60}")
    log(f"🚀 QUICK APPROVE ALL — {len(all_matricules)} véhicule(s) — {num_workers} worker(s)")
    log(f"   📋 Carte grise pour TOUS les types | PAS d'attribution")
    if _FAST_MODE:
        log(f"   ⚡ Mode --fast actif (délais réduits entre matricules)")
    if args.reverse:
        log(f"   ↩️ Mode --reverse: premier matricule = {all_matricules[0]}, dernier = {all_matricules[-1]}")
    log(f"   🔌 Ports Chrome: {args.base_port} → {args.base_port + num_workers - 1}")
    log(f"{'='*60}")
    
    send_slack_message(
        f"🚀 *Batch Approval ALL Started*\n"
        f"📊 {len(all_matricules)} véhicules | {num_workers} worker(s) parallèle(s)\n"
        f"📋 Carte grise TOUS types, sans attribution"
    )
    
    image_index = build_image_index(IMAGES_OCR_DIR)
    matricule_meta = build_matricule_meta_index()
    
    # Découper la liste de matricules en parts pour chaque worker
    chunks = split_list(all_matricules, num_workers)
    
    for i, chunk in enumerate(chunks):
        log(f"   Worker {i}: {len(chunk)} véhicule(s)")
    
    start_time = time.time()
    all_results = []
    
    if num_workers == 1:
        # Mode séquentiel (pas besoin de threads)
        results = run_worker(0, chunks[0], args.headed, args.base_port, image_index, matricule_meta)
        all_results.extend(results)
    else:
        # Mode parallèle avec ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {}
            for i, chunk in enumerate(chunks):
                if not chunk:
                    continue
                future = executor.submit(
                    run_worker, i, chunk, args.headed, args.base_port, image_index, matricule_meta
                )
                futures[future] = i
            
            for future in as_completed(futures):
                worker_id = futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    log(f"✅ Worker {worker_id} terminé: {sum(1 for r in results if r['success'])}/{len(results)} OK")
                except Exception as e:
                    log(f"❌ Worker {worker_id} CRASH: {e}", "ERROR")
    
    elapsed = time.time() - start_time
    elapsed_min = elapsed / 60
    
    # ─── RÉSUMÉ FINAL ────────────────────────────────────────────────────────
    log(f"\n{'='*60}")
    log(f"📊 RÉSUMÉ FINAL — {num_workers} worker(s) — {elapsed_min:.1f} min")
    log(f"{'='*60}")
    
    success_count = sum(1 for r in all_results if r["success"])
    doc_count = sum(1 for r in all_results if r["document_approved"])
    fleet_count = sum(1 for r in all_results if r["fleet_approved"])
    upload_count = sum(1 for r in all_results if r["carte_grise_uploaded"])
    crash_count = sum(1 for r in all_results if any("CRASH" in err for err in r.get("errors", [])))
    duplicates_total = sum(r.get("duplicates_found", 1) for r in all_results if r.get("duplicates_found", 1) > 1)
    duplicates_approved = sum(r.get("duplicates_approved", 0) for r in all_results if r.get("duplicates_found", 1) > 1)
    
    log(f"✅ Approbations complètes: {success_count}/{len(all_results)}")
    log(f"📄 Documents approuvés:    {doc_count}/{len(all_results)}")
    log(f"📷 Cartes grises uploadées: {upload_count}/{len(all_results)}")
    log(f"🚗 Flottes approuvées:     {fleet_count}/{len(all_results)}")
    if duplicates_total > 0:
        log(f"🔁 Doublons traités:       {duplicates_approved}/{duplicates_total} lignes")
    if crash_count > 0:
        log(f"💥 Crashs (mais continué): {crash_count}")
    log(f"⏱️  Durée totale: {elapsed_min:.1f} min ({elapsed:.0f}s)")
    if num_workers > 1:
        seq_estimate = elapsed * num_workers
        log(f"⚡ Gain estimé: ~{seq_estimate/60:.0f} min séquentiel → {elapsed_min:.1f} min parallèle ({num_workers}x)")
    
    for r in all_results:
        status = "✅" if r["success"] else "❌"
        crash_mark = " 💥" if any("CRASH" in err for err in r.get("errors", [])) else ""
        cg_info = " 📷CG" if r["carte_grise_uploaded"] else ""
        dup_info = f" 🔁{r.get('duplicates_approved', 0)}/{r.get('duplicates_found', 1)}" if r.get("duplicates_found", 1) > 1 else ""
        errors = f" ({', '.join(r['errors'][:2])})" if r["errors"] else ""
        log(f"   {status} {r['matricule']}{cg_info}{dup_info}{crash_mark}{errors}")
    
    log(f"\n📝 Log: {LOG_FILE}")
    
    slack_summary = f"""✅ *Batch ALL Terminé* — {num_workers} worker(s)

📊 *Résultats* ({elapsed_min:.1f} min):
• Approbations: {success_count}/{len(all_results)}
• Documents: {doc_count} | Flottes: {fleet_count}
• Cartes grises uploadées: {upload_count}
{f"• 🔁 Doublons: {duplicates_approved}/{duplicates_total} lignes" if duplicates_total > 0 else ""}
{f"• 💥 Crashs: {crash_count}" if crash_count > 0 else ""}

📁 Log: `{LOG_FILE.name}`"""
    send_slack_message(slack_summary, mention_channel=(crash_count > 0 or success_count < len(all_results) * 0.8))
    
    sys.exit(0 if success_count == len(all_results) else 1)


if __name__ == "__main__":
    main()
