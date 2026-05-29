#!/usr/bin/env python3
"""
process_pending_approvals_auto.py
=================================
Valide uniquement les conducteurs éligibles sur /fleet-drivers/pending :
  - "Approuvé" -> flux Upload -> Mise à jour -> Approuver
  - "En attente d'approbation" -> Approuver directement
Tous les autres statuts sont ignorés (réservés à process_pending_deletions_auto.py).

Boucle continue avec une pause de 5 s entre chaque cycle pour traiter
rapidement les nouvelles inscriptions.

Usage:
  python3 process_pending_approvals_auto.py [--headed] [--dry-run]
"""

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import urllib.request
import unicodedata
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

BASE_URL   = "https://upjunoo-server-new.junooapps.com"
ADMIN_LOGIN = f"{BASE_URL}/login/admin"
PENDING_URL = f"{BASE_URL}/fleet-drivers/pending"
ADMIN_EMAIL = os.getenv("UPJUNOO_EMAIL", "admin@upjunoo.com")
ADMIN_PASS  = os.getenv("UPJUNOO_PASSWORD", "Upjunoo@Admin")

REPORT_DIR  = Path(__file__).parent / "output" / "drivers_pending"
SLACK_WEBHOOK = os.getenv("WEBHOOK_URL", "").strip()

STATUS_APPROUVE = "Approuvé"
STATUS_EN_ATTENTE = "En attente d'approbation"
CYCLE_PAUSE_SECONDS = 5
_SHUTDOWN_REQUESTED = False


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("utf-8", errors="replace").decode("utf-8"), flush=True)


def _signal_handler(signum, frame):
    """Gère SIGTERM/SIGHUP pour un arrêt propre avec log et notification."""
    global _SHUTDOWN_REQUESTED
    sig_name = signal.Signals(signum).name
    log(f"⚠️ Signal {sig_name} (code {signum}) reçu — arrêt propre demandé...", "WARNING")
    try:
        slack_notify(f"⚠️ *Approvals* Signal {sig_name} reçu — arrêt propre du script.")
    except Exception:
        pass
    _SHUTDOWN_REQUESTED = True


def install_signal_handlers():
    """Installe les handlers pour SIGTERM et SIGHUP."""
    for sig in (signal.SIGTERM,):
        signal.signal(sig, _signal_handler)
    # SIGHUP n'existe que sur Unix
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _signal_handler)
    log("Signal handlers installés (SIGTERM, SIGHUP)")


def cleanup_zombie_chrome():
    """Nettoie les processus chromedriver/chrome orphelins et zombies."""
    try:
        result = subprocess.run(
            ["bash", "-c",
             "ps aux | grep -E 'chromedriver|[c]hrome' | grep -v grep | "
             "awk '{print $2, $8, $11}' | grep -E 'Z|defunct'"],
            capture_output=True, text=True, timeout=10
        )
        if result.stdout.strip():
            zombie_pids = []
            for line in result.stdout.strip().split("\n"):
                parts = line.split()
                if parts:
                    zombie_pids.append(parts[0])
            if zombie_pids:
                log(f"Nettoyage de {len(zombie_pids)} processus Chrome zombies: {zombie_pids}", "WARNING")
                for pid in zombie_pids:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
    except Exception as e:
        log(f"Erreur nettoyage zombies: {e}", "WARNING")


def is_browser_dead(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        isinstance(exc, InvalidSessionIdException)
        or "invalid session id" in msg
        or "session deleted" in msg
        or "not connected to devtools" in msg
        or "disconnected" in msg
        or "connection refused" in msg
        or "failed to establish a new connection" in msg
        or ("max retries exceeded" in msg and "localhost" in msg)
    )


def quit_driver_safe(driver) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass


def restart_browser(driver, headed: bool):
    log("Chrome deconnecte: redemarrage du navigateur...", "WARNING")
    quit_driver_safe(driver)
    time.sleep(3)
    new_driver = setup_driver(headed=headed)
    if not admin_login(new_driver):
        quit_driver_safe(new_driver)
        raise RuntimeError("Echec reconnexion admin apres crash Chrome")
    log("Navigateur redemarre et session admin retablie", "WARNING")
    return new_driver


def ensure_driver_session(driver, headed: bool):
    try:
        _ = driver.current_url
        return driver
    except Exception as exc:
        if is_browser_dead(exc):
            return restart_browser(driver, headed)
        log(f"Session instable ({exc}), redemarrage preventif...", "WARNING")
        return restart_browser(driver, headed)


