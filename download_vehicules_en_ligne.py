#!/usr/bin/env python3
"""
download_vehicules_en_ligne.py
==============================

Connexion admin UpJunoo puis téléchargement de la liste des véhicules / conducteurs
EN LIGNE au format Excel depuis :

    Géolocalisation → Vue cartographique → Filtres
      → Conducteurs = "En ligne"
      → Appliquer
      → Exporter Excel   (produit un fichier drivers-godseye-*.xls)

Le fichier est enregistré dans output/partner_automation/ (réutilisable ensuite par
generate_chauffeurs_actifs_state.py comme référence "en ligne").

Réutilise la connexion admin robuste de quick_approve_all_vehicle_vps.py
(identifiants UPJUNOO_EMAIL / UPJUNOO_PASSWORD du .env).

Usage:
  python download_vehicules_en_ligne.py
  python download_vehicules_en_ligne.py --headed
  python download_vehicules_en_ligne.py --map-url https://.../vue-cartographique
  python download_vehicules_en_ligne.py --output-dir output/partner_automation
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# On réutilise la logique de connexion admin déjà éprouvée.
import quick_approve_all_vehicle_vps as qa

BASE_URL = qa.BASE_URL
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output" / "partner_automation"

# Routes candidates pour la vue cartographique (essayées avant la navigation par menu).
# URL confirmée : /map/gods_eye
MAP_URL_CANDIDATES = [
    f"{BASE_URL}/map/gods_eye",
    f"{BASE_URL}/vue-cartographique",
    f"{BASE_URL}/geolocation/map-view",
    f"{BASE_URL}/godseye",
    f"{BASE_URL}/map-view",
    f"{BASE_URL}/live-tracking",
]


# Console Windows : éviter UnicodeEncodeError (cp1252) sur les emojis.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}][{level}] {msg}", flush=True)


# ───────────────────────────────────────────────────────────────────────────────
# DRIVER (téléchargement ACTIVÉ, contrairement à quick_approve qui le bloque)
# ───────────────────────────────────────────────────────────────────────────────

def setup_driver(download_dir: Path, headed: bool = False):
    download_dir.mkdir(parents=True, exist_ok=True)
    opts = Options()
    if headed:
        opts.add_argument("--start-maximized")
    else:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.page_load_strategy = "eager"
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    prefs = {
        "profile.default_content_setting_values.notifications": 2,
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
    }
    opts.add_experimental_option("prefs", prefs)

    chromedriver = ChromeDriverManager().install()
    log(f"ChromeDriver: {chromedriver}")
    driver = webdriver.Chrome(service=Service(chromedriver), options=opts)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)

    # En headless "new", il faut autoriser explicitement les téléchargements via CDP.
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(download_dir)},
        )
    except Exception:
        pass
    return driver


# ───────────────────────────────────────────────────────────────────────────────
# NAVIGATION VERS LA VUE CARTOGRAPHIQUE
# ───────────────────────────────────────────────────────────────────────────────

def _on_map_view(driver) -> bool:
    """Heuristique : présence du panneau Filtres / champ Conducteurs / boutons export."""
    try:
        markers = driver.find_elements(
            By.XPATH,
            "//*[contains(normalize-space(.),'Conducteurs')]"
            " | //button[contains(.,'Exporter Excel')]"
            " | //button[contains(.,'Exporter CSV')]"
            " | //*[contains(normalize-space(.),'Vue cartographique')]",
        )
        return any(m.is_displayed() for m in markers)
    except Exception:
        return False


def _click_text(driver, text: str, timeout: float = 6) -> bool:
    """Clique le premier élément cliquable contenant exactement (ou partiellement) `text`."""
    xpaths = [
        f"//a[normalize-space()='{text}']",
        f"//span[normalize-space()='{text}']",
        f"//li[normalize-space()='{text}']",
        f"//*[self::a or self::button or self::span or self::li or self::div]"
        f"[contains(normalize-space(.),'{text}')]",
    ]
    for xp in xpaths:
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xp))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            return True
        except TimeoutException:
            continue
        except Exception:
            continue
    return False


def navigate_to_map_view(driver, map_url: str | None) -> bool:
    """Atteint la Vue cartographique via URL directe, puis fallback navigation par menu."""
    # 1) URL fournie explicitement
    if map_url:
        qa._safe_get(driver, map_url)
        time.sleep(2)
        if _on_map_view(driver):
            log(f"✓ Vue cartographique (URL fournie): {driver.current_url}")
            return True

    # 2) URLs candidates connues
    for url in MAP_URL_CANDIDATES:
        qa._safe_get(driver, url)
        time.sleep(2)
        if _on_map_view(driver):
            log(f"✓ Vue cartographique (URL: {url})")
            return True

    # 3) Fallback : navigation par le menu latéral
    log("↪ Navigation par le menu : Géolocalisation → Vue cartographique")
    qa._safe_get(driver, f"{BASE_URL}/dashboard")
    time.sleep(2)
    _click_text(driver, "Géolocalisation", timeout=8)
    time.sleep(1)
    if not _click_text(driver, "Vue cartographique", timeout=8):
        # Variante d'orthographe éventuelle
        _click_text(driver, "Vue Cartographique", timeout=4)
    time.sleep(3)

    if _on_map_view(driver):
        log(f"✓ Vue cartographique (menu): {driver.current_url}")
        return True

    log(f"❌ Vue cartographique introuvable (URL actuelle: {driver.current_url})", "ERROR")
    return False


# ───────────────────────────────────────────────────────────────────────────────
# FILTRE "EN LIGNE" + EXPORT EXCEL
# ───────────────────────────────────────────────────────────────────────────────

# Composant @vueform/multiselect (Vue) — IDs stables relevés sur /map/gods_eye.
DRIVER_MODE_INPUT_ID = "select_driver_mode"
DRIVER_MODE_OPTION_ONLINE_ID = "select_driver_mode-multiselect-option-online"


def select_conducteurs_en_ligne(driver) -> bool:
    """Ouvre le multiselect 'Conducteurs' et choisit l'option 'En ligne'."""
    # 1) Ouvrir le menu : cliquer le wrapper du multiselect contenant l'input #select_driver_mode.
    opened = False
    try:
        inp = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, DRIVER_MODE_INPUT_ID))
        )
        # Remonter au conteneur .multiselect puis cliquer le wrapper pour déplier.
        try:
            wrapper = inp.find_element(
                By.XPATH, "./ancestor::div[contains(@class,'multiselect')][1]"
                          "//div[contains(@class,'multiselect-wrapper')]"
            )
        except Exception:
            wrapper = inp
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", wrapper)
        try:
            wrapper.click()
        except Exception:
            driver.execute_script("arguments[0].click();", wrapper)
        opened = True
    except Exception as e:
        log(f"❌ Impossible d'ouvrir le filtre Conducteurs: {e}", "ERROR")
        return False

    if not opened:
        return False

    time.sleep(0.8)

    # 2) Cliquer l'option 'En ligne' (ID stable, sinon repli sur le texte / aria-label).
    option_locators = [
        (By.ID, DRIVER_MODE_OPTION_ONLINE_ID),
        (By.CSS_SELECTOR, "li.multiselect-option[aria-label='En ligne']"),
        (By.XPATH, "//li[contains(@class,'multiselect-option')][normalize-space()='En ligne']"),
    ]
    for by, sel in option_locators:
        try:
            opt = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((by, sel))
            )
            try:
                opt.click()
            except Exception:
                driver.execute_script("arguments[0].click();", opt)
            time.sleep(0.5)
            # Vérifier la sélection (tag affiché ou aria-selected)
            if driver.find_elements(
                By.XPATH, "//*[contains(@class,'multiselect-tag')][contains(.,'En ligne')]"
            ) or (opt.get_attribute("aria-selected") == "true"):
                log("✓ Option 'En ligne' sélectionnée")
            else:
                log("✓ Option 'En ligne' cliquée")
            return True
        except Exception:
            continue

    log("❌ Option 'En ligne' introuvable dans le menu", "ERROR")
    return False


