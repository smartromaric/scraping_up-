"""
Script de diagnostic - Capture le HTML du formulaire de création de flotte
pour identifier les bons sélecteurs CSS/XPath.
"""

import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "https://upjunoo-server-new.junooapps.com"
CREATE_FLEET_URL = f"{BASE_URL}/manage-fleet/create"

def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-notifications")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)

def main():
    driver = setup_driver()
    driver.get(f"{BASE_URL}/login/owner-login")
    
    input("\n👉 Connectez-vous puis allez sur la page CRÉER de la flotte, puis appuyez sur [ENTRÉE]...")
    
    time.sleep(2)
    
    # S'assurer qu'on est sur la bonne page
    if "create" not in driver.current_url:
        driver.get(CREATE_FLEET_URL)
        time.sleep(3)
    
    print("\n" + "="*80)
    print("📋 DIAGNOSTIC DU FORMULAIRE")
    print("="*80)
    
    # 1. Capturer TOUS les <select> natifs
    selects = driver.find_elements(By.TAG_NAME, "select")
    print(f"\n🔹 Nombre de <select> natifs trouvés : {len(selects)}")
    for i, sel in enumerate(selects):
        print(f"  Select #{i}: id='{sel.get_attribute('id')}' name='{sel.get_attribute('name')}' class='{sel.get_attribute('class')}'")
        options = sel.find_elements(By.TAG_NAME, "option")
        for opt in options:
            print(f"    Option: value='{opt.get_attribute('value')}' text='{opt.text}'")
    
    # 2. Capturer TOUS les <input>
    inputs = driver.find_elements(By.TAG_NAME, "input")
    print(f"\n🔹 Nombre de <input> trouvés : {len(inputs)}")
    for i, inp in enumerate(inputs):
        print(f"  Input #{i}: type='{inp.get_attribute('type')}' id='{inp.get_attribute('id')}' name='{inp.get_attribute('name')}' placeholder='{inp.get_attribute('placeholder')}' class='{inp.get_attribute('class')}'")
    
    # 3. Capturer TOUS les <button>
    buttons = driver.find_elements(By.TAG_NAME, "button")
    print(f"\n🔹 Nombre de <button> trouvés : {len(buttons)}")
    for i, btn in enumerate(buttons):
        print(f"  Button #{i}: text='{btn.text}' type='{btn.get_attribute('type')}' id='{btn.get_attribute('id')}' class='{btn.get_attribute('class')}'")
    
    # 4. Chercher les éléments de type dropdown custom (div avec role=listbox, combobox, etc.)
    custom_selects = driver.find_elements(By.CSS_SELECTOR, "[role='listbox'], [role='combobox'], [role='select'], [class*='select'], [class*='dropdown'], [class*='Select']")
    print(f"\n🔹 Nombre d'éléments custom select/dropdown trouvés : {len(custom_selects)}")
    for i, cs in enumerate(custom_selects):
        tag = cs.tag_name
        text = cs.text[:100] if cs.text else ""
        print(f"  Custom #{i}: tag='{tag}' role='{cs.get_attribute('role')}' class='{cs.get_attribute('class')}' text='{text}'")
    
    # 5. Chercher tout élément contenant le texte "type" ou "Sélectionner"
    type_elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'type') or contains(text(), 'Type') or contains(text(), 'Sélectionner')]")
    print(f"\n🔹 Éléments contenant 'type' ou 'Sélectionner' : {len(type_elements)}")
    for i, el in enumerate(type_elements):
        print(f"  Element #{i}: tag='{el.tag_name}' text='{el.text[:80]}' class='{el.get_attribute('class')}'")

    # 6. Capturer tout le HTML du formulaire (form tag ou la zone principale)
    forms = driver.find_elements(By.TAG_NAME, "form")
    print(f"\n🔹 Nombre de <form> trouvés : {len(forms)}")
    
    # 7. Dumper le HTML de la zone principale du contenu
    main_content = driver.find_elements(By.CSS_SELECTOR, "main, .main-content, .content, #content, .page-content, [class*='create']")
    print(f"\n🔹 Zones de contenu principal trouvées : {len(main_content)}")
    
    # Sauvegarder le HTML complet de la page dans un fichier
    page_html = driver.page_source
    with open("form_debug.html", "w", encoding="utf-8") as f:
        f.write(page_html)
    print(f"\n💾 HTML complet sauvegardé dans form_debug.html ({len(page_html)} caractères)")
    
    # 8. Chercher les <label> pour comprendre la structure
    labels = driver.find_elements(By.TAG_NAME, "label")
    print(f"\n🔹 Nombre de <label> trouvés : {len(labels)}")
    for i, lbl in enumerate(labels):
        print(f"  Label #{i}: text='{lbl.text}' for='{lbl.get_attribute('for')}' class='{lbl.get_attribute('class')}'")
    
    print("\n" + "="*80)
    print("✅ DIAGNOSTIC TERMINÉ")
    print("="*80)
    
    input("\nAppuyez sur [ENTRÉE] pour fermer...")
    driver.quit()

if __name__ == "__main__":
    main()
