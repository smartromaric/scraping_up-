"""Calcul des KPI dashboard (mêmes règles que generate_activation_report.py)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from generate_activation_report import (
    PartnerRechargeData,
    RECHARGE_AMOUNT,
    RECHARGE_BUDGET_PER_CAMPAIGN,
    aggregate_status_breakdown,
    build_executive_summary,
    build_recharge_index,
    classify_drivers,
    classify_vehicles,
    compute_recharge_budget,
    filter_report_range,
    fmt_fcfa,
    global_recharge_totals,
    load_state,
    partners_indexed,
    sum_driver_approval_counts,
)

from partner_dashboard.config import OUTPUT_DIR, STATE_FILE


def report_vehicle_total(data: dict[str, Any]) -> int:
    """Total véhicules déclaré dans l'export (ignore les JSON vides / échec scrape)."""
    totals = data.get("totals") or {}
    total = int(totals.get("vehicles") or 0)
    if total > 0:
        return total
    partners = data.get("partners") or []
    return sum(int(p.get("vehicles_count") or 0) for p in partners)


def is_valid_export(data: dict[str, Any]) -> bool:
    return report_vehicle_total(data) > 0


def load_json_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def find_newest_export_json(out_dir: Path | None = None) -> Path | None:
    """Dernier export valide (véhicules > 0) ; sinon le plus récent."""
    return find_best_export_json(out_dir)


