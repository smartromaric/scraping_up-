import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def normalize_status(value):
    return (value or "").strip().upper()


def safe_ratio(numerator, denominator):
    if denominator == 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def load_partner_payload(partner_file: Path):
    with partner_file.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    drivers = payload.get("drivers", []) or []
    return payload, drivers


def compute_partner_kpis(partner_file: Path, payload: dict, drivers: list):

    assignment_counter = Counter()
    transport_counter = Counter()
    vehicle_fleet_status_counter = Counter()
    owner_approval_counter = Counter()

    excluded_unassigned = 0

    for d in drivers:
        assignment_status = normalize_status(d.get("assignment_status", "MISSING"))
        if assignment_status == "UNASSIGNED_DRIVERS":
            excluded_unassigned += 1
            continue

        assignment_counter[assignment_status] += 1
        transport_counter[(d.get("type_transport") or "INCONNU").strip()] += 1
        owner_approval_counter[normalize_status(d.get("owner_approval_status", "MISSING"))] += 1

        vehicle = d.get("vehicle") or {}
        vehicle_fleet_status_counter[normalize_status(vehicle.get("statut_flotte", "MISSING"))] += 1

    total_drivers = len(drivers) - excluded_unassigned
    done_assignments = assignment_counter.get("DONE", 0)
    missing_assignment = assignment_counter.get("MISSING", 0)

    return {
        "dossier_partenaire": partner_file.parent.name,
        "nom_partenaire": payload.get("nom", partner_file.parent.name),
        "email": payload.get("email"),
        "total_chauffeurs": total_drivers,
        "assignations_done": done_assignments,
        "non_assignes_ou_autre_statut": total_drivers - done_assignments,
        "taux_assignation_pourcent": safe_ratio(done_assignments, total_drivers),
        "assignment_status_manquant": missing_assignment,
        "exclus_unassigned_drivers": excluded_unassigned,
        "repartition_assignment_status": dict(assignment_counter),
        "repartition_statut_flotte_vehicule": dict(vehicle_fleet_status_counter),
        "repartition_owner_approval_status": dict(owner_approval_counter),
        "repartition_type_transport": dict(transport_counter),
    }


def find_partner_data_files(organized_dir):
    if not organized_dir.exists():
        return []
    excluded_partner_folders = {"UNASSIGNED_DRIVERS"}
    return sorted(
        p
        for p in organized_dir.iterdir()
        if p.is_dir()
        and p.name.upper() not in excluded_partner_folders
        and (p / "data.json").exists()
    )


def resolve_organized_dir(preferred_dir):
    """Trouve automatiquement un dossier organized_by_partner valide."""
    candidates = []

    if preferred_dir:
        candidates.append(Path(preferred_dir))

    candidates.extend(
        [
            Path("vps_deploy/output/organized_by_partner"),
            Path("output/organized_by_partner"),
        ]
    )

    seen = set()
    unique_candidates = []
    for c in candidates:
        key = str(c)
        if key not in seen:
            seen.add(key)
            unique_candidates.append(c)

    for c in unique_candidates:
        if find_partner_data_files(c):
            return c

    return Path(preferred_dir) if preferred_dir else Path("output/organized_by_partner")


def write_csv(output_csv, partner_rows):
    fields = [
        "dossier_partenaire",
        "nom_partenaire",
        "email",
        "total_chauffeurs",
        "assignations_done",
        "non_assignes_ou_autre_statut",
        "taux_assignation_pourcent",
        "assignment_status_manquant",
    ]
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in partner_rows:
            writer.writerow({k: row.get(k) for k in fields})


def extract_driver_details_rows(partner_file: Path, payload: dict, drivers: list):
    partner_dossier = partner_file.parent.name
    partner_nom = payload.get("nom", partner_dossier)
    partner_email = payload.get("email")

    rows = []
    for d in drivers:
        # On ne met dans la liste que les chauffeurs qui ont bien un assignment_status renseigné.
        # (sinon la colonne serait vide, ce que tu ne veux pas)
        raw_assignment_status = d.get("assignment_status")
        assignment_status = normalize_status(raw_assignment_status)
        if not assignment_status:
            continue
        if assignment_status == "UNASSIGNED_DRIVERS":
            continue

        vehicle = d.get("vehicle") or {}

        rows.append(
            {
                "nom_partenaire": partner_nom,
                "email": partner_email,
                "chauffeur_nom": d.get("nom"),
                "chauffeur_telephone": d.get("telephone"),
                "emplacement": d.get("emplacement"),
                "type_transport": d.get("type_transport"),
                "type_vehicule": d.get("type_vehicule"),
                "assignment_status": d.get("assignment_status"),
                "vehicle_type": vehicle.get("type"),
                "vehicle_marque": vehicle.get("marque"),
                "vehicle_modele": vehicle.get("modele"),
                "vehicle_matricule": vehicle.get("matricule"),
            }
        )
    return rows


