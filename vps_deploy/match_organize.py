"""
Script VPS pour matcher et organiser les données - UpJunoo
=========================================================
1. Lit partenaires.json et conducteurs_vehicles.json
2. Filtre partenaires par regex (Partenaire-N)
3. Match les chauffeurs aux partenaires
4. Exporte JSON/Excel/HTML par partenaire
5. Génère all_partners_enriched.json
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

# Configuration
OUTPUT_DIR = Path(__file__).parent / "output"
PARTNERS_FILE = OUTPUT_DIR / "partenaires.json"
DRIVERS_FILE = OUTPUT_DIR / "conducteurs_vehicles.json"
ORGANIZED_DIR = OUTPUT_DIR / "organized_by_partner"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Regex pour filtrer les partenaires
PARTNER_NAME_RE = re.compile(r'^\s*partenaires?-?\s*(\d+)\s*$', re.I)
PARTNER_MIN = 1

def log(message):
    """Log avec timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def send_slack(message, color="#36a64f"):
    """Envoie notification Slack"""
    if not WEBHOOK_URL:
        return
    try:
        import urllib.request
        payload = json.dumps({
            "username": os.getenv("SLACK_BOT_NAME", "UpJunoo Bot"),
            "icon_emoji": os.getenv("SLACK_ICON_EMOJI", ":car:"),
            "attachments": [{"color": color, "text": message}]
        }).encode("utf-8")
        req = urllib.request.Request(WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"⚠️ Slack erreur: {e}")

def match_drivers_to_partners():
    """Match les chauffeurs aux partenaires"""
    log("🔍 Chargement des données...")
    
    # Charger les partenaires
    with open(PARTNERS_FILE, 'r', encoding='utf-8') as f:
        partners = json.load(f)
    
    # Charger les conducteurs
    with open(DRIVERS_FILE, 'r', encoding='utf-8') as f:
        drivers = json.load(f)
    
    log(f"📊 {len(partners)} partenaires, {len(drivers)} conducteurs")
    
    # Filtrer les partenaires par regex
    filtered_partners = []
    for partner in partners:
        name = partner.get('nom', '')
        match = PARTNER_NAME_RE.match(name)
        if match:
            partner_num = int(match.group(1))
            if partner_num >= PARTNER_MIN:
                filtered_partners.append(partner)
    
    log(f"🎯 {len(filtered_partners)} partenaires filtrés (Partenaire-N)")
    
    # Matcher les chauffeurs
    for partner in filtered_partners:
        partner_name = partner.get('nom', '')
        partner_email = partner.get('email', '')
        
        # Chercher les chauffeurs correspondants
        matched_drivers = []
        for driver in drivers:
            driver_email = driver.get('email', '')
            driver_phone = driver.get('telephone', '')
            
            # Match par email ou téléphone
            if driver_email == partner_email or driver_phone in partner.get('telephone', ''):
                matched_drivers.append(driver)
        
        partner['drivers'] = matched_drivers
        partner['driver_count'] = len(matched_drivers)
    
    # Ajouter les chauffeurs non assignés
    assigned_drivers = set()
    for partner in filtered_partners:
        for driver in partner.get('drivers', []):
            assigned_drivers.add(driver.get('telephone', ''))
    
    unassigned = [d for d in drivers if d.get('telephone', '') not in assigned_drivers]
    
    unassigned_partner = {
        'nom': 'UNASSIGNED_DRIVERS',
        'email': '',
        'telephone': '',
        'drivers': unassigned,
        'driver_count': len(unassigned)
    }
    
    all_partners = filtered_partners + [unassigned_partner]
    
    log(f"✅ Matching terminé: {len(filtered_partners)} partenaires + {len(unassigned)} non assignés")
    
    return all_partners

