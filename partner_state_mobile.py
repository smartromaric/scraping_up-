#!/usr/bin/env python3
"""
Charge la file de transferts mobile depuis output/partner_automation/state.json.

Critère d'éligibilité : admin.reason == "assigned_approved"
et transfer_2000_done != true (anti-doublon par numéro).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = SCRIPT_DIR / "output" / "partner_automation" / "state.json"
TRANSFER_REASON_ELIGIBLE = "assigned_approved"
TRANSFER_PRESERVE_KEYS = ("transfer_2000_done", "transfer_2000_at", "transfer_2000_source")


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def normalize_name(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def names_match(a: str, b: str) -> bool:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    pa, pb = set(na.split()), set(nb.split())
    return len(pa & pb) >= min(2, min(len(pa), len(pb)))


def driver_match_key(driver: dict[str, Any]) -> str:
    phone = normalize_phone(str(driver.get("phone", "")))
    if phone:
        return f"phone:{phone}"
    name = normalize_name(str(driver.get("name", "")))
    return f"name:{name}" if name else f"row:{id(driver)}"


def is_transfer_done(value: Any) -> bool:
    return value in (True, "true", "True", 1, "1", "oui", "OK")


def find_existing_driver(
    old_drivers: list[dict[str, Any]],
    fresh: dict[str, Any],
) -> dict[str, Any] | None:
    fp = normalize_phone(str(fresh.get("phone", "")))
    if fp:
        for od in old_drivers:
            if normalize_phone(str(od.get("phone", ""))) == fp:
                return od
    fn = str(fresh.get("name", ""))
    for od in old_drivers:
        if names_match(fn, str(od.get("name", ""))):
            return od
    return None


def merge_driver_record(
    existing: dict[str, Any] | None,
    fresh: dict[str, Any],
) -> dict[str, Any]:
    """Fusionne un chauffeur scrapé avec l'existant — ne supprime jamais transfer_2000_done."""
    if not existing:
        return dict(fresh)
    out = dict(existing)
    for key, value in fresh.items():
        if key in TRANSFER_PRESERVE_KEYS:
            continue
        if key == "admin" and isinstance(value, dict):
            out["admin"] = {**(existing.get("admin") or {}), **value}
        else:
            out[key] = value
    if is_transfer_done(existing.get("transfer_2000_done")):
        for key in TRANSFER_PRESERVE_KEYS:
            if existing.get(key) is not None:
                out[key] = existing[key]
    return out


def merge_partner_record(
    existing: dict[str, Any] | None,
    fresh: dict[str, Any],
) -> dict[str, Any]:
    """
    Met à jour le bloc partenaire.

    Si ``drivers_replace_from_scan`` (scan owner réussi) : la liste reflète
    uniquement le dernier scrape — pas d'anciens chauffeurs conservés (0 = []).
    Sinon : fusion classique (conserve l'historique hors scan du tour).
    """
    if not existing:
        return dict(fresh)

    out = dict(existing)
    for field in ("index", "email", "name", "display_name", "password_hint"):
        if fresh.get(field) is not None:
            out[field] = fresh[field]
    if fresh.get("last_owner_scan"):
        out["last_owner_scan"] = fresh["last_owner_scan"]
    if fresh.get("last_admin_check"):
        out["last_admin_check"] = fresh["last_admin_check"]

    errs = list(existing.get("errors") or [])
    for err in fresh.get("errors") or []:
        if err not in errs:
            errs.append(err)
    out["errors"] = errs

    old_drivers = list(existing.get("drivers") or [])
    fresh_drivers = list(fresh.get("drivers") or [])

    if fresh.get("drivers_replace_from_scan"):
        merged: list[dict[str, Any]] = []
        for fd in fresh_drivers:
            od = find_existing_driver(old_drivers, fd)
            merged.append(merge_driver_record(od, fd))
        out["drivers"] = merged
        if fresh.get("proof_screenshots"):
            out["proof_screenshots"] = fresh["proof_screenshots"]
        else:
            out.pop("proof_screenshots", None)
        return out

    merged = []
    consumed_keys: set[str] = set()
    for fd in fresh_drivers:
        od = find_existing_driver(old_drivers, fd)
        md = merge_driver_record(od, fd)
        merged.append(md)
        if od is not None:
            consumed_keys.add(driver_match_key(od))

    for od in old_drivers:
        if driver_match_key(od) not in consumed_keys:
            merged.append(od)

    out["drivers"] = merged
    return out


