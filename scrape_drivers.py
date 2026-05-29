"""
Script de scraping RAPIDE des conducteurs - UpJunoo Admin Panel
============================================================
Workflow semi-automatique :
1. Le script se connecte automatiquement (credentials via env vars
   UPJUNOO_EMAIL / UPJUNOO_PASSWORD, sinon demandés au clavier).
2. Il ouvre /fleet-drivers.
3. L'utilisateur règle la pagination sur 500 puis appuie sur [ENTRÉE].
4. Le script scrape la page et passe automatiquement à la page suivante
   jusqu'à ce que le bouton "suivant" soit désactivé.
5. Export JSON + CSV + HTML.
"""

import csv
import json
import os
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

# ─── Configuration ──────────────────────────────────────────────────────────────
BASE_URL = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL = f"{BASE_URL}/login/admin"
DRIVERS_URL = f"{BASE_URL}/fleet-drivers"
OUTPUT_DIR = Path(__file__).parent / "output"
JSON_OUT = OUTPUT_DIR / "conducteurs.json"
CSV_OUT = OUTPUT_DIR / "conducteurs.csv"

# Timings
PAGE_LOAD_TIMEOUT = 30   # secondes max pour attendre un rechargement de tableau
LOGIN_TIMEOUT = 30

# ═══════════════════════════════════════════════════════════════════════════════
#  EXPORTS
# ═══════════════════════════════════════════════════════════════════════════════

def export_all(data):
    """Exporte les données dans plusieurs formats."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 1. JSON (Clean)
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✅ JSON exporté: {JSON_OUT}")

    # 2. CSV
    with open(CSV_OUT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Nom", "Téléphone", "URL Document", "Lien Profil"])
        for row in data:
            writer.writerow([row["nom"], row["telephone"], row["document_url"], row["view_profile"]])
    print(f"  ✅ CSV exporté: {CSV_OUT}")

    # 3. HTML (Visual) - Version allégée
    html_path = OUTPUT_DIR / "conducteurs.html"
    rows_html = "".join([f'<tr><td>{r["nom"]}</td><td>{r["telephone"]}</td><td><a href="{r["document_url"]}" target="_blank">Lien Doc</a></td><td><a href="{r.get("view_profile", "N/A")}" target="_blank">Lien Profil</a></td></tr>' for r in data])
    html = f"""<html><head><style>body{{font-family:sans-serif;padding:20px;background:#f4f4f9}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;border:1px solid #ddd;text-align:left}}th{{background:#eee}}</style></head><body><h1>Liste Conducteurs ({len(data)})</h1><table><thead><tr><th>Nom</th><th>Téléphone</th><th>Lien Document</th><th>Lien Profil</th></tr></thead><tbody>{rows_html}</tbody></table></body></html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✅ HTML exporté: {html_path}")


# ═══════════════════════════════════════════════════════════════════════════════
#  SCRAPING
# ═══════════════════════════════════════════════════════════════════════════════

def get_credentials():
    """Récupère email/mot de passe depuis l'env, sinon demande interactivement."""
    email = os.getenv("UPJUNOO_EMAIL")
    password = os.getenv("UPJUNOO_PASSWORD")
    if not email:
        email = input("📧 Email admin UpJunoo : ").strip()
    if not password:
        password = getpass("🔑 Mot de passe : ")
    return email, password


def auto_login(driver, email, password):
    """Remplit et soumet le formulaire de login admin."""
    print(f"\n🔐 Connexion automatique à {LOGIN_URL}...")
    driver.get(LOGIN_URL)

    wait = WebDriverWait(driver, LOGIN_TIMEOUT)
    # Champs: on cherche large pour être robuste
    email_input = wait.until(EC.presence_of_element_located((
        By.CSS_SELECTOR, "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
    )))
    pwd_input = driver.find_element(
        By.CSS_SELECTOR, "input[type='password'], input[name='password']"
    )

    email_input.clear()
    email_input.send_keys(email)
    pwd_input.clear()
    pwd_input.send_keys(password)

    # Bouton submit
    try:
        btn = driver.find_element(
            By.XPATH,
            "//button[@type='submit'] | //button[contains(translate(., 'LOGIN', 'login'),'login')]"
            " | //button[contains(translate(., 'CONNEXION', 'connexion'),'connexion')]"
            " | //button[contains(translate(., 'SE CONNECTER', 'se connecter'),'se connecter')]",
        )
        btn.click()
    except NoSuchElementException:
        pwd_input.submit()

    # Attend de quitter la page de login
    wait.until(lambda d: "/login" not in d.current_url)
    print(f"  ✅ Connecté ! URL actuelle : {driver.current_url}")


def set_page_size_500(driver):
    """Sélectionne automatiquement '500' dans le selecteur de taille de page."""
    try:
        from selenium.webdriver.support.ui import Select
        sel_el = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select.form-select.form-select-sm.w-auto"))
        )
        Select(sel_el).select_by_visible_text("500")
        print("  ✅ Pagination réglée sur 500 automatiquement.")
        # Attendre que le tableau se repeuple
        time.sleep(1.5)
        WebDriverWait(driver, 30).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "table tbody tr")) > 100
        )
        return True
    except Exception as e:
        print(f"  ⚠️  Impossible de régler 500 auto : {e}")
        return False


