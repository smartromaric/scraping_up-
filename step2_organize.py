"""
Step 2 — Organisation par partenaire + détection d'anomalies
=============================================================
Usage:  python3 step2_organize.py [--input path/to/json]

Lit le JSON de step1 et crée :
  output/organized_by_partner/
    ├── Partenaire-1/  → data.json, data.csv, data.html
    ├── Partenaire-2/
    └── ...
  output/reports/
    ├── anomalies.json          (conducteurs mal placés, sans véhicule, doublons)
    └── step2_summary.json      (résumé global)

Pas de Selenium — pur Python.
"""

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ─── Configuration ────────────────────────────────────────────────────────────
OUTPUT_DIR      = Path(__file__).parent / "output"
DEFAULT_INPUT   = OUTPUT_DIR / "step1_partners_complete.json"
ORGANIZED_DIR   = OUTPUT_DIR / "organized_by_partner"
REPORTS_DIR     = OUTPUT_DIR / "reports"


def sanitize_folder_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', '_', name)
    return name[:100]


def load_json(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ═════════════════════════════════════════════════════════════════════════════
#  ANOMALY DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def detect_anomalies(partners: list) -> dict:
    """Détecte les anomalies dans les données."""
    anomalies = {
        "doublons_telephone": [],
        "doublons_matricule": [],
        "conducteurs_sans_vehicule": [],
        "vehicules_incomplets": [],
        "conducteurs_potentiellement_mal_places": [],
    }

    # Construire les index globaux
    phone_to_partners = defaultdict(list)  # téléphone → [(partenaire, driver)]
    plate_to_partners = defaultdict(list)  # matricule → [(partenaire, driver)]

    for partner in partners:
        pname = partner.get("nom", "?")
        for drv in partner.get("drivers", []):
            phone = drv.get("telephone", "").strip()
            vehicle = drv.get("vehicle", {})
            matricule = vehicle.get("matricule", "N/A")

            # Index par téléphone
            if phone:
                phone_to_partners[phone].append({
                    "partenaire": pname,
                    "conducteur": drv.get("nom", "?"),
                    "telephone": phone,
                })

            # Index par matricule
            if matricule and matricule != "N/A":
                plate_to_partners[matricule].append({
                    "partenaire": pname,
                    "conducteur": drv.get("nom", "?"),
                    "matricule": matricule,
                })

            # Sans véhicule
            if matricule == "N/A":
                anomalies["conducteurs_sans_vehicule"].append({
                    "partenaire": pname,
                    "conducteur": drv.get("nom", "?"),
                    "telephone": phone,
                })

            # Véhicule incomplet (a une plaque mais pas de marque/modèle)
            if matricule != "N/A":
                if vehicle.get("marque", "N/A") == "N/A" or vehicle.get("modele", "N/A") == "N/A":
                    anomalies["vehicules_incomplets"].append({
                        "partenaire": pname,
                        "conducteur": drv.get("nom", "?"),
                        "matricule": matricule,
                        "marque": vehicle.get("marque", "N/A"),
                        "modele": vehicle.get("modele", "N/A"),
                    })

    # Doublons téléphone (même numéro chez plusieurs partenaires)
    for phone, entries in phone_to_partners.items():
        partners_set = set(e["partenaire"] for e in entries)
        if len(partners_set) > 1:
            anomalies["doublons_telephone"].append({
                "telephone": phone,
                "partenaires": list(partners_set),
                "conducteurs": [e["conducteur"] for e in entries],
            })

    # Doublons matricule (même plaque chez plusieurs partenaires)
    for plate, entries in plate_to_partners.items():
        partners_set = set(e["partenaire"] for e in entries)
        if len(partners_set) > 1:
            anomalies["doublons_matricule"].append({
                "matricule": plate,
                "partenaires": list(partners_set),
                "conducteurs": [e["conducteur"] for e in entries],
            })
            # Ce sont aussi des potentiellement mal placés
            for entry in entries:
                anomalies["conducteurs_potentiellement_mal_places"].append({
                    "conducteur": entry["conducteur"],
                    "matricule": plate,
                    "partenaire_actuel": entry["partenaire"],
                    "aussi_chez": [p for p in partners_set if p != entry["partenaire"]],
                    "raison": "Matricule présent chez plusieurs partenaires",
                })

    return anomalies


# ═════════════════════════════════════════════════════════════════════════════
#  EXPORT PAR PARTENAIRE
# ═════════════════════════════════════════════════════════════════════════════

def export_partner_json(partner: dict, partner_dir: Path):
    path = partner_dir / "data.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(partner, f, ensure_ascii=False, indent=2)


def export_partner_csv(partner: dict, partner_dir: Path):
    path = partner_dir / "data.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")

        # Info partenaire
        writer.writerow(["# Partenaire", partner.get("nom", "N/A")])
        writer.writerow(["# Email", partner.get("email", "N/A")])
        writer.writerow(["# Téléphone", partner.get("telephone", "N/A")])
        writer.writerow([])

        # Conducteurs
        writer.writerow([
            "Nom", "Téléphone", "Emplacement", "Type Transport",
            "Type Véhicule", "Marque", "Modèle", "Matricule",
        ])
        for drv in partner.get("drivers", []):
            v = drv.get("vehicle", {})
            writer.writerow([
                drv.get("nom", "N/A"),
                drv.get("telephone", "N/A"),
                drv.get("emplacement", "N/A"),
                drv.get("type_transport", "N/A"),
                v.get("type", drv.get("type_vehicule", "N/A")),
                v.get("marque", "N/A"),
                v.get("modele", "N/A"),
                v.get("matricule", "N/A"),
            ])