def load_state(state_path: Path | str) -> dict[str, Any]:
    path = Path(state_path)
    if not path.is_file():
        raise FileNotFoundError(f"state.json introuvable: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict[str, Any], state_path: Path | str) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state.setdefault("meta", {})
    state["meta"]["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_partner_block(state: dict[str, Any], partner_index: int) -> dict[str, Any]:
    partners = state.get("partners") or {}
    key = str(partner_index)
    if key not in partners:
        raise KeyError(
            f"Partenaire {partner_index} absent de state.json "
            f"(clés présentes: {', '.join(sorted(partners.keys())) or 'aucune'})",
        )
    return partners[key]


def build_transfer_queue(
    state: dict[str, Any],
    partner_index: int,
    *,
    amount: int | str = 2000,
    reason: str = TRANSFER_REASON_ELIGIBLE,
) -> tuple[list[dict[str, str]], dict[str, Any], list[dict[str, str]]]:
    """
    Retourne (lignes à traiter, bloc partenaire, chauffeurs ignorés avec motif).
    """
    partner = get_partner_block(state, partner_index)
    drivers = partner.get("drivers") or []
    pending: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    seen_phones: set[str] = set()
    amount_str = str(amount)

    for i, d in enumerate(drivers):
        name = str(d.get("name", "")).strip()
        phone = str(d.get("phone", "")).strip()
        admin = d.get("admin") or {}
        admin_reason = str(admin.get("reason", "")).strip()
        done = is_transfer_done(d.get("transfer_2000_done"))

        if done:
            skipped.append({"name": name, "phone": phone, "skip": "transfer_2000_done"})
            continue
        if admin_reason != reason:
            skipped.append(
                {
                    "name": name,
                    "phone": phone,
                    "skip": f"reason={admin_reason or '(vide)'}",
                },
            )
            continue
        if not phone:
            skipped.append({"name": name, "phone": phone, "skip": "phone_missing"})
            continue

        norm = normalize_phone(phone)
        if norm and norm in seen_phones:
            skipped.append({"name": name, "phone": phone, "skip": "duplicate_phone"})
            continue
        if norm:
            seen_phones.add(norm)

        pending.append(
            {
                "row_index": str(i + 1),
                "numero": phone,
                "montant": amount_str,
                "statut": "",
                "name": name,
                "admin_reason": admin_reason,
                "plate": str(admin.get("plate", "")),
            },
        )

    return pending, partner, skipped


def mark_transfer_done(
    state_path: Path | str,
    partner_index: int,
    phone: str,
) -> None:
    """Marque transfer_2000_done sur le(s) chauffeur(s) correspondant au numéro."""
    path = Path(state_path)
    state = load_state(path)
    partner = get_partner_block(state, partner_index)
    norm_target = normalize_phone(phone)
    now = datetime.now().isoformat(timespec="seconds")
    updated = 0
    for d in partner.get("drivers") or []:
        if normalize_phone(str(d.get("phone", ""))) != norm_target:
            continue
        d["transfer_2000_done"] = True
        d["transfer_2000_at"] = now
        d["transfer_2000_source"] = d.get("transfer_2000_source") or "app"
        updated += 1
    if updated == 0:
        raise KeyError(f"Aucun chauffeur avec le numéro {phone!r} pour partenaire {partner_index}")
    save_state(state, path)


def resolve_partner_credentials(
    partner: dict[str, Any],
    *,
    email_env: str,
    password_env: str,
    default_password: str,
) -> tuple[str, str]:
    email = email_env.strip() or str(partner.get("email", "")).strip()
    password = password_env.strip() or default_password
    if not email:
        raise ValueError("Email partenaire manquant (state.json ou PARTNER_EMAIL).")
    if not password:
        raise ValueError("Mot de passe partenaire manquant (PARTNER_PASSWORD).")
    return email, password
