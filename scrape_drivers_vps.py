"""
Script VPS - Scraping conducteurs UpJunoo (Full Auto)
======================================================
Usage: python scrape_drivers_vps.py [--webhook URL]

Scrape tous les conducteurs de /fleet-drivers avec pagination auto.
Export JSON + CSV + HTML + rapport webhook.

Variables d'environnement:
  UPJUNOO_EMAIL    - Email de connexion
  UPJUNOO_PASSWORD - Mot de passe
  WEBHOOK_URL      - URL du webhook Slack (optionnel)
"""

import argparse
import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from webdriver_manager.chrome import ChromeDriverManager

# ─── Configuration ──────────────────────────────────────────────────────────
BASE_URL = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL = f"{BASE_URL}/login/admin"
DRIVERS_URL = f"{BASE_URL}/fleet-drivers"
OUTPUT_DIR = Path(__file__).parent / "output"

PAGE_LOAD_TIMEOUT = 30
LOGIN_TIMEOUT = 30


# ═════════════════════════════════════════════════════════════════════════════
#  CLASSE DE RAPPORT
# ═════════════════════════════════════════════════════════════════════════════

class DriversReport:
    def __init__(self):
        self.start_time = datetime.now()
        self.end_time: Optional[datetime] = None
        self.total_drivers = 0
        self.pages_scraped = 0
        self.errors: List[str] = []
        
    def add_error(self, error: str):
        self.errors.append({"message": error, "timestamp": datetime.now().isoformat()})
        
    def finalize(self, total: int, pages: int):
        self.end_time = datetime.now()
        self.total_drivers = total
        self.pages_scraped = pages
        
    def to_dict(self) -> Dict:
        duration = (self.end_time - self.start_time).total_seconds() if self.end_time else 0
        return {
            "script": "scrape_drivers",
            "status": "success" if not self.errors else "partial" if self.total_drivers > 0 else "failed",
            "summary": {
                "total_drivers": self.total_drivers,
                "pages_scraped": self.pages_scraped,
                "duration_seconds": duration,
                "errors_count": len(self.errors)
            },
            "timing": {
                "started": self.start_time.isoformat(),
                "finished": self.end_time.isoformat() if self.end_time else None,
                "duration_seconds": duration
            },
            "errors": self.errors
        }
        
    def to_markdown(self) -> str:
        d = self.to_dict()
        status_emoji = "✅" if d["status"] == "success" else "⚠️" if d["status"] == "partial" else "❌"
        
        return f"""{status_emoji} **Rapport Scraping Conducteurs**

**Statut:** {'Succès' if d['status'] == 'success' else 'Succès partiel' if d['status'] == 'partial' else 'Échec'}
**Durée:** {d['summary']['duration_seconds']:.1f}s

**Résumé:**
- 👥 Conducteurs: {d['summary']['total_drivers']}
- 📄 Pages scrapées: {d['summary']['pages_scraped']}
- ❌ Erreurs: {d['summary']['errors_count']}
"""


# ═════════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ═════════════════════════════════════════════════════════════════════════════

def send_webhook(report: DriversReport, webhook_url: str) -> bool:
    """Envoie le rapport vers le webhook (Slack compatible)."""
    if not webhook_url:
        return False
    
    is_slack = "slack.com" in webhook_url.lower()
    
    try:
        d = report.to_dict()
        summary = d["summary"]
        
        if is_slack:
            color = "#36a64f" if d["status"] == "success" else "#ff9900" if d["status"] == "partial" else "#ff0000"
            
            payload = {
                "text": f"Scraping conducteurs: {summary['total_drivers']} trouvés",
                "username": os.getenv("SLACK_BOT_NAME", "UpJunoo Drivers"),
                "icon_emoji": os.getenv("SLACK_ICON_EMOJI", ":busts_in_silhouette:"),
                "attachments": [{
                    "color": color,
                    "title": "Rapport Scraping Conducteurs",
                    "fields": [
                        {"title": "👥 Conducteurs", "value": str(summary["total_drivers"]), "short": True},
                        {"title": "📄 Pages", "value": str(summary["pages_scraped"]), "short": True},
                        {"title": "⏱️ Durée", "value": f"{summary['duration_seconds']:.1f}s", "short": True},
                        {"title": "❌ Erreurs", "value": str(summary["errors_count"]), "short": True}
                    ],
                    "footer": "UpJunoo VPS",
                    "ts": int(datetime.now().timestamp())
                }]
            }
        else:
            payload = {"text": report.to_markdown(), "data": d}
        
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        response.raise_for_status()
        print(f"✅ Webhook envoyé (HTTP {response.status_code})")
        return True
        
    except Exception as e:
        print(f"❌ Échec webhook: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  DRIVER & AUTH
# ═════════════════════════════════════════════════════════════════════════════

def setup_driver_headless():
    """Initialise Chrome en mode headless pour VPS."""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument("--remote-debugging-port=9222")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--single-process")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.0")

    import shutil, os
    # Chercher le binaire Chrome/Chromium disponible sur le système
    for binary in ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable"]:
        path = shutil.which(binary)
        if path:
            chrome_options.binary_location = path
            print(f"   ✅ Binaire Chrome trouvé: {path}")
            break

    # Chercher le chromedriver système (évite ChromeDriverManager incompatible)
    chromedriver_path = None
    for cd in ["chromedriver", "/usr/bin/chromedriver", "/usr/lib/chromium-browser/chromedriver",
               "/usr/lib/chromium/chromedriver"]:
        if os.path.isfile(cd) or shutil.which(cd):
            chromedriver_path = cd if os.path.isfile(cd) else shutil.which(cd)
            print(f"   ✅ ChromeDriver trouvé: {chromedriver_path}")
            break

    if chromedriver_path:
        service = Service(chromedriver_path)
    else:
        print("   ⚠️ ChromeDriver système introuvable, utilisation de ChromeDriverManager...")
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver


