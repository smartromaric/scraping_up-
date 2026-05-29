"""
Résumé du nombre de conducteurs par partenaire
===============================================
Lit les données déjà scrappées depuis organized_by_partner/
et affiche + exporte un tableau récapitulatif trié.
"""

import json
import csv
import re
from pathlib import Path

# ─── Chemins ──────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
ENRICHED_JSON = BASE / "vps_deploy/output/organized_by_partner/all_partners_enriched.json"
FALLBACK_DIR  = BASE / "vps_deploy/output/organized_by_partner"
OUTPUT_CSV    = BASE / "output/drivers_par_partenaire.csv"
OUTPUT_TXT    = BASE / "output/drivers_par_partenaire.txt"


def natural_key(name: str):
    """Tri naturel : Partenaire-10 après Partenaire-9."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', name)]


def load_data():
    if ENRICHED_JSON.exists():
        with open(ENRICHED_JSON, encoding="utf-8") as f:
            return json.load(f)

    # Fallback : lire chaque data.json individuellement
    partners = []
    for data_file in sorted(FALLBACK_DIR.glob("*/data.json")):
        with open(data_file, encoding="utf-8") as f:
            partners.append(json.load(f))
    return partners


def main():
    partners = load_data()
    if not partners:
        print("❌ Aucune donnée trouvée.")
        return

    # Construire le résumé
    rows = []
    for p in partners:
        nom = p.get("nom", "N/A")
        drivers = p.get("drivers", [])
        total = len(drivers)
        approuves = sum(1 for d in drivers if "APPROUVÉ" in str(d.get("type_vehicule", "")))
        desapprouves = total - approuves
        rows.append((nom, total, approuves, desapprouves))

    # Trier par nom (ordre naturel), UNASSIGNED à la fin
    rows.sort(key=lambda r: (r[0] == "UNASSIGNED_DRIVERS", natural_key(r[0])))

    grand_total = sum(r[1] for r in rows)

    # ── Affichage console ────────────────────────────────────────────────────
    header = f"{'Partenaire':<30} {'Total':>7} {'Approuvés':>10} {'Désapprouvés':>13}"
    sep = "-" * len(header)
    print("\n" + "="*len(header))
    print("  CONDUCTEURS PAR PARTENAIRE")
    print("="*len(header))
    print(header)
    print(sep)
    for nom, total, appr, desappr in rows:
        print(f"{nom:<30} {total:>7} {appr:>10} {desappr:>13}")
    print(sep)
    print(f"{'TOTAL':<30} {grand_total:>7}")
    print("="*len(header) + "\n")

    # ── Export CSV ───────────────────────────────────────────────────────────
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Partenaire", "Total Conducteurs", "Approuvés", "Désapprouvés"])
        for row in rows:
            writer.writerow(row)
        writer.writerow(["TOTAL", grand_total, "", ""])
    print(f"✅ CSV exporté : {OUTPUT_CSV}")

    # ── Export TXT ───────────────────────────────────────────────────────────
    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write(header + "\n" + sep + "\n")
        for nom, total, appr, desappr in rows:
            f.write(f"{nom:<30} {total:>7} {appr:>10} {desappr:>13}\n")
        f.write(sep + "\n")
        f.write(f"{'TOTAL':<30} {grand_total:>7}\n")
    print(f"✅ TXT exporté : {OUTPUT_TXT}")


if __name__ == "__main__":
    main()
