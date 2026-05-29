"""
Script de matching et organisation des données
Partenaires + Conducteurs
================================================
1. Charge les données partenaires et conducteurs
2. Fait un matching intelligent
3. Crée des dossiers par partenaire
4. Exporte les données enrichies dans chaque dossier
"""

import json
import csv
import os
import re
from pathlib import Path
from datetime import datetime

# ─── Configuration ──────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent / "output"
PARTNERS_FILE = OUTPUT_DIR / "partenaires.json"
DRIVERS_FILE = OUTPUT_DIR / "conducteurs_vehicles.json"
ORGANIZED_DIR = OUTPUT_DIR / "organized_by_partner"

# ═══════════════════════════════════════════════════════════════════════════════
#  LOADING DATA
# ═══════════════════════════════════════════════════════════════════════════════

def load_json(file_path):
    """Charge un fichier JSON."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Erreur chargement {file_path}: {e}")
        return []

def sanitize_folder_name(name):
    """Convertit un nom en dossier valide."""
    # Supprimer les caractères invalides
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    # Remplacer les espaces par des underscores
    name = re.sub(r'\s+', '_', name)
    # Limiter la longueur
    name = name[:100]
    return name

# ═══════════════════════════════════════════════════════════════════════════════
#  MATCHING LOGIC
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_text(text):
    """Normalise un texte pour le matching."""
    return text.lower().strip()

def calculate_similarity(str1, str2):
    """Calcule un score de similarité simple entre deux chaînes."""
    str1_norm = normalize_text(str1)
    str2_norm = normalize_text(str2)
    
    # Exact match
    if str1_norm == str2_norm:
        return 100
    
    # Contient
    if str1_norm in str2_norm or str2_norm in str1_norm:
        return 80
    
    # Mots communs
    words1 = set(str1_norm.split())
    words2 = set(str2_norm.split())
    common_words = len(words1 & words2)
    if common_words > 0:
        similarity = (common_words * 2) / (len(words1) + len(words2)) * 100
        return similarity
    
    return 0

def match_drivers_to_partners(partners, drivers):
    """
    Fait un matching entre partenaires et chauffeurs.
    Retourne une liste enrichie de partenaires avec leurs chauffeurs.
    """
    print("\n🔍 Matching des chauffeurs avec les partenaires...")
    
    # Créer un dictionnaire pour tracer les chauffeurs assignés
    assigned_drivers = set()
    
    for partner in partners:
        partner['drivers'] = partner.get('drivers', [])
        partner_name = partner.get('nom', '')
        
        # Si pas de chauffeurs vides, on cherche
        if not partner['drivers']:
            matching_drivers = []
            
            for driver in drivers:
                driver_name = driver.get('nom', '')
                
                # Chercher une correspondance
                # Stratégie 1: Le nom du partenaire est dans le nom du chauffeur
                similarity = calculate_similarity(partner_name, driver_name)
                
                if similarity > 50:  # Seuil de similarité
                    matching_drivers.append({
                        'driver': driver,
                        'similarity': similarity
                    })
            
            # Trier par similarité et garder les meilleures correspondances
            matching_drivers.sort(key=lambda x: x['similarity'], reverse=True)
            
            # Ajouter les chauffeurs matchés
            for item in matching_drivers:
                if item['driver']['nom'] not in assigned_drivers:
                    # Ensure driver has vehicle object
                    driver = item['driver'].copy()
                    if 'vehicle' not in driver:
                        driver['vehicle'] = {
                            "type": "N/A",
                            "marque": "N/A",
                            "modele": "N/A",
                            "matricule": "N/A"
                        }
                    partner['drivers'].append(driver)
                    assigned_drivers.add(driver['nom'])
        else:
            # Marquer les chauffeurs existants comme assignés
            for driver in partner['drivers']:
                assigned_drivers.add(driver.get('nom', ''))
    
    # Ajouter les chauffeurs non-assignés à un partenaire "Unassigned"
    unassigned_drivers = [d for d in drivers if d.get('nom') not in assigned_drivers]
    if unassigned_drivers:
        # Ensure all unassigned drivers have vehicle object
        for driver in unassigned_drivers:
            if 'vehicle' not in driver:
                driver['vehicle'] = {
                    "type": "N/A",
                    "marque": "N/A",
                    "modele": "N/A",
                    "matricule": "N/A"
                }
        
        partners.append({
            'nom': 'UNASSIGNED_DRIVERS',
            'email': 'N/A',
            'telephone': 'N/A',
            'document_url': 'N/A',
            'owner_id': 'N/A',
            'drivers': unassigned_drivers
        })
    
    # Ensure all drivers have vehicle object
    for partner in partners:
        for driver in partner.get('drivers', []):
            if 'vehicle' not in driver:
                driver['vehicle'] = {
                    "type": "N/A",
                    "marque": "N/A",
                    "modele": "N/A",
                    "matricule": "N/A"
                }
    
    return partners

# ═══════════════════════════════════════════════════════════════════════════════
#  EXPORT FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def export_partner_data(partner, partner_dir):
    """Exporte les données d'un partenaire dans JSON, CSV et HTML."""
    
    # JSON Export (already includes all data)
    json_path = partner_dir / "data.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(partner, f, ensure_ascii=False, indent=2)
    print(f"    ✅ JSON: {json_path.name}")
    
    # CSV Export - Updated to include all driver fields
    csv_path = partner_dir / "data.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        
        # Header for partner
        writer.writerow(["Type", "Nom", "Email", "Téléphone", "URL Document", "Owner ID"])
        
        # Partner info
        writer.writerow([
            "Partenaire",
            partner.get('nom', 'N/A'),
            partner.get('email', 'N/A'),
            partner.get('telephone', 'N/A'),
            partner.get('document_url', 'N/A'),
            partner.get('owner_id', 'N/A')
        ])
        
        # Drivers info with all fields
        if partner.get('drivers'):
            writer.writerow([])  # Empty row for separation
            writer.writerow(["Conducteur", "Nom", "Emplacement", "Téléphone", "Type Transport", "Type Véhicule", "Type Véhicule", "Marque", "Modèle", "Matricule", "URL Document"])
            
            for driver in partner.get('drivers', []):
                vehicle = driver.get('vehicle', {})
                writer.writerow([
                    "Conducteur",
                    driver.get('nom', 'N/A'),
                    driver.get('emplacement', 'N/A'),
                    driver.get('telephone', 'N/A'),
                    driver.get('type_transport', 'N/A'),
                    driver.get('type_vehicule', 'N/A'),
                    vehicle.get('type', 'N/A'),
                    vehicle.get('marque', 'N/A'),
                    vehicle.get('modele', 'N/A'),
                    vehicle.get('matricule', 'N/A'),
                    driver.get('document_url', 'N/A')
                ])
    print(f"    ✅ CSV: {csv_path.name}")
    
    # HTML Export - Updated to show all driver information
    html_path = partner_dir / "data.html"
    
    drivers_html = ""
    if partner.get('drivers'):
        drivers_html = "".join([
            f'''
            <tr>
                <td>{d.get("nom", "N/A")}</td>
                <td>{d.get("emplacement", "N/A")}</td>
                <td>{d.get("telephone", "N/A")}</td>
                <td>{d.get("type_transport", "N/A")}</td>
                <td>{d.get("type_vehicule", "N/A")}</td>
                <td>
                    <strong>Type:</strong> {d.get("vehicle", {}).get("type", "N/A")}<br>
                    <strong>Marque:</strong> {d.get("vehicle", {}).get("marque", "N/A")}<br>
                    <strong>Modèle:</strong> {d.get("vehicle", {}).get("modele", "N/A")}<br>
                    <strong>Matricule:</strong> {d.get("vehicle", {}).get("matricule", "N/A")}
                </td>
                <td><a href="{d.get("document_url", "#")}" target="_blank">📄 Document</a></td>
            </tr>
            '''
            for d in partner.get('drivers', [])
        ])
    
    html_content = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <title>{partner.get('nom', 'Partner')} - Data</title>
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                margin: 0;
                padding: 20px;
                color: #333;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            .section {{
                background: white;
                padding: 30px;
                margin: 20px 0;
                border-radius: 10px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
            }}
            h1 {{
                color: #667eea;
                margin-bottom: 10px;
                font-size: 2.2em;
                text-align: center;
            }}
            h2 {{
                color: #555;
                margin-top: 30px;
                margin-bottom: 20px;
                border-bottom: 2px solid #667eea;
                padding-bottom: 10px;
            }}
            .partner-info {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin: 20px 0;
            }}
            .info-card {{
                background: #f8f9fa;
                padding: 20px;
                border-radius: 8px;
                border-left: 4px solid #667eea;
            }}
            .info-label {{
                font-weight: bold;
                color: #667eea;
                margin-bottom: 5px;
            }}
            .info-value {{
                color: #333;
                word-break: break-all;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 20px;
                background: white;
                border-radius: 8px;
                overflow: hidden;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            }}
            th, td {{
                padding: 15px;
                text-align: left;
                border-bottom: 1px solid #ddd;
            }}
            th {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                font-weight: bold;
                font-size: 0.9em;
            }}
            tr:nth-child(even) {{
                background: #f8f9fa;
            }}
            tr:hover {{
                background: #e3f2fd;
                transition: background 0.3s;
            }}
            .vehicle-info {{
                font-size: 0.85em;
                line-height: 1.4;
            }}
            a {{
                color: #667eea;
                text-decoration: none;
                font-weight: bold;
            }}
            a:hover {{
                text-decoration: underline;
            }}
            .no-data {{
                text-align: center;
                color: #999;
                font-style: italic;
                padding: 40px;
            }}
            .stats {{
                display: flex;
                justify-content: center;
                gap: 30px;
                margin: 20px 0;
            }}
            .stat {{
                text-align: center;
                background: #667eea;
                color: white;
                padding: 15px 25px;
                border-radius: 8px;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            }}
            .stat-number {{
                font-size: 2em;
                font-weight: bold;
                display: block;
            }}
            .stat-label {{
                font-size: 0.9em;
                opacity: 0.9;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="section">
                <h1>📋 {partner.get('nom', 'Partner')}</h1>
                
                <div class="stats">
                    <div class="stat">
                        <span class="stat-number">{len(partner.get('drivers', []))}</span>
                        <span class="stat-label">Conducteurs</span>
                    </div>
                </div>
                
                <div class="partner-info">
                    <div class="info-card">
                        <div class="info-label">Email</div>
                        <div class="info-value">{partner.get('email', 'N/A')}</div>
                    </div>
                    <div class="info-card">
                        <div class="info-label">Téléphone</div>
                        <div class="info-value">{partner.get('telephone', 'N/A')}</div>
                    </div>
                    <div class="info-card">
                        <div class="info-label">Owner ID</div>
                        <div class="info-value">{partner.get('owner_id', 'N/A')}</div>
                    </div>
                    <div class="info-card">
                        <div class="info-label">Document</div>
                        <div class="info-value"><a href="{partner.get('document_url', '#')}" target="_blank">📄 Voir Document</a></div>
                    </div>
                </div>
            </div>
            
            <div class="section">
                <h2>🚗 Conducteurs Associés</h2>
                {f'''
                <table>
                    <thead>
                        <tr>
                            <th>Nom</th>
                            <th>Emplacement</th>
                            <th>Téléphone</th>
                            <th>Type Transport</th>
                            <th>Statut Véhicule</th>
                            <th>Informations Véhicule</th>
                            <th>Document</th>
                        </tr>
                    </thead>
                    <tbody>
                        {drivers_html}
                    </tbody>
                </table>
                ''' if drivers_html else '<div class="no-data">Aucun conducteur associé à ce partenaire</div>'}
            </div>
        </div>
    </body>
    </html>
    """
    
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"    ✅ HTML: {html_path.name}")

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN PROCESS
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "="*60)
    print("📊 ORGANISATION DES DONNÉES")
    print("="*60)
    
    # 1. Load data
    print("\n📂 Chargement des données...")
    partners = load_json(PARTNERS_FILE)
    drivers = load_json(DRIVERS_FILE)
    print(f"  ✅ {len(partners)} partenaires chargés")
    print(f"  ✅ {len(drivers)} chauffeurs chargés")
    
    # Create vehicle mapping from drivers data
    vehicle_map = {}
    for driver in drivers:
        phone = driver.get('telephone', '').strip()
        if phone:
            vehicle_map[phone] = driver.get('vehicle', {})
    print(f"  ✅ Mapping véhicule créé pour {len(vehicle_map)} chauffeurs (par téléphone)")
    
    # 2. Match data
    enriched_partners = match_drivers_to_partners(partners, drivers)
    
    # 3. Update vehicle data for all drivers
    print("\n🔄 Mise à jour des données véhicule...")
    updated_count = 0
    for partner in enriched_partners:
        for driver in partner.get('drivers', []):
            driver_phone = driver.get('telephone', '').strip()
            if driver_phone in vehicle_map:
                driver['vehicle'] = vehicle_map[driver_phone]
                updated_count += 1
    print(f"  ✅ {updated_count} véhicules mis à jour")
    
    print("  ✅ Mise à jour des véhicules terminée")
    
    # 4. Create organized directory structure
    print("\n📁 Création de la structure de dossiers...")
    ORGANIZED_DIR.mkdir(parents=True, exist_ok=True)
    
    # 5. Export for each partner
    print("\n📤 Exportation des données par partenaire...")
    for idx, partner in enumerate(enriched_partners, 1):
        partner_name = partner.get('nom', f'Partner_{idx}')
        folder_name = sanitize_folder_name(partner_name)
        partner_dir = ORGANIZED_DIR / folder_name
        partner_dir.mkdir(parents=True, exist_ok=True)
        
        num_drivers = len(partner.get('drivers', []))
        print(f"\n  📋 [{idx}/{len(enriched_partners)}] {partner_name}")
        print(f"      └─ Conducteurs: {num_drivers}")
        
        export_partner_data(partner, partner_dir)
    
    # 6. Create summary report
    print("\n📊 Création du rapport de synthèse...")
    summary_path = ORGANIZED_DIR / "SUMMARY.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("RÉSUMÉ DE L'ORGANISATION DES DONNÉES\n")
        f.write("="*60 + "\n")
        f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"Total Partenaires: {len(enriched_partners)}\n")
        f.write(f"Total Chauffeurs: {len(drivers)}\n")
        f.write(f"Chauffeurs Assignés: {sum(len(p.get('drivers', [])) for p in enriched_partners if p.get('nom') != 'UNASSIGNED_DRIVERS')}\n")
        f.write(f"Chauffeurs Non-Assignés: {len([p for p in enriched_partners if p.get('nom') == 'UNASSIGNED_DRIVERS'])}\n\n")
        
        f.write("DÉTAILS PAR PARTENAIRE:\n")
        f.write("-"*60 + "\n")
        for partner in enriched_partners:
            num_drivers = len(partner.get('drivers', []))
            f.write(f"\n• {partner.get('nom', 'N/A')}\n")
            f.write(f"  Email: {partner.get('email', 'N/A')}\n")
            f.write(f"  Téléphone: {partner.get('telephone', 'N/A')}\n")
            f.write(f"  Conducteurs: {num_drivers}\n")
    
    print(f"  ✅ {summary_path.name}")
    
    # 7. Create global JSON with enriched data
    enriched_json = ORGANIZED_DIR / "all_partners_enriched.json"
    with open(enriched_json, "w", encoding="utf-8") as f:
        json.dump(enriched_partners, f, ensure_ascii=False, indent=2)
    print(f"  ✅ {enriched_json.name}")
    
    print("\n" + "="*60)
    print(f"✅ ORGANISATION TERMINÉE")
    print(f"📁 Dossier de sortie: {ORGANIZED_DIR}")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()