def slack_notify(msg: str) -> None:
    if not SLACK_WEBHOOK:
        return
    try:
        data = json.dumps({"text": msg}).encode("utf-8")
        req = urllib.request.Request(
            SLACK_WEBHOOK, data=data,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Slack error: {e}", "WARNING")


def setup_driver(headed=False) -> webdriver.Chrome:
    opts = Options()
    if not headed:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--disable-software-rasterizer")
    opts.page_load_strategy = "eager"

    path = shutil.which("chromedriver") or shutil.which("/usr/bin/chromedriver")
    svc = Service(path) if path else Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)
    return driver


def _fill_input(driver, elem, value: str) -> None:
    driver.execute_script("""
        var el = arguments[0], val = arguments[1];
        var nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeSetter.call(el, val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    """, elem, value)


def admin_login(driver, retries=2) -> bool:
    log(f"Connexion admin: {ADMIN_EMAIL}")
    for attempt in range(1, retries + 1):
        try:
            driver.get(ADMIN_LOGIN)
            time.sleep(2)
            wait = WebDriverWait(driver, 30)
            em = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='email'], #email-input")))
            pw = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password'], #password-input")))
            _fill_input(driver, em, ADMIN_EMAIL)
            _fill_input(driver, pw, ADMIN_PASS)
            time.sleep(0.5)
            btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(3)
            WebDriverWait(driver, 30).until(lambda d: "login" not in d.current_url.lower())
            log(f"Admin connecté — {driver.current_url}")
            return True
        except Exception as exc:
            log(f"Tentative {attempt} échouée: {exc}", "WARNING")
        time.sleep(2)
    log("Login admin échoué", "ERROR")
    return False


def set_page_size_500(driver) -> bool:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            time.sleep(5)
            try:
                sel_el = WebDriverWait(driver, 15).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, 
                        "select.form-select, select[name*='_length'], .dataTables_length select"))
                )
            except:
                sel_el = driver.find_element(By.CSS_SELECTOR, "select")
            
            select_obj = Select(sel_el)
            options = [opt.text.strip() for opt in select_obj.options]
            target_option = next((opt for opt in options if "500" in opt), None)
            
            if not target_option:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return False
            
            driver.execute_script("arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('change'));", sel_el, target_option if target_option.isdigit() else "500")
            time.sleep(3)
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(3)
                driver.refresh()
                time.sleep(2)
    return False


def _get_active_page_number(driver):
    try:
        return driver.find_element(By.CSS_SELECTOR, "ul.pagination li.page-item.active").text.strip()
    except:
        return None

def go_to_next_page(driver) -> bool:
    try:
        driver.find_element(By.CSS_SELECTOR, "ul.pagination li.page-item.disabled a.page-link[aria-label='Next']")
        return False
    except NoSuchElementException:
        pass
        
    btn = None
    for sel in [
        "ul.pagination li.page-item:not(.disabled) a.page-link[aria-label='Next']",
        "li.next:not(.disabled) a",
        "a.next"
    ]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed(): break
        except: continue

    if not btn:
        try:
            btn = driver.find_element(By.XPATH, "//ul[contains(@class, 'pagination')]//li[not(contains(@class, 'disabled'))]//a[contains(text(), 'Next') or contains(text(), 'Suivant')]")
        except: pass

    if not btn: return False

    prev_page = _get_active_page_number(driver)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", btn)
    except:
        return False
    
    start = time.time()
    while time.time() - start < 30:
        time.sleep(1.0)
        curr_page = _get_active_page_number(driver)
        if curr_page and curr_page != prev_page:
            time.sleep(2.0)
            return True
    return False


def extract_status_from_document_page(driver, wait) -> str:
    """Retourne le statut du premier document, ou 'Non téléchargé' si aucun."""
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table")))
        time.sleep(1)
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            return "Non téléchargé"
        
        cells = rows[0].find_elements(By.TAG_NAME, "td")
        if len(cells) >= 4:
            return cells[3].text.strip()
        else:
            # Si le tableau a des colonnes bizarres ou "No data"
            if "no data" in rows[0].text.lower() or "aucun" in rows[0].text.lower():
                return "Non téléchargé"
            return "Inconnu"
    except Exception:
        return "Non téléchargé"


