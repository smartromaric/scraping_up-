#!/usr/bin/env python3
"""
sync_payment_history_state.py
=============================

Parcourt l'admin UpJunoo → profil partenaire → onglet « Historique des paiements »,
lit les lignes « transfered-to-NOM » (F2000) et marque les chauffeurs correspondants
comme rechargés dans output/partner_automation/state.json (anti-doublon).

Navigation / connexion : même logique que export_partner_fleet_drivers_report.py.

Usage :
  python sync_payment_history_state.py --only 19 --headed
  python sync_payment_history_state.py --start 1 --end 20
  python sync_payment_history_state.py --only 1 --dry-run
  python sync_payment_history_state.py --apply-report output/partner_automation/payment_history_sync_20260529_222127.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from selenium.webdriver.common.by import By

import export_partner_fleet_drivers_report as rpt
import quick_approve_all_vehicle_vps as qa
from partner_state_mobile import (
    is_transfer_done,
    load_state,
    names_match,
    normalize_name,
    save_state,
)

try:
    from partner_dashboard.recharge_service import recharge_summary
except ImportError:
    recharge_summary = None  # type: ignore[misc, assignment]

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output" / "partner_automation"
STATE_FILE = OUTPUT_DIR / "state.json"
LOG_FILE = OUTPUT_DIR / "payment_history_sync.log"
BACKUP_DIR = OUTPUT_DIR / "state_backups"

MARK_SOURCE = "payment_history"
REMARK_RE = re.compile(
    r"transfer(?:r)?ed(?:\s|-)?to[-\s]+(.+)",
    re.IGNORECASE,
)
_PAYMENT_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{1,2}):(\d{2})\s*(AM|PM)?",
    re.IGNORECASE,
)
_FOOTER_RE = re.compile(
    r"Affichage\s+(\d+)\s+[àa]\s+(\d+)\s+de\s+(\d+)\s+entr",
    re.IGNORECASE,
)
_F2000_AMOUNT_RE = re.compile(r"^F?2000(?:FCFA)?$", re.IGNORECASE)
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def log(msg: str, level: str = "INFO") -> None:
    rpt.log(msg, level)


def backup_state() -> Path | None:
    if not STATE_FILE.is_file():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"state_before_payment_sync_{ts}.json"
    shutil.copy2(STATE_FILE, dest)
    log(f"   Backup state → {dest.name}", "OK")
    return dest


def open_payment_history_tab(driver) -> bool:
    """Onglet « Historique des paiements » sur view-profile."""
    if not rpt.is_on_partner_profile(driver):
        log("   [PAIEMENTS] Pas sur view-profile", "WARNING")
        return False
    selectors = (
        "//a[contains(., 'Historique des paiements')]",
        "//span[contains(., 'Historique des paiements')]",
        "//button[contains(., 'Historique des paiements')]",
        "//a[contains(., 'Payment History')]",
        "//a[contains(., 'Historique') and contains(., 'paiement')]",
    )
    for sel in selectors:
        try:
            for el in driver.find_elements(By.XPATH, sel):
                if not el.is_displayed():
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.3)
                driver.execute_script("arguments[0].click();", el)
                time.sleep(4)
                rpt.wait_for_tab_table(driver, timeout=30)
                log("   [PAIEMENTS] Onglet ouvert — pagination via boutons en bas (500 non fiable)", "OK")
                return True
        except Exception:
            continue
    log("   [PAIEMENTS] Onglet introuvable", "WARNING")
    return False


def _payment_pagination_root(driver):
    """Wrapper DataTables de l'onglet actif (table + footer pagination)."""
    pane = rpt._get_active_tab_pane(driver)
    candidates: list[Any] = []
    if pane:
        candidates.extend(pane.find_elements(By.CSS_SELECTOR, ".dataTables_wrapper"))
        candidates.append(pane)
    candidates.extend(driver.find_elements(By.CSS_SELECTOR, ".tab-pane.active.show .dataTables_wrapper"))
    candidates.extend(driver.find_elements(By.CSS_SELECTOR, ".tab-pane.active .dataTables_wrapper"))
    for root in candidates:
        try:
            if not root.is_displayed():
                continue
            if root.find_elements(By.CSS_SELECTOR, "ul.pagination, .dataTables_info"):
                return root
        except Exception:
            continue
    return pane or driver


