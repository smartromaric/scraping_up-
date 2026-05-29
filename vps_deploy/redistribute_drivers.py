"""
redistribute_drivers.py — LOCAL (non-headless)
================================================
Répartit les conducteurs non assignés vers les partenaires incomplets.

Étapes :
  1. Charge le JSON des conducteurs non assignés (avec champ "edit")
  2. Login admin automatique
  3. Récupère la liste des partenaires depuis le dropdown de l'interface
  4. Charge les compteurs actuels depuis drivers_par_partenaire.csv
     (nouveaux partenaires absents du CSV → compteur 0)
  5. Construit le plan d'allocation :
       Passe 1 : partenaires 1–99 → compléter jusqu'à 100
       Passe 2 : partenaires à 0  → alimenter jusqu'à 100
  6. Affiche le plan en mode dry-run
  7. Exécute l'assignation et met à jour le JSON source en temps réel :
       "status": "assigné", "assigned_to": "Partenaire-XX"
     → reprend là où il s'était arrêté si relancé

Usage :
  python3 redistribute_drivers.py --json output/conducteurs_non_assignes_XXX.json --partners-json /path/to/partenaires_XXX.json
  python3 redistribute_drivers.py --json output/conducteurs_non_assignes_XXX.json --partners-json /path/to/partenaires_XXX.json --run
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

BASE_URL        = "https://upjunoo-server-new.junooapps.com"
LOGIN_URL       = f"{BASE_URL}/login/admin"
DRIVERS_URL     = f"{BASE_URL}/fleet-drivers"
OUTPUT_DIR      = Path(__file__).parent / "output"
PARTNERS_CSV    = OUTPUT_DIR / "drivers_par_partenaire.csv"
PAGE_TIMEOUT    = 20
TARGET_COUNT    = 100


# ═════════════════════════════════════════════════════════════════════════════
#  CHROME LOCAL
# ═════════════════════════════════════════════════════════════════════════════

def setup_driver(headless: bool = False):
    import shutil
    chrome_options = Options()
    chrome_options.add_argument("--disable-notifications")
    if headless:
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-setuid-sandbox")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--single-process")
        chrome_options.add_argument("--remote-debugging-port=9223")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.0")
        for binary in ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable"]:
            path = shutil.which(binary)
            if path:
                chrome_options.binary_location = path
                print(f"   ✅ Binaire Chrome: {path}")
                break
        chromedriver_path = None
        for cd in ["chromedriver", "/usr/bin/chromedriver",
                   "/usr/lib/chromium-browser/chromedriver", "/usr/lib/chromium/chromedriver"]:
            if os.path.isfile(cd) or shutil.which(cd):
                chromedriver_path = cd if os.path.isfile(cd) else shutil.which(cd)
                print(f"   ✅ ChromeDriver: {chromedriver_path}")
                break
        service = Service(chromedriver_path) if chromedriver_path else Service(ChromeDriverManager().install())
    else:
        chrome_options.add_argument("--window-size=1400,900")
        service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


# ═════════════════════════════════════════════════════════════════════════════
#  LOGIN ADMIN
# ═════════════════════════════════════════════════════════════════════════════

def auto_login(driver, email: str, password: str) -> bool:
    print(f"🔐 Connexion admin sur {LOGIN_URL}...")
    wait = WebDriverWait(driver, 30)
    try:
        driver.get(LOGIN_URL)
        time.sleep(3)

        email_input = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
        )))
        pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password']")

        email_input.clear()
        email_input.send_keys(email)
        time.sleep(0.3)
        pwd_input.clear()
        pwd_input.send_keys(password)
        time.sleep(0.3)

        try:
            btn = driver.find_element(By.XPATH, "//button[@type='submit']")
            btn.click()
        except:
            pwd_input.submit()

        wait.until(lambda d: "/login" not in d.current_url)
        print(f"✅ Connecté: {driver.current_url}")
        return True
    except Exception as e:
        print(f"❌ Erreur login: {e}")
        # Sauvegarder le HTML pour debug
        try:
            debug = OUTPUT_DIR / "login_debug.html"
            debug.write_text(driver.page_source, encoding="utf-8")
            print(f"   💾 Debug HTML: {debug}")
        except:
            pass
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  CHARGEMENT DONNÉES
# ═════════════════════════════════════════════════════════════════════════════

def load_unassigned_drivers(json_path: Path) -> list:
    """Charge les conducteurs non assignés. Skip ceux déjà assignés."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    total    = len(data)
    pending  = [d for d in data if d.get("status") != "assigné"]
    assigned = total - len(pending)
    print(f"📂 {json_path.name}: {total} conducteurs ({assigned} déjà assignés, {len(pending)} restants)")
    return data


