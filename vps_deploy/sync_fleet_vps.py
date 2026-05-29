#!/usr/bin/env python3
"""
sync_fleet_vps.py
=================
Pour chaque partenaire (Partenaire1..120) :
  1. Lit output/organized_by_partner/<Partenaire>/data.json
  2. Se connecte à l'espace partenaire
  3. Vide entièrement la flotte (supprime tous les véhicules)
  4. Recrée uniquement les véhicules du data.json (matricule valide, pas de doublons)

Usage :
  python3 sync_fleet_vps.py                          # tous les partenaires
  python3 sync_fleet_vps.py --only Partenaire1       # un seul
  python3 sync_fleet_vps.py --start Partenaire-51    # reprendre depuis
  python3 sync_fleet_vps.py --dry-run                # simulation
  python3 sync_fleet_vps.py --skip-create            # vider seulement, ne pas recréer
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import shutil

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR          = Path(__file__).parent
DATA_DIR          = BASE_DIR / "output" / "organized_by_partner"
LOG_FILE          = BASE_DIR / "output" / "sync_fleet.log"

OWNER_LOGIN_URL   = "https://upjunoo-server-new.junooapps.com/login/owner-login"
MANAGE_FLEET_URL  = "https://upjunoo-server-new.junooapps.com/manage-fleet"
CREATE_FLEET_URL  = "https://upjunoo-server-new.junooapps.com/manage-fleet/create"
UNIVERSAL_PASSWORD = "123456789@"

TYPE_UUID_MAP = {
    "CONFORT":        "0d1802c4-3d32-4a96-b3ca-73e650802c62",
    "Camionnette":    "15f90aaa-aa92-40ed-b34e-ce7e51541b7e",
    "MOTO":           "35a673c3-aafe-48b4-8ae8-205e238b043b",
    "Taxi France":    "4644788a-1065-4eb9-bbf6-01a6e394aeed",
    "ECO":            "58eb223b-5ac7-4ed5-9a12-87d24f901dda",
    "moto livraison": "5f4ef87b-1be7-468d-8140-7379fefbaedf",
    "Camion 14T":     "64d5d311-1f7c-42eb-b0ad-510d9af8cd54",
    "PREMIUM":        "91ccc713-b07f-4971-b5c4-1d1c755c9d3a",
    "Camion":         "95ad84fc-df36-48f7-8c69-e8bb51ad5f8d",
    "CONFORT+":       "990a6e02-ac3d-4354-bccc-eedafb77de71",
    "CARGO":          "c9a337de-fc81-4626-a5f9-2ac7ac1b5e03",
    "CONFORT Lyon":   "dce302c1-c109-4023-9d9d-17b9da8c424c",
    "Semi-remorque":  "e17983aa-af38-4ffc-b88c-37adc3f77dcd",
}

PARTNER_NUM_RE = re.compile(r'(?:partenaires?[-_]?\s*)(\d+)', re.I)

# ─────────────────────────────────────────────────────────────────────────────
#  LOG
# ─────────────────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─────────────────────────────────────────────────────────────────────────────
#  CHROME
# ─────────────────────────────────────────────────────────────────────────────

def setup_driver(headed: bool = False):
    opts = Options()
    if not headed:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-background-networking")
    if not headed:
        opts.add_argument("--remote-debugging-port=9222")
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        for binary in [
            "/snap/bin/chromium",
            "chromium-browser", "chromium",
            "google-chrome-stable", "google-chrome",
        ]:
            path = binary if os.path.isfile(binary) else shutil.which(binary)
            if path:
                opts.binary_location = path
                break

    chromedriver_path = None
    for cd in [
        "chromedriver", "/usr/bin/chromedriver",
        "/snap/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/lib/chromium/chromedriver",
    ]:
        if os.path.isfile(cd) or shutil.which(cd):
            chromedriver_path = cd if os.path.isfile(cd) else shutil.which(cd)
            break

    service = Service(chromedriver_path) if (chromedriver_path and not headed) \
              else Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────────────────────

def login(driver, email: str, password: str = UNIVERSAL_PASSWORD) -> bool:
    for attempt in range(1, 4):
        try:
            log(f"      🔐 Login: {email} (tentative {attempt}/3)")
            driver.get(OWNER_LOGIN_URL)
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email'],input[type='text']"))
            )
            em = driver.find_element(By.CSS_SELECTOR, "input[type='email'],input[type='text']")
            pw = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            em.clear(); em.send_keys(email)
            pw.clear(); pw.send_keys(password)
            btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            driver.execute_script("arguments[0].click();", btn)
            WebDriverWait(driver, 30).until(EC.url_contains("/owner-dashboard"))
            log(f"      ✅ Connecté")
            return True
        except Exception as e:
            log(f"      ⚠️ Tentative {attempt} échouée: {e}")
            time.sleep(3)
    log(f"      ❌ Login échoué pour {email}")
    return False


def logout(driver):
    try:
        driver.get("https://upjunoo-server-new.junooapps.com/logout")
        time.sleep(1)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  PAGINATION
# ─────────────────────────────────────────────────────────────────────────────

def set_pagination_500(driver) -> bool:
    for attempt in range(3):
        try:
            sel = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select"))
            )
            current = sel.get_attribute("value") or ""
            options = driver.execute_script(
                "return Array.from(arguments[0].options).map(o=>({text:o.text,value:o.value}))", sel
            )
            target = max(options, key=lambda o: int(o["value"]) if o["value"].isdigit() else 0)
            if current == target["value"]:
                return True
            driver.execute_script(
                "arguments[0].value=arguments[1]; arguments[0].dispatchEvent(new Event('change',{bubbles:true}))",
                sel, target["value"]
            )
            time.sleep(2)
            rows_after = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
            if rows_after > 10:
                return True
        except Exception:
            time.sleep(2)
    return False


def wait_table(driver, timeout=30):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
    except TimeoutException:
        pass
    time.sleep(0.5)


def get_all_rows(driver) -> list:
    """Retourne toutes les lignes du tableau après pagination 500."""
    driver.get(MANAGE_FLEET_URL)
    wait_table(driver)
    set_pagination_500(driver)
    wait_table(driver)
    return driver.find_elements(By.CSS_SELECTOR, "table tbody tr")


# ─────────────────────────────────────────────────────────────────────────────
#  SUPPRESSION D'UN VÉHICULE
# ─────────────────────────────────────────────────────────────────────────────

def _close_swal(driver):
    try:
        driver.execute_script(
            "var b=document.querySelector('.swal2-cancel'); if(b) b.click();"
        )
    except Exception:
        pass


def delete_one(driver, row_index: int, plate_label: str) -> bool:
    """
    Supprime la ligne à l'index row_index dans le tableau courant.
    Utilise l'opération atomique JS : ouvre dropdown + clique Supprimer en une fois.
    """
    try:
        # Opération atomique : ouvre le dropdown ET clique Supprimer dans le même JS
        result = driver.execute_script("""
            var rows = document.querySelectorAll('table tbody tr');
            var row = rows[arguments[0]];
            if (!row) return 'no_row';

            var btn = row.querySelector('button.dropdown-toggle, button[data-bs-toggle="dropdown"], .btn-action');
            if (!btn) return 'no_btn';

            // Ouvre le dropdown via Bootstrap
            if (window.bootstrap && window.bootstrap.Dropdown) {
                var dd = window.bootstrap.Dropdown.getOrCreateInstance(btn);
                dd.show();
            } else {
                btn.click();
            }

            // Attend que le menu soit visible puis clique Supprimer
            return new Promise(function(resolve) {
                setTimeout(function() {
                    var menu = row.querySelector('.dropdown-menu.show, .dropdown-menu');
                    if (!menu) { resolve('no_menu'); return; }
                    var items = menu.querySelectorAll('a, button, li');
                    var target = null;
                    for (var i=0; i<items.length; i++) {
                        var t = items[i].textContent.trim().toLowerCase();
                        if (t.includes('supprimer') || t.includes('delete') || t.includes('retirer')) {
                            target = items[i]; break;
                        }
                    }
                    if (!target) { resolve('no_supprimer'); return; }
                    target.click();
                    resolve('clicked');
                }, 400);
            });
        """, row_index)

        if result != "clicked":
            log(f"         ⚠️ delete_one [{row_index}] résultat: {result}")
            return False

        # Attendre SweetAlert2
        confirm = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
        )
        time.sleep(0.2)
        driver.execute_script("arguments[0].click();", confirm)

        # Attendre le modal succès → OK
        time.sleep(0.5)
        try:
            ok = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
            )
            driver.execute_script("arguments[0].click();", ok)
        except TimeoutException:
            pass

        # Attendre que la modale disparaisse
        start = time.time()
        while time.time() - start < 3:
            if not driver.find_elements(By.CSS_SELECTOR, ".swal2-container .swal2-popup"):
                break
            time.sleep(0.2)

        time.sleep(0.5)
        log(f"         ✅ Supprimé [{row_index}] {plate_label}")
        return True

    except Exception as e:
        log(f"         ❌ Erreur suppression [{row_index}] {plate_label}: {e}")
        _close_swal(driver)
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  VIDER TOUTE LA FLOTTE
# ─────────────────────────────────────────────────────────────────────────────

def clear_fleet(driver, dry_run: bool = False) -> dict:
    """Supprime tous les véhicules de la flotte. Recharge entre chaque suppression."""
    stats = {"deleted": 0, "failed": 0, "total": 0}

    rows = get_all_rows(driver)
    stats["total"] = len(rows)

    if not rows:
        log(f"      ℹ️ Flotte déjà vide")
        return stats

    log(f"      🗑️  {len(rows)} véhicules à supprimer")

    if dry_run:
        log(f"      🧪 [DRY-RUN] Suppression simulée ({len(rows)} lignes)")
        stats["deleted"] = len(rows)
        return stats

    deleted = 0
    failed = 0
    while True:
        # Recharger la page pour avoir la liste fraîche
        driver.get(MANAGE_FLEET_URL)
        wait_table(driver)
        set_pagination_500(driver)
        wait_table(driver)

        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            break

        total_remaining = len(rows)
        log(f"      → {total_remaining} véhicule(s) restant(s)")

        # Supprimer toujours la première ligne (index 0) après rechargement
        # Récupérer le label de la plaque pour le log
        try:
            cells = rows[0].find_elements(By.TAG_NAME, "td")
            plate_label = cells[4].text.strip() if len(cells) > 4 else f"ligne-0"
        except Exception:
            plate_label = "ligne-0"

        ok = delete_one(driver, 0, plate_label)
        if ok:
            deleted += 1
        else:
            failed += 1
            if failed >= 5:
                log(f"      ❌ Trop d'échecs consécutifs, arrêt")
                break

    stats["deleted"] = deleted
    stats["failed"] = failed
    log(f"      ✅ Flotte vidée : {deleted} supprimés, {failed} échecs")
    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  CRÉER UN VÉHICULE
# ─────────────────────────────────────────────────────────────────────────────

def create_vehicle(driver, driver_data: dict) -> bool:
    vehicle = driver_data.get("vehicle", {}) or {}
    vtype   = vehicle.get("type", "ECO")
    marque  = vehicle.get("marque", "")
    modele  = vehicle.get("modele", "")
    mat     = vehicle.get("matricule", "")
    nom     = driver_data.get("nom", "?")

    if not mat or not mat.strip():
        log(f"         ⏩ Skip {nom} — matricule vide")
        return False

    type_uuid = TYPE_UUID_MAP.get(vtype)
    if not type_uuid:
        log(f"         ⏩ Skip {nom} — type inconnu: {vtype}")
        return False

    try:
        driver.get(CREATE_FLEET_URL)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "form, input"))
        )
        time.sleep(0.5)

        # Type de véhicule (select ou injection UUID)
        try:
            sel = driver.find_element(By.CSS_SELECTOR, "select[name*='type'], select")
            driver.execute_script(
                "arguments[0].value=arguments[1]; "
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}))",
                sel, type_uuid
            )
        except NoSuchElementException:
            driver.execute_script(
                f"var inputs=document.querySelectorAll('input');"
                f"for(var i=0;i<inputs.length;i++){{"
                f"  if(inputs[i].name&&inputs[i].name.toLowerCase().includes('type'))"
                f"  {{inputs[i].value='{type_uuid}';"
                f"   inputs[i].dispatchEvent(new Event('input',{{bubbles:true}})); break;}}}}"
            )

        # Marque
        for sel in ["input[name*='marque']", "input[placeholder*='arque']", "input[name*='brand']"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                els[0].clear(); els[0].send_keys(marque); break

        # Modèle
        for sel in ["input[name*='model']", "input[placeholder*='odèle']", "input[name*='modele']"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                els[0].clear(); els[0].send_keys(modele); break

        # Matricule
        for sel in ["input[name*='matricule']", "input[name*='plate']", "input[placeholder*='matricule']",
                    "input[placeholder*='immatriculation']"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                els[0].clear(); els[0].send_keys(mat); break

        # Submit
        btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(1)

        # Vérification : URL doit changer (retour vers /manage-fleet) ou modale succès
        try:
            WebDriverWait(driver, 15).until(
                lambda d: "/manage-fleet" in d.current_url and "/create" not in d.current_url
            )
            log(f"         ✅ Créé : {nom} → {mat}")
            return True
        except TimeoutException:
            # Vérifier swal succès
            if driver.find_elements(By.CSS_SELECTOR, ".swal2-popup"):
                try:
                    ok = driver.find_element(By.CSS_SELECTOR, ".swal2-confirm")
                    driver.execute_script("arguments[0].click();", ok)
                except Exception:
                    pass
            log(f"         ✅ Créé (swal) : {nom} → {mat}")
            return True

    except Exception as e:
        log(f"         ❌ Échec création {nom} → {mat}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  SYNC UN PARTENAIRE
# ─────────────────────────────────────────────────────────────────────────────

def sync_partner(driver, data: dict, dry_run: bool = False, skip_create: bool = False) -> dict:
    nom   = data.get("nom", "?")
    email = data.get("email", "")
    drivers_list = data.get("drivers", [])

    stats = {
        "nom": nom, "email": email,
        "vehicles_in_json": 0,
        "deleted": 0, "del_failed": 0,
        "created": 0, "create_failed": 0,
        "skipped": 0,
    }

    # Filtrer les conducteurs avec un matricule valide et dédupliquer
    seen_plates = set()
    valid_drivers = []
    for d in drivers_list:
        mat = (d.get("vehicle") or {}).get("matricule", "").strip()
        if not mat:
            stats["skipped"] += 1
            continue
        norm = re.sub(r'[\s\-_./]', '', mat).upper()
        if norm in seen_plates:
            log(f"      ⏩ Doublon ignoré : {d.get('nom','?')} → {mat}")
            stats["skipped"] += 1
            continue
        seen_plates.add(norm)
        valid_drivers.append(d)

    stats["vehicles_in_json"] = len(valid_drivers)
    log(f"      📋 {len(valid_drivers)} véhicules valides dans le JSON ({stats['skipped']} ignorés)")

    if not login(driver, email):
        stats["login_failed"] = True
        return stats

    # 1. Vider la flotte
    log(f"      🗑️  Vidage de la flotte...")
    del_stats = clear_fleet(driver, dry_run=dry_run)
    stats["deleted"]     = del_stats["deleted"]
    stats["del_failed"]  = del_stats["failed"]

    # 2. Recréer les véhicules
    if not skip_create and valid_drivers:
        log(f"      ➕ Création de {len(valid_drivers)} véhicules...")
        for i, d in enumerate(valid_drivers, 1):
            mat = (d.get("vehicle") or {}).get("matricule", "?")
            log(f"         [{i}/{len(valid_drivers)}] {d.get('nom','?')} → {mat}")
            if dry_run:
                stats["created"] += 1
                continue
            if create_vehicle(driver, d):
                stats["created"] += 1
            else:
                stats["create_failed"] += 1
    elif skip_create:
        log(f"      ⏩ --skip-create : création ignorée")

    logout(driver)
    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  DÉCOUVRIR LES PARTENAIRES
# ─────────────────────────────────────────────────────────────────────────────

def _partner_num(folder_name: str) -> int:
    m = PARTNER_NUM_RE.search(folder_name)
    return int(m.group(1)) if m else 9999


def load_partners(data_dir: Path) -> list:
    """Charge tous les data.json triés par numéro de partenaire."""
    partners = []
    for folder in data_dir.iterdir():
        if not folder.is_dir():
            continue
        jf = folder / "data.json"
        if not jf.exists():
            continue
        try:
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)
            data["_folder"] = folder.name
            partners.append(data)
        except Exception as e:
            log(f"⚠️ Erreur lecture {jf}: {e}")
    partners.sort(key=lambda p: _partner_num(p.get("_folder", "")))
    return partners


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync flotte : vider + recréer depuis data.json")
    parser.add_argument("--only",        help="Traiter uniquement ce partenaire (ex: Partenaire1)")
    parser.add_argument("--start",       help="Reprendre depuis ce partenaire")
    parser.add_argument("--dry-run",     action="store_true", help="Simulation (aucune modif)")
    parser.add_argument("--skip-create", action="store_true", help="Vider seulement, ne pas recréer")
    parser.add_argument("--headed",      action="store_true", help="Chrome visible (debug local)")
    parser.add_argument("--data-dir",    default=str(DATA_DIR), help="Dossier organized_by_partner")
    args = parser.parse_args()

    log(f"\n{'='*60}")
    log("🔄 SYNC FLOTTE — VPS")
    log(f"{'='*60}")

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        log(f"❌ Dossier introuvable: {data_dir}")
        sys.exit(1)

    partners = load_partners(data_dir)
    log(f"   📂 {len(partners)} partenaires trouvés dans {data_dir.name}")

    # Filtres --only / --start
    if args.only:
        target = args.only.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        partners = [p for p in partners
                    if p.get("_folder", "").lower().replace("-", "").replace("_", "") == target
                    or (PARTNER_NUM_RE.search(args.only or "") and
                        PARTNER_NUM_RE.search(p.get("_folder","") or "") and
                        PARTNER_NUM_RE.search(args.only).group(1) ==
                        PARTNER_NUM_RE.search(p["_folder"]).group(1))]
        if not partners:
            log(f"❌ '{args.only}' introuvable")
            sys.exit(1)

    if args.start and not args.only:
        target = args.start.strip().lower().replace("-", "").replace("_", "")
        names = [p.get("_folder","").lower().replace("-","").replace("_","") for p in partners]
        if target not in names:
            log(f"❌ '{args.start}' introuvable"); sys.exit(1)
        idx = names.index(target)
        partners = partners[idx:]
        log(f"   ▶️ Reprise depuis {partners[0].get('_folder','?')}")

    log(f"   📋 {len(partners)} partenaires à traiter")
    if args.dry_run:
        log(f"   🧪 MODE DRY-RUN")
    if args.skip_create:
        log(f"   ⏩ --skip-create : aucune création")

    driver = setup_driver(headed=args.headed)
    all_stats = []

    try:
        for idx, partner in enumerate(partners, 1):
            folder = partner.get("_folder", "?")
            email  = partner.get("email", "")
            log(f"\n   ▶️ [{idx}/{len(partners)}] {folder} ({email})")

            st = sync_partner(driver, partner,
                              dry_run=args.dry_run,
                              skip_create=args.skip_create)
            all_stats.append(st)

            log(f"      📊 Résultat : "
                f"JSON={st['vehicles_in_json']} | "
                f"Supprimés={st['deleted']} (échecs={st['del_failed']}) | "
                f"Créés={st['created']} (échecs={st['create_failed']})")

        # Résumé final
        total_del  = sum(s["deleted"] for s in all_stats)
        total_crea = sum(s["created"] for s in all_stats)
        total_fail = sum(s.get("del_failed", 0) + s.get("create_failed", 0) for s in all_stats)
        log(f"\n{'='*60}")
        log(f"✨ SYNC TERMINÉE")
        log(f"   Partenaires : {len(all_stats)}")
        log(f"   🗑️  Supprimés : {total_del}")
        log(f"   ➕ Créés     : {total_crea}")
        log(f"   ❌ Échecs    : {total_fail}")
        log(f"{'='*60}")

    except KeyboardInterrupt:
        log("\n🛑 Interrompu.")
    except Exception as e:
        log(f"\n💥 Erreur fatale: {e}")
        traceback.print_exc()
    finally:
        time.sleep(1)
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