def _payment_footer_info(driver) -> tuple[int, int, int] | None:
    """Lit « Affichage 31 à 42 de 42 entrées » dans l'onglet actif."""
    root = _payment_pagination_root(driver)
    try:
        for el in root.find_elements(
            By.CSS_SELECTOR,
            ".dataTables_info, .dataTables_wrapper .dataTables_info",
        ):
            m = _FOOTER_RE.search(el.text or "")
            if m:
                return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        pass
    m = _FOOTER_RE.search(root.text or "")
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def _payment_first_row_sig(driver) -> str | None:
    try:
        rows = rpt._get_active_table_rows(driver)
        return (rows[0].text or "")[:200] if rows else None
    except Exception:
        return None


def _payment_click_pagination(driver, el) -> bool:
    prev = _payment_first_row_sig(driver)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.3)
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)
    start = time.time()
    while time.time() - start < 15:
        time.sleep(0.5)
        new_sig = _payment_first_row_sig(driver)
        if new_sig != prev:
            time.sleep(1)
            return True
        info = _payment_footer_info(driver)
        if info and info[0] != info[1]:
            return True
    return False


def _payment_go_to_first_page(driver) -> None:
    root = _payment_pagination_root(driver)
    for el in root.find_elements(
        By.XPATH,
        ".//ul[contains(@class,'pagination')]//a[normalize-space(text())='1']",
    ):
        if not el.is_displayed():
            continue
        li = el.find_element(By.XPATH, "./..")
        if "active" in (li.get_attribute("class") or ""):
            return
        if _payment_click_pagination(driver, el):
            rpt.wait_for_tab_table(driver, timeout=20)
        return


def _payment_go_to_page_number(driver, page_num: int) -> bool:
    xpath = f".//ul[contains(@class,'pagination')]//a[normalize-space(text())='{page_num}']"
    roots = []
    for r in (_payment_pagination_root(driver), rpt._get_active_tab_pane(driver), driver):
        if r is not None and r not in roots:
            roots.append(r)
    for root in roots:
        for el in root.find_elements(By.XPATH, xpath):
            if not el.is_displayed():
                continue
            li = el.find_element(By.XPATH, "./..")
            if "active" in (li.get_attribute("class") or ""):
                return True
            if _payment_click_pagination(driver, el):
                return True
    return False


def _payment_go_next_page(driver) -> bool:
    """Page suivante — Suivant ou numéro de page actif + 1."""
    root = _payment_pagination_root(driver)
    for css in (
        "ul.pagination li.page-item.next:not(.disabled) a.page-link",
        "ul.pagination li.next:not(.disabled) a",
        ".pagination .next:not(.disabled) a",
    ):
        try:
            for el in root.find_elements(By.CSS_SELECTOR, css):
                if not el.is_displayed():
                    continue
                li = el.find_element(By.XPATH, "./..")
                if "disabled" in (li.get_attribute("class") or ""):
                    continue
                if _payment_click_pagination(driver, el):
                    return True
        except Exception:
            continue
    for el in root.find_elements(
        By.XPATH,
        ".//ul[contains(@class,'pagination')]//a[normalize-space(text())='Suivant']",
    ):
        if not el.is_displayed():
            continue
        li = el.find_element(By.XPATH, "./..")
        if "disabled" in (li.get_attribute("class") or ""):
            return False
        if _payment_click_pagination(driver, el):
            return True

    info = _payment_footer_info(driver)
    if info:
        if info[1] >= info[2]:
            return False
        for el in root.find_elements(
            By.CSS_SELECTOR,
            "ul.pagination li.page-item.active a, ul.pagination li.active a",
        ):
            try:
                cur = int((el.text or "").strip())
                return _payment_go_to_page_number(driver, cur + 1)
            except ValueError:
                pass
        return _payment_go_to_page_number(driver, 2)
    return False


