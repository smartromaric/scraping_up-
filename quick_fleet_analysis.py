#!/usr/bin/env python3
"""
Analyse rapide des flottes incomplètes
===================================
"""

import json
from pathlib import Path

def quick_analysis():
    input_file = Path("vps_deploy/output/organized_by_partner/all_partners_enriched.json")
    
    with open(input_file, 'r') as f:
        data = json.load(f)
    
    print("🔍 ANALYSE RAPIDE - PARTENAIRES AVEC FLOTTES INCOMPLÈTES")
    print("=" * 60)
    
    incomplete_count = 0
    total_missing = 0
    
    for partner in data:
        partner_name = partner.get('nom', '')
        
        if 'UNASSIGNED' in partner_name.upper():
            continue
            
        drivers = partner.get('drivers', [])
        missing = 0
        
        for driver in drivers:
            vehicle = driver.get('vehicle', {})
            if not vehicle or vehicle.get('matricule') == 'N/A':
                missing += 1
        
        if missing > 0:
            incomplete_count += 1
            total_missing += missing
            print(f"❌ {partner_name}: {missing} chauffeurs sans véhicule")
    
    print(f"\n📊 RÉSUMÉ:")
    print(f"   • Partenaires avec flotte incomplète: {incomplete_count}")
    print(f"   • Total chauffeurs sans véhicule: {total_missing}")
    
    # Créer fichier pour création
    missing_vehicles = []
    for partner in data:
        partner_name = partner.get('nom', '')
        if 'UNASSIGNED' in partner_name.upper():
            continue
            
        for driver in partner.get('drivers', []):
            vehicle = driver.get('vehicle', {})
            if not vehicle or vehicle.get('matricule') == 'N/A':
                missing_vehicles.append({
                    'partner': partner_name,
                    'driver_name': driver.get('nom', ''),
                    'driver_phone': driver.get('telephone', ''),
                    'transport_type': driver.get('type_transport', 'Taxi')
                })
    
    output_file = Path("vps_deploy/output/missing_vehicles.json")
    with open(output_file, 'w') as f:
        json.dump(missing_vehicles, f, indent=2)
    
    print(f"💾 Fichier de création généré: {output_file}")
    print(f"   {len(missing_vehicles)} entrées à traiter")

if __name__ == "__main__":
    quick_analysis()