def organize_data(partners):
    """Organise et exporte les données par partenaire"""
    ORGANIZED_DIR.mkdir(parents=True, exist_ok=True)
    
    log("📁 Organisation des données...")
    
    # Créer le fichier global
    global_file = ORGANIZED_DIR / "all_partners_enriched.json"
    with open(global_file, 'w', encoding='utf-8') as f:
        json.dump(partners, f, ensure_ascii=False, indent=2)
    
    log(f"✅ Fichier global créé: {global_file}")
    
    # Exporter par partenaire
    for partner in partners:
        partner_name = partner.get('nom', '').replace('/', '_')
        partner_dir = ORGANIZED_DIR / partner_name
        partner_dir.mkdir(exist_ok=True)
        
        # Export JSON
        json_file = partner_dir / "data.json"
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(partner, f, ensure_ascii=False, indent=2)
        
        # Export Excel (optionnel)
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            
            # Feuille Partenaire
            ws1 = wb.active
            ws1.title = "Partenaire"
            ws1.append(['Nom', 'Email', 'Téléphone', 'Nombre chauffeurs'])
            ws1.append([
                partner.get('nom', ''),
                partner.get('email', ''),
                partner.get('telephone', ''),
                len(partner.get('drivers', []))
            ])
            
            # Feuille Conducteurs
            ws2 = wb.create_sheet("Conducteurs")
            ws2.append(['Nom', 'Téléphone', 'Email', 'Véhicule', 'Plaque'])
            
            for driver in partner.get('drivers', []):
                vehicle = driver.get('vehicle', {})
                ws2.append([
                    driver.get('nom', ''),
                    driver.get('telephone', ''),
                    driver.get('email', ''),
                    f"{vehicle.get('type', '')} {vehicle.get('marque', '')} {vehicle.get('modele', '')}",
                    vehicle.get('matricule', '')
                ])
            
            excel_file = partner_dir / "data.xlsx"
            wb.save(excel_file)
            
        except ImportError:
            log("⚠️ openpyxl non disponible, export Excel ignoré")
        
        # Export HTML
        html_content = f"""
        <html>
        <head>
            <title>{partner_name} - UpJunoo</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #f2f2f2; }}
                .partner-info {{ background-color: #e8f4fd; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
            </style>
        </head>
        <body>
            <div class="partner-info">
                <h2>{partner_name}</h2>
                <p><strong>Email:</strong> {partner.get('email', '')}</p>
                <p><strong>Téléphone:</strong> {partner.get('telephone', '')}</p>
                <p><strong>Chauffeurs:</strong> {len(partner.get('drivers', []))}</p>
            </div>
            
            <h3>Chauffeurs</h3>
            <table>
                <tr>
                    <th>Nom</th>
                    <th>Téléphone</th>
                    <th>Email</th>
                    <th>Véhicule</th>
                    <th>Plaque</th>
                </tr>
        """
        
        for driver in partner.get('drivers', []):
            vehicle = driver.get('vehicle', {})
            html_content += f"""
                <tr>
                    <td>{driver.get('nom', '')}</td>
                    <td>{driver.get('telephone', '')}</td>
                    <td>{driver.get('email', '')}</td>
                    <td>{vehicle.get('type', '')} {vehicle.get('marque', '')} {vehicle.get('modele', '')}</td>
                    <td>{vehicle.get('matricule', '')}</td>
                </tr>
            """
        
        html_content += """
            </table>
        </body>
        </html>
        """
        
        html_file = partner_dir / "data.html"
        with open(html_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
    
    log(f"✅ Données organisées pour {len(partners)} partenaires")

def main():
    """Fonction principale"""
    try:
        # Matching
        partners = match_drivers_to_partners()
        
        # Organisation
        organize_data(partners)
        
        # Notification
        total_drivers = sum(p.get('driver_count', 0) for p in partners)
        send_slack(f"✅ match_organize terminé: {len(partners)} partenaires, {total_drivers} chauffeurs")
        
        log("🎉 Traitement terminé avec succès!")
        
    except Exception as e:
        log(f"❌ Erreur: {e}")
        send_slack(f"❌ match_organize erreur: {e}", "#ff0000")

if __name__ == "__main__":
    main()
