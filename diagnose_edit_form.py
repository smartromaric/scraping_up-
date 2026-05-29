"""
diagnose_edit_form.py — LOCAL
Ouvre la page edit d'un conducteur en tant qu'admin et liste tous les
éléments interactifs du formulaire pour identifier les bons sélecteurs.
"""
import os, time
from pathlib import Path
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

BASE_URL  = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL = f"{BASE_URL}/login/admin"
EDIT_URL  = f"{BASE_URL}/fleet-drivers/edit/15258"
OUT       = Path(__file__).parent / "output" / "edit_form_debug.html"

def main():
    opts = Options()
    opts.add_argument("--window-size=1400,900")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    wait   = WebDriverWait(driver, 20)

    # Login admin
    print("🔐 Login admin...")
    driver.get(LOGIN_URL)
    email = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email'], input[name='email']")))
    pwd   = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
    email.send_keys(os.getenv("UPJUNOO_EMAIL", ""))
    pwd.send_keys(os.getenv("UPJUNOO_PASSWORD", ""))
    try:
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
    except:
        pwd.submit()
    wait.until(lambda d: "/login" not in d.current_url)
    print(f"✅ Connecté: {driver.current_url}")

    # Aller sur la page edit
    print(f"\n🌐 Navigation vers {EDIT_URL}...")
    driver.get(EDIT_URL)
    time.sleep(3)

    # Sauvegarder HTML
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(driver.page_source, encoding="utf-8")
    print(f"💾 HTML sauvegardé: {OUT}")

    # ── Tous les <select> ──────────────────────────────────────────
    selects = driver.find_elements(By.TAG_NAME, "select")
    print(f"\n📋 {len(selects)} <select> trouvés:")
    for i, s in enumerate(selects):
        opts_text = [o.text.strip() for o in s.find_elements(By.TAG_NAME, "option") if o.text.strip()]
        print(f"  [{i}] id='{s.get_attribute('id')}' name='{s.get_attribute('name')}' | {len(opts_text)} options")
        print(f"       Premières options: {opts_text[:6]}")

    # ── Tous les <input> ───────────────────────────────────────────
    inputs = driver.find_elements(By.TAG_NAME, "input")
    print(f"\n📋 {len(inputs)} <input> trouvés:")
    for inp in inputs:
        print(f"  type='{inp.get_attribute('type')}' id='{inp.get_attribute('id')}' "
              f"name='{inp.get_attribute('name')}' value='{inp.get_attribute('value')}' "
              f"placeholder='{inp.get_attribute('placeholder')}'")

    # ── Tous les <button> ──────────────────────────────────────────
    buttons = driver.find_elements(By.TAG_NAME, "button")
    print(f"\n📋 {len(buttons)} <button> trouvés:")
    for b in buttons:
        print(f"  type='{b.get_attribute('type')}' id='{b.get_attribute('id')}' text='{b.text.strip()[:40]}'")

    input("\n👆 Inspecte la page dans le navigateur, puis appuie sur [ENTRÉE] pour fermer...")
    driver.quit()

if __name__ == "__main__":
    main()