def click_button(driver, label: str, timeout: float = 8) -> bool:
    xpaths = [
        f"//button[normalize-space()='{label}']",
        f"//button[contains(normalize-space(.),'{label}')]",
        f"//*[self::a or self::button][contains(normalize-space(.),'{label}')]",
    ]
    for xp in xpaths:
        try:
            btn = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xp))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            try:
                btn.click()
            except Exception:
                driver.execute_script("arguments[0].click();", btn)
            log(f"✓ Bouton '{label}' cliqué")
            return True
        except Exception:
            continue
    log(f"❌ Bouton '{label}' introuvable", "ERROR")
    return False


def wait_for_download(download_dir: Path, before: set[str], timeout: float = 60) -> Path | None:
    """Attend l'apparition d'un nouveau fichier (xls/xlsx) téléchargé et stabilisé."""
    end = time.time() + timeout
    while time.time() < end:
        files = {p for p in download_dir.glob("*") if p.is_file()}
        new = [
            p for p in (files - before)
            if p.suffix.lower() in (".xls", ".xlsx")
            and not p.name.endswith(".crdownload")
        ]
        # Ignorer si un .crdownload est encore en cours
        in_progress = any(p.name.endswith(".crdownload") for p in files)
        if new and not in_progress:
            newest = max(new, key=lambda p: p.stat().st_mtime)
            # Stabilité : taille constante sur 2 lectures
            size1 = newest.stat().st_size
            time.sleep(1)
            if newest.exists() and newest.stat().st_size == size1 and size1 > 0:
                return newest
        time.sleep(1)
    return None