def auto_login(driver, email: str, password: str) -> bool:
    """Connexion automatique au panel admin."""
    print(f"🔐 Connexion à {LOGIN_URL}...")
    wait = WebDriverWait(driver, LOGIN_TIMEOUT)
    
    try:
        driver.get(LOGIN_URL)
        
        email_input = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
        )))
        pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password']")
        
        email_input.clear()
        email_input.send_keys(email)
        pwd_input.clear()
        pwd_input.send_keys(password)
        
        # Bouton submit
        try:
            btn = driver.find_element(By.XPATH, "//button[@type='submit']")
            btn.click()
        except:
            pwd_input.submit()
        
        wait.until(lambda d: "/login" not in d.current_url)
        print(f"✅ Connecté: {driver.current_url}")
        return True
        
    except Exception as e:
        print(f"❌ Erreur connexion: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  SCRAPING
# ═════════════════════════════════════════════════════════════════════════════

def set_page_size_500(driver):
    """
    Règle la pagination sur 500 avec plusieurs stratégies de fallback.
    Essaie par ordre: select visible, select par options, JavaScript direct.
    """
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            print(f"🔄 Tentative {attempt + 1}/{max_retries} pour pagination 500...")
            
            # Stratégie 1: Selecteur Bootstrap standard
            try:
                sel_el = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, 
                        "select.form-select.form-select-sm.w-auto, "
                        "select[name='DataTables_Table_0_length'], "
                        "select[data-dt-idx='0']"))
                )
            except:
                # Stratégie 2: Tout select dans la zone de pagination
                sel_el = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, 
                        ".dataTables_length select, "
                        "select.form-select"))
                )
            
            # Vérifier les options disponibles
            select_obj = Select(sel_el)
            options = [opt.text.strip() for opt in select_obj.options]
            print(f"   Options disponibles: {options}")
            
            # Chercher l'option 500 (peut être "500" ou "500 entries")
            target_option = None
            for opt in options:
                if "500" in opt:
                    target_option = opt
                    break
            
            if not target_option:
                print(f"   ⚠️ Option 500 non trouvée dans {options}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return False
            
            # Sélectionner l'option
            select_obj.select_by_visible_text(target_option)
            print(f"   ✅ Option '{target_option}' sélectionnée")
            
            # Attendre le rechargement avec plusieurs vérifications
            time.sleep(3)  # Attendre le début du rechargement
            
            # Vérification 1: Le tableau doit avoir changé ou être stable
            prev_count = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
            print(f"   📊 Lignes avant attente: {prev_count}")
            
            # Attendre que le tableau se stabilise avec plus de lignes
            start_wait = time.time()
            stable_count = 0
            last_count = 0
            
            while time.time() - start_wait < 30:  # Timeout 30s
                time.sleep(1)
                current_rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                current_count = len(current_rows)
                
                # Détecter stabilité (même nombre pendant 3 secondes consécutives)
                if current_count == last_count and current_count > 0:
                    stable_count += 1
                    if stable_count >= 3 and current_count >= 100:
                        print(f"   ✅ Tableau stable: {current_count} lignes")
                        return True
                else:
                    stable_count = 0
                    last_count = current_count
                    print(f"   ⏳ Chargement... {current_count} lignes")
            
            # Si on arrive ici, on a au moins quelques lignes
            if last_count > 0:
                print(f"   ✅ Pagination activée avec {last_count} lignes")
                return True
                
        except Exception as e:
            print(f"   ❌ Erreur tentative {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(3)
                driver.refresh()
                time.sleep(2)
            else:
                print(f"   ⚠️ Toutes les tentatives échouées")
                
    return False


def fast_scrape_page(driver):
    """Extrait les données de la page actuelle."""
    page_data = []
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6:
                    continue
                
                nom = cells[0].text.strip()
                telephone = cells[3].text.strip()
                
                # URL document (colonne 5)
                document_url = "N/A"
                view_profile = "N/A"
                try:
                    link_el = cells[5].find_element(By.TAG_NAME, "a")
                    document_url = link_el.get_attribute("href")
                    if document_url and "/document/" in document_url:
                        view_profile = document_url.replace("/document/", "/view-profile/")
                except:
                    pass
                
                page_data.append({
                    "nom": nom,
                    "telephone": telephone,
                    "document_url": document_url,
                    "view_profile": view_profile
                })
            except StaleElementReferenceException:
                continue
            except Exception as e:
                print(f"⚠️ Erreur ligne: {e}")
                
    except Exception as e:
        print(f"⚠️ Erreur page: {e}")
    
    return page_data


def _get_first_row_signature(driver):
    """Signature de la 1ère ligne pour détecter rechargement."""
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            return None
        return rows[0].text[:200]
    except:
        return None


def _is_next_disabled(driver):
    """True si bouton 'Next' est désactivé."""
    try:
        driver.find_element(By.CSS_SELECTOR, "ul.pagination li.page-item.disabled a.page-link[aria-label='Next']")
        return True
    except NoSuchElementException:
        return False


def _find_next_button(driver):
    """Cherche le bouton 'Suivant'."""
    try:
        a = driver.find_element(By.CSS_SELECTOR, "ul.pagination li.page-item:not(.disabled) a.page-link[aria-label='Next']")
        if a.is_displayed():
            return a
    except NoSuchElementException:
        pass
    return None


def go_to_next_page(driver):
    """Clique sur 'suivant' et attend rechargement."""
    if _is_next_disabled(driver):
        return False
    
    btn = _find_next_button(driver)
    if btn is None:
        return False
    
    prev_sig = _get_first_row_signature(driver)
    
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            btn.click()
        except:
            driver.execute_script("arguments[0].click();", btn)
    except Exception as e:
        print(f"⚠️ Clic 'suivant' impossible: {e}")
        return False
    
    # Attendre rechargement
    start = time.time()
    while time.time() - start < PAGE_LOAD_TIMEOUT:
        time.sleep(0.5)
        new_sig = _get_first_row_signature(driver)
        if new_sig and new_sig != prev_sig:
            time.sleep(1.0)
            return True
        if _is_next_disabled(driver):
            return False
    
    print("⏱️ Timeout attente nouvelle page")
    return False


def export_all(data, report: DriversReport):
    """Exporte les données dans plusieurs formats."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 1. JSON
    json_path = OUTPUT_DIR / f"conducteurs_{timestamp}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ JSON: {json_path}")
    
    # 2. CSV
    csv_path = OUTPUT_DIR / f"conducteurs_{timestamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Nom", "Téléphone", "URL Document", "Lien Profil"])
        for row in data:
            writer.writerow([row["nom"], row["telephone"], row["document_url"], row["view_profile"]])
    print(f"✅ CSV: {csv_path}")
    
    # 3. HTML
    html_path = OUTPUT_DIR / f"conducteurs_{timestamp}.html"
    rows_html = "".join([f'<tr><td>{r["nom"]}</td><td>{r["telephone"]}</td><td><a href="{r["document_url"]}" target="_blank">Doc</a></td><td><a href="{r.get("view_profile", "N/A")}" target="_blank">Profil</a></td></tr>' for r in data])
    html = f"""<html><head><style>body{{font-family:sans-serif;padding:20px;background:#f4f4f9}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;border:1px solid #ddd;text-align:left}}th{{background:#eee}}</style></head><body><h1>Conducteurs ({len(data)})</h1><table><thead><tr><th>Nom</th><th>Téléphone</th><th>Document</th><th>Profil</th></tr></thead><tbody>{rows_html}</tbody></table></body></html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ HTML: {html_path}")
    
    # 4. Rapport JSON
    report_path = OUTPUT_DIR / f"drivers_report_{timestamp}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
    print(f"📄 Rapport: {report_path}")


def scrape_all(driver, report: DriversReport):
    """Boucle de scraping avec pagination auto."""
    all_data = []
    page_num = 1
    
    while True:
        print(f"\n📄 Page {page_num}: scraping...")
        start_time = time.time()
        
        data = fast_scrape_page(driver)
        all_data.extend(data)
        
        elapsed = time.time() - start_time
        print(f"✅ {len(data)} conducteurs en {elapsed:.2f}s (Total: {len(all_data)})")
        
        if len(data) == 0:
            print("⚠️ Aucune ligne → arrêt")
            break
        
        print("➡️ Page suivante...")
        if not go_to_next_page(driver):
            print("🏁 Fin de pagination")
            break
        
        page_num += 1
    
    report.finalize(len(all_data), page_num)
    return all_data


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def diagnose_pagination(driver):
    """Diagnostic: affiche les éléments de pagination trouvés."""
    print("\n🔍 DIAGNOSTIC PAGINATION:")
    
    # Chercher tous les select
    selects = driver.find_elements(By.TAG_NAME, "select")
    print(f"   {len(selects)} select(s) trouvé(s)")
    for i, sel in enumerate(selects):
        try:
            classes = sel.get_attribute("class")
            name = sel.get_attribute("name")
            options = [opt.text for opt in Select(sel).options]
            print(f"   Select #{i}: class='{classes}' name='{name}' options={options}")
        except:
            pass
    
    # Chercher les éléments dataTables
    dt_lengths = driver.find_elements(By.CSS_SELECTOR, ".dataTables_length")
    print(f"   {len(dt_lengths)} .dataTables_length trouvé(s)")
    
    # Compter les lignes actuelles
    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    print(f"   {len(rows)} lignes dans le tableau")
    
    print("   Fin diagnostic\n")


def main():
    parser = argparse.ArgumentParser(description="Scraping conducteurs UpJunoo (VPS)")
    parser.add_argument("--webhook", help="URL webhook", default=os.getenv("WEBHOOK_URL"))
    parser.add_argument("--email", help="Email", default=os.getenv("UPJUNOO_EMAIL"))
    parser.add_argument("--password", help="Mot de passe", default=os.getenv("UPJUNOO_PASSWORD"))
    parser.add_argument("--debug", action="store_true", help="Mode debug avec diagnostic pagination")
    args = parser.parse_args()
    
    if not args.email or not args.password:
        print("❌ Erreur: UPJUNOO_EMAIL et UPJUNOO_PASSWORD requis")
        sys.exit(1)
    
    report = DriversReport()
    driver = setup_driver_headless()
    
    try:
        # Connexion
        if not auto_login(driver, args.email, args.password):
            report.add_error("Échec authentification")
            report.finalize(0, 0)
            export_all([], report)
            if args.webhook:
                send_webhook(report, args.webhook)
            sys.exit(1)
        
        # Navigation
        print(f"\n📍 Ouverture {DRIVERS_URL}...")
        driver.get(DRIVERS_URL)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        
        # Mode debug si demandé
        if args.debug:
            diagnose_pagination(driver)
        
        # Pagination 500 - CRITIQUE pour récupérer tous les conducteurs
        pagination_ok = set_page_size_500(driver)
        if not pagination_ok:
            report.add_error("CRITIQUE: Pagination 500 impossible - risque de données incomplètes")
            print("⚠️⚠️⚠️  ATTENTION: La pagination n'est pas à 500!")
            print("⚠️⚠️⚠️  Seuls les conducteurs de la 1ère page seront récupérés.")
            print("⚠️⚠️⚠️  Vérifie manuellement la pagination ou relance le script.")
            
            # Option: arrêter ici plutôt que de continuer avec des données incomplètes
            # Décommente les 3 lignes suivantes pour forcer l'arrêt:
            # report.finalize(0, 0)
            # driver.quit()
            # sys.exit(1)
        
        # Si pagination échouée, au moins attendre que le tableau actuel soit chargé
        time.sleep(3)
        
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        print(f"📊 {len(rows)} lignes détectées sur page 1")
        
        # Scraping
        start_time = time.time()
        results = scrape_all(driver, report)
        total_time = time.time() - start_time
        
        print("\n" + "="*50)
        print(f"🎉 TERMINÉ en {total_time/60:.2f} minutes")
        print(f"📊 TOTAL: {len(results)} conducteurs sur {report.pages_scraped} pages")
        print("="*50)
        
        # Exports
        export_all(results, report)
        
        # Notification
        if args.webhook:
            send_webhook(report, args.webhook)
        
        # Résumé
        print("\n" + report.to_markdown())
        
    except Exception as e:
        report.add_error(f"Erreur critique: {traceback.format_exc()}")
        report.finalize(0, 0)
        export_all([], report)
        if args.webhook:
            send_webhook(report, args.webhook)
    finally:
        driver.quit()
    
    sys.exit(0 if report.to_dict()["status"] == "success" else 1)


if __name__ == "__main__":
    main()
