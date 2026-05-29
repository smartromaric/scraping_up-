#!/usr/bin/env python3
"""
Analyse des flottes incomplètes sur VPS
===================================
Travaille dans organized_by_partner et exclut UNASSIGNED_DRIVERS
"""

import json
from pathlib import Path

def analyze_missing_fleet():
    """Analyse les partenaires avec véhicules manquants"""
    
    input_file = Path("output/organized_by_partner/all_partners_enriched.json")
    
    if not input_file.exists():
        print(f"❌ Fichier introuvable: {input_file}")
        return
    
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print("🔍 ANALYSE DES FLOTTES MANQUANTES (VPS)")
    print("=" * 60)
    
    incomplete_partners = []
    missing_vehicles = []
    
    for partner in data:
        partner_name = partner.get('nom', '')
        
        # Exclure UNASSIGNED_DRIVERS
        if 'UNASSIGNED' in partner_name.upper():
            continue
        
        drivers = partner.get('drivers', [])
        total_drivers = len(drivers)
        missing_count = 0
        
        for driver in drivers:
            vehicle = driver.get('vehicle', {})
            if not vehicle or vehicle.get('matricule') == 'N/A':
                missing_count += 1
                missing_vehicles.append({
                    'partner': partner_name,
                    'partner_email': partner.get('email', ''),
                    'driver_name': driver.get('nom', ''),
                    'driver_phone': driver.get('telephone', ''),
                    'driver_location': driver.get('emplacement', ''),
                    'transport_type': driver.get('type_transport', 'Taxi'),
                    'vehicle_type': driver.get('type_vehicule', '')
                })
        
        if missing_count > 0:
            completion_rate = ((total_drivers - missing_count) / total_drivers * 100) if total_drivers > 0 else 0
            incomplete_partners.append({
                'partner': partner_name,
                'total_drivers': total_drivers,
                'missing_vehicles': missing_count,
                'completion_rate': round(completion_rate, 1)
            })
    
    # Trier par nombre de véhicules manquants (décroissant)
    incomplete_partners.sort(key=lambda x: x['missing_vehicles'], reverse=True)
    
    print(f"📊 PARTENAIRES AVEC FLOTTES INCOMPLÈTES:")
    for i, p in enumerate(incomplete_partners[:10]):  # Top 10
        print(f"{i+1:2d}. {p['partner']:20s} | {p['missing_vehicles']:2d} manquants | {p['completion_rate']:5.1f}% complet")
    
    print(f"\n📈 STATISTIQUES:")
    print(f"   • Total partenaires analysés: {len(data)}")
    print(f"   • Partenaires avec flotte incomplète: {len(incomplete_partners)}")
    print(f"   • Total véhicules manquants: {len(missing_vehicles)}")
    
    # Sauvegarder pour le script de création
    output_file = Path("output/organized_by_partner/missing_vehicles.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(missing_vehicles, f, ensure_ascii=False, indent=2)
    
    print(f"💾 Fichier de création généré: {output_file}")
    
    return missing_vehicles

if __name__ == "__main__":
    analyze_missing_fleet()
