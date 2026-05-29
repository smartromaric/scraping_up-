#!/usr/bin/env python3
"""
Vérifie les noms des rapports PDF recharge vs state.json.
- Déjà transfer_2000_done → OK
- Sinon → marque recharge manuelle (transfer_2000_source=manual)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from partner_state_mobile import (
    is_transfer_done,
    names_match,
    normalize_name,
    save_state,
    load_state,
)
STATE_PATH = SCRIPT_DIR / "output" / "partner_automation" / "state.json"

REPORTS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "rapport_recharge_chauffeurs20_05_2026.pdf": (
        "2026-05-20T18:00:00",
        [
            ("Tre bi tizie Didier", "AA318RV01"),
            ("KAMBIRE BONTIKO KOKO JOEL", "977KR01"),
            ("TRAORE AMARA FELIX", "AA-013-XQ-01"),
            ("ADON", "AA-657-BS"),
            ("SERY RICHMOND ROMEO", "AA-693-JJ"),
        ],
    ),
    "Rapport de recharge — chauffeurs partenaire21_05_2026.pdf": (
        "2026-05-21T18:00:00",
        [
            ("BIAGNET FELICIEN DODO", "AA-369-SJ-01"),
            ("ISSAU YUSSUF AYO", "2025-55238 WWW-01"),
            ("Diallo issouf", "AA922AX01"),
            ("BOGUI", "AA-894-TF-01"),
            ("DAKOURI KOUDOU GERVAIS JUNIOR", "AB-928-AK"),
            ("FOFANA GOGBE YOUSSOUF", "AA-102-HC-01"),
            ("Yacouba Konaté", "AA-623-BB"),
            ("Diomande lancina", "AA116VF01"),
            ("KONAN KOFFI ENOC", "AA156AJ01"),
            ("CISSE OUSMANE", "AA-247-VK-01"),
            ("Keita", "AA-791-BV"),
            ("SERY RICHMOND ROMEO", "AA-693-JJ"),
        ],
    ),
}


def normalize_plate(p: str) -> str:
    return re.sub(r"[\s\-]", "", (p or "").upper())


def find_drivers_in_state(
    state: dict,
    name: str,
    plate: str,
) -> list[tuple[int, dict]]:
    """Nom d'abord ; plaque sert à départager les doublons."""
    want_plate = normalize_plate(plate)
    by_name: list[tuple[int, dict]] = []
    for pk, partner in state.get("partners", {}).items():
        try:
            idx = int(pk)
        except ValueError:
            continue
        for d in partner.get("drivers") or []:
            if names_match(name, str(d.get("name", ""))):
                by_name.append((idx, d))
    if not by_name:
        return []
    if len(by_name) == 1 or not want_plate:
        return by_name
    plate_hits = []
    for idx, d in by_name:
        row_plate = normalize_plate((d.get("admin") or {}).get("plate", ""))
        if not row_plate or want_plate == row_plate:
            plate_hits.append((idx, d))
        elif want_plate in row_plate or row_plate in want_plate:
            plate_hits.append((idx, d))
    return plate_hits if plate_hits else by_name


def add_driver_from_report(
    state: dict,
    partner_index: int,
    name: str,
    plate: str,
    report_name: str,
    report_at: str,
) -> dict:
    """Crée une entrée minimale si le chauffeur du rapport n'existe pas encore."""
    key = str(partner_index)
    partner = state.setdefault("partners", {}).setdefault(
        key,
        {
            "index": partner_index,
            "email": f"campagne{partner_index}@upjunoo.com",
            "name": f"Campagne UPJUNOO {partner_index}",
            "drivers": [],
        },
    )
    driver = {
        "name": name,
        "phone": "",
        "owner_status": "APPROUVÉ",
        "scraped_at": report_at,
        "admin": {
            "partner_found": True,
            "vehicle_assigned": True,
            "fleet_status": "APPROUVÉ",
            "plate": plate,
            "matched_driver_on_row": name,
            "visual": "green",
            "reason": "assigned_approved",
            "checked_at": report_at,
        },
        "transfer_2000_done": True,
        "transfer_2000_at": report_at,
        "transfer_2000_source": "manual",
        "transfer_2000_rapport": report_name,
        "source": "rapport_recharge_manual",
    }
    partner.setdefault("drivers", []).append(driver)
    return driver


def main() -> None:
    state = load_state(STATE_PATH)
    summary: list[dict] = []
    marked = 0
    already = 0
    missing = 0

    for report_name, (report_at, rows) in REPORTS.items():
        for name, plate in rows:
            hits = find_drivers_in_state(state, name, plate)
            entry = {
                "rapport": report_name,
                "nom_rapport": name,
                "plaque_rapport": plate,
                "statut": "",
                "detail": "",
            }
            if not hits:
                # Rapports partenaire 1 — création entrée manuelle
                add_driver_from_report(
                    state, 1, name, plate, report_name, report_at
                )
                entry["statut"] = "AJOUT_MANUEL"
                entry["detail"] = (
                    f"Absent de state.json — entrée créée partenaire 1, "
                    f"recharge manuelle ({report_name})"
                )
                entry["partenaire"] = 1
                missing += 1
                marked += 1
                summary.append(entry)
                continue

            seen_keys: set[str] = set()
            for idx, d in hits:
                dedupe = f"{idx}|{normalize_name(str(d.get('name', '')))}|{normalize_plate((d.get('admin') or {}).get('plate', ''))}"
                if dedupe in seen_keys:
                    continue
                seen_keys.add(dedupe)
                entry = dict(entry)
                entry["partenaire"] = idx
                entry["nom_state"] = d.get("name")
                entry["phone"] = d.get("phone", "")
                entry["plaque_state"] = (d.get("admin") or {}).get("plate", "")

                if is_transfer_done(d.get("transfer_2000_done")):
                    entry["statut"] = "OK"
                    entry["detail"] = (
                        f"Déjà rechargé (source: {d.get('transfer_2000_source', '?')}, "
                        f"à {d.get('transfer_2000_at', '?')})"
                    )
                    already += 1
                else:
                    d["transfer_2000_done"] = True
                    d["transfer_2000_at"] = report_at
                    d["transfer_2000_source"] = "manual"
                    d["transfer_2000_rapport"] = report_name
                    entry["statut"] = "MARQUE_MANUEL"
                    entry["detail"] = f"Recharge rapport {report_name} — saisie manuelle state.json"
                    marked += 1
                summary.append(entry)

    save_state(state, STATE_PATH)

    out_json = STATE_PATH.parent / "rapport_recharge_verification_state.json"
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"state.json mis à jour: {STATE_PATH}")
    print(f"Détail: {out_json}")
    print(f"OK (déjà rechargés): {already}")
    print(f"Marqués manuel: {marked}")
    print(f"Absents state: {missing}")
    print()
    for s in summary:
        print(f"[{s['statut']}] {s.get('nom_rapport', s.get('nom_state'))} | {s.get('detail', '')}")


if __name__ == "__main__":
    main()
