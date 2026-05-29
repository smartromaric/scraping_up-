#!/usr/bin/env python3
"""
Génère les rapports HTML d'activation UPJUNOO PRO à partir du JSON
export_partner_fleet_drivers_report.py.

Usage :
  python generate_activation_report.py \\
    --input output/partner_automation/rapport_partenaires_20260522_150342.json \\
    --state output/partner_automation/state.json
  python generate_activation_report.py --input ...json --only 2
"""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

TEAL = "#016d71"
GOLD = "#f8bb10"
TEXT_MUTED = "#64748b"
VERSION = "1.0"
DESTINATAIRE = "Direction Générale"
FOOTER_TEAM = "Équipe de Développement | Ingénierie & Systèmes"
RECHARGE_AMOUNT = 2000
RECHARGE_BUDGET_PER_CAMPAIGN = 200_000
DEFAULT_STATE = Path("output/partner_automation/state.json")

MODEL_LIKE = re.compile(
    r"^(dzire|s-presso|alto|swift|vitz|belta|t55|espresso|predso|presso|confort|eco|premium)$",
    re.I,
)


def esc(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def parse_fr_datetime(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        mois = [
            "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
            "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
        ]
        return f"{dt.day:02d} {mois[dt.month - 1]} {dt.year} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    except Exception:
        return iso or "—"


def parse_vehicle_from_row(v: dict[str, Any]) -> dict[str, str]:
    parts = [p.strip() for p in (v.get("row_text") or "").split("|")]
    vtype = parts[0] if len(parts) > 0 else ""
    brand = parts[1] if len(parts) > 1 else ""
    model = parts[2] if len(parts) > 2 else ""
    vehicle_label = f"{brand} {model}".strip() or model or brand or "—"
    plate = (v.get("plate") or "").strip() or (parts[3] if len(parts) > 3 else "—")
    driver = (v.get("driver_name") or "").strip()
    status = (v.get("fleet_status") or "Non renseigné").strip()
    return {
        "type": vtype or "—",
        "vehicle": vehicle_label,
        "plate": plate,
        "driver": driver,
        "status": status,
    }


def is_valid_driver_name(name: str) -> bool:
    n = (name or "").strip()
    if not n or n in ("-", "—", "N/A"):
        return False
    if len(n) < 4 and " " not in n:
        return False
    if MODEL_LIKE.match(n):
        return False
    return True


def _status_key(status: str) -> str:
    """Normalise pour éviter que « Désapprouvé » matche « approuv »."""
    return (status or "").lower().replace("é", "e").replace("è", "e").replace("ê", "e")


def status_badge(status: str) -> str:
    s = _status_key(status)
    if "desapprouv" in s:
        cls = "badge-err"
    elif "rejet" in s or "refus" in s:
        cls = "badge-err"
    elif "approuv" in s:
        cls = "badge-ok"
    elif "attente" in s:
        cls = "badge-warn"
    else:
        cls = "badge-neutral"
    return f'<span class="badge {cls}">{esc(status)}</span>'


def driver_action_motif(status: str) -> str:
    s = _status_key(status)
    if "desapprouv" in s:
        return "Profil désapprouvé — Action corrective requise"
    if "rejet" in s or "refus" in s:
        return "Profil rejeté — Dossier à compléter"
    if "attente" in s:
        return "Dossier en attente de validation"
    if "approuv" in s:
        return "Profil validé — Prêt pour affectation"
    return "Dossier incomplet"


def fmt_fcfa(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " F"


def normalize_plate(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").upper())


def normalize_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def parse_fr_date_short(iso: str) -> str:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso or "—"


@dataclass
class RechargeEntry:
    name: str
    plate: str
    date_iso: str
    date_display: str
    amount: int = RECHARGE_AMOUNT


@dataclass
class PartnerRechargeData:
    entries: list[RechargeEntry] = field(default_factory=list)
    plates_recharged: set[str] = field(default_factory=set)
    names_recharged: set[str] = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def budget(self) -> int:
        return self.count * RECHARGE_AMOUNT


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def build_recharge_index(state: dict[str, Any]) -> dict[int, PartnerRechargeData]:
    """Recharges depuis state.json (transfer_2000_done). Aucune source affichée."""
    index: dict[int, PartnerRechargeData] = {}
    for key, pent in (state.get("partners") or {}).items():
        try:
            idx = int(pent.get("index") or key)
        except (TypeError, ValueError):
            continue
        data = PartnerRechargeData()
        for d in pent.get("drivers") or []:
            if not d.get("transfer_2000_done"):
                continue
            admin = d.get("admin") or {}
            name = (d.get("name") or "").strip()
            plate = (admin.get("plate") or "").strip()
            at = (d.get("transfer_2000_at") or "").strip()
            entry = RechargeEntry(
                name=name,
                plate=plate,
                date_iso=at,
                date_display=parse_fr_date_short(at),
            )
            data.entries.append(entry)
            if plate:
                data.plates_recharged.add(normalize_plate(plate))
            if name:
                data.names_recharged.add(normalize_name(name))
        index[idx] = data
    return index


def global_recharge_totals(recharge_index: dict[int, PartnerRechargeData]) -> tuple[int, int]:
    total_budget = sum(d.budget for d in recharge_index.values())
    total_active = sum(d.count for d in recharge_index.values())
    return total_active, total_budget


def group_recharge_waves(entries: list[RechargeEntry]) -> list[tuple[str, str, list[RechargeEntry]]]:
    """Regroupe par jour (Vague 1, 2…) sans exposer transfer_2000_source / rapport."""
    by_day: dict[str, list[RechargeEntry]] = {}
    for e in entries:
        day = (e.date_iso or "")[:10] or "unknown"
        by_day.setdefault(day, []).append(e)
    waves: list[tuple[str, str, list[RechargeEntry]]] = []
    for i, day in enumerate(sorted(by_day.keys()), start=1):
        day_entries = sorted(by_day[day], key=lambda x: (x.date_iso, x.name))
        period = parse_fr_date_short(day_entries[0].date_iso).split()[0] if day_entries else day
        waves.append((f"Vague {i}", period, day_entries))
    return waves


def vehicle_recharge_cell(row: dict[str, str], rd: PartnerRechargeData | None) -> str:
    if not rd or not rd.entries:
        return "—"
    plate = normalize_plate(row.get("plate", ""))
    driver = normalize_name(row.get("driver", ""))
    if plate and plate in rd.plates_recharged:
        return fmt_fcfa(RECHARGE_AMOUNT)
    if driver and driver in rd.names_recharged:
        return fmt_fcfa(RECHARGE_AMOUNT)
    return "—"


def table_recharges(rd: PartnerRechargeData) -> str:
    if not rd.entries:
        return '<div class="empty-msg">Aucune recharge enregistrée pour cette campagne.</div>'
    parts: list[str] = []
    for vague_label, period, items in group_recharge_waves(rd.entries):
        rows = "".join(
            f"<tr><td>{esc(e.date_display)}</td><td>{esc(e.name)}</td>"
            f"<td>{esc(e.plate or '—')}</td><td>{esc(fmt_fcfa(e.amount))}</td>"
            f"<td>{status_badge('Effectué')}</td></tr>"
            for e in items
        )
        parts.append(
            f'<div class="block-title">{esc(vague_label)}</div>'
            f'<p class="block-note">Période : {esc(period)} — {len(items)} recharge(s).</p>'
            f"""<table class="data">
            <thead><tr>
              <th>Date</th><th>Conducteur</th><th>Plaque</th><th>Montant</th><th>Statut</th>
            </tr></thead>
            <tbody>{rows}</tbody></table>""",
        )
    parts.append(
        f'<p class="block-note" style="font-style:normal;font-weight:600">'
        f"Total campagne : {esc(fmt_fcfa(rd.budget))} ({rd.count} recharge(s) × {fmt_fcfa(RECHARGE_AMOUNT)}).</p>",
    )
    return "\n".join(parts)


def base_styles() -> str:
    return f"""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: 'Inter', sans-serif;
  background: #e2e8f0;
  color: #0f172a;
  line-height: 1.45;
}}
.page {{
  width: 210mm;
  min-height: 297mm;
  margin: 12px auto;
  background: #fff;
  position: relative;
  page-break-after: always;
}}
.page:last-child {{ page-break-after: auto; }}
@media print {{
  body {{ background: #fff; }}
  .page {{ margin: 0; box-shadow: none; }}
}}

/* Couverture */
.cover {{
  background: {TEAL};
  color: #fff;
  min-height: 297mm;
  padding: 48px 52px;
  overflow: hidden;
}}
.cover::before, .cover::after {{
  content: '';
  position: absolute;
  border-radius: 50%;
  background: rgba(255,255,255,.06);
}}
.cover::before {{ width: 420px; height: 420px; top: -120px; right: -80px; }}
.cover::after {{ width: 280px; height: 280px; bottom: 60px; left: -60px; }}
.cover-inner {{ position: relative; z-index: 1; height: 100%; display: flex; flex-direction: column; }}
.logo {{ font-size: 28px; font-weight: 800; letter-spacing: .5px; }}
.logo span {{ color: {GOLD}; }}
.badge-pill {{
  display: inline-block;
  margin-top: 28px;
  background: {GOLD};
  color: #0f172a;
  font-size: 11px;
  font-weight: 700;
  padding: 6px 14px;
  border-radius: 999px;
}}
.cover h1 {{
  margin-top: 48px;
  font-size: 42px;
  font-weight: 800;
  line-height: 1.15;
  max-width: 90%;
}}
.cover h1 .gold {{ color: {GOLD}; display: block; margin-top: 8px; }}
.cover .lead {{
  margin-top: 24px;
  max-width: 520px;
  font-size: 14px;
  opacity: .92;
}}
.cover .meta-line {{
  width: 48px;
  height: 4px;
  background: {GOLD};
  margin: 36px 0 20px;
}}
.cover .meta-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 24px;
  font-size: 12px;
}}
.cover .meta-grid label {{
  display: block;
  font-size: 10px;
  opacity: .75;
  text-transform: uppercase;
  letter-spacing: .06em;
  margin-bottom: 4px;
}}
.cover-footer {{
  margin-top: auto;
  padding-top: 40px;
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  font-size: 11px;
  opacity: .9;
}}
.cover-footer .dv {{
  width: 36px; height: 36px; border-radius: 50%;
  background: rgba(255,255,255,.2);
  display: inline-flex; align-items: center; justify-content: center;
  font-weight: 700; margin-right: 10px;
}}

/* Pages intérieures */
.inner {{ padding: 40px 48px 56px; }}
h2.section-title {{
  font-size: 26px;
  font-weight: 800;
  color: {TEAL};
  margin-bottom: 8px;
}}
h2.section-title .gold {{ color: {GOLD}; }}
.sub {{ color: {TEXT_MUTED}; font-size: 13px; margin-bottom: 28px; }}
.kpi-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}}
.kpi {{
  background: #fff;
  border-radius: 12px;
  padding: 20px 16px;
  box-shadow: 0 4px 14px rgba(0,0,0,.08);
  border-top: 4px solid {TEAL};
}}
.kpi.gold-top {{ border-top-color: {GOLD}; }}
.kpi .val {{ font-size: 32px; font-weight: 800; color: #0f172a; }}
.kpi .lbl {{
  font-size: 10px;
  font-weight: 700;
  color: {TEAL};
  text-transform: uppercase;
  letter-spacing: .04em;
  margin-top: 6px;
}}
.charts {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin: 24px 0 32px;
}}
.chart-box {{
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 16px;
}}
.chart-box h4 {{ font-size: 12px; color: {TEXT_MUTED}; margin-bottom: 12px; }}
.bar-track {{
  height: 10px;
  background: #e2e8f0;
  border-radius: 5px;
  overflow: hidden;
}}
.bar-fill {{ height: 100%; background: {TEAL}; border-radius: 5px; }}
.chart-pct {{ font-size: 22px; font-weight: 800; color: {TEAL}; margin-top: 8px; }}

.block-title {{
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 28px 0 12px;
  font-size: 14px;
  font-weight: 800;
  color: #0f172a;
  text-transform: uppercase;
  letter-spacing: .03em;
}}
.block-title::before {{
  content: '';
  width: 4px;
  height: 22px;
  background: {TEAL};
  border-radius: 2px;
}}
.block-note {{ font-size: 12px; color: {TEXT_MUTED}; margin-bottom: 12px; font-style: italic; }}
.empty-msg {{
  padding: 16px;
  background: #f8fafc;
  border-radius: 8px;
  color: {TEXT_MUTED};
  font-size: 13px;
  margin-bottom: 20px;
}}
.warn-banner {{
  background: #fef3c7;
  border-left: 4px solid {GOLD};
  padding: 12px 16px;
  margin-bottom: 20px;
  font-size: 13px;
  border-radius: 0 8px 8px 0;
}}
table.data {{
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 24px;
  font-size: 12px;
}}
table.data thead th {{
  background: {TEAL};
  color: #fff;
  text-align: left;
  padding: 10px 12px;
  font-weight: 700;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .03em;
}}
table.data tbody td {{
  padding: 10px 12px;
  border-bottom: 1px solid #e2e8f0;
}}
table.data tbody tr:nth-child(even) td {{ background: #f8fafc; }}
.badge {{
  display: inline-block;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
}}
.badge-ok {{ background: #dcfce7; color: #166534; }}
.badge-warn {{ background: #fef3c7; color: #92400e; }}
.badge-err {{ background: #fecaca; color: #b91c1c; border: 1px solid #f87171; font-weight: 800; }}
.badge-neutral {{ background: #f1f5f9; color: #475569; }}
.page-foot {{
  position: absolute;
  bottom: 24px;
  left: 48px;
  right: 48px;
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: #94a3b8;
}}
.note-global {{
  font-size: 12px;
  color: {TEXT_MUTED};
  font-style: italic;
  margin-top: 12px;
}}
.exec-summary {{
  margin: 24px 0 28px;
  padding: 20px 24px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
}}
.exec-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
  margin-top: 20px;
}}
@media (max-width: 720px) {{
  .exec-grid {{ grid-template-columns: 1fr; }}
}}
.exec-col {{
  background: #fff;
  border-radius: 10px;
  padding: 16px 18px;
  border-top: 4px solid {TEAL};
  box-shadow: 0 1px 3px rgba(15,23,42,.06);
}}
.exec-col.gold {{ border-top-color: {GOLD}; }}
.exec-col h3 {{
  font-size: 13px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: {TEAL};
  margin-bottom: 12px;
}}
.exec-col.gold h3 {{ color: #b45309; }}
.exec-list {{
  list-style: none;
  margin: 0;
  padding: 0;
}}
.exec-list li {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid #f1f5f9;
  font-size: 12px;
}}
.exec-list li:last-child {{ border-bottom: none; }}
.exec-lbl {{ color: {TEXT_MUTED}; }}
.exec-val {{ font-weight: 800; color: #0f172a; font-size: 14px; }}
"""


def wrap_html(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{esc(title)}</title>
  <style>{base_styles()}</style>
</head>
<body>
{body}
</body>
</html>"""


def cover_page(
    *,
    badge: str,
    line1: str,
    line2_gold: str,
    lead: str,
    date_str: str,
    extra_meta: list[tuple[str, str]],
) -> str:
    meta_cells = "".join(
        f'<div><label>{esc(lbl)}</label>{esc(val)}</div>' for lbl, val in extra_meta
    )
    return f"""
<div class="page cover">
  <div class="cover-inner">
    <div class="logo">UPJUNOO <span>PRO</span></div>
    <div class="badge-pill">{esc(badge)}</div>
    <h1>{esc(line1)}<span class="gold">{esc(line2_gold)}</span></h1>
    <p class="lead">{esc(lead)}</p>
    <div class="meta-line"></div>
    <div class="meta-grid">
      <div><label>Date de publication</label>{esc(date_str)}</div>
      <div><label>Destinataire</label>{DESTINATAIRE}</div>
      {meta_cells}
    </div>
    <div class="cover-footer">
      <div><span class="dv">DV</span>{FOOTER_TEAM}</div>
      <div>CONFIDENTIEL DIRECTION | Version {VERSION}</div>
    </div>
  </div>
</div>"""


def kpi_block(cards_row1: list[tuple[str, str]], cards_row2: list[tuple[str, str]]) -> str:
    def card(val: str, lbl: str, gold: bool = False) -> str:
        cls = "kpi gold-top" if gold else "kpi"
        return f'<div class="{cls}"><div class="val">{esc(val)}</div><div class="lbl">{esc(lbl)}</div></div>'

    all_cards = list(cards_row1) + list(cards_row2)
    n_row1 = len(cards_row1)
    parts: list[str] = []
    for i, (v, l) in enumerate(all_cards):
        parts.append(card(v, l, gold=(i >= n_row1)))
    return f'<div class="kpi-grid">{"".join(parts)}</div>'


def chart_svg(label: str, pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    return f"""
<div class="chart-box">
  <h4>{esc(label)}</h4>
  <div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%"></div></div>
  <div class="chart-pct">{pct:.0f}%</div>
</div>"""


def table_vehicles(
    rows: list[dict[str, str]],
    recharge_data: PartnerRechargeData | None = None,
) -> str:
    if not rows:
        return '<div class="empty-msg">Aucun enregistrement pour cette section.</div>'
    trs = []
    for r in rows:
        rc = vehicle_recharge_cell(r, recharge_data)
        trs.append(
            f"<tr><td>{esc(r['type'])}</td><td>{esc(r['vehicle'])}</td>"
            f"<td>{esc(r['plate'])}</td><td>{esc(r['driver'] or '—')}</td>"
            f"<td>{esc(rc)}</td><td>{status_badge(r['status'])}</td></tr>",
        )
    body = "".join(trs)
    return f"""
<table class="data">
  <thead><tr>
    <th>Type</th><th>Véhicule</th><th>Plaque</th><th>Conducteur</th><th>Recharge</th><th>Statut</th>
  </tr></thead>
  <tbody>{body}</tbody>
</table>"""


def table_drivers(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty-msg">Aucun enregistrement pour cette section.</div>'
    trs = []
    for d in rows:
        st = d.get("approval_status") or "—"
        motif = driver_action_motif(st)
        trs.append(
            f"<tr><td>{esc(d.get('name'))}</td><td>{esc(d.get('vehicle_type') or '—')}</td>"
            f"<td>{status_badge(st)}</td><td>{esc(motif)}</td></tr>",
        )
    return f"""
<table class="data">
  <thead><tr>
    <th>Nom du conducteur</th><th>Type de véhicule</th><th>Statut profil</th><th>Motif / Action</th>
  </tr></thead>
  <tbody>{"".join(trs)}</tbody>
</table>"""


def page_footer(left: str, page_num: int, total: int) -> str:
    return f"""
<div class="page-foot">
  <span>{esc(left)}</span>
  <span>Page {page_num} / {total}</span>
</div>"""


def classify_vehicles(vehicles: list[dict[str, Any]]) -> tuple[list, list, list]:
    approved_assigned: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    approved_unassigned: list[dict[str, str]] = []

    for v in vehicles:
        row = parse_vehicle_from_row(v)
        st = _status_key(row["status"])
        driver_ok = is_valid_driver_name(row["driver"])

        if "desapprouv" in st or "rejet" in st or "refus" in st:
            pending.append(row)
        elif "attente" in st:
            pending.append(row)
        elif "approuv" in st:
            if driver_ok:
                row["driver"] = row["driver"]
                approved_assigned.append(row)
            else:
                row["driver"] = "—"
                approved_unassigned.append(row)
        else:
            if driver_ok:
                approved_assigned.append(row)
            else:
                pending.append(row)
    return approved_assigned, pending, approved_unassigned


def classify_drivers(drivers: list[dict[str, Any]]) -> tuple[list, list]:
    ok, pending = [], []
    for d in drivers:
        st = _status_key(d.get("approval_status") or "")
        if "desapprouv" in st or "rejet" in st or "refus" in st or "attente" in st:
            pending.append(d)
        elif "approuv" in st:
            ok.append(d)
        else:
            pending.append(d)
    return ok, pending


def sum_driver_approval_counts(partners: list[dict[str, Any]]) -> tuple[int, int]:
    """Total conducteurs approuvés / en attente sur une liste de partenaires."""
    approved = pending = 0
    for p in partners:
        ok, pend = classify_drivers(p.get("drivers") or [])
        approved += len(ok)
        pending += len(pend)
    return approved, pending


def partners_indexed(report: dict[str, Any], slots: int = 20) -> dict[int, dict[str, Any]]:
    by_idx: dict[int, dict[str, Any]] = {}
    for p in report.get("partners") or []:
        idx = int(p.get("index") or 0)
        if idx:
            by_idx[idx] = p
    for i in range(1, slots + 1):
        if i not in by_idx:
            by_idx[i] = {
                "index": i,
                "name": f"Campagne UPJUNOO {i}",
                "email": f"campagne{i}@upjunoo.com",
                "vehicles_count": 0,
                "drivers_count": 0,
                "vehicles_by_status": {},
                "drivers_by_status": {},
                "vehicles": [],
                "drivers": [],
                "errors": [],
            }
    return by_idx


def _sum_status(partners: list[dict[str, Any]], key: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for p in partners:
        for status, count in (p.get(key) or {}).items():
            totals[str(status)] = totals.get(str(status), 0) + int(count or 0)
    return totals


def aggregate_status_breakdown(
    by_status: dict[str, Any],
    total_registered: int | None = None,
    *,
    disapproved_as_pending: bool = False,
) -> dict[str, int]:
    """Répartition Approuvé / En attente / Refusé pour l'executive summary."""
    approved = pending = rejected = 0
    for status, count in (by_status or {}).items():
        c = int(count or 0)
        s = _status_key(str(status))
        if disapproved_as_pending and "desapprouv" in s:
            pending += c
        elif "desapprouv" in s or "rejet" in s or "refus" in s:
            rejected += c
        elif "attente" in s:
            pending += c
        elif "approuv" in s:
            approved += c
    registered = (
        int(total_registered)
        if total_registered is not None
        else approved + pending + rejected
    )
    return {
        "registered": registered,
        "approved": approved,
        "pending": pending,
        "rejected": rejected,
    }


def compute_recharge_budget(used: int, campaign_count: int = 1) -> dict[str, Any]:
    """Enveloppe 200 000 F par campagne — utilisé depuis state.json, reste = budget − utilisé."""
    count = max(1, int(campaign_count or 1))
    budget_global = RECHARGE_BUDGET_PER_CAMPAIGN * count
    used = int(used or 0)
    reste = max(0, budget_global - used)
    return {
        "budget_global": budget_global,
        "budget_global_display": fmt_fcfa(budget_global),
        "montant_utilise": used,
        "montant_utilise_display": fmt_fcfa(used),
        "reste": reste,
        "reste_display": fmt_fcfa(reste),
        "budget_per_campaign": RECHARGE_BUDGET_PER_CAMPAIGN,
        "budget_per_campaign_display": fmt_fcfa(RECHARGE_BUDGET_PER_CAMPAIGN),
        "campaign_count": count,
    }


def build_executive_summary(
    totals: dict[str, Any],
    recharge_index: dict[int, PartnerRechargeData],
    *,
    campaign_count: int | None = None,
) -> dict[str, Any]:
    """Indicateurs executive summary (véhicules, conducteurs, recharges)."""
    vbs = totals.get("vehicles_by_status") or {}
    dbs = totals.get("drivers_by_status") or {}
    vehicles = aggregate_status_breakdown(vbs, totals.get("vehicles"))
    drivers = aggregate_status_breakdown(
        dbs,
        totals.get("drivers"),
        disapproved_as_pending=True,
    )
    active, used = global_recharge_totals(recharge_index)
    count = campaign_count if campaign_count is not None else max(1, len(recharge_index))
    recharge = compute_recharge_budget(used, count)
    recharge["recharge_count"] = active
    recharge["unit_amount"] = RECHARGE_AMOUNT
    return {
        "vehicles": vehicles,
        "drivers": drivers,
        "recharge": recharge,
    }


def _recharge_summary_rows(r: dict[str, Any]) -> list[tuple[str, str]]:
    count = int(r.get("campaign_count") or 1)
    per = r.get("budget_per_campaign_display") or fmt_fcfa(RECHARGE_BUDGET_PER_CAMPAIGN)
    if count > 1:
        budget_lbl = f"Budget global ({count} × {per})"
    else:
        budget_lbl = f"Budget campagne ({per})"
    return [
        (budget_lbl, r.get("budget_global_display", "0 F")),
        ("Montant utilisé", r.get("montant_utilise_display", "0 F")),
        ("Reste", r.get("reste_display", "0 F")),
    ]


def executive_summary_html(summary: dict[str, Any]) -> str:
    """Bloc HTML executive summary (3 colonnes)."""
    v = summary.get("vehicles") or {}
    d = summary.get("drivers") or {}
    r = summary.get("recharge") or {}

    def col(title: str, rows: list[tuple[str, int | str]], gold: bool = False) -> str:
        cls = "exec-col gold" if gold else "exec-col"
        items = "".join(
            f'<li><span class="exec-lbl">{esc(lbl)}</span>'
            f'<span class="exec-val">{esc(str(val))}</span></li>'
            for lbl, val in rows
        )
        return f'<div class="{cls}"><h3>{esc(title)}</h3><ul class="exec-list">{items}</ul></div>'

    return f"""
<div class="exec-summary">
  <div class="exec-summary-head">
    <h2 class="section-title" style="margin:0">Executive <span class="gold">Summary</span></h2>
    <p class="sub" style="margin:8px 0 0">Synthèse des indicateurs clés — lecture direction</p>
  </div>
  <div class="exec-grid">
    {col("Véhicules", [
        ("Total enregistré", v.get("registered", 0)),
        ("Total approuvé", v.get("approved", 0)),
        ("Total en attente", v.get("pending", 0)),
        ("Total refusé", v.get("rejected", 0)),
    ])}
    {col("Conducteurs", [
        ("Total enregistré", d.get("registered", 0)),
        ("Total approuvé", d.get("approved", 0)),
        ("Total en attente", d.get("pending", 0)),
        ("Total refusé", d.get("rejected", 0)),
        ("Total actif (rechargé)", r.get("recharge_count", 0)),
    ])}
    {col("Recharge", _recharge_summary_rows(r), gold=True)}
  </div>
</div>"""


def filter_report_range(report: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    partners = [
        p for p in (report.get("partners") or [])
        if start <= int(p.get("index") or 0) <= end
    ]
    filtered = dict(report)
    filtered["partners"] = partners
    filtered["partners_count"] = len(partners)
    filtered["totals"] = {
        "vehicles": sum(int(p.get("vehicles_count") or 0) for p in partners),
        "drivers": sum(int(p.get("drivers_count") or 0) for p in partners),
        "vehicles_by_status": _sum_status(partners, "vehicles_by_status"),
        "drivers_by_status": _sum_status(partners, "drivers_by_status"),
    }
    return filtered


def render_global(
    report: dict[str, Any],
    recharge_index: dict[int, PartnerRechargeData],
    *,
    start_index: int = 1,
    end_index: int = 20,
) -> str:
    totals = report.get("totals") or {}
    date_str = parse_fr_datetime(report.get("generated_at", ""))
    by_idx = partners_indexed(report, slots=end_index)
    empty_slots = [
        i for i in range(start_index, end_index + 1)
        if by_idx[i].get("vehicles_count", 0) == 0 and by_idx[i].get("drivers_count", 0) == 0
    ]
    empty_note = ""
    if empty_slots:
        if len(empty_slots) > 8:
            ranges = f"{empty_slots[0]} à {empty_slots[-1]}"
        else:
            ranges = ", ".join(str(x) for x in empty_slots)
        empty_note = (
            f'<p class="note-global">Note : Les campagnes partenaires {ranges} n\'ont '
            f"actuellement aucun enregistrement (véhicules ou chauffeurs) dans l'export admin "
            f"du {esc(date_str.split()[0] if date_str else '')}.</p>"
        )

    approved_v = (totals.get("vehicles_by_status") or {}).get("Approuvé", 0)
    total_v = totals.get("vehicles", 0) or 0
    partners_list = report.get("partners") or []
    drivers_approved, drivers_pending = sum_driver_approval_counts(partners_list)
    active_global, budget_used = global_recharge_totals(recharge_index)
    campaign_count = end_index - start_index + 1
    exec_summary = build_executive_summary(
        totals,
        recharge_index,
        campaign_count=campaign_count,
    )
    exec_html = executive_summary_html(exec_summary)

    cover = cover_page(
        badge="● RAPPORT CONSOLIDÉ",
        line1="Bilan Global d'Activation",
        line2_gold=(
            "Toutes Campagnes Partenaires"
            if (start_index, end_index) == (1, 20)
            else f"Campagnes {start_index} à {end_index}"
        ),
        lead=(
            "Synthèse stratégique et opérationnelle de l'ensemble des campagnes "
            "d'activation de la flotte partenaire UPJUNOO."
            if (start_index, end_index) == (1, 20)
            else f"Synthèse stratégique et opérationnelle des campagnes {start_index} à {end_index}."
        ),
        date_str=date_str,
        extra_meta=[],
    )

    kpis = kpi_block(
        [
            (str(total_v), "Total véhicules"),
            (str(approved_v), "Total approuvés"),
            (str(drivers_approved), "Conducteurs approuvés"),
        ],
        [
            (str(drivers_pending), "Conducteurs non approuvés"),
            (str(active_global), "Chauffeurs actifs"),
            (fmt_fcfa(budget_used), "Montant rechargé"),
        ],
    )

    recap_rows = []
    for i in range(start_index, end_index + 1):
        p = by_idx[i]
        appr = (p.get("vehicles_by_status") or {}).get("Approuvé", 0)
        rd = recharge_index.get(i) or PartnerRechargeData()
        recap_rows.append(
            f"<tr><td>Campagne {i}</td><td>{esc(p.get('name', 'Flotte Partenaire'))}</td>"
            f"<td>{p.get('vehicles_count', 0)}</td><td>{appr}</td>"
            f"<td>{p.get('drivers_count', 0)}</td><td>{esc(fmt_fcfa(rd.budget))}</td></tr>",
        )

    inner = f"""
<div class="page inner" style="position:relative">
  <h2 class="section-title">Synthèse <span class="gold">Globale</span></h2>
  <p class="sub">Flotte et conducteurs : export admin. Recharges : registre partenaire ({active_global} recharge(s)).</p>
  {exec_html}
  {kpis}
  <div class="block-title">Récapitulatif par Campagne ({start_index} à {end_index})</div>
  {empty_note}
  <table class="data">
    <thead><tr>
      <th>Campagne</th><th>Partenaire / Client</th><th>Véhicules</th>
      <th>Approuvés</th><th>Conducteurs</th><th>Recharges</th>
    </tr></thead>
    <tbody>{"".join(recap_rows)}</tbody>
  </table>
  {page_footer("UPJUNOO PRO — Rapport Global", 1, 1)}
</div>"""

    return wrap_html("Bilan Global d'Activation — UPJUNOO PRO", cover + inner)


def render_campaign(
    partner: dict[str, Any],
    report: dict[str, Any],
    recharge_data: PartnerRechargeData | None = None,
) -> str:
    rd = recharge_data or PartnerRechargeData()
    idx = partner.get("index", "?")
    date_str = parse_fr_datetime(partner.get("scraped_at") or report.get("generated_at", ""))
    vehicles = partner.get("vehicles") or []
    drivers = partner.get("drivers") or []
    vbs = partner.get("vehicles_by_status") or {}
    approved_n = vbs.get("Approuvé", 0)

    appr_v, pend_v, unass_v = classify_vehicles(vehicles)
    drv_ok, drv_pend = classify_drivers(drivers)

    pct_fleet = (approved_n / len(vehicles) * 100) if vehicles else 0
    pct_drv = (
        sum(1 for d in drivers if "approuv" in (d.get("approval_status") or "").lower())
        / len(drivers) * 100
        if drivers else 0
    )

    errors = partner.get("errors") or []
    warn = ""
    if errors:
        warn = f'<div class="warn-banner">Avertissement scrape : {esc("; ".join(errors))}</div>'

    driver_admin_note = ""
    if not drivers and vehicles:
        driver_admin_note = (
            '<div class="empty-msg">Aucun conducteur dans l\'onglet admin « Détails du conducteur ». '
            "Les noms affichés sur la flotte sont des conducteurs assignés aux véhicules.</div>"
        )

    cover = cover_page(
        badge="● RAPPORT D'OPÉRATIONS",
        line1="Bilan d'Activation de la",
        line2_gold="Campagne Flotte",
        lead="Analyse détaillée de l'état de validation de la flotte de véhicules, "
        "de l'affectation des conducteurs et des allocations de rechargement de démarrage.",
        date_str=date_str,
        extra_meta=[("Campagne active", f"{partner.get('name')} — {partner.get('email')}")],
    )

    recharge_fin = compute_recharge_budget(rd.budget, campaign_count=1)
    camp_exec = {
        "vehicles": aggregate_status_breakdown(
            partner.get("vehicles_by_status") or {},
            partner.get("vehicles_count"),
        ),
        "drivers": aggregate_status_breakdown(
            partner.get("drivers_by_status") or {},
            partner.get("drivers_count"),
            disapproved_as_pending=True,
        ),
        "recharge": {**recharge_fin, "recharge_count": rd.count, "unit_amount": RECHARGE_AMOUNT},
    }
    camp_exec_html = executive_summary_html(camp_exec)

    kpis = kpi_block(
        [
            (str(approved_n), "Véhicules approuvés"),
            (str(len(drv_ok)), "Conducteurs approuvés"),
            (str(len(drv_pend)), "Conducteurs non approuvés"),
        ],
        [
            (str(rd.count), "Chauffeurs actifs (recharges)"),
            (recharge_fin["montant_utilise_display"], "Montant utilisé"),
            (recharge_fin["reste_display"], "Reste"),
        ],
    )

    charts = f'<div class="charts">{chart_svg("Taux d\'approbation flotte", pct_fleet)}'
    charts += chart_svg("Taux d'approbation conducteur", pct_drv) + "</div>"

    sections = f"""
  <div class="block-title">Véhicules approuvés &amp; assignés ({len(appr_v)})</div>
  <p class="block-note">Véhicules validés par l'administration avec conducteur affecté.</p>
  {table_vehicles(appr_v, rd)}

  <div class="block-title">Véhicules en attente de documents ({len(pend_v)})</div>
  <p class="block-note">Motif par défaut : Carte grise (non détaillé dans l'export admin).</p>
  {table_vehicles(pend_v, rd)}

  <div class="block-title">Véhicules approuvés non assignés ({len(unass_v)})</div>
  <p class="block-note">Motif par défaut : Permis / conducteur non affecté.</p>
  {table_vehicles(unass_v, rd)}

  <div class="block-title">Conducteurs approuvés ({len(drv_ok)})</div>
  <p class="block-note">Conducteurs validés prêts pour affectation.</p>
  {driver_admin_note}
  {table_drivers(drv_ok)}

  <div class="block-title">Conducteurs en attente ({len(drv_pend)})</div>
  {table_drivers(drv_pend)}
"""

    page2 = f"""
<div class="page inner" style="position:relative">
  <h2 class="section-title">Tableau de Bord <span class="gold">Exécutif</span></h2>
  <p class="sub">Campagne {esc(idx)} — {esc(partner.get('email'))}</p>
  {warn}
  {camp_exec_html}
  {kpis}
  {charts}
  {sections}
  {page_footer(f"UPJUNOO PRO — Rapport de Campagne {idx}", 2, 3)}
</div>"""

    page3 = f"""
<div class="page inner" style="position:relative">
  <h2 class="section-title">Suivi des <span class="gold">Rechargements</span></h2>
  <p class="sub">Historique des transactions ({fmt_fcfa(RECHARGE_AMOUNT)} par conducteur).</p>
  {table_recharges(rd)}
  {page_footer(f"UPJUNOO PRO — Rapport de Campagne {idx}", 3, 3)}
</div>"""

    title = f"Bilan d'Activation — Campagne {idx} — UPJUNOO PRO"
    return wrap_html(title, cover + page2 + page3)


def generate(
    report: dict[str, Any],
    out_dir: Path,
    *,
    recharge_index: dict[int, PartnerRechargeData] | None = None,
    only: int | None = None,
    start: int | None = None,
    end: int | None = None,
    global_only: bool = False,
) -> list[Path]:
    recharge_index = recharge_index or {}
    if only is not None:
        start = end = only
    start = 1 if start is None else start
    end = 20 if end is None else end
    if start < 1 or end < 1 or start > end:
        raise ValueError(f"Plage invalide : {start} à {end}")

    report = filter_report_range(report, start, end)
    recharge_index = {
        idx: data for idx, data in recharge_index.items()
        if start <= idx <= end
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    global_dir = out_dir / "global"
    camp_dir = out_dir / "campagnes"
    global_dir.mkdir(exist_ok=True)
    camp_dir.mkdir(exist_ok=True)

    stamp = ""
    gen = report.get("generated_at", "")
    if gen:
        try:
            stamp = datetime.fromisoformat(gen).strftime("%Y%m%d")
        except Exception:
            stamp = "export"

    range_suffix = "" if (start, end) == (1, 20) else f"_P{start:02d}_P{end:02d}"
    g_path = global_dir / f"rapport_activation_global_{stamp}{range_suffix}.html"
    g_path.write_text(
        render_global(report, recharge_index, start_index=start, end_index=end),
        encoding="utf-8",
    )
    written.append(g_path)

    if global_only:
        return written

    partners = report.get("partners") or []

    for p in partners:
        idx = int(p.get("index") or 0)
        if not idx:
            continue
        c_path = camp_dir / f"P{idx:02d}_rapport_activation.html"
        rd = recharge_index.get(idx) or PartnerRechargeData()
        c_path.write_text(render_campaign(p, report, rd), encoding="utf-8")
        written.append(c_path)

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Génère rapports HTML activation UPJUNOO PRO")
    parser.add_argument(
        "--input",
        required=True,
        help="JSON rapport_partenaires_*.json",
    )
    parser.add_argument(
        "--out",
        default="output/partner_automation/rapports_activation",
        help="Dossier de sortie",
    )
    parser.add_argument("--only", type=int, help="Une seule campagne")
    parser.add_argument("--start", type=int, help="Première campagne à inclure dans le rapport global")
    parser.add_argument("--end", type=int, help="Dernière campagne à inclure dans le rapport global")
    parser.add_argument("--global-only", action="store_true")
    parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE),
        help="state.json (recharges transfer_2000_done)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        raise SystemExit(f"Fichier introuvable : {input_path}")

    with input_path.open(encoding="utf-8") as f:
        report = json.load(f)

    state_path = Path(args.state)
    state = load_state(state_path)
    recharge_index = build_recharge_index(state) if state else {}
    if state:
        active, budget = global_recharge_totals(recharge_index)
        print(f"State : {state_path} — {active} recharge(s), budget {fmt_fcfa(budget)}")
    else:
        print(f"State : absent ({state_path}) — recharges à 0")

    out_dir = Path(args.out)
    files = generate(
        report,
        out_dir,
        recharge_index=recharge_index,
        only=args.only,
        start=args.start,
        end=args.end,
        global_only=args.global_only,
    )

    print(f"Export : {input_path}")
    print(f"Sortie : {out_dir.resolve()}")
    for p in files:
        print(f"  - {p.resolve()}")


if __name__ == "__main__":
    main()
