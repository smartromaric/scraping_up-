"""Liste chauffeurs à recharger + comparaison / fusion state.json."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from partner_dashboard.config import DASHBOARD_DIR, STATE_FILE
from partner_state_mobile import (
    TRANSFER_REASON_ELIGIBLE,
    driver_match_key,
    is_transfer_done,
    merge_partner_record,
    normalize_phone,
    save_state,
)

_INVALID_PHONE_HINTS = ("+225+225", "225225")

# state.json, state (1).json, state(2).json, state_20260529_143052.json …
_STATE_UPLOAD_NAME_RE = re.compile(
    r"^state(?:"
    r"\s*\(\s*\d+\s*\)"  # state (1).json
    r"|\(\d+\)"  # state(2).json
    r"|[-_\s.]+\d+"  # state-1.json
    r"|_\d{8}_\d{6}"  # state_20260529_143052.json
    r"|-\d{8}-\d{6}"  # state-20260529-143052.json
    r")?\.json$",
    re.IGNORECASE,
)

MARK_SOURCE_DASHBOARD = "dashboard"


def is_valid_state_upload_filename(filename: str | None) -> bool:
    """Accepte les exports Windows type « state (1).json »."""
    if not filename:
        return False
    name = Path(filename).name.strip()
    if _STATE_UPLOAD_NAME_RE.match(name):
        return True
    low = name.lower()
    return low.endswith(".json") and low.startswith("state")


def safe_upload_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name[:120] if name else "state_upload.json"


def state_export_filename(ts: datetime | None = None) -> str:
    """Nom d'export horodaté : state_YYYYMMDD_HHMMSS.json."""
    when = ts or datetime.now()
    return f"state_{when.strftime('%Y%m%d_%H%M%S')}.json"


