"""
Diagnostic : login auto + ouverture /fleet-drivers + dump des éléments
de pagination pour valider les sélecteurs utilisés par scrape_drivers.py.

Usage :
    export UPJUNOO_EMAIL="admin@upjunoo.com"
    export UPJUNOO_PASSWORD='123456789'
    python inspect_pagination.py
"""

import os
import time
from getpass import getpass
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL = f"{BASE_URL}/login/admin"
DRIVERS_URL = f"{BASE_URL}/fleet-drivers"
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def get_credentials():
    email = os.getenv("UPJUNOO_EMAIL") or input("📧 Email : ").strip()
    password = os.getenv("UPJUNOO_PASSWORD") or getpass("🔑 Mot de passe : ")
    return email, password


def auto_login(driver, email, password):
    print(f"\n🔐 Login sur {LOGIN_URL}...")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 30)

    email_input = wait.until(EC.presence_of_element_located((
        By.CSS_SELECTOR,
        "input[type='email'], input[name='email'], input[placeholder*='mail' i]",
    )))
    pwd_input = driver.find_element(
        By.CSS_SELECTOR, "input[type='password'], input[name='password']"
    )
    email_input.clear(); email_input.send_keys(email)
    pwd_input.clear(); pwd_input.send_keys(password)

    try:
        btn = driver.find_element(
            By.XPATH,
            "//button[@type='submit'] | //button[contains(translate(.,'LOGIN','login'),'login')]"
            " | //button[contains(translate(.,'CONNEXION','connexion'),'connexion')]"
            " | //button[contains(translate(.,'SE CONNECTER','se connecter'),'se connecter')]",
        )
        btn.click()
    except NoSuchElementException:
        pwd_input.submit()

    wait.until(lambda d: "/login" not in d.current_url)
    print(f"  ✅ Connecté. URL : {driver.current_url}")


def dump_candidates(driver):
    print("\n" + "=" * 70)
    print("🔍 ANALYSE DES BOUTONS DE PAGINATION")
    print("=" * 70)

    # 1. Ant Design pagination
    for sel in [
        "ul.ant-pagination li",
        "li.ant-pagination-next",
        "li.ant-pagination-prev",
        ".ant-pagination-options",
    ]:
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els:
            print(f"\n[AntD] '{sel}' → {len(els)} element(s)")
            for i, el in enumerate(els[:10]):
                print(f"  #{i} class='{el.get_attribute('class')}' text='{el.text[:60]}'")

    # 2. Pagination générique
    for sel in [
        ".pagination li",
        ".pagination a",
        "nav[aria-label*='pag' i] button",
        "button[aria-label*='next' i]",
        "button[aria-label*='suivant' i]",
        "a[aria-label*='next' i]",
    ]:
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els:
            print(f"\n[Generic] '{sel}' → {len(els)} element(s)")
            for i, el in enumerate(els[:10]):
                print(f"  #{i} tag={el.tag_name} class='{el.get_attribute('class')}' "
                      f"aria-label='{el.get_attribute('aria-label')}' text='{el.text[:60]}'")

    # 3. Page size selector
    for sel in [
        "select",
        ".ant-select",
        ".ant-pagination-options-size-changer",
    ]:
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els:
            print(f"\n[PageSize] '{sel}' → {len(els)} element(s)")
            for i, el in enumerate(els[:5]):
                print(f"  #{i} tag={el.tag_name} class='{el.get_attribute('class')}' "
                      f"text='{el.text[:80]}'")

    # 4. Nombre de lignes actuellement chargées
    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    print(f"\n📊 Lignes du tableau actuellement : {len(rows)}")
    if rows:
        print(f"  première ligne : {rows[0].text[:120]}")

    # 5. Sauvegarder le HTML de la zone bas de page
    html_path = OUTPUT_DIR / "pagination_debug.html"
    try:
        # chercher un conteneur "pagination" si possible
        el = driver.find_element(By.CSS_SELECTOR, "ul.ant-pagination, .pagination, nav[aria-label*='pag' i]")
        html_path.write_text(el.get_attribute("outerHTML"), encoding="utf-8")
        print(f"\n💾 HTML pagination → {html_path}")
    except NoSuchElementException:
        (OUTPUT_DIR / "page_full.html").write_text(driver.page_source, encoding="utf-8")
        print(f"\n💾 Zone pagination non trouvée, page complète → output/page_full.html")


def main():
    email, password = get_credentials()
    opts = Options()
    opts.add_argument("--window-size=1600,1000")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

    try:
        auto_login(driver, email, password)

        print(f"\n📍 Ouverture de {DRIVERS_URL}...")
        driver.get(DRIVERS_URL)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
            )
        except TimeoutException:
            print("  ⚠️  Tableau non détecté.")

        print("\n👉 Régle la pagination sur 500 dans le navigateur, puis appuie sur [ENTRÉE]...")
        input()

        dump_candidates(driver)

        print("\n✅ Diagnostic terminé. Copie/colle la sortie ici.")
        input("Appuie sur [ENTRÉE] pour fermer le navigateur...")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