def save_drivers_json(json_path: Path, data: list):
    """Sauvegarde le JSON en préservant l'ordre."""
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_partners_data(partners_json_path: Path) -> list:
    """
    Charge les données complètes des partenaires depuis le JSON de scrape_partners_vps.py.
    Retourne liste de {"nom", "owner_id", "count"}.
    """
    data = json.loads(partners_json_path.read_text(encoding="utf-8"))
    result = []
    for p in data:
        nom      = p.get("nom", "").strip()
        owner_id = p.get("owner_id", "").strip()
        # owner_id peut aussi être dans l'URL du profil
        if not owner_id and p.get("profile_url"):
            owner_id = p["profile_url"].rstrip("/").split("/")[-1]
        count = len(p.get("drivers", []))
        if nom and owner_id:
            result.append({"nom": nom, "owner_id": owner_id, "count": count})
    print(f"📊 {len(result)} partenaires chargés depuis {partners_json_path.name}")
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  DÉTECTION DU SÉLECTEUR DE PARTENAIRE
# ═════════════════════════════════════════════════════════════════════════════

def detect_partner_select(driver, first_driver_id: str) -> str:
    """
    Ouvre la page d'édition pour trouver le <select> partenaire.
    Sauvegarde le HTML et liste tous les selects + options trouvées.
    Retourne le système de sélection utilisé (id ou css selector).
    """
    edit_url = f"{BASE_URL}/fleet-drivers/edit/{first_driver_id}"
    print(f"\n🔍 Analyse du formulaire sur {edit_url}...")
    driver.get(edit_url)
    time.sleep(3)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    debug_path = OUTPUT_DIR / "edit_form_debug.html"
    debug_path.write_text(driver.page_source, encoding="utf-8")
    print(f"   💾 HTML sauvegardé: {debug_path}")

    # Lister tous les selects avec leurs options pour identifier le bon
    all_selects = driver.find_elements(By.TAG_NAME, "select")
    print(f"   📊 {len(all_selects)} <select> sur la page:")
    for s in all_selects:
        opts = [o.text.strip() for o in s.find_elements(By.TAG_NAME, "option") if o.text.strip()]
        print(f"      id='{s.get_attribute('id')}' name='{s.get_attribute('name')}' options={opts[:5]}...")

    # Chercher le select partenaire (confirmé localement: id='owner')
    candidates = [
        (By.ID,           "owner"),
        (By.CSS_SELECTOR, "select[id*='owner']"),
        (By.CSS_SELECTOR, "select[id*='partner']"),
        (By.ID,           "select_partner"),
        (By.ID,           "owner_id"),
    ]
    for by, val in candidates:
        try:
            el = driver.find_element(by, val)
            opts = [o.text.strip() for o in el.find_elements(By.TAG_NAME, "option") if o.text.strip()]
            print(f"   ✅ Select trouvé: {by}='{val}' | {len(opts)} options ex: {opts[:3]}")
            return val
        except NoSuchElementException:
            continue

    print(f"   ❌ Aucun select partenaire trouvé — voir {debug_path}")
    return None


# ═════════════════════════════════════════════════════════════════════════════
#  PLAN D'ALLOCATION
# ═════════════════════════════════════════════════════════════════════════════

def build_allocation_plan(partners_data: list) -> list:
    """
    Construit le plan d'allocation trié :
      Passe 1 : partenaires avec 1–99 conducteurs (plus proches de 100 en premier)
      Passe 2 : partenaires avec 0 conducteurs

    Retourne liste de {"nom", "owner_id", "current", "need", "passe"}
    """
    passe1 = []
    passe2 = []

    for p in partners_data:
        current = p["count"]
        if 1 <= current < TARGET_COUNT:
            need = TARGET_COUNT - current
            passe1.append({**p, "current": current, "need": need, "passe": 1})
        elif current == 0:
            passe2.append({**p, "current": 0, "need": TARGET_COUNT, "passe": 2})

    def partner_num(p):
        import re
        m = re.search(r"\d+", p["nom"])
        return int(m.group()) if m else 9999

    passe1.sort(key=lambda x: x["current"], reverse=True)
    passe2.sort(key=partner_num)
    return passe1 + passe2