def parse_uploaded_state(raw: bytes, filename: str | None = None) -> dict[str, Any]:
    if filename and not is_valid_state_upload_filename(filename):
        raise ValueError(
            "Nom de fichier non reconnu. Utilisez state.json, state (1).json, "
            "state_20260529_143052.json, etc."
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON invalide: {e}") from e
    if not isinstance(data.get("partners"), dict):
        raise ValueError("Format invalide: clé « partners » manquante ou incorrecte.")
    return data


def archive_uploaded_state(uploaded: dict[str, Any], original_filename: str) -> Path:
    """Conserve une copie nommée comme le fichier importé (ex. state (1).json)."""
    uploads_dir = DASHBOARD_DIR / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    safe = safe_upload_filename(original_filename)
    dest = uploads_dir / safe
    if dest.exists():
        stem = Path(safe).stem
        dest = uploads_dir / f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    dest.write_text(json.dumps(uploaded, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def is_unassigned_driver(row: dict[str, Any]) -> bool:
    """Chauffeur sans assignation approuvée (admin.reason ≠ assigned_approved)."""
    reason = str(row.get("admin_reason", "")).strip()
    return reason != TRANSFER_REASON_ELIGIBLE


def _admin_reason_label(reason: str) -> str:
    labels = {
        "not_assigned": "Non assigné",
        "driver_not_on_fleet_table": "Absent table flotte",
        "assigned_pending": "Assigné — en attente",
        "assigned_unknown_status": "Assigné — statut inconnu",
        "assigned_approved": "Assigné — approuvé",
    }
    return labels.get(reason, reason or "—")


def _camp_num(partner: dict[str, Any], pk: str) -> int:
    name = partner.get("name", "")
    m = re.search(r"(\d+)\s*$", name or "")
    if m:
        return int(m.group(1))
    try:
        return int(pk)
    except ValueError:
        return 0


def _phone_invalid(phone: str) -> bool:
    raw = phone or ""
    norm = normalize_phone(raw)
    if len(norm) < 10:
        return True
    return any(h in raw for h in _INVALID_PHONE_HINTS)


def _driver_row(
    partner_index: int,
    partner_name: str,
    driver: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    admin = driver.get("admin") or {}
    phone = str(driver.get("phone", "")).strip()
    done = is_transfer_done(driver.get("transfer_2000_done"))
    reason = str(admin.get("reason", "")).strip()
    eligible = reason == TRANSFER_REASON_ELIGIBLE and not done and bool(phone)
    return {
        "partner_index": partner_index,
        "partner_name": partner_name,
        "name": str(driver.get("name", "")).strip(),
        "phone": phone,
        "phone10": normalize_phone(phone),
        "plate": str(admin.get("plate", "")).strip(),
        "admin_reason": reason,
        "transfer_done": done,
        "transfer_at": driver.get("transfer_2000_at") or "",
        "transfer_source": driver.get("transfer_2000_source") or "",
        "eligible": eligible and not _phone_invalid(phone),
        "phone_invalid": _phone_invalid(phone),
        "match_key": driver_match_key(driver),
        "source": source,
        "vehicle_assigned": bool(admin.get("vehicle_assigned")),
        "admin_reason_label": _admin_reason_label(reason),
        "is_unassigned": is_unassigned_driver({"admin_reason": reason}),
    }


def load_state_dict(path: Path | None = None) -> dict[str, Any]:
    p = path or STATE_FILE
    if not p.is_file():
        raise FileNotFoundError(f"state.json introuvable: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def list_all_drivers(state: dict[str, Any], *, source: str = "current") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    partners = state.get("partners") or {}
    iterable = partners.items() if isinstance(partners, dict) else enumerate(partners)
    for pk, partner in iterable:
        idx = _camp_num(partner, str(pk))
        pname = str(partner.get("name", ""))
        for drv in partner.get("drivers") or []:
            rows.append(_driver_row(idx, pname, drv, source=source))
    return rows


def recharge_summary(state: dict[str, Any]) -> dict[str, int]:
    rows = list_all_drivers(state)
    actifs = [r for r in rows if r["admin_reason"] == TRANSFER_REASON_ELIGIBLE]
    to_recharge = [r for r in actifs if r["eligible"]]
    recharged = [r for r in actifs if r["transfer_done"]]
    invalid = [r for r in actifs if not r["transfer_done"] and r["phone_invalid"]]
    non_assignes = [r for r in rows if is_unassigned_driver(r)]
    return {
        "drivers_total": len(rows),
        "actifs": len(actifs),
        "non_assignes": len(non_assignes),
        "recharges": len(recharged),
        "a_recharger": len(to_recharge),
        "invalid_phone": len(invalid),
    }


def _driver_status(row: dict[str, Any]) -> str:
    if row["transfer_done"]:
        return "recharged"
    if is_unassigned_driver(row):
        reason = row.get("admin_reason", "")
        if reason == "not_assigned":
            return "unassigned"
        if reason == "driver_not_on_fleet_table":
            return "not_on_fleet"
        if reason == "assigned_pending":
            return "pending_assignment"
        return "unassigned"
    if row["eligible"]:
        return "to_recharge"
    if row["phone_invalid"]:
        return "invalid_phone"
    return "pending"


def list_to_recharge(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [r for r in list_all_drivers(state) if r["eligible"]]
    rows.sort(key=lambda r: (r["partner_index"], r["name"].upper()))
    return rows


def list_partners_for_filter(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Partenaires présents dans le state (pour filtre liste)."""
    by_idx: dict[int, dict[str, Any]] = {}
    for r in list_all_drivers(state):
        idx = r["partner_index"]
        if idx not in by_idx:
            by_idx[idx] = {
                "partner_index": idx,
                "partner_name": r["partner_name"],
                "drivers_total": 0,
            }
        by_idx[idx]["drivers_total"] += 1
    return sorted(by_idx.values(), key=lambda x: x["partner_index"])


def list_drivers_filtered(
    state: dict[str, Any],
    *,
    view: str = "to_recharge",
    partner_index: int | None = None,
) -> list[dict[str, Any]]:
    """
    view: to_recharge | all | recharged | actifs | non_assignes
    """
    rows = list_all_drivers(state)
    if partner_index is not None:
        rows = [r for r in rows if r["partner_index"] == partner_index]

    if view == "to_recharge":
        rows = [r for r in rows if r["eligible"]]
    elif view == "recharged":
        rows = [
            r
            for r in rows
            if r["admin_reason"] == TRANSFER_REASON_ELIGIBLE and r["transfer_done"]
        ]
    elif view == "actifs":
        rows = [r for r in rows if r["admin_reason"] == TRANSFER_REASON_ELIGIBLE]
    elif view == "non_assignes":
        rows = [r for r in rows if is_unassigned_driver(r)]
    elif view != "all":
        raise ValueError(f"Filtre inconnu: {view}")

    rows = [{**r, "status": _driver_status(r)} for r in rows]
    rows.sort(key=lambda r: (r["partner_index"], r["name"].upper()))
    return rows


def _index_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["match_key"]
        if key.startswith("phone:") and key in out:
            continue
        out[key] = r
    return out


def compare_states(
    current: dict[str, Any],
    uploaded: dict[str, Any],
) -> dict[str, Any]:
    cur_rows = list_all_drivers(current, source="current")
    up_rows = list_all_drivers(uploaded, source="uploaded")
    cur_idx = _index_by_key(cur_rows)
    up_idx = _index_by_key(up_rows)

    all_keys = set(cur_idx) | set(up_idx)
    diff: list[dict[str, Any]] = []
    for key in sorted(all_keys):
        c = cur_idx.get(key)
        u = up_idx.get(key)
        if c and u:
            changes: list[str] = []
            if c["transfer_done"] != u["transfer_done"]:
                changes.append("transfer_2000_done")
            if c["admin_reason"] != u["admin_reason"]:
                changes.append("admin.reason")
            if c["phone"] != u["phone"]:
                changes.append("phone")
            if changes:
                diff.append(
                    {
                        "match_key": key,
                        "name": u["name"] or c["name"],
                        "partner_index": u["partner_index"] or c["partner_index"],
                        "changes": changes,
                        "current": {
                            "transfer_done": c["transfer_done"],
                            "admin_reason": c["admin_reason"],
                            "eligible": c["eligible"],
                        },
                        "uploaded": {
                            "transfer_done": u["transfer_done"],
                            "admin_reason": u["admin_reason"],
                            "eligible": u["eligible"],
                        },
                    }
                )
        elif c and not u:
            diff.append(
                {
                    "match_key": key,
                    "kind": "only_current",
                    "name": c["name"],
                    "partner_index": c["partner_index"],
                    "current": c,
                }
            )
        elif u and not c:
            diff.append(
                {
                    "match_key": key,
                    "kind": "only_uploaded",
                    "name": u["name"],
                    "partner_index": u["partner_index"],
                    "uploaded": u,
                }
            )

    cur_recharge = {r["match_key"]: r for r in list_to_recharge(current)}
    up_recharge = {r["match_key"]: r for r in list_to_recharge(uploaded)}

    only_current_recharge = [
        cur_recharge[k] for k in sorted(set(cur_recharge) - set(up_recharge))
    ]
    only_uploaded_recharge = [
        up_recharge[k] for k in sorted(set(up_recharge) - set(cur_recharge))
    ]
    became_recharged = [
        {
            "match_key": k,
            "name": up_idx[k]["name"],
            "partner_index": up_idx[k]["partner_index"],
            "uploaded_at": up_idx[k].get("transfer_at"),
            "uploaded_source": up_idx[k].get("transfer_source"),
        }
        for k in sorted(set(up_idx) & set(cur_idx))
        if not cur_idx[k]["transfer_done"] and up_idx[k]["transfer_done"]
    ]

    return {
        "summary_current": recharge_summary(current),
        "summary_uploaded": recharge_summary(uploaded),
        "to_recharge_current": list_to_recharge(current),
        "to_recharge_uploaded": list_to_recharge(uploaded),
        "only_current_recharge": only_current_recharge,
        "only_uploaded_recharge": only_uploaded_recharge,
        "became_recharged_in_upload": became_recharged,
        "diff_count": len(diff),
        "diff_sample": diff[:80],
    }


def merge_uploaded_into_current(
    uploaded: dict[str, Any],
    *,
    backup: bool = True,
    uploaded_filename: str = "state_upload.json",
) -> dict[str, Any]:
    """Fusionne le state uploadé dans state.json (préserve transfer_2000_* existants)."""
    if not STATE_FILE.is_file():
        current = {"version": 1, "partners": {}}
    else:
        current = load_state_dict(STATE_FILE)

    if backup and STATE_FILE.is_file():
        bak_dir = DASHBOARD_DIR / "state_backups"
        bak_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(STATE_FILE, bak_dir / f"state_before_upload_{ts}.json")

    up_partners = uploaded.get("partners") or {}
    if not isinstance(up_partners, dict):
        raise ValueError("Format state.json invalide: partners doit être un objet.")

    cur_partners = current.setdefault("partners", {})
    merged_partners = 0
    for pk, up_block in up_partners.items():
        existing = cur_partners.get(str(pk))
        cur_partners[str(pk)] = merge_partner_record(existing, up_block)
        merged_partners += 1

    archived_path = archive_uploaded_state(uploaded, uploaded_filename)
    save_state(current, STATE_FILE)

    return {
        "merged_partners": merged_partners,
        "backup_dir": str(DASHBOARD_DIR / "state_backups"),
        "uploaded_archive": str(archived_path.resolve()),
        "uploaded_filename": safe_upload_filename(uploaded_filename),
        "state_path": str(STATE_FILE.resolve()),
        "summary": recharge_summary(current),
        "to_recharge": list_to_recharge(current),
        "partners_pending": list_partners_pending(current),
    }


def _backup_state_file() -> Path | None:
    if not STATE_FILE.is_file():
        return None
    bak_dir = DASHBOARD_DIR / "state_backups"
    bak_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = bak_dir / f"state_before_edit_{ts}.json"
    shutil.copy2(STATE_FILE, dest)
    return dest


def _mark_driver_recharged(driver: dict[str, Any], *, source: str) -> bool:
    if is_transfer_done(driver.get("transfer_2000_done")):
        return False
    now = datetime.now().isoformat(timespec="seconds")
    driver["transfer_2000_done"] = True
    driver["transfer_2000_at"] = now
    driver["transfer_2000_source"] = source
    return True


def _iter_partner_drivers(state: dict[str, Any]):
    partners = state.get("partners") or {}
    if not isinstance(partners, dict):
        return
    for pk, partner in partners.items():
        idx = _camp_num(partner, str(pk))
        pname = str(partner.get("name", ""))
        for drv in partner.get("drivers") or []:
            yield str(pk), idx, pname, drv


def _find_driver_by_match_key(state: dict[str, Any], match_key: str) -> dict[str, Any] | None:
    for _pk, _idx, _pname, drv in _iter_partner_drivers(state):
        if driver_match_key(drv) == match_key:
            return drv
    return None


def _partner_index_matches(partner_index: int, pk: str, camp_idx: int) -> bool:
    if camp_idx == partner_index:
        return True
    try:
        return int(pk) == partner_index
    except ValueError:
        return False


def _pending_in_partner(driver: dict[str, Any]) -> bool:
    admin = driver.get("admin") or {}
    if str(admin.get("reason", "")).strip() != TRANSFER_REASON_ELIGIBLE:
        return False
    return not is_transfer_done(driver.get("transfer_2000_done"))


def list_partners_pending(state: dict[str, Any]) -> list[dict[str, Any]]:
    counts: dict[int, dict[str, Any]] = {}
    for pk, idx, pname, drv in _iter_partner_drivers(state):
        if not _pending_in_partner(drv):
            continue
        row = counts.get(idx)
        if not row:
            counts[idx] = {
                "partner_index": idx,
                "partner_key": pk,
                "partner_name": pname,
                "pending": 0,
            }
        counts[idx]["pending"] += 1
    return sorted(counts.values(), key=lambda r: r["partner_index"])


def export_state_timestamped() -> tuple[Path, str]:
    """Copie state.json local vers state_YYYYMMDD_HHMMSS.json dans OUTPUT_DIR."""
    if not STATE_FILE.is_file():
        raise FileNotFoundError(f"state.json introuvable: {STATE_FILE}")
    filename = state_export_filename()
    dest = STATE_FILE.parent / filename
    shutil.copy2(STATE_FILE, dest)
    exports_dir = DASHBOARD_DIR / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(STATE_FILE, exports_dir / filename)
    return dest, filename


def mark_drivers_recharged(
    *,
    match_keys: list[str] | None = None,
    partner_index: int | None = None,
    all_pending_in_partner: bool = False,
    phones: list[str] | None = None,
    source: str = MARK_SOURCE_DASHBOARD,
    backup: bool = True,
) -> dict[str, Any]:
    """Marque un ou plusieurs chauffeurs comme rechargés (transfer_2000_done)."""
    if not STATE_FILE.is_file():
        raise FileNotFoundError(f"state.json introuvable: {STATE_FILE}")

    keys = [k.strip() for k in (match_keys or []) if k and k.strip()]
    phone_list = [normalize_phone(p) for p in (phones or []) if str(p).strip()]
    phone_list = [p for p in phone_list if p]

    if not keys and not phone_list and not (partner_index is not None and all_pending_in_partner):
        raise ValueError(
            "Indiquez match_keys, phones, ou partner_index + all_pending_in_partner."
        )
    if all_pending_in_partner and partner_index is None:
        raise ValueError("all_pending_in_partner requiert partner_index.")

    state = load_state_dict(STATE_FILE)
    backup_path = _backup_state_file() if backup else None

    marked: list[dict[str, Any]] = []
    skipped_already: list[str] = []
    not_found: list[str] = []

    if keys:
        for key in keys:
            drv = _find_driver_by_match_key(state, key)
            if not drv:
                not_found.append(key)
                continue
            if _mark_driver_recharged(drv, source=source):
                marked.append({"match_key": key, "name": drv.get("name", "")})
            else:
                skipped_already.append(key)

    if phone_list:
        targets = set(phone_list)
        for pk, idx, pname, drv in _iter_partner_drivers(state):
            p10 = normalize_phone(str(drv.get("phone", "")))
            if p10 not in targets:
                continue
            if partner_index is not None and not _partner_index_matches(
                partner_index, pk, idx
            ):
                continue
            key = driver_match_key(drv)
            if _mark_driver_recharged(drv, source=source):
                marked.append(
                    {
                        "match_key": key,
                        "name": drv.get("name", ""),
                        "partner_index": idx,
                        "phone": drv.get("phone", ""),
                    }
                )
            else:
                skipped_already.append(key)

    if partner_index is not None and all_pending_in_partner:
        for pk, idx, pname, drv in _iter_partner_drivers(state):
            if not _partner_index_matches(partner_index, pk, idx):
                continue
            if not _pending_in_partner(drv):
                continue
            key = driver_match_key(drv)
            if _mark_driver_recharged(drv, source=source):
                marked.append(
                    {
                        "match_key": key,
                        "name": drv.get("name", ""),
                        "partner_index": idx,
                    }
                )
            else:
                skipped_already.append(key)

    save_state(state, STATE_FILE)
    return {
        "marked_count": len(marked),
        "marked": marked,
        "skipped_already": skipped_already,
        "not_found": not_found,
        "backup_path": str(backup_path) if backup_path else None,
        "summary": recharge_summary(state),
        "to_recharge": list_to_recharge(state),
        "partners_pending": list_partners_pending(state),
    }


def export_recharge_csv_rows(state: dict[str, Any]) -> list[dict[str, str]]:
    """Format compatible Appium (sans +225)."""
    rows = []
    for r in list_to_recharge(state):
        if r["phone_invalid"]:
            continue
        p10 = r["phone10"]
        rows.append(
            {
                "numero": p10,
                "montant": "2000",
                "name": r["name"],
                "campagne": str(r["partner_index"]),
                "statut": "",
            }
        )
    return rows