def _extract_name_from_row(cells: list[str]) -> tuple[str, str]:
    """Retourne (nom_chauffeur, remarque_brute) ou ('', '')."""
    for cell in cells:
        m = REMARK_RE.search(cell or "")
        if m:
            return m.group(1).strip(), cell.strip()
    joined = " | ".join(cells)
    m = REMARK_RE.search(joined)
    if m:
        return m.group(1).strip(), joined
    return "", ""


def _is_f2000_amount(cells: list[str]) -> bool:
    """F2000 uniquement — exclut F200000 (dépôts crédit)."""
    for cell in cells:
        t = re.sub(r"[\s\u202f\xa0]", "", (cell or ""))
        if _F2000_AMOUNT_RE.match(t):
            return True
        tu = t.upper()
        if tu in ("2000", "F2000", "2000FCFA", "2000F"):
            return True
    return False


def parse_payment_page(driver) -> list[dict[str, str]]:
    rows_out: list[dict[str, str]] = []
    for row in rpt._get_active_table_rows(driver):
        if rpt._is_placeholder_row(row):
            continue
        try:
            cells = [(c.text or "").strip() for c in row.find_elements(By.TAG_NAME, "td")]
            if len(cells) < 2:
                continue
            name, remark = _extract_name_from_row(cells)
            if not name:
                continue
            if not _is_f2000_amount(cells):
                continue
            rows_out.append(
                {
                    "name": name,
                    "date": cells[0] if cells else "",
                    "remark": remark,
                    "row_text": " | ".join(cells),
                },
            )
        except Exception:
            continue
    return rows_out


def scrape_payment_history(driver) -> list[dict[str, str]]:
    all_rows: list[dict[str, str]] = []
    seen: set[str] = set()
    _payment_go_to_first_page(driver)
    rpt.wait_for_tab_table(driver, timeout=20)
    page = 1
    max_pages = 50

    while page <= max_pages:
        batch = parse_payment_page(driver)
        new_count = 0
        for item in batch:
            key = f"{normalize_name(item['name'])}|{item.get('date', '')}"
            if key in seen:
                continue
            seen.add(key)
            all_rows.append(item)
            new_count += 1

        info = _payment_footer_info(driver)
        if info:
            start, end, total = info
            log(
                f"   [PAIEMENTS] Page {page} : +{new_count} transfert(s) F2000 "
                f"(lignes {start}-{end}/{total}, cumul {len(all_rows)})",
            )
            if end >= total:
                break
        else:
            log(
                f"   [PAIEMENTS] Page {page} : +{new_count} transfert(s) F2000 "
                f"(cumul {len(all_rows)}, footer absent)",
            )
            if page > 1 and new_count == 0:
                break

        if not _payment_go_next_page(driver):
            advanced = False
            for next_num in range(page + 1, max_pages + 1):
                if _payment_go_to_page_number(driver, next_num):
                    advanced = True
                    break
            if not advanced:
                if info:
                    log(
                        f"   [PAIEMENTS] Pagination bloquée page {page} "
                        f"({info[1]}/{info[2]} entrées lues)",
                        "WARNING",
                    )
                break
        page += 1
        rpt.wait_for_tab_table(driver, timeout=20)
        time.sleep(1)

    log(f"   [PAIEMENTS] Fin pagination — {len(all_rows)} transfert(s) sur {page} page(s)")
    return all_rows


def find_driver_in_partner(
    drivers: list[dict[str, Any]],
    payment_name: str,
) -> dict[str, Any] | None:
    for d in drivers:
        if names_match(payment_name, str(d.get("name", ""))):
            return d
    return None