def print_plan(plan: list, available_drivers: int):
    total_needed = sum(p["need"] for p in plan)
    print(f"\n{'='*60}")
    print(f"📋 PLAN D'ALLOCATION")
    print(f"{'='*60}")
    print(f"   Conducteurs disponibles : {available_drivers}")
    print(f"   Conducteurs nécessaires : {total_needed}")
    print(f"   Partenaires à traiter   : {len(plan)}")
    print()

    drivers_left = available_drivers
    for i, p in enumerate(plan, 1):
        will_assign = min(p["need"], drivers_left)
        status = "✅" if will_assign == p["need"] else "⚠️  partiel"
        print(f"  [{i:3}] Passe {p['passe']} | {p['nom']:<30} | {p['current']:>3} → {p['current']+will_assign:>3} (+{will_assign}) {status}")
        drivers_left -= will_assign
        if drivers_left <= 0:
            remaining = len(plan) - i
            if remaining > 0:
                print(f"\n  ⛔  Pool épuisé — {remaining} partenaire(s) ne pourront pas être traités")
            break

    print(f"\n  📦 Conducteurs utilisés  : {min(total_needed, available_drivers)}/{available_drivers}")
    print(f"{'='*60}\n")


# ═════════════════════════════════════════════════════════════════════════════
#  ASSIGNATION
# ═════════════════════════════════════════════════════════════════════════════