def wait_for_pagination_setup(driver):
    """Va sur la page conducteurs et règle la pagination à 500."""
    print(f"\n📍 Ouverture de {DRIVERS_URL}...")
    driver.get(DRIVERS_URL)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
    except TimeoutException:
        print("  ⚠️  Tableau non détecté dans les temps, continue quand même.")

    ok = set_page_size_500(driver)
    if not ok:
        print("\n👉 Règle la pagination sur '500' manuellement dans le navigateur,")
        input("   puis appuie sur [ENTRÉE] pour continuer...")

    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    print(f"  � {len(rows)} lignes détectées sur la page 1.")

def fast_scrape_page(driver):
    """Extrait les données de la page actuelle sans cliquer."""
    page_data = []
    try:
        # Attendre que le tableau soit présent
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6: continue
                
                nom = cells[0].text.strip()
                telephone = cells[3].text.strip()
                
                # Récupérer l'URL du document (colonne 5 - index 5)
                # On cherche l'élément 'a' qui contient le lien
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
                print(f"  ⚠️ Erreur ligne: {e}")
                
    except Exception as e:
        print(f"  ⚠️ Erreur page: {e}")
        
    return page_data

def _get_first_row_signature(driver):
    """Renvoie une signature de la 1ère ligne pour détecter un rechargement."""
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            return None
        return rows[0].text[:200]
    except Exception:
        return None


def _find_next_button(driver):
    """Cherche le bouton 'Suivant' (Bootstrap pagination de l'app UpJunoo)."""
    try:
        # L'élément <a> 'Next' (si <li> parent n'est pas .disabled)
        a = driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item:not(.disabled) a.page-link[aria-label='Next']",
        )
        if a.is_displayed():
            return a
    except NoSuchElementException:
        pass
    return None


def _is_next_disabled(driver):
    """True si le <li> contenant 'Next' porte la classe .disabled (fin de pagination)."""
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item.disabled a.page-link[aria-label='Next']",
        )
        return True
    except NoSuchElementException:
        return False


def go_to_next_page(driver):
    """Clique sur 'suivant' et attend le rechargement du tableau.
    Retourne True si une nouvelle page est chargée, False sinon."""
    if _is_next_disabled(driver):
        return False

    btn = _find_next_button(driver)
    if btn is None:
        print("  ℹ️  Bouton 'suivant' introuvable → fin de pagination.")
        return False

    prev_sig = _get_first_row_signature(driver)

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
    except Exception as e:
        print(f"  ⚠️  Clic 'suivant' impossible : {e}")
        return False

    # Attendre le rechargement : 1ère ligne différente OU tableau repeuplé
    start = time.time()
    while time.time() - start < PAGE_LOAD_TIMEOUT:
        time.sleep(0.5)
        new_sig = _get_first_row_signature(driver)
        if new_sig and new_sig != prev_sig:
            # Petite attente supplémentaire pour que toutes les lignes finissent de rendre
            time.sleep(1.0)
            return True
        if _is_next_disabled(driver):
            return False
    print("  ⏱️  Timeout en attendant la nouvelle page.")
    return False


def scrape_all(driver):
    """Boucle de scraping avec pagination AUTO."""
    all_data = []
    page_num = 1

    while True:
        print(f"\n📄 Page {page_num} : scraping en cours...")
        start_time = time.time()

        data = fast_scrape_page(driver)
        all_data.extend(data)

        elapsed = time.time() - start_time
        print(f"  ✅ {len(data)} conducteurs récupérés en {elapsed:.2f}s")
        print(f"  📊 Total accumulé : {len(all_data)}")

        # Sauvegarde progressive
        export_all(all_data)

        if len(data) == 0:
            print("  ⚠️  Aucune ligne sur la page → arrêt.")
            break

        print("  ➡️  Passage à la page suivante...")
        if not go_to_next_page(driver):
            print("\n🏁 Fin de pagination.")
            break

        page_num += 1

    return all_data

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    opts = Options()
    opts.add_argument("--window-size=1600,1000")
    # On ne met pas de headless pour permettre la connexion manuelle
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    
    try:
        email, password = get_credentials()
        auto_login(driver, email, password)

        # Phase manuelle minimale : réglage pagination 500
        wait_for_pagination_setup(driver)

        # Phase Scraping auto
        start_time = time.time()
        results = scrape_all(driver)
        total_time = time.time() - start_time
        
        print("\n" + "="*60)
        print(f"🎉 TERMINÉ en {total_time/60:.2f} minutes")
        print(f"📊 TOTAL : {len(results)} conducteurs")
        print("="*60)
        
        if results:
            export_all(results)
            
    except Exception as e:
        print(f"\n❌ ERREUR CRITIQUE: {e}")
        traceback.print_exc()
    finally:
        print("\n👋 Script terminé. Fermeture dans 5 secondes...")
        time.sleep(5)
        driver.quit()

if __name__ == "__main__":
    run()
