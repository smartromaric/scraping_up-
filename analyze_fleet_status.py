#!/usr/bin/env python3
"""
Analyse du statut des flottes de véhicules pour tous les partenaires
==========================================================
Identifie les partenaires avec des chauffeurs sans véhicule assigné
et génère un rapport pour la création de flotte manquante.
"""

import json
from pathlib import Path

def analyze_fleet_status():
    """Analyse le fichier all_partners_enriched.json pour identifier les flottes incomplètes"""
    
    input_file = Path("vps_deploy/output/organized_by_partner/all_partners_enriched.json")
    
    if not input_file.exists():
        print(f"❌ Fichier introuvable: {input_file}")
        return
    
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    partners_analysis = []
    missing_vehicles = []
    
    for partner in data:
        partner_name = partner.get('nom', '')
        
        # Ignorer UNASSIGNED_DRIVERS
        if 'UNASSIGNED' in partner_name.upper():
            continue
        
        drivers = partner.get('drivers', [])
        total_drivers = len(drivers)
        drivers_with_vehicle = 0
        drivers_without_vehicle = 0
        vehicle_types = {}
        
        # Analyser chaque chauffeur
        for driver in drivers:
            vehicle = driver.get('vehicle', {})
            if vehicle and vehicle.get('matricule') != 'N/A':
                drivers_with_vehicle += 1
                vtype = vehicle.get('type', 'N/A')
                vehicle_types[vtype] = vehicle_types.get(vtype, 0) + 1
            else:
                drivers_without_vehicle += 1
                # Ajouter à la liste des véhicules manquants
                missing_vehicles.append({
                    'partner': partner_name,
                    'partner_email': partner.get('email', ''),
                    'driver_name': driver.get('nom', ''),
                    'driver_phone': driver.get('telephone', ''),
                    'driver_location': driver.get('emplacement', ''),
                    'transport_type': driver.get('type_transport', ''),
                    'vehicle_type': driver.get('type_vehicule', '')
                })
        
        completion_rate = (drivers_with_vehicle / total_drivers * 100) if total_drivers > 0 else 0
        
        partners_analysis.append({
            'partner': partner_name,
            'total_drivers': total_drivers,
            'drivers_with_vehicle': drivers_with_vehicle,
            'drivers_without_vehicle': drivers_without_vehicle,
            'completion_rate': round(completion_rate, 1),
            'vehicle_types': vehicle_types
        })
    
    # Trier par taux de complétion (croissant)
    partners_analysis.sort(key=lambda x: x['completion_rate'])
    
    # Afficher l'analyse
    print('📊 ANALYSE DES FLOTTES - PARTENAIRES AVEC VÉHICULES MANQUANTS')
    print('=' * 80)
    
    incomplete_partners = [p for p in partners_analysis if p['drivers_without_vehicle'] > 0]
    
    for i, p in enumerate(incomplete_partners[:15]):  # Top 15 des plus incomplets
        print(f'{i+1:2d}. {p["partner"]:20s} | {p["total_drivers"]:3d} chauffeurs | '
              f'{p["drivers_without_vehicle"]:3d} sans véhicule | '
              f'{p["completion_rate"]:5.1f}% complet')
        if p['vehicle_types']:
            types_str = ', '.join([f'{k}:{v}' for k, v in p['vehicle_types'].items()])
            print(f'     🚗 Types actuels: {types_str}')
        print()
    
    print(f'📈 STATISTIQUES GLOBALES:')
    print(f'   • Total partenaires analysés: {len(partners_analysis)}')
    print(f'   • Partenaires avec flotte incomplète: {len(incomplete_partners)}')
    print(f'   • Total chauffeurs sans véhicule: {len(missing_vehicles)}')
    
    # Sauvegarder les données pour le script de création
    output_data = {
        'analysis_date': '2025-04-18',
        'total_partners': len(partners_analysis),
        'incomplete_partners': len(incomplete_partners),
        'total_missing_vehicles': len(missing_vehicles),
        'missing_vehicles': missing_vehicles
    }
    
    output_file = Path("vps_deploy/output/fleet_analysis_report.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print(f'💾 Rapport détaillé sauvegardé: {output_file}')
    print(f'📋 Données pour création sauvegardées: {len(missing_vehicles)} entrées')
    
    return output_data

if __name__ == "__main__":
    analyze_fleet_status()