def parse_payment_date_iso(date_str: str, *, fallback_year: int | None = None) -> str:
    """Convertit « 29th May 01:58 PM » → ISO local."""
    raw = (date_str or "").strip()
    if not raw:
        return datetime.now().isoformat(timespec="seconds")
    m = _PAYMENT_DATE_RE.search(raw)
    if not m:
        return raw
    day = int(m.group(1))
    mon_key = m.group(2).lower()
    month = _MONTHS.get(mon_key) or _MONTHS.get(mon_key[:3])
    if not month:
        return raw
    hour = int(m.group(3))
    minute = int(m.group(4))
    ampm = (m.group(5) or "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    year = fallback_year or datetime.now().year
    try:
        return datetime(year, month, day, hour, minute).isoformat(timespec="seconds")
    except ValueError:
        return raw


def canonical_recharge_fields(
    pay: dict[str, str],
    *,
    partner_index: int,
    sync_at: str,
) -> dict[str, Any]:
    """Attributs state.json normalisés — preuve admin Historique des paiements."""
    remark = pay.get("remark", "")
    at = parse_payment_date_iso(pay.get("date", ""))
    return {
        "transfer_2000_done": True,
        "transfer_2000_at": at,
        "transfer_2000_source": MARK_SOURCE,
        "transfer_2000_remark": remark,
        "transfer_2000_rapport": f"Historique paiements P{partner_index:02d}",
        "transfer_2000_sync_at": sync_at,
    }


def _recharge_attrs_differ(drv: dict[str, Any], canonical: dict[str, Any]) -> bool:
    for key, val in canonical.items():
        if key == "transfer_2000_sync_at":
            continue
        if drv.get(key) != val:
            return True
    return False


def apply_recharge_to_driver(
    drv: dict[str, Any],
    pay: dict[str, str],
    *,
    partner_index: int,
    sync_at: str,
    dry_run: bool = False,
) -> str:
    """
    Marque ou corrige un chauffeur. Retourne : MARQUE | CORRIGE | OK
    """
    canonical = canonical_recharge_fields(pay, partner_index=partner_index, sync_at=sync_at)
    was_done = is_transfer_done(drv.get("transfer_2000_done"))

    if not was_done:
        if not dry_run:
            drv.update(canonical)
        return "MARQUE"

    if _recharge_attrs_differ(drv, canonical):
        if not dry_run:
            prev_source = drv.get("transfer_2000_source", "")
            drv.update(canonical)
            if prev_source and prev_source != MARK_SOURCE:
                drv["transfer_2000_previous_source"] = prev_source
        return "CORRIGE"

    return "OK"


def apply_payments_to_state(
    state: dict[str, Any],
    partner_index: int,
    payments: list[dict[str, str]],
    *,
    dry_run: bool = False,
    sync_at: str | None = None,
) -> dict[str, Any]:
    key = str(partner_index)
    partner = (state.get("partners") or {}).get(key)
    sync_ts = sync_at or datetime.now().isoformat(timespec="seconds")

    if not partner:
        return {
            "partner_index": partner_index,
            "payments_found": len(payments),
            "marked": 0,
            "corrected": 0,
            "already": 0,
            "absent": len(payments),
            "details": [
                {
                    "payment_name": p["name"],
                    "statut": "PARTNER_ABSENT",
                    "detail": f"Partenaire {partner_index} absent de state.json",
                }
                for p in payments
            ],
        }

    drivers = list(partner.get("drivers") or [])
    marked = corrected = already = absent = 0
    details: list[dict[str, Any]] = []
    payment_names_norm: set[str] = set()

    for pay in payments:
        pname = pay["name"]
        payment_names_norm.add(normalize_name(pname))
        drv = find_driver_in_partner(drivers, pname)
        entry: dict[str, Any] = {
            "payment_name": pname,
            "payment_date": pay.get("date", ""),
            "payment_date_iso": parse_payment_date_iso(pay.get("date", "")),
            "remark": pay.get("remark", ""),
        }
        if not drv:
            absent += 1
            entry["statut"] = "ABSENT_STATE"
            entry["detail"] = "Transfert admin trouvé mais chauffeur absent de state.json"
            details.append(entry)
            continue

        entry["state_name"] = drv.get("name", "")
        entry["phone"] = drv.get("phone", "")
        action = apply_recharge_to_driver(
            drv,
            pay,
            partner_index=partner_index,
            sync_at=sync_ts,
            dry_run=dry_run,
        )
        entry["statut"] = action if not dry_run or action != "MARQUE" else "MARQUE_DRY_RUN"
        if action == "MARQUE":
            marked += 1
            entry["detail"] = "Marqué rechargé (Historique paiements admin)"
        elif action == "CORRIGE":
            corrected += 1
            entry["detail"] = "Attributs recharge normalisés (source payment_history + date admin)"
        else:
            already += 1
            entry["detail"] = "Déjà conforme (payment_history)"
        if not dry_run and action in ("MARQUE", "CORRIGE"):
            entry["state_after"] = {
                k: drv.get(k)
                for k in (
                    "transfer_2000_done",
                    "transfer_2000_at",
                    "transfer_2000_source",
                    "transfer_2000_remark",
                    "transfer_2000_rapport",
                )
            }
        details.append(entry)

    return {
        "partner_index": partner_index,
        "partner_name": partner.get("name", ""),
        "payments_found": len(payments),
        "marked": marked,
        "corrected": corrected,
        "already": already,
        "absent": absent,
        "payment_names_norm": sorted(payment_names_norm),
        "details": details,
    }


def apply_report_to_state(
    report: dict[str, Any],
    state: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Croise un rapport payment_history_sync_*.json avec state.json."""
    sync_at = report.get("generated_at") or datetime.now().isoformat(timespec="seconds")
    summary_before = recharge_summary(state) if recharge_summary else {}
    partner_results: list[dict[str, Any]] = []

    for block in report.get("partners") or []:
        idx = int(block.get("partner_index") or 0)
        if not idx:
            continue
        payments = [
            {
                "name": d.get("payment_name", ""),
                "date": d.get("payment_date", ""),
                "remark": d.get("remark", ""),
            }
            for d in block.get("details") or []
            if d.get("payment_name") and d.get("statut") not in ("PARTNER_ABSENT",)
        ]
        if not payments and block.get("details"):
            payments = [
                {
                    "name": d.get("payment_name", ""),
                    "date": d.get("payment_date", ""),
                    "remark": d.get("remark", ""),
                }
                for d in block.get("details") or []
                if d.get("payment_name")
            ]
        partner_results.append(
            apply_payments_to_state(
                state,
                idx,
                payments,
                dry_run=dry_run,
                sync_at=sync_at,
            ),
        )

    summary_after = recharge_summary(state) if recharge_summary else {}
    if not dry_run:
        state.setdefault("meta", {})["last_payment_history_sync"] = sync_at
        state["meta"]["payment_history_sync_source"] = report.get("source", MARK_SOURCE)

    return {
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "report_generated_at": sync_at,
        "summary_before": summary_before,
        "summary_after": summary_after,
        "totals": {
            "partners": len(partner_results),
            "payments_found": sum(r.get("payments_found", 0) for r in partner_results),
            "marked": sum(r.get("marked", 0) for r in partner_results),
            "corrected": sum(r.get("corrected", 0) for r in partner_results),
            "already": sum(r.get("already", 0) for r in partner_results),
            "absent": sum(r.get("absent", 0) for r in partner_results),
        },
        "partners": partner_results,
    }


def _write_apply_summary(apply_result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(apply_result, f, ensure_ascii=False, indent=2)


def _log_apply_summary(apply_result: dict[str, Any]) -> None:
    t = apply_result.get("totals") or {}
    before = apply_result.get("summary_before") or {}
    after = apply_result.get("summary_after") or {}
    log("\nCROISEMENT RAPPORT ↔ state.json")
    log(f"   Marqués (nouveaux)     : {t.get('marked', 0)}")
    log(f"   Corrigés (attributs)   : {t.get('corrected', 0)}")
    log(f"   Déjà conformes         : {t.get('already', 0)}")
    log(f"   Absents du state       : {t.get('absent', 0)}")
    if before and after:
        log(
            f"   À recharger : {before.get('a_recharger', '?')} → {after.get('a_recharger', '?')} "
            f"| Rechargés : {before.get('recharges', '?')} → {after.get('recharges', '?')}",
        )


def sync_partner(
    driver,
    partner: dict[str, Any],
    state: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    idx = partner.get("index")
    log(f"\n{'='*50}")
    log(f"PARTENAIRE {idx} | {partner.get('email')} | {partner.get('name', '')}")
    log(f"{'='*50}")

    result: dict[str, Any] = {
        "partner_index": idx,
        "partner_email": partner.get("email", ""),
        "partner_name": partner.get("name", ""),
        "payments_found": 0,
        "marked": 0,
        "already": 0,
        "absent": 0,
        "errors": [],
        "details": [],
    }

    if not rpt.open_partner_profile_admin(driver, partner):
        result["errors"].append("partner_not_found")
        log(f"   Partenaire introuvable", "WARNING")
        return result

    rpt.capture_profile_meta(driver, partner)

    if not open_payment_history_tab(driver):
        result["errors"].append("payment_tab_failed")
        log("   Onglet Historique des paiements inaccessible", "WARNING")
        return result

    payments = scrape_payment_history(driver)
    result["payments_found"] = len(payments)
    log(f"   [PAIEMENTS] {len(payments)} transfert(s) F2000 « transfered-to-* »")

    apply = apply_payments_to_state(
        state,
        int(idx),
        payments,
        dry_run=dry_run,
        sync_at=datetime.now().isoformat(timespec="seconds"),
    )
    result.update(
        {
            "marked": apply["marked"],
            "corrected": apply.get("corrected", 0),
            "already": apply["already"],
            "absent": apply["absent"],
            "details": apply["details"],
        },
    )
    log(
        f"   Bilan P{idx:02d} : {apply['marked']} marqué(s), "
        f"{apply.get('corrected', 0)} corrigé(s), "
        f"{apply['already']} déjà OK, {apply['absent']} absent(s) du state",
    )
    return result


def run_apply_report(report_path: Path, *, dry_run: bool = False) -> int:
    if not report_path.is_file():
        log(f"Rapport introuvable: {report_path}", "ERROR")
        return 1
    if not STATE_FILE.is_file():
        log(f"state.json introuvable: {STATE_FILE}", "ERROR")
        return 1

    with report_path.open(encoding="utf-8") as f:
        report = json.load(f)

    state = load_state(STATE_FILE)
    if not dry_run:
        backup_state()

    apply_result = apply_report_to_state(report, state, dry_run=dry_run)

    if not dry_run:
        save_state(state, STATE_FILE)
        log(f"   state.json mis à jour → {STATE_FILE}", "OK")

    out = OUTPUT_DIR / f"payment_history_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    _write_apply_summary(apply_result, out)
    _log_apply_summary(apply_result)
    log(f"   Rapport croisement : {out}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync recharges depuis Historique des paiements admin → state.json",
    )
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=20)
    parser.add_argument("--only", type=int, help="Un seul partenaire (ex. 19)")
    parser.add_argument("--excel", default="", help="DOSSIER_PARTENAIRES.xlsx (optionnel)")
    parser.add_argument("--email-template", default=rpt.DEFAULT_EMAIL_TEMPLATE)
    parser.add_argument("--password", default=rpt.DEFAULT_PARTNER_PASSWORD)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--config", default=str(rpt.DEFAULT_CONFIG_JSON))
    parser.add_argument("--dry-run", action="store_true", help="Lecture seule — ne modifie pas state.json")
    parser.add_argument(
        "--apply-report",
        metavar="JSON",
        help="Croise un rapport payment_history_sync_*.json existant avec state.json (sans navigateur)",
    )
    args = parser.parse_args()

    if args.apply_report:
        sys.exit(run_apply_report(Path(args.apply_report), dry_run=args.dry_run))

    if args.only is not None:
        args.start = args.end = args.only

    partners = rpt.resolve_partners(args)
    if not partners:
        log("Aucun partenaire à traiter.", "ERROR")
        sys.exit(2)

    log("SYNC HISTORIQUE PAIEMENTS → state.json")
    log(f"   Partenaires: {len(partners)} | dry_run={args.dry_run}")

    if not STATE_FILE.is_file():
        log(f"state.json introuvable: {STATE_FILE}", "ERROR")
        sys.exit(1)

    state = load_state(STATE_FILE)
    summary_before = recharge_summary(state) if recharge_summary else {}
    if not args.dry_run:
        backup_state()

    driver = qa.setup_driver(headed=args.headed)
    all_results: list[dict[str, Any]] = []

    try:
        if not rpt.admin_login(driver):
            sys.exit(1)

        for slot, partner in enumerate(partners, start=1):
            log(f"\n── Campagne {slot}/{len(partners)} (n°{partner['index']}) ──")
            try:
                all_results.append(sync_partner(driver, partner, state, dry_run=args.dry_run))
            except Exception as e:
                log(f"Erreur partenaire {partner.get('index')}: {e}", "ERROR")
                all_results.append(
                    {
                        "partner_index": partner.get("index"),
                        "errors": [str(e)],
                        "marked": 0,
                        "already": 0,
                        "absent": 0,
                        "payments_found": 0,
                    },
                )

        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "dry_run": args.dry_run,
            "source": MARK_SOURCE,
            "totals": {
                "partners": len(all_results),
                "payments_found": sum(r.get("payments_found", 0) for r in all_results),
                "marked": sum(r.get("marked", 0) for r in all_results),
                "corrected": sum(r.get("corrected", 0) for r in all_results),
                "already": sum(r.get("already", 0) for r in all_results),
                "absent": sum(r.get("absent", 0) for r in all_results),
            },
            "partners": all_results,
        }
        report_path = OUTPUT_DIR / f"payment_history_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        t = report["totals"]
        log("\nBILAN SCRAPE")
        log(f"   Transferts F2000 lus : {t['payments_found']}")
        log(f"   Marqués rechargés   : {t['marked']}")
        log(f"   Attributs corrigés  : {t['corrected']}")
        log(f"   Déjà conformes      : {t['already']}")
        log(f"   Absents du state    : {t['absent']}")
        log(f"   Rapport scrape      : {report_path}")

        if not args.dry_run:
            state.setdefault("meta", {})["last_payment_history_sync"] = report["generated_at"]
            state["meta"]["payment_history_sync_source"] = MARK_SOURCE
            save_state(state, STATE_FILE)
            log(f"   state.json mis à jour → {STATE_FILE}", "OK")

        summary_after = recharge_summary(state) if recharge_summary else {}
        apply_path = OUTPUT_DIR / f"payment_history_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        apply_result = {
            "applied_at": datetime.now().isoformat(timespec="seconds"),
            "dry_run": args.dry_run,
            "report_generated_at": report["generated_at"],
            "summary_before": summary_before,
            "summary_after": summary_after,
            "totals": report["totals"],
            "partners": all_results,
        }
        _write_apply_summary(apply_result, apply_path)
        _log_apply_summary(apply_result)
        log(f"   Rapport croisement  : {apply_path}")

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