def normalize_status(status: str) -> str:
    # Normalise accents/apostrophes/espaces pour comparer des libellés UI variables.
    s = unicodedata.normalize("NFKD", status or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("’", "'").strip().lower()
    return " ".join(s.split())


def get_approval_action(status: str) -> str | None:
    """Retourne le type d'action d'approbation, ou None si le statut doit être ignoré."""
    s = normalize_status(status)
    if s == normalize_status(STATUS_APPROUVE):
        return "APPROUVER_COMPLET"
    if s == normalize_status(STATUS_EN_ATTENTE):
        return "APPROUVER_DIRECT"
    return None


def is_pending_approval_status(status: str) -> bool:
    s = normalize_status(status)
    return "attente" in s and "approbation" in s


def is_document_fully_approved(status: str) -> bool:
    s = normalize_status(status)
    if "attente" in s:
        return False
    return "approuve" in s


def find_document_row_approve_button(driver):
    """Bouton Approuver de la premiere ligne du tableau documents."""
    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    if not rows:
        return None
    row = rows[0]
    for btn in row.find_elements(By.XPATH, ".//button | .//a"):
        try:
            txt = normalize_status(btn.text or "")
            if "approu" in txt and "declin" not in txt and btn.is_displayed() and btn.is_enabled():
                return btn
        except Exception:
            continue
    return None


def confirm_swal2_if_present(driver, timeout: int = 12) -> None:
    try:
        confirm = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", confirm)
        driver.execute_script("arguments[0].click();", confirm)
        time.sleep(0.4)
        try:
            ok = WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
            )
            driver.execute_script("arguments[0].click();", ok)
        except TimeoutException:
            pass
        deadline = time.time() + 8
        while time.time() < deadline:
            if not driver.find_elements(By.CSS_SELECTOR, ".swal2-container .swal2-popup"):
                break
            time.sleep(0.2)
    except TimeoutException:
        pass


def wait_for_document_approved(driver, wait, timeout: int = 25) -> bool:
    """Attend que le document ne soit plus en attente d'approbation."""
    start = time.time()
    while time.time() - start < timeout:
        status = extract_status_from_document_page(driver, wait)
        if is_document_fully_approved(status):
            return True
        if not is_pending_approval_status(status) and not find_document_row_approve_button(driver):
            return True
        time.sleep(1.5)
        try:
            driver.refresh()
            time.sleep(2)
        except Exception:
            pass
    status = extract_status_from_document_page(driver, wait)
    log(f"    Verification finale: statut='{status}'", "WARNING")
    return is_document_fully_approved(status)


def click_and_verify_document_approval(driver, wait, max_attempts: int = 2) -> bool:
    for attempt in range(1, max_attempts + 1):
        approve_btn = find_document_row_approve_button(driver)
        if not approve_btn:
            status = extract_status_from_document_page(driver, wait)
            if is_document_fully_approved(status):
                return True
            log(f"    Tentative {attempt}: bouton Approuver introuvable", "WARNING")
            return False

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", approve_btn)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", approve_btn)
        confirm_swal2_if_present(driver)
        time.sleep(1)

        if wait_for_document_approved(driver, wait):
            return True

        log(f"    Tentative {attempt}: document toujours en attente, nouvel essai...", "WARNING")
        try:
            driver.refresh()
            time.sleep(2)
        except Exception:
            pass

    return False


def process_approuve_flow(driver, wait, driver_id: int) -> bool:
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows: return False
        row = rows[0]
        
        upload_btn = None
        try:
            upload_btn = row.find_element(By.XPATH, ".//a[contains(@href, 'document-upload')]")
        except: pass

        if not upload_btn:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                doc_cell = cells[-2] if len(cells) >= 2 else cells[-1]
                icons = doc_cell.find_elements(By.XPATH, ".//a | .//button")
                if len(icons) >= 2: upload_btn = icons[1]
                elif icons: upload_btn = icons[0]
            except: pass

        if not upload_btn:
            try:
                upload_btn = driver.find_element(By.XPATH, "//a[contains(@href, 'document-upload')]")
            except: pass

        if not upload_btn:
            return False

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", upload_btn)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", upload_btn)

        # Etape 2: Mise à jour
        wait.until(lambda d: "document-upload" in d.current_url or "modifier" in d.current_url.lower())
        time.sleep(1)
        mise_a_jour_btn = None
        for xpath in [
            "//button[contains(text(), 'Mise') and contains(text(), 'jour')]",
            "//button[normalize-space()='Mise a jour']",
            "//input[@type='submit']",
            "//button[@type='submit']",
        ]:
            try:
                btn = driver.find_element(By.XPATH, xpath)
                if btn.is_displayed():
                    mise_a_jour_btn = btn
                    break
            except: continue
            
        if not mise_a_jour_btn: return False
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", mise_a_jour_btn)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", mise_a_jour_btn)

        # Etape 3: Approuver + verifier le statut document
        wait.until(lambda d: f"/document/{driver_id}" in d.current_url)
        time.sleep(1.5)
        return click_and_verify_document_approval(driver, wait)
    except Exception as e:
        log(f"    [ERREUR Approuvé flow] {e}", "ERROR")
        return False


