import argparse
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime

# Encodage UTF-8 Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ─── Config ───────────────────────────────────────────────────────────────────
_SCRIPT_DIR    = Path(__file__).parent
# On utilise le même dossier output que les autres scripts
OUTPUT_DIR     = _SCRIPT_DIR / "output"
ORGANIZED_DIR  = OUTPUT_DIR / "organized_by_partner"

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def normalize_immat(text: str) -> str:
    """Nettoie l'immatriculation pour faciliter le matching (ex: AA•773•QZ -> AA773QZ)"""
    if not text: return ""
    # Enlever tout ce qui n'est pas lettre ou chiffre
    return re.sub(r'[^a-zA-Z0-9]', '', text).upper()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Nom du partenaire précis (ex: Partenaire1)")
    parser.add_argument("--start", type=int, default=1, help="Numéro de partenaire de départ")
    args = parser.parse_args()

    # 1. Lister et trier les partenaires
    if not ORGANIZED_DIR.exists():
        log(f"[ERROR] Dossier non trouvé : {ORGANIZED_DIR}")
        return

    partners = [d for d in ORGANIZED_DIR.iterdir() if d.is_dir() and "unassigned" not in d.name.lower()]
    def get_num(d):
        m = re.search(r'\d+', d.name)
        return int(m.group()) if m else 0
    partners.sort(key=get_num)
    
    # Filtrage par start
    partners = [p for p in partners if get_num(p) >= args.start]
    
    # Filtrage par only
    if args.only:
        partners = [p for p in partners if p.name.lower() == args.only.lower()]

    if not partners:
        log("[INFO] Aucun partenaire à traiter.")
        return

    log(f"[START] Fusion des données (matching) pour {len(partners)} partenaires.")

    for p_dir in partners:
        data_path = p_dir / "data.json"
        if not data_path.exists():
            log(f"   [SKIP] {p_dir.name} : data.json manquant.")
            continue

        # Trouver le rapport d'audit le plus récent
        audit_files = list(p_dir.glob("fleet_approval_report_final_*.json"))
        if not audit_files:
            # Tenter les rapports non-finaux si absents
            audit_files = list(p_dir.glob("fleet_approval_report_*.json"))
            
        if not audit_files:
            log(f"   [SKIP] {p_dir.name} : aucun rapport d'audit trouvé.")
            continue
        
        # Le plus récent par date de modif
        latest_audit = max(audit_files, key=os.path.getmtime)
        
        try:
            # Charger les fichiers
            with open(data_path, "r", encoding="utf-8") as f:
                data_partner = json.load(f)
            
            with open(latest_audit, "r", encoding="utf-8") as f:
                audit_data = json.load(f)

            # Créer un dictionnaire de recherche pour l'audit (clé = immat normalisée)
            audit_lookup = {}
            for veh in audit_data.get("vehicles", []):
                immat_raw = veh.get("immat") or ""
                norm = normalize_immat(immat_raw)
                if norm:
                    audit_lookup[norm] = veh

            # Fusionner avec les chauffeurs
            final_drivers_list = []
            drivers = data_partner.get("drivers", [])
            
            for drv in drivers:
                # Récupérer l'immat du véhicule affecté au chauffeur dans data.json
                veh_info = drv.get("vehicle", {})
                matricule = veh_info.get("matricule", "")
                norm_matricule = normalize_immat(matricule)
                
                # Chercher la correspondance dans l'audit
                audit_result = audit_lookup.get(norm_matricule)
                
                merged_entry = {
                    "chauffeur_nom": drv.get("nom"),
                    "chauffeur_tel": drv.get("telephone"),
                    "chauffeur_transport": drv.get("type_transport"),
                    "vehicle_matricule": matricule,
                    "vehicle_marque": veh_info.get("marque"),
                    "vehicle_modele": veh_info.get("modele"),
                    "audit_status_tab": "N/A",
                    "audit_detailed_status": "Non trouvé dans l'audit",
                    "audit_doc_link": None
                }
                
                if audit_result:
                    merged_entry.update({
                        "audit_status_tab": audit_result.get("status_tab"),
                        "audit_detailed_status": audit_result.get("detailed_status"),
                        "audit_doc_link": audit_result.get("doc_link")
                    })
                
                final_drivers_list.append(merged_entry)

            # Structure du fichier final
            final_output = {
                "partner_nom": data_partner.get("nom"),
                "partner_email": data_partner.get("email"),
                "generated_at": datetime.now().isoformat(),
                "audit_file_used": latest_audit.name,
                "drivers_count": len(final_drivers_list),
                "data": final_drivers_list
            }
            
            out_path = p_dir / "data_final.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(final_output, f, indent=2, ensure_ascii=False)
            
            log(f"   [OK] {p_dir.name} : data_final.json créé ({len(final_drivers_list)} chauffeurs matched).")

        except Exception as e:
            log(f"   [ERROR] Erreur sur {p_dir.name} : {e}")

    log("[END] Terminé.")

if __name__ == "__main__":
    main()