def assign_driver(driver, driver_entry: dict, target_owner_id: str,
                  target_name: str, select_selector: str) -> bool:
    """
    Ouvre la page d'édition du conducteur, change le partenaire, sauvegarde.
    Retourne True si succès.
    """
    driver_id = None
    if driver_entry.get("edit") and "/edit/" in driver_entry["edit"]:
        driver_id = driver_entry["edit"].split("/edit/")[-1]
    elif driver_entry.get("document_url") and "/document/" in driver_entry["document_url"]:
        driver_id = driver_entry["document_url"].split("/document/")[-1]

    if not driver_id:
        print(f"   ⚠️  ID introuvable pour {driver_entry['nom']}")
        return False

    edit_url = f"{BASE_URL}/fleet-drivers/edit/{driver_id}"
    try:
        driver.get(edit_url)
        wait = WebDriverWait(driver, PAGE_TIMEOUT)

        # Attendre que le select partenaire soit présent (peut être caché sous un custom dropdown)
        wait.until(EC.presence_of_element_located((By.ID, select_selector)))
        # Attendre que les options soient chargées via JS (> 50) — cibler select#id explicitement
        js_selector = f"select#{select_selector}"
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script(
                "var s=document.querySelector(arguments[0]); return s ? s.options.length : 0;",
                js_selector
            ) > 50
        )
        # Sélectionner via JavaScript (fonctionne même si le select est caché)
        matched = driver.execute_script("""
            var sel = document.querySelector(arguments[0]);
            var name = arguments[1].toLowerCase().trim();
            for (var i = 0; i < sel.options.length; i++) {
                if (sel.options[i].text.trim().toLowerCase() === name) {
                    sel.selectedIndex = i;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    sel.dispatchEvent(new Event('input',  {bubbles: true}));
                    return sel.options[i].text;
                }
            }
            return null;
        """, js_selector, target_name)
        if not matched:
            raise Exception(f"Option '{target_name}' non trouvée dans le select")
        print(f"[sélectionné: {matched}] ", end="", flush=True)
        time.sleep(0.8)

        # Cliquer sur le bouton "Mise à jour" via JS (plusieurs submit buttons sur la page)
        saved = driver.execute_script("""
            var buttons = document.querySelectorAll('button[type="submit"]');
            for (var b of buttons) {
                if (b.textContent.trim().toLowerCase().includes('mise') ||
                    b.textContent.trim().toLowerCase().includes('update') ||
                    b.textContent.trim().toLowerCase().includes('enregistrer')) {
                    b.click();
                    return b.textContent.trim();
                }
            }
            return null;
        """)
        if not saved:
            raise Exception("Bouton 'Mise à jour' introuvable")
        time.sleep(2)
        return True
    except Exception as e:
        print(f"   ❌ {driver_entry['nom']}: [{type(e).__name__}] {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Redistribution des conducteurs non assignés")
    parser.add_argument(
        "--json", required=True,
        help="Chemin vers le fichier JSON des conducteurs non assignés"
    )
    parser.add_argument(
        "--run", action="store_true",
        help="Exécuter l'assignation (sans ce flag = dry-run uniquement)"
    )
    parser.add_argument(
        "--partners-json", required=True,
        help="Chemin vers partenaires_YYYYMMDD.json (output de scrape_partners_vps.py)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limiter le nombre d'assignations (ex: --limit 1 pour tester)"
    )
    parser.add_argument("--headless", action="store_true", help="Mode headless (VPS)")
    parser.add_argument("--email",    default=os.getenv("UPJUNOO_EMAIL"),    help="Email admin")
    parser.add_argument("--password", default=os.getenv("UPJUNOO_PASSWORD"), help="Mot de passe admin")
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"❌ Fichier introuvable: {json_path}")
        sys.exit(1)

    if not args.email or not args.password:
        print("❌ UPJUNOO_EMAIL et UPJUNOO_PASSWORD requis (.env ou --email/--password)")
        sys.exit(1)

    # 1. Charger les conducteurs
    all_drivers = load_unassigned_drivers(json_path)
    pending     = [d for d in all_drivers if d.get("status") != "assigné"]

    if not pending:
        print("✅ Tous les conducteurs ont déjà été assignés.")
        sys.exit(0)

    # 2. Charger les données partenaires
    partners_json_path = Path(args.partners_json)
    if not partners_json_path.exists():
        print(f"⚠️  Fichier partenaires introuvable: {partners_json_path}")
        sys.exit(1)
    partners_data = load_partners_data(partners_json_path)

    # 3. Démarrer Chrome
    print("\n🌐 Démarrage du navigateur...")
    selenium_driver = setup_driver(headless=args.headless)

    try:
        # 4. Login admin
        if not auto_login(selenium_driver, args.email, args.password):
            sys.exit(1)

        # 5. Détecter le sélecteur du select partenaire
        first_id = None
        for d in pending:
            if d.get("edit") and "/edit/" in d["edit"]:
                first_id = d["edit"].split("/edit/")[-1]
                break
            elif d.get("document_url") and "/document/" in d["document_url"]:
                first_id = d["document_url"].split("/document/")[-1]
                break

        select_selector = detect_partner_select(selenium_driver, first_id)
        if not select_selector:
            print("❌ Select partenaire introuvable — voir output/edit_form_debug.html")
            sys.exit(1)

        # 6. Construire le plan
        plan = build_allocation_plan(partners_data)

        if not plan:
            print("✅ Aucun partenaire incomplet trouvé — rien à faire.")
            print("   (Relance scrape_partners_vps.py pour mettre à jour drivers_par_partenaire.csv)")
            sys.exit(0)

        # 7. Afficher le plan
        print_plan(plan, len(pending))

        if not args.run:
            print("ℹ️  Mode DRY-RUN — aucune modification. Ajoute --run pour exécuter.")
            return

        # 8. Confirmation
        print(f"\n⚠️  Prêt à assigner {min(sum(p['need'] for p in plan), len(pending))} conducteurs.")
        confirm = input("   Confirmer ? [o/N]: ").strip().lower()
        if confirm not in ("o", "oui", "y", "yes"):
            print("❌ Annulé.")
            return

        # 9. Exécution
        driver_pool  = list(pending)  # conducteurs non encore assignés
        pool_index   = 0
        total_ok     = 0
        total_fail   = 0

        limit = args.limit
        assigned_total = 0

        for partner in plan:
            if pool_index >= len(driver_pool):
                print("⛔  Pool de conducteurs épuisé.")
                break
            if limit is not None and assigned_total >= limit:
                print(f"⛔  Limite de {limit} assignation(s) atteinte.")
                break

            assigned_count = 0
            print(f"\n🏷️  Partenaire: {partner['nom']} (besoin: {partner['need']})")

            while assigned_count < partner["need"] and pool_index < len(driver_pool):
                if limit is not None and assigned_total >= limit:
                    break
                d = driver_pool[pool_index]
                print(f"   [{pool_index+1}/{len(driver_pool)}] {d['nom']}...", end=" ", flush=True)

                ok = assign_driver(selenium_driver, d, partner["owner_id"], partner["nom"], select_selector)

                if ok:
                    d["status"]      = "assigné"
                    d["assigned_to"] = partner["nom"]
                    save_drivers_json(json_path, all_drivers)
                    assigned_total += 1
                    print(f"✅ → {partner['nom']}")
                    assigned_count += 1
                    total_ok       += 1
                else:
                    print(f"❌ échec")
                    total_fail += 1

                pool_index += 1
                time.sleep(0.8)

        # 10. Bilan
        print(f"\n{'='*60}")
        print(f"🎉 TERMINÉ")
        print(f"   ✅ Assignés avec succès : {total_ok}")
        print(f"   ❌ Échecs               : {total_fail}")
        print(f"   📦 Restants dans le pool: {len(driver_pool) - pool_index}")
        print(f"   💾 Fichier mis à jour   : {json_path}")
        print(f"{'='*60}")

    except KeyboardInterrupt:
        print("\n\n⚠️  Interrompu par l'utilisateur — progression sauvegardée dans le JSON.")
    finally:
        selenium_driver.quit()


if __name__ == "__main__":
    main()