# ───────────────────────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Télécharge la liste des véhicules EN LIGNE en Excel (vue cartographique admin)."
    )
    parser.add_argument("--headed", action="store_true", help="Navigateur visible (debug).")
    parser.add_argument("--map-url", default=None, help="URL directe de la vue cartographique.")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Dossier de téléchargement du fichier Excel.",
    )
    parser.add_argument(
        "--export-label",
        default="Exporter Excel",
        help="Libellé du bouton d'export (défaut: 'Exporter Excel').",
    )
    args = parser.parse_args()

    download_dir = Path(args.output_dir).resolve()
    log(f"📂 Dossier de téléchargement: {download_dir}")

    driver = setup_driver(download_dir, headed=args.headed)
    try:
        # 1) Connexion admin (logique réutilisée)
        if not qa.admin_login(driver):
            log("❌ Connexion admin échouée.", "ERROR")
            return 2

        # 2) Vue cartographique
        if not navigate_to_map_view(driver, args.map_url):
            try:
                shot = download_dir / "debug_map_view.png"
                driver.save_screenshot(str(shot))
                log(f"🖼️  Capture debug: {shot}", "WARNING")
            except Exception:
                pass
            return 3

        # 3) Filtre Conducteurs = En ligne
        if not select_conducteurs_en_ligne(driver):
            return 4

        # 4) Appliquer
        click_button(driver, "Appliquer", timeout=8)
        time.sleep(3)  # laisser la carte/données se rafraîchir

        # 5) Exporter Excel
        before = {p for p in download_dir.glob("*") if p.is_file()}
        if not click_button(driver, args.export_label, timeout=10):
            return 5

        # 6) Attente du fichier
        log("⏳ Attente du fichier Excel...")
        downloaded = wait_for_download(download_dir, before, timeout=90)
        if downloaded:
            log(f"✅ Fichier téléchargé: {downloaded}")
            return 0

        log("❌ Aucun fichier Excel détecté après l'export.", "ERROR")
        return 6

    finally:
        time.sleep(1)
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