def export_partner_html(partner: dict, partner_dir: Path):
    drivers = partner.get("drivers", [])
    rows_html = ""
    for d in drivers:
        v = d.get("vehicle", {})
        rows_html += f"""<tr>
            <td>{d.get('nom', 'N/A')}</td>
            <td>{d.get('telephone', 'N/A')}</td>
            <td>{d.get('emplacement', 'N/A')}</td>
            <td>{v.get('type', d.get('type_vehicule', 'N/A'))}</td>
            <td>{v.get('marque', 'N/A')}</td>
            <td>{v.get('modele', 'N/A')}</td>
            <td>{v.get('matricule', 'N/A')}</td>
        </tr>"""

    vehicles_ok = sum(1 for d in drivers if d.get("vehicle", {}).get("matricule", "N/A") != "N/A")

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<title>{partner.get('nom', 'Partner')}</title>
<style>
body{{font-family:'Segoe UI',sans-serif;background:#f4f6f9;margin:0;padding:20px}}
.container{{max-width:1100px;margin:0 auto}}
.card{{background:#fff;border-radius:10px;padding:25px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,0.1)}}
h1{{color:#4a5568;margin:0 0 10px}}
.stats{{display:flex;gap:15px;margin:15px 0}}
.stat{{background:#667eea;color:#fff;padding:12px 20px;border-radius:8px;text-align:center}}
.stat b{{font-size:1.4em;display:block}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{padding:10px;border:1px solid #e2e8f0;text-align:left;font-size:0.9em}}
th{{background:#667eea;color:#fff}}
tr:nth-child(even){{background:#f8fafc}}
.info{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:10px 0}}
.info div{{background:#f8fafc;padding:10px;border-radius:6px;border-left:3px solid #667eea}}
.info .label{{font-size:0.8em;color:#667eea;font-weight:bold}}
</style></head>
<body><div class="container">
<div class="card">
    <h1>{partner.get('nom', 'Partner')}</h1>
    <div class="info">
        <div><span class="label">Email</span><br>{partner.get('email', 'N/A')}</div>
        <div><span class="label">Téléphone</span><br>{partner.get('telephone', 'N/A')}</div>
        <div><span class="label">Owner ID</span><br>{partner.get('owner_id', 'N/A')}</div>
    </div>
    <div class="stats">
        <div class="stat"><b>{len(drivers)}</b>Conducteurs</div>
        <div class="stat"><b>{vehicles_ok}</b>Véhicules OK</div>
        <div class="stat"><b>{len(drivers) - vehicles_ok}</b>Sans véhicule</div>
    </div>
</div>
<div class="card">
    <h2>Conducteurs</h2>
    <table><thead><tr>
        <th>Nom</th><th>Téléphone</th><th>Emplacement</th>
        <th>Type</th><th>Marque</th><th>Modèle</th><th>Matricule</th>
    </tr></thead><tbody>{rows_html}</tbody></table>
</div>
</div></body></html>"""

    path = partner_dir / "data.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Step 2 — Organisation par partenaire + anomalies")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="JSON d'entrée (step1)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"❌ Fichier introuvable: {input_path}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print("📁 STEP 2 : Organisation par partenaire")
    print(f"{'='*60}")

    partners = load_json(input_path)
    print(f"   📂 {len(partners)} partenaires chargés depuis {input_path.name}")

    # ── Création des dossiers ──
    ORGANIZED_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    total_drivers = 0
    for i, partner in enumerate(partners, 1):
        pname = partner.get("nom", f"Partner_{i}")
        folder = ORGANIZED_DIR / sanitize_folder_name(pname)
        folder.mkdir(parents=True, exist_ok=True)

        n_drv = len(partner.get("drivers", []))
        total_drivers += n_drv
        print(f"   [{i}/{len(partners)}] {pname} ({n_drv} conducteurs)")

        export_partner_json(partner, folder)
        export_partner_csv(partner, folder)
        export_partner_html(partner, folder)

    # ── Anomalies ──
    print(f"\n🔍 Détection d'anomalies...")
    anomalies = detect_anomalies(partners)

    anomalies_path = REPORTS_DIR / "anomalies.json"
    with open(anomalies_path, "w", encoding="utf-8") as f:
        json.dump(anomalies, f, ensure_ascii=False, indent=2)

    n_dup_phone = len(anomalies["doublons_telephone"])
    n_dup_plate = len(anomalies["doublons_matricule"])
    n_no_vehicle = len(anomalies["conducteurs_sans_vehicule"])
    n_misplaced = len(anomalies["conducteurs_potentiellement_mal_places"])
    n_incomplete = len(anomalies["vehicules_incomplets"])

    print(f"   📞 Doublons téléphone : {n_dup_phone}")
    print(f"   🔢 Doublons matricule : {n_dup_plate}")
    print(f"   🚫 Sans véhicule : {n_no_vehicle}")
    print(f"   ⚠️  Véhicules incomplets : {n_incomplete}")
    print(f"   🔀 Potentiellement mal placés : {n_misplaced}")

    if n_misplaced > 0:
        print(f"\n   🔀 Conducteurs mal placés (détail) :")
        for a in anomalies["conducteurs_potentiellement_mal_places"][:10]:
            print(f"      • {a['conducteur']} ({a['matricule']}) → chez {a['partenaire_actuel']}, aussi chez {', '.join(a['aussi_chez'])}")
        if n_misplaced > 10:
            print(f"      ... et {n_misplaced - 10} autres (voir anomalies.json)")

    # ── Résumé ──
    summary = {
        "timestamp": datetime.now().isoformat(),
        "input_file": str(input_path),
        "total_partners": len(partners),
        "total_drivers": total_drivers,
        "anomalies_summary": {
            "doublons_telephone": n_dup_phone,
            "doublons_matricule": n_dup_plate,
            "conducteurs_sans_vehicule": n_no_vehicle,
            "vehicules_incomplets": n_incomplete,
            "conducteurs_mal_places": n_misplaced,
        },
        "partners": [
            {
                "nom": p.get("nom"),
                "drivers": len(p.get("drivers", [])),
                "vehicles_ok": sum(
                    1 for d in p.get("drivers", [])
                    if d.get("vehicle", {}).get("matricule", "N/A") != "N/A"
                ),
            }
            for p in partners
        ],
    }
    summary_path = REPORTS_DIR / "step2_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # ── Résumé global (all_partners_enriched.json pour compatibilité update_fleet) ──
    enriched_path = ORGANIZED_DIR / "all_partners_enriched.json"
    with open(enriched_path, "w", encoding="utf-8") as f:
        json.dump(partners, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"✅ STEP 2 TERMINÉ")
    print(f"   📁 Dossiers: {ORGANIZED_DIR}")
    print(f"   📊 Rapports: {REPORTS_DIR}")
    print(f"   🏢 {len(partners)} partenaires | 👥 {total_drivers} conducteurs")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