def write_drivers_csv(output_csv: Path, driver_rows: list):
    fields = [
        "nom_partenaire",
        "email",
        "chauffeur_nom",
        "chauffeur_telephone",
        "emplacement",
        "type_transport",
        "type_vehicule",
        "assignment_status",
        "vehicle_type",
        "vehicle_marque",
        "vehicle_modele",
        "vehicle_matricule",
    ]

    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in driver_rows:
            writer.writerow({k: row.get(k) for k in fields})


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Calcule les KPI d'assignation par partenaire a partir des data.json "
            "dans organized_by_partner."
        )
    )
    parser.add_argument(
        "--organized-dir",
        default="vps_deploy/output/organized_by_partner",
        help=(
            "Chemin vers organized_by_partner. Si introuvable, "
            "le script essaie automatiquement output/organized_by_partner."
        ),
    )
    parser.add_argument(
        "--only",
        help="Nom du dossier partenaire a traiter uniquement (ex: Partenaire1)",
    )
    parser.add_argument(
        "--output-json",
        default="output/partner_assignment_kpis.json",
        help="Chemin du fichier JSON de sortie",
    )
    parser.add_argument(
        "--output-csv",
        default="output/partner_assignment_kpis.csv",
        help="Chemin du fichier CSV de sortie (resume)",
    )
    parser.add_argument(
        "--output-drivers-json",
        default="output/partner_drivers_details.json",
        help="Chemin du JSON de sortie avec la liste des chauffeurs par partenaire",
    )
    parser.add_argument(
        "--output-drivers-csv",
        default="output/partner_drivers_details.csv",
        help="Chemin du CSV de sortie (1 ligne = 1 chauffeur)",
    )
    args = parser.parse_args()

    organized_dir = resolve_organized_dir(args.organized_dir)
    partner_dirs = find_partner_data_files(organized_dir)

    if args.only:
        partner_dirs = [p for p in partner_dirs if p.name.lower() == args.only.lower()]

    if not partner_dirs:
        if args.only:
            raise SystemExit(
                f"Aucun dossier '{args.only}' avec data.json trouve dans: {organized_dir}"
            )
        raise SystemExit(f"Aucun dossier partenaire avec data.json trouve dans: {organized_dir}")

    partner_kpis = []
    partner_driver_groups = []
    all_driver_rows = []
    all_driver_rows_count_with_assignment_status = 0
    global_total_drivers = 0
    global_done = 0
    global_assignment_counter = Counter()
    global_vehicle_fleet_counter = Counter()
    global_owner_approval_counter = Counter()
    global_transport_counter = Counter()

    for partner_dir in partner_dirs:
        partner_file = partner_dir / "data.json"
        payload, drivers = load_partner_payload(partner_file)
        row = compute_partner_kpis(partner_file, payload, drivers)
        partner_kpis.append(row)

        driver_rows = extract_driver_details_rows(partner_file, payload, drivers)
        all_driver_rows.extend(driver_rows)
        all_driver_rows_count_with_assignment_status += len(driver_rows)
        partner_driver_groups.append(
            {
                "dossier_partenaire": partner_file.parent.name,
                "nom_partenaire": payload.get("nom", partner_file.parent.name),
                "email": payload.get("email"),
                "chauffeurs": driver_rows,
            }
        )

        global_total_drivers += row["total_chauffeurs"]
        global_done += row["assignations_done"]
        global_assignment_counter.update(row["repartition_assignment_status"])
        global_vehicle_fleet_counter.update(row["repartition_statut_flotte_vehicule"])
        global_owner_approval_counter.update(row["repartition_owner_approval_status"])
        global_transport_counter.update(row["repartition_type_transport"])

    global_summary = {
        "nombre_partenaires": len(partner_kpis),
        "total_chauffeurs": global_total_drivers,
        "assignations_done": global_done,
        "non_assignes_ou_autre_statut": global_total_drivers - global_done,
        "taux_assignation_pourcent": safe_ratio(global_done, global_total_drivers),
        "repartition_assignment_status": dict(global_assignment_counter),
        "repartition_statut_flotte_vehicule": dict(global_vehicle_fleet_counter),
        "repartition_owner_approval_status": dict(global_owner_approval_counter),
        "repartition_type_transport": dict(global_transport_counter),
    }

    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_payload = {
        "resume_global": global_summary,
        "partenaires": partner_kpis,
    }
    output_json.write_text(
        json.dumps(output_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    output_csv = Path(args.output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    write_csv(output_csv, partner_kpis)

    output_drivers_json = Path(args.output_drivers_json)
    output_drivers_json.parent.mkdir(parents=True, exist_ok=True)
    resume_global_for_details = dict(global_summary)
    resume_global_for_details["chauffeurs_avec_assignment_status"] = all_driver_rows_count_with_assignment_status
    drivers_payload = {"resume_global": resume_global_for_details, "partenaires": partner_driver_groups}
    output_drivers_json.write_text(
        json.dumps(drivers_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    output_drivers_csv = Path(args.output_drivers_csv)
    output_drivers_csv.parent.mkdir(parents=True, exist_ok=True)
    write_drivers_csv(output_drivers_csv, all_driver_rows)

    print("")
    print("=== KPI ASSIGNATION PARTENAIRES ===")
    print(f"Dossiers partenaires analyses : {global_summary['nombre_partenaires']}")
    print(f"Total chauffeurs : {global_summary['total_chauffeurs']}")
    print(f"Assignations DONE : {global_summary['assignations_done']}")
    print(f"Taux d'assignation : {global_summary['taux_assignation_pourcent']}%")
    print("")
    print("Top 10 partenaires (par nombre de DONE):")
    top = sorted(partner_kpis, key=lambda x: x["assignations_done"], reverse=True)[:10]
    for idx, row in enumerate(top, start=1):
        print(
            f"{idx:02d}. {row['dossier_partenaire']} | DONE={row['assignations_done']} | "
            f"chauffeurs={row['total_chauffeurs']} | taux={row['taux_assignation_pourcent']}%"
        )
    print("")
    print(f"JSON écrit : {output_json}")
    print(f"CSV écrit  : {output_csv}")
    print(f"JSON chauffeurs : {output_drivers_json}")
    print(f"CSV chauffeurs  : {output_drivers_csv}")


if __name__ == "__main__":
    main()
