# -*- coding: utf-8 -*-
"""
Liste les conducteurs par partenaire pour identification et réassignation.
Usage: python3 list_drivers_by_partner.py
"""

import json
import os
import sys
from pathlib import Path

# Fichier de sortie
OUTPUT_FILE = Path(__file__).parent / "output" / "drivers_by_partner.json"

def list_drivers_by_partner():
    """Charge organized_by_partner et liste les conducteurs avec leurs IDs."""
    
    base_dir = Path(__file__).parent / "output" / "organized_by_partner"
    
    if not base_dir.exists():
        print(f"❌ Dossier introuvable: {base_dir}")
        print("   Lance d'abord: python3 match_and_organize.py")
        return None
    
    all_partners = {}
    
    for partner_dir in sorted(base_dir.iterdir()):
        if not partner_dir.is_dir():
            continue
        
        data_file = partner_dir / "data.json"
        if not data_file.exists():
            continue
        
        try:
            data = json.loads(data_file.read_text(encoding="utf-8"))
            drivers = data.get("drivers", [])
            
            driver_list = []
            for d in drivers:
                # Extraire l'ID du view_profile URL
                view_profile = d.get("view_profile", "N/A")
                driver_id = "N/A"
                edit_url = None
                
                if view_profile != "N/A" and "/view-profile/" in view_profile:
                    driver_id = view_profile.split("/view-profile/")[-1].split("/")[0]
                    edit_url = f"https://upjunoo-server-new.junooapps.com/fleet-drivers/edit/{driver_id}"
                
                driver_list.append({
                    "nom": d.get("nom", "N/A"),
                    "telephone": d.get("telephone", "N/A"),
                    "driver_id": driver_id,
                    "view_profile": view_profile,
                    "edit_url": edit_url,
                    "vehicle": d.get("vehicle", {})
                })
            
            all_partners[partner_dir.name] = {
                "nom": data.get("nom", partner_dir.name),
                "email": data.get("email", "N/A"),
                "driver_count": len(driver_list),
                "drivers": driver_list
            }
            
            print(f"✅ {partner_dir.name}: {len(driver_list)} conducteurs")
            
        except Exception as e:
            print(f"⚠️ Erreur {partner_dir.name}: {e}")
    
    # Sauvegarder
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(all_partners, indent=2, ensure_ascii=False), encoding="utf-8")
    
    print(f"\n📁 Fichier sauvegardé: {OUTPUT_FILE}")
    print(f"\n📊 Total: {len(all_partners)} partenaires, {sum(p['driver_count'] for p in all_partners.values())} conducteurs")
    
    return all_partners


def find_partner_containing(name_fragment):
    """Trouve les partenaires dont le nom contient un fragment."""
    data = list_drivers_by_partner()
    if not data:
        return
    
    matches = {}
    fragment_lower = name_fragment.lower()
    
    for partner_name, info in data.items():
        if fragment_lower in partner_name.lower() or fragment_lower in info["nom"].lower():
            matches[partner_name] = info
    
    if matches:
        print(f"\n🔍 Partenaires trouvés pour '{name_fragment}':")
        for name, info in matches.items():
            print(f"\n   📌 {name}")
            print(f"      Email: {info['email']}")
            print(f"      Conducteurs: {info['driver_count']}")
            for d in info['drivers'][:5]:  # Afficher max 5
                print(f"         - {d['nom']} | Tel: {d['telephone']}")
                if d['edit_url']:
                    print(f"           ↳ Edit: {d['edit_url']}")
            if len(info['drivers']) > 5:
                print(f"         ... et {len(info['drivers']) - 5} autres")
    else:
        print(f"❌ Aucun partenaire trouvé pour '{name_fragment}'")
    
    return matches


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Mode recherche: python3 list_drivers_by_partner.py "UPJUNOO CI"
        find_partner_containing(sys.argv[1])
    else:
        # Mode liste complète
        list_drivers_by_partner()
        print("\n💡 Usage pour chercher un partenaire spécifique:")
        print("   python3 list_drivers_by_partner.py 'Partenaires-57'")
        print("   python3 list_drivers_by_partner.py 'UPJUNOO CI'")
