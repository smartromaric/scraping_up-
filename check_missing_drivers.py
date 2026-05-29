"""
Rapport des partenaires avec moins de 100 conducteurs
======================================================
Lit le dernier fichier partenaires_*.json et liste
les partenaires incomplets avec le nombre manquant.
"""

import json
import sys
from pathlib import Path

TARGET = 100
OUTPUT_DIR = Path(__file__).parent / "output"


def find_latest_json():
    files = sorted(OUTPUT_DIR.glob("partenaires_*.json"), reverse=True)
    for f in files:
        if "report" not in f.name:
            return f
    return None


def main():
    json_file = Path(sys.argv[1]) if len(sys.argv) > 1 else find_latest_json()
    if not json_file or not json_file.exists():
        print(f"❌ Aucun fichier partenaires_*.json trouvé dans {OUTPUT_DIR}")
        sys.exit(1)

    print(f"📂 Fichier analysé: {json_file.name}\n")

    with open(json_file, encoding="utf-8") as f:
        partners = json.load(f)

    incomplets = [
        (p.get("nom", "N/A"), len(p.get("drivers", [])))
        for p in partners
        if len(p.get("drivers", [])) < TARGET
    ]

    incomplets.sort(key=lambda x: x[1])  # Trier par nombre croissant

    if not incomplets:
        print(f"✅ Tous les partenaires ont {TARGET} conducteurs ou plus.")
        return

    header = f"{'Partenaire':<30} {'Conducteurs':>12} {'Manquants':>10}"
    sep    = "-" * len(header)

    print(f"⚠️  {len(incomplets)} partenaire(s) avec moins de {TARGET} conducteurs :\n")
    print(header)
    print(sep)
    for nom, count in incomplets:
        manquants = TARGET - count
        print(f"{nom:<30} {count:>12} {manquants:>10}")
    print(sep)
    print(f"{'TOTAL MANQUANTS':<30} {sum(TARGET - c for _, c in incomplets):>10}")

    # Export TXT
    out_txt = OUTPUT_DIR / "partenaires_incomplets.txt"
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(f"Fichier source: {json_file.name}\n\n")
        f.write(f"{len(incomplets)} partenaire(s) avec moins de {TARGET} conducteurs\n\n")
        f.write(header + "\n" + sep + "\n")
        for nom, count in incomplets:
            manquants = TARGET - count
            f.write(f"{nom:<30} {count:>12} {manquants:>10}\n")
        f.write(sep + "\n")
        f.write(f"{'TOTAL MANQUANTS':<30} {sum(TARGET - c for _, c in incomplets):>10}\n")

    print(f"\n✅ Rapport exporté: {out_txt}")


if __name__ == "__main__":
    main()
