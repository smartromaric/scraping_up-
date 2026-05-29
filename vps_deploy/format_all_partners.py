#!/usr/bin/env python3
import json
import csv
from pathlib import Path

# --- Configuration ---
OUTPUT_DIR = Path(__file__).parent / "output"
INPUT_FILE = OUTPUT_DIR / "organized_by_partner" / "all_partners_enriched.json"
OUTPUT_TXT = OUTPUT_DIR / "partners_formatted.txt"

def format_partners():
    """Formate all_partners_enriched.json en format CSV txt"""
    
    if not INPUT_FILE.exists():
        print(f"❌ Fichier introuvable: {INPUT_FILE}")
        return
    
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Extraire les chauffeurs de tous les partenaires
    all_drivers = []
    for partner in data:  # data est directement une liste
        partner_name = partner.get('nom', '').replace(';', '_')  # Remplacer ; pour éviter les conflits
        
        # Ignorer UNASSIGNED_DRIVERS
        if partner_name.upper() == 'UNASSIGNED_DRIVERS':
            continue
            
        drivers = partner.get('drivers', [])
        
        for driver in drivers:
            # Format: nom_prenoms;telephone
            nom_complet = driver.get('nom', '').replace(';', '_')
            telephone = driver.get('telephone', '').replace(';', '_')
            
            line = f"{partner_name};{nom_complet};{telephone}"
            all_drivers.append(line)
    
    # Écrire le fichier TXT
    with open(OUTPUT_TXT, 'w', encoding='utf-8') as f:
        f.write("nom_prenoms;telephone\n")  # Header
        for line in all_drivers:
            f.write(line + "\n")
    
    print(f"✅ Fichier généré: {OUTPUT_TXT}")
    print(f"📊 {len(all_drivers)} chauffeurs exportés")

if __name__ == "__main__":
    format_partners()