def find_best_export_json(out_dir: Path | None = None) -> Path | None:
    out_dir = out_dir or OUTPUT_DIR
    candidates = sorted(
        out_dir.glob("rapport_partenaires_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    for path in candidates:
        try:
            data = load_json_file(path)
        except (json.JSONDecodeError, OSError):
            continue
        if is_valid_export(data):
            return path
    return candidates[0]


def list_export_reports(out_dir: Path | None = None, limit: int = 40) -> list[dict[str, Any]]:
    out_dir = out_dir or OUTPUT_DIR
    rows: list[dict[str, Any]] = []
    for path in sorted(
        out_dir.glob("rapport_partenaires_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]:
        try:
            data = load_json_file(path)
            vehicles = report_vehicle_total(data)
            valid = vehicles > 0
            generated_at = data.get("generated_at")
        except (json.JSONDecodeError, OSError):
            vehicles = 0
            valid = False
            generated_at = None
        rows.append(
            {
                "name": path.name,
                "path": str(path.resolve()),
                "mtime": path.stat().st_mtime,
                "vehicles": vehicles,
                "valid": valid,
                "generated_at": generated_at,
            },
        )
    return rows


def load_report(path: Path | None = None) -> dict[str, Any]:
    p = path or find_best_export_json()
    if not p or not p.is_file():
        return {}
    data = load_json_file(p)
    data["_source_file"] = str(p.resolve())
    data["_export_valid"] = is_valid_export(data)
    return data


def _pct(num: float, den: float) -> float:
    if not den:
        return 0.0
    return round(num / den * 100, 1)


def campaign_metrics(
    partner: dict[str, Any],
    recharge: PartnerRechargeData | None = None,
) -> dict[str, Any]:
    rd = recharge or PartnerRechargeData()
    vehicles = partner.get("vehicles") or []
    drivers = partner.get("drivers") or []
    vbs = partner.get("vehicles_by_status") or {}
    approved_n = int(vbs.get("Approuvé", 0) or 0)
    appr_v, pend_v, unass_v = classify_vehicles(vehicles)
    drv_ok, drv_pend = classify_drivers(drivers)
    pct_fleet = _pct(approved_n, len(vehicles))
    pct_drv = _pct(
        sum(
            1
            for d in drivers
            if "approuv" in (d.get("approval_status") or "").lower()
        ),
        len(drivers),
    )
    v_count = int(partner.get("vehicles_count") or len(vehicles))
    d_count = int(partner.get("drivers_count") or len(drivers))
    vehicles_br = aggregate_status_breakdown(
        partner.get("vehicles_by_status") or {},
        v_count,
    )
    drivers_br = aggregate_status_breakdown(
        partner.get("drivers_by_status") or {},
        d_count,
        disapproved_as_pending=True,
    )
    used = rd.budget
    recharge_block = compute_recharge_budget(used, campaign_count=1)

    return {
        "index": int(partner.get("index") or 0),
        "name": partner.get("name") or "",
        "email": partner.get("email") or "",
        "vehicles_count": v_count,
        "drivers_count": d_count,
        "vehicles_approved": approved_n,
        "vehicles_assigned": len(appr_v),
        "vehicles_pending": len(pend_v),
        "vehicles_unassigned": len(unass_v),
        "drivers_approved": len(drv_ok),
        "drivers_pending": len(drv_pend),
        "recharge_count": rd.count,
        "recharge_budget": rd.budget,
        "recharge_budget_display": fmt_fcfa(rd.budget),
        "pct_fleet_approval": pct_fleet,
        "pct_driver_approval": pct_drv,
        "errors": partner.get("errors") or [],
        "scraped_at": partner.get("scraped_at"),
        "executive_summary": {
            "vehicles": vehicles_br,
            "drivers": drivers_br,
            "recharge": {
                **recharge_block,
                "recharge_count": rd.count,
                "unit_amount": RECHARGE_AMOUNT,
            },
        },
    }


def global_metrics(
    report: dict[str, Any],
    recharge_index: dict[int, PartnerRechargeData],
    *,
    start: int = 1,
    end: int = 20,
) -> dict[str, Any]:
    filtered = filter_report_range(report, start, end)
    totals = filtered.get("totals") or {}
    partners_list = filtered.get("partners") or []
    recharge_subset = {
        idx: data for idx, data in recharge_index.items() if start <= idx <= end
    }
    drivers_approved, drivers_pending = sum_driver_approval_counts(partners_list)
    approved_v = int((totals.get("vehicles_by_status") or {}).get("Approuvé", 0) or 0)
    total_v = int(totals.get("vehicles", 0) or 0)
    total_d = int(totals.get("drivers", 0) or 0)
    active_global, budget_used = global_recharge_totals(recharge_subset)
    campaign_count = end - start + 1
    executive_summary = build_executive_summary(
        totals,
        recharge_subset,
        campaign_count=campaign_count,
    )

    by_idx = partners_indexed(report, slots=end)
    campaigns: list[dict[str, Any]] = []
    for i in range(start, end + 1):
        p = by_idx[i]
        rd = recharge_index.get(i) or PartnerRechargeData()
        appr = int((p.get("vehicles_by_status") or {}).get("Approuvé", 0) or 0)
        cm = campaign_metrics(p, rd)
        campaigns.append(
            {
                "index": i,
                "name": p.get("name") or f"Campagne UPJUNOO {i}",
                "email": p.get("email") or "",
                "vehicles_count": cm["vehicles_count"],
                "vehicles_approved": appr,
                "drivers_count": cm["drivers_count"],
                "drivers_approved": cm["drivers_approved"],
                "drivers_pending": cm["drivers_pending"],
                "recharge_budget": rd.budget,
                "recharge_budget_display": fmt_fcfa(rd.budget),
                "pct_fleet_approval": cm["pct_fleet_approval"],
                "pct_driver_approval": cm["pct_driver_approval"],
                "empty": cm["vehicles_count"] == 0 and cm["drivers_count"] == 0,
            },
        )

    return {
        "generated_at": report.get("generated_at"),
        "source_file": report.get("_source_file"),
        "partners_count": len(partners_list),
        "range": {"start": start, "end": end},
        "totals": {
            "vehicles": total_v,
            "drivers": total_d,
            "vehicles_approved": approved_v,
            "drivers_approved": drivers_approved,
            "drivers_pending": drivers_pending,
            "recharge_active": active_global,
            "recharge_budget": budget_used,
            "recharge_budget_display": fmt_fcfa(budget_used),
            "pct_fleet_approval": _pct(approved_v, total_v),
        },
        "executive_summary": executive_summary,
        "campaigns": campaigns,
        "recharge_amount_unit": RECHARGE_AMOUNT,
    }


def build_dashboard_payload(
    report_path: Path | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    skipped_invalid: str | None = None
    if report_path is None:
        out_dir = OUTPUT_DIR
        newest = sorted(
            out_dir.glob("rapport_partenaires_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        best = find_best_export_json(out_dir)
        if newest and best and newest[0] != best:
            try:
                if not is_valid_export(load_json_file(newest[0])):
                    skipped_invalid = newest[0].name
            except (json.JSONDecodeError, OSError):
                skipped_invalid = newest[0].name
        report_path = best

    report = load_report(report_path)
    if not report:
        return {
            "ok": False,
            "message": "Aucun export rapport_partenaires_*.json trouvé.",
            "global": None,
            "campaigns": [],
        }
    state = load_state(state_path or STATE_FILE)
    recharge_index = build_recharge_index(state) if state else {}
    g = global_metrics(report, recharge_index)
    by_idx = partners_indexed(report)
    campaigns = [
        campaign_metrics(by_idx[i], recharge_index.get(i))
        for i in sorted(by_idx.keys())
        if 1 <= i <= 20
    ]
    return {
        "ok": True,
        "report_path": report.get("_source_file"),
        "export_valid": bool(report.get("_export_valid", True)),
        "skipped_invalid_report": skipped_invalid,
        "available_reports": list_export_reports(),
        "state_path": str(state_path or STATE_FILE) if (state_path or STATE_FILE).is_file() else None,
        "global": g,
        "campaigns": campaigns,
        "loaded_at": datetime.now().isoformat(timespec="seconds"),
    }