def process_en_attente_flow(driver, wait, driver_id: int) -> bool:
    try:
        return click_and_verify_document_approval(driver, wait)
    except Exception as e:
        log(f"    [ERREUR En Attente flow] {e}", "ERROR")
        return False


def main():
    parser = argparse.ArgumentParser(description="Approbation des conducteurs éligibles (pending)")
    parser.add_argument("--headed", action="store_true", help="Afficher le navigateur")
    parser.add_argument("--dry-run", action="store_true", help="Mode test, aucune modification")
    parser.add_argument("--start-page", type=int, default=1, help="Page de départ")
    args = parser.parse_args()

    install_signal_handlers()
    log("Démarrage du script d'approbation des conducteurs...")
    slack_notify("🚀 *Démarrage des approbations* (conducteurs Approuvé / En attente)")

    driver = None
    try:
        driver = setup_driver(headed=args.headed)
        if not admin_login(driver):
            slack_notify("❌ *Échec connexion admin*")
            return

        current_start_page = args.start_page

        while not _SHUTDOWN_REQUESTED:
            # Nettoyage périodique des processus Chrome orphelins
            cleanup_zombie_chrome()

            log("Verification de la session navigateur...")
            try:
                driver = ensure_driver_session(driver, args.headed)
            except Exception as exc:
                log(f"Impossible de retablir la session navigateur: {exc}", "ERROR")
                slack_notify(f"❌ *Approvals* echec redemarrage navigateur: {exc}")
                time.sleep(30)
                continue

            log("\n" + "="*5)
            log("🔄 Démarrage d'un nouveau cycle d'approbation...")
            log("="*5)

            stats = {
                "approuvés": 0,
                "en_attente_approuvés": 0,
                "ignorés": 0,
                "échecs": 0,
                "total": 0,
            }
            processed_ids = set()

            try:
                log(f"Chargement de {PENDING_URL}...")
                driver.get(PENDING_URL)
                time.sleep(5)

                # Activer pagination 500
                if not set_page_size_500(driver):
                    log("Pagination 500 a échoué. Poursuite avec la taille par défaut.", "WARNING")

                page_num = 1
                while True:
                    if page_num < current_start_page:
                        log(f"Passage de la page {page_num}...")
                        if not go_to_next_page(driver):
                            break
                        page_num += 1
                        continue

                    log(f"\n--- Traitement de la Page {page_num} ---")

                    while True:  # Boucle pour traiter les lignes de la page courante
                        try:
                            WebDriverWait(driver, 15).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
                            )
                            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                        except TimeoutException:
                            rows = []

                        if not rows or "aucune" in rows[0].text.lower() or "no data" in rows[0].text.lower():
                            break

                        target_row = None
                        driver_id = None
                        nom = "Inconnu"
                        doc_url = None

                        for r in rows:
                            try:
                                cells = r.find_elements(By.TAG_NAME, "td")
                                if len(cells) < 6:
                                    continue
                                nom_cell = cells[0].text.strip()

                                a_tags = cells[5].find_elements(By.TAG_NAME, "a")
                                if a_tags:
                                    url = a_tags[0].get_attribute("href")
                                    if url and "/document/" in url:
                                        m = re.search(r"/document/(\d+)", url)
                                        if m:
                                            did = int(m.group(1))
                                            if did not in processed_ids:
                                                target_row = r
                                                driver_id = did
                                                nom = nom_cell
                                                doc_url = url
                                                break
                            except StaleElementReferenceException:
                                break

                        if not target_row or not driver_id:
                            break

                        processed_ids.add(driver_id)
                        stats["total"] += 1
                        log(f"Conducteur ID={driver_id} | {nom}")

                        if args.dry_run:
                            log(f"  [DRY-RUN] Traitement simulé pour {nom}")
                            continue

                        main_window = driver.current_window_handle

                        try:
                            driver.execute_script("window.open(arguments[0], '_blank');", doc_url)
                            driver.switch_to.window(driver.window_handles[-1])

                            wait = WebDriverWait(driver, 15)
                            status = extract_status_from_document_page(driver, wait)
                            log(f"  Statut document: {status}")

                            action_result = False
                            action_type = get_approval_action(status)
                            if action_type == "APPROUVER_COMPLET":
                                log("  -> Action: Flux Approuvé (Upload -> MàJ -> Approuver)")
                                action_result = process_approuve_flow(driver, wait, driver_id)
                            elif action_type == "APPROUVER_DIRECT":
                                log("  -> Action: Flux En Attente (Approuver directement)")
                                action_result = process_en_attente_flow(driver, wait, driver_id)
                            else:
                                log(f"  -> Action: Ignoré (Statut: {status})")
                                action_type = "IGNORER"
                                action_result = False

                        except Exception as e:
                            log(f"  [ERREUR] {e}", "ERROR")
                            if is_browser_dead(e):
                                raise
                            action_result = False
                            action_type = "ERROR"
                        finally:
                            try:
                                if len(driver.window_handles) > 1:
                                    driver.close()
                                driver.switch_to.window(main_window)
                            except Exception:
                                pass

                        if action_type == "IGNORER":
                            log("  [INFO] Conducteur ignoré (réservé au script de suppression)")
                            stats["ignorés"] += 1
                        elif action_type == "APPROUVER_COMPLET":
                            if action_result:
                                log("  [SUCCES] Conducteur approuvé (Flux complet)")
                                stats["approuvés"] += 1
                            else:
                                log("  [ECHEC] Échec du flux d'approbation", "WARNING")
                                stats["échecs"] += 1
                        elif action_type == "APPROUVER_DIRECT":
                            if action_result:
                                log("  [SUCCES] Conducteur approuvé (Direct)")
                                stats["en_attente_approuvés"] += 1
                            else:
                                log("  [ECHEC] Échec du flux d'approbation", "WARNING")
                                stats["échecs"] += 1

                    log(f"Fin de la page {page_num}.")
                    if not go_to_next_page(driver):
                        break
                    page_num += 1

                REPORT_DIR.mkdir(parents=True, exist_ok=True)
                report_path = REPORT_DIR / f"process_approvals_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(stats, f, indent=2, ensure_ascii=False)

                msg = (
                    f"🏁 *FIN du cycle d'approbation*\n"
                    f"✅ Approuvés (Complet): {stats['approuvés']}\n"
                    f"✅ Approuvés (Direct): {stats['en_attente_approuvés']}\n"
                    f"⏭️ Ignorés (autres statuts): {stats['ignorés']}\n"
                    f"❌ Échecs: {stats['échecs']}\n"
                    f"Total traités: {stats['total']}"
                )
                log(f"\n{msg}")
                if stats["total"] > 0:
                    slack_notify(msg)

            except Exception as e:
                log(f"❌ Erreur critique durant le cycle : {e}", "ERROR")
                if is_browser_dead(e):
                    try:
                        driver = restart_browser(driver, args.headed)
                        slack_notify("⚠️ *Approvals* Chrome a crashé — navigateur redémarré automatiquement")
                    except Exception as restart_err:
                        log(f"Echec redemarrage navigateur: {restart_err}", "ERROR")
                        slack_notify(f"❌ *Approvals* impossible de redémarrer Chrome: {restart_err}")
                        time.sleep(30)

            current_start_page = 1
            if _SHUTDOWN_REQUESTED:
                log("Arrêt propre demandé — sortie de la boucle.", "WARNING")
                break
            log(f"⏳ Pause de {CYCLE_PAUSE_SECONDS} s avant le prochain cycle...")
            time.sleep(CYCLE_PAUSE_SECONDS)

        if _SHUTDOWN_REQUESTED:
            log("Script arrêté proprement suite à un signal.", "WARNING")
            slack_notify("🛑 *Approvals* Script arrêté proprement suite à un signal.")
    finally:
        quit_driver_safe(driver)
        cleanup_zombie_chrome()
        log("Nettoyage final terminé.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Arret demande par l'utilisateur", "WARNING")
