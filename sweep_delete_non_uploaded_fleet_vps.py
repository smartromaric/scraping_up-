#!/usr/bin/env python3
"""
sweep_delete_non_uploaded_fleet_vps.py
======================================

Balayage manage-fleet : supprime les véhicules dont la carte grise n'est pas téléchargée.
Boucle comme sweep_approve_pending_documents_vps.py (scan haut du tableau, --until, --dry-run).

Workflow par véhicule :
  1. Scan top N lignes (récentes en haut)
  2. Candidat si « non téléchargé » dans le tableau OU flotte EN ATTENTE (+ accès doc)
  3. Ouvre la page document (URL directe, pas de clic sur ligne stale)
  4. Confirme statut carte grise = non téléchargé
  5. Ne supprime PAS si doc en attente d'approbation / déjà approuvé
  6. Menu ⋮ → Supprimer + confirmation SweetAlert (sauf --dry-run)

Usage:
  python sweep_delete_non_uploaded_fleet_vps.py --dry-run --headed
  python sweep_delete_non_uploaded_fleet_vps.py --dry-run --scan-top 30
  python sweep_delete_non_uploaded_fleet_vps.py --until 17:00 --fast
  python sweep_delete_non_uploaded_fleet_vps.py --window 00:00-06:00
  python sweep_delete_non_uploaded_fleet_vps.py --from 00:00 --until 06:00
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from datetime import datetime, timedelta

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import quick_approve_all_vehicle_vps as qa

RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
qa.LOG_FILE = qa.LOG_DIR / f"sweep_delete_non_uploaded_{RUN_TIMESTAMP}.log"

COL_PLAQUE_INDICES = (5, 4, 6)

_UNTIL_TIME_RE = re.compile(
    r"^(\d{1,2})(?:(?:h|H|:)(\d{2}))?\s*(?:h|H)?$",
    re.IGNORECASE,
)

END_ROUND_PAUSE_NORMAL = 6.0
END_ROUND_PAUSE_FAST = 3.0
_DEFAULT_SCAN_TOP = 30
_SCAN_TOP = _DEFAULT_SCAN_TOP

_NON_UPLOADED_ROW_HINTS = (
    "non téléchargé",
    "non telecharge",
    "not downloaded",
    "non telecharge",
)


def log(msg: str, level: str = "INFO") -> None:
    qa.log(msg, level, worker_id=0)


def _ui_delete_banner(driver, text: str, color: str = "#37474f") -> None:
    if not qa._ui_enabled(driver):
        return
    qa._ui_set_banner(driver, f"🗑️ SWEEP DELETE — {text}", color)


def _ui_mark_scan_results(driver, rows_info: list[dict]) -> None:
    """
    Surlignage tableau manage-fleet (--headed) :
      rouge foncé = candidat suppression | orange = EN ATTENTE (hors cible)
      vert = approuvé | bleu pâle = autre
    """
    if not qa._ui_enabled(driver):
        return
    qa._ui_clear_marks(driver)
    for item in rows_info:
        row = item.get("row")
        if row is None:
            continue
        if item.get("eligible"):
            qa._ui_highlight_row(driver, row, "delete")
        elif qa._is_row_en_attente(row):
            qa._ui_highlight_row(driver, row, "active")
        elif qa._is_row_approved(row):
            qa._ui_highlight_row(driver, row, "done")
        else:
            qa._ui_highlight_row(driver, row, "dim")


def _ui_on_document_page(driver, matricule: str, status: str, *, ok_delete: bool) -> None:
    if not qa._ui_enabled(driver):
        return
    color = "#b71c1c" if ok_delete else "#f57c00"
    _ui_delete_banner(
        driver,
        f"{matricule} | carte grise : {status}",
        color,
    )
    row = qa._find_carte_grise_row(driver)
    if row:
        qa._ui_highlight_row(driver, row, "delete" if ok_delete else "warn")


def parse_clock_parts(value: str) -> tuple[int, int]:
    raw = (value or "").strip().replace(" ", "")
    m = _UNTIL_TIME_RE.match(raw)
    if not m:
        raise ValueError(
            f"Heure invalide « {value} » — formats : 08:30, 15h15, 14, 16h (24h)"
        )
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"Heure hors plage 24h : {hour}:{minute:02d}")
    return hour, minute


def parse_until_time(value: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    hour, minute = parse_clock_parts(value)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def resolve_run_window(
    from_time: str, until_time: str, now: datetime | None = None
) -> tuple[datetime | None, datetime]:
    """
    Fenêtre planifiée (ex. 00:00 → 06:00).
    Retourne (début_attente, fin) ; début None = lancer tout de suite (déjà dans la fenêtre).
    """
    now = now or datetime.now()
    fh, fm = parse_clock_parts(from_time)
    uh, um = parse_clock_parts(until_time)
    start = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
    end = now.replace(hour=uh, minute=um, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    span = end - start

    if start <= now < start + span:
        return None, start + span
    if now < start:
        return start, start + span
    start_next = start + timedelta(days=1)
    while start_next + span <= now:
        start_next += timedelta(days=1)
    return start_next, start_next + span


def wait_until(target: datetime) -> None:
    log(f"   ⏳ Attente jusqu'à {format_deadline(target)} ({time_remaining(target)})…")
    while datetime.now() < target:
        remaining = int((target - datetime.now()).total_seconds())
        time.sleep(min(30, max(1, remaining)))
    log("   ▶ Fenêtre d'exécution — démarrage")


def format_deadline(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def time_remaining(deadline: datetime | None) -> str:
    if deadline is None:
        return "illimité"
    sec = max(0, int((deadline - datetime.now()).total_seconds()))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}min"
    if m:
        return f"{m}min {s:02d}s"
    return f"{s}s"


def should_stop(deadline: datetime | None) -> bool:
    return deadline is not None and datetime.now() >= deadline


def end_round_pause() -> float:
    return END_ROUND_PAUSE_FAST if qa._FAST_MODE else END_ROUND_PAUSE_NORMAL


def _looks_like_plate(text: str) -> bool:
    if not text or text.lower() in ("n/a", "na", "-", "?"):
        return False
    low = text.lower()
    if any(
        k in low
        for k in (
            "attente",
            "approuv",
            "rejet",
            "télécharg",
            "telecharg",
            "blocked",
            "campagne",
            "business",
        )
    ):
        return False
    norm = qa.normalize_plate(text)
    return bool(norm and 2 <= len(norm) <= 20)


def get_matricule_from_row(row) -> str:
    cells = qa._row_table_cells(row)
    for idx in COL_PLAQUE_INDICES:
        if len(cells) > idx:
            plate = (cells[idx].text or "").strip()
            if _looks_like_plate(plate):
                return plate
    try:
        for cell in cells:
            plate = (cell.text or "").strip()
            if _looks_like_plate(plate):
                return plate
    except Exception:
        pass
    return "?"


def row_has_document_access(row) -> bool:
    if qa.get_row_doc_uuid(row) or qa.get_row_doc_href(row):
        return True
    try:
        for sel in (
            "a[href*='document']",
            "svg[data-icon='file-alt']",
            ".document-icon",
        ):
            if row.find_elements(qa.By.CSS_SELECTOR, sel):
                return True
    except Exception:
        pass
    return False


def row_hints_non_uploaded(row) -> bool:
    try:
        low = (row.text or "").lower()
    except Exception:
        return False
    return any(h in low for h in _NON_UPLOADED_ROW_HINTS)


def build_doc_link(row) -> str:
    link = qa.get_row_doc_href(row)
    if link:
        return link
    doc_uuid = qa.get_row_doc_uuid(row)
    if doc_uuid:
        return f"{qa.BASE_URL}/manage-fleet/document/{doc_uuid}"
    return ""


def scan_fleet_rows(driver, max_rows: int | None = None) -> list[tuple[int, object]]:
    limit = _SCAN_TOP if max_rows is None else max_rows
    for attempt in range(1, 4):
        try:
            rows = driver.find_elements(qa.By.CSS_SELECTOR, "table tbody tr")
            subset = rows if not limit or limit <= 0 else rows[:limit]
            out = []
            for i, row in enumerate(subset):
                try:
                    txt = (row.text or "").strip()
                except Exception:
                    continue
                if not txt:
                    continue
                low = txt.lower()
                if "aucun" in low and "résultat" in low:
                    continue
                out.append((i, row))
            return out
        except Exception:
            time.sleep(0.35 * attempt)
    return []


def evaluate_row_for_delete(row) -> dict:
    """
    Pré-filtre tableau : accès document + (indice « non téléchargé » dans la ligne
    OU flotte EN ATTENTE). Le statut carte grise est confirmé sur la page document
    (le tableau n'affiche souvent pas « Non téléchargé » — ex. plaque 123456).
    """
    matricule = get_matricule_from_row(row)
    fleet = qa.get_fleet_status_from_row(row)
    if qa._is_row_approved(row) and not qa._is_row_en_attente(row):
        return {
            "eligible": False,
            "matricule": matricule,
            "fleet": fleet,
            "reasons": ["flotte_deja_approuvee"],
        }
    if not row_has_document_access(row):
        return {
            "eligible": False,
            "matricule": matricule,
            "fleet": fleet,
            "reasons": ["pas_acces_document"],
        }
    reasons: list[str] = []
    if row_hints_non_uploaded(row):
        reasons.append("non_telecharge_dans_ligne")
    if qa._is_row_en_attente(row):
        reasons.append("flotte_en_attente→verif_page_doc")
    if not reasons:
        return {
            "eligible": False,
            "matricule": matricule,
            "fleet": fleet,
            "reasons": ["pas_indice_suppression_dans_tableau"],
        }
    return {
        "eligible": True,
        "matricule": matricule,
        "fleet": fleet,
        "doc_uuid": qa.get_row_doc_uuid(row),
        "reasons": reasons,
    }


def collect_candidates(
    driver, *, debug: bool = False, visual: bool = False
) -> list[dict]:
    candidates = []
    seen: set[str] = set()
    rows_info: list[dict] = []
    scanned = scan_fleet_rows(driver)
    limit_label = f"top {_SCAN_TOP}" if _SCAN_TOP > 0 else "tout le tableau"

    for pos, (idx, row) in enumerate(scanned, 1):
        info = evaluate_row_for_delete(row)
        info["row"] = row
        info["index"] = idx
        info["pos"] = pos
        rows_info.append(info)

        if debug:
            mark = "🗑️" if info["eligible"] else ("⏳" if qa._is_row_en_attente(row) else "✅")
            log(
                f"      L{pos}: {mark} plaque={info['matricule']!r} | flotte={info['fleet']!r} | "
                f"eligible={info['eligible']} | {', '.join(info['reasons'])}"
            )
        if not info["eligible"]:
            continue
        matricule = info["matricule"]
        row_key = qa._row_identity_key(row, matricule)
        if row_key in seen:
            continue
        seen.add(row_key)
        candidates.append(
            {
                "row_index": idx,
                "matricule": matricule,
                "row_key": row_key,
                "partner": qa.get_partner_from_row(row) or "?",
                "fleet_status": info["fleet"],
                "doc_uuid": info.get("doc_uuid") or "",
                "reasons": info["reasons"],
            }
        )

    log(
        f"   Scan {limit_label} : {len(scanned)} l. | "
        f"candidats non téléchargé: {len(candidates)}"
    )
    for c in candidates:
        log(f"      → {c['matricule']} | {c['fleet_status']} | {c['partner']}")

    if visual:
        _ui_mark_scan_results(driver, rows_info)
        _ui_delete_banner(
            driver,
            f"{len(candidates)} candidat(s) | 🔴=à supprimer 🟠=EN ATTENTE 🟢=OK",
            "#455a64",
        )

    return candidates


def navigate_manage_fleet_full(driver) -> bool:
    if not qa._safe_get(driver, qa.MANAGE_FLEET_URL):
        return False
    qa._wait_table(driver, 10)
    qa.set_pagination_max(driver, 0)
    qa._wait_table(driver, 5)
    return True


def open_document_safe(driver, doc_link: str) -> bool:
    if not doc_link:
        return False
    if not qa.open_document_page(driver, None, 0, doc_link=doc_link):
        return False
    return True


def confirm_non_uploaded_on_doc_page(
    driver, matricule: str = "?"
) -> tuple[bool, str]:
    """Vérifie le statut carte grise sur la page document."""
    qa._wait_table(driver, 6, 0)
    row = qa._find_carte_grise_row(driver)
    if not row:
        return False, "ligne_carte_grise_introuvable"
    status = qa._doc_status_from_row(row)
    log(f"   Statut document (carte grise): {status!r}")
    sl = status.lower()
    if "approuvé" in sl or "approved" in sl:
        _ui_on_document_page(driver, matricule, status, ok_delete=False)
        return False, "deja_approuve_skip"
    if qa._needs_document_approve_click(status):
        _ui_on_document_page(driver, matricule, status, ok_delete=False)
        return False, "en_attente_approbation_skip"
    if qa._needs_document_upload(status):
        _ui_on_document_page(driver, matricule, status, ok_delete=True)
        return True, "non_telecharge_confirme"
    _ui_on_document_page(driver, matricule, status, ok_delete=False)
    return False, f"statut_non_supprimable:{status}"


def _close_swal(driver) -> None:
    try:
        driver.execute_script(
            "var b=document.querySelector('.swal2-cancel'); if(b) b.click();"
        )
    except Exception:
        pass


def delete_row_at_index(driver, row_index: int) -> bool:
    """Menu ⋮ → Supprimer + SweetAlert (pattern create_missing_driver_vehicle_assoc)."""
    try:
        result = driver.execute_script(
            """
            var rows = document.querySelectorAll('table tbody tr');
            if (!rows || arguments[0] >= rows.length) return 'no_row';
            var row = rows[arguments[0]];
            var btn = row.querySelector(
                'button.dropdown-toggle, button[data-bs-toggle="dropdown"], .btn-action, button'
            );
            if (!btn) return 'no_btn';
            if (window.bootstrap && window.bootstrap.Dropdown) {
                var dd = window.bootstrap.Dropdown.getOrCreateInstance(btn);
                dd.show();
            } else { btn.click(); }
            return new Promise(function(resolve) {
                setTimeout(function() {
                    var menu = row.querySelector('.dropdown-menu.show, .dropdown-menu');
                    if (!menu) { resolve('no_menu'); return; }
                    var items = menu.querySelectorAll('a, button, li');
                    for (var i = 0; i < items.length; i++) {
                        var t = items[i].textContent.trim().toLowerCase();
                        if (t.includes('supprimer') || t.includes('delete') || t.includes('retirer')) {
                            items[i].click();
                            resolve('clicked');
                            return;
                        }
                    }
                    resolve('no_supprimer');
                }, 400);
            });
            """,
            row_index,
        )
        if result != "clicked":
            log(f"   Suppression [{row_index}] : {result}", "WARNING")
            return False

        confirm = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
        )
        qa._pause(0.2, 0.05)
        driver.execute_script("arguments[0].click();", confirm)

        qa._pause(0.5, 0.1)
        try:
            ok = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-popup .swal2-confirm"))
            )
            driver.execute_script("arguments[0].click();", ok)
        except TimeoutException:
            pass

        end = time.time() + 4
        while time.time() < end:
            if not driver.find_elements(By.CSS_SELECTOR, ".swal2-container .swal2-popup"):
                break
            qa._pause(0.2, 0.05)

        return True
    except Exception as e:
        log(f"   Erreur suppression: {e}", "ERROR")
        _close_swal(driver)
        return False


def find_row_index_by_key(driver, matricule: str, row_key: str, max_rows: int | None = None) -> int | None:
    for idx, row in scan_fleet_rows(driver, max_rows=max_rows):
        try:
            if qa._row_identity_key(row, matricule) == row_key:
                return idx
        except Exception:
            continue
    return None


def sweep_delete_one(driver, candidate: dict, *, dry_run: bool, show_ui: bool = False) -> dict:
    matricule = candidate["matricule"]
    row_index = candidate["row_index"]
    partner = candidate["partner"]

    result = {
        "matricule": matricule,
        "success": False,
        "deleted": False,
        "skipped": False,
        "skip_reason": "",
        "errors": [],
    }

    log(f"   Cible: {matricule} | {partner} | flotte={candidate['fleet_status']}")

    doc_link = ""
    if not navigate_manage_fleet_full(driver):
        result["errors"].append("navigation")
        return result

    idx = find_row_index_by_key(driver, matricule, candidate["row_key"])
    if idx is not None:
        row_index = idx

    rows = scan_fleet_rows(driver)
    row = None
    for i, r in rows:
        if i == row_index:
            row = r
            break

    if show_ui and row is not None:
        qa._ui_clear_marks(driver)
        qa._ui_highlight_row(driver, row, "delete")
        _ui_delete_banner(
            driver,
            f"{matricule} — {partner} | vérif. document…",
            "#b71c1c",
        )

    doc_link = build_doc_link(row) if row else ""
    if not doc_link:
        doc_uuid = candidate.get("doc_uuid")
        if doc_uuid:
            doc_link = f"{qa.BASE_URL}/manage-fleet/document/{doc_uuid}"

    if not doc_link:
        result["errors"].append("pas_lien_document")
        return result

    log("   Ouverture page document (URL directe)…")
    if not open_document_safe(driver, doc_link):
        result["errors"].append("ouverture_document")
        return result

    ok, reason = confirm_non_uploaded_on_doc_page(driver, matricule)
    if not ok:
        result["skipped"] = True
        result["skip_reason"] = reason
        log(f"   SKIP — {reason}", "WARNING")
        if show_ui:
            _ui_delete_banner(driver, f"SKIP {matricule} — {reason}", "#f57c00")
        return result

    log("   Confirmé : carte grise non téléchargée")
    if dry_run:
        log(f"   [DRY-RUN] Serait supprimé : {matricule} (ligne index {row_index})", "WARNING")
        if show_ui:
            _ui_delete_banner(
                driver,
                f"[DRY-RUN] Serait supprimé : {matricule}",
                "#7b1fa2",
            )
            if row is not None:
                qa._ui_highlight_row(driver, row, "dryrun")
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "dry_run"
        return result

    if not navigate_manage_fleet_full(driver):
        result["errors"].append("retour_tableau")
        return result

    idx = find_row_index_by_key(driver, matricule, candidate["row_key"])
    if idx is None:
        idx = row_index

    if show_ui:
        rows2 = scan_fleet_rows(driver)
        for i, r in rows2:
            if i == idx:
                qa._ui_highlight_row(driver, r, "delete")
                break
        _ui_delete_banner(driver, f"Suppression en cours : {matricule}…", "#b71c1c")

    if delete_row_at_index(driver, idx):
        log(f"   Supprimé : {matricule}")
        if show_ui:
            _ui_delete_banner(driver, f"✅ Supprimé : {matricule}", "#2e7d32")
        result["success"] = True
        result["deleted"] = True
    else:
        if show_ui:
            _ui_delete_banner(driver, f"❌ Échec suppression : {matricule}", "#c62828")
        result["errors"].append("echec_suppression")

    return result


def refresh_end_of_round(driver, label: str) -> None:
    log(f"   Refresh ({label}) — pause {end_round_pause():.0f}s…")
    navigate_manage_fleet_full(driver)
    qa._pause(end_round_pause(), 1.0)


def run_sweep(
    driver,
    *,
    max_per_round: int,
    max_empty_rounds: int,
    max_rounds: int,
    deadline: datetime | None,
    dry_run: bool,
    debug_scan: bool,
    show_ui: bool = False,
) -> dict:
    stats = {
        "rounds": 0,
        "processed": 0,
        "deleted": 0,
        "skipped": 0,
        "failed": 0,
        "stop_reason": "",
    }
    empty_rounds = 0
    round_num = 0

    if not navigate_manage_fleet_full(driver):
        stats["stop_reason"] = "navigation_initiale"
        return stats

    mode = "DRY-RUN (aucune suppression)" if dry_run else "SUPPRESSION ACTIVE"
    log(f"   Mode : {mode} | scan top {_SCAN_TOP if _SCAN_TOP > 0 else 'tout'}")
    if show_ui:
        qa._ui_enable(driver, True)
        banner_color = "#7b1fa2" if dry_run else "#b71c1c"
        _ui_delete_banner(
            driver,
            f"{mode} | tour 0 — scan en cours…",
            banner_color,
        )

    while round_num < max_rounds:
        if should_stop(deadline):
            stats["stop_reason"] = f"heure_limite ({format_deadline(deadline)})"
            break

        round_num += 1
        stats["rounds"] = round_num
        log(f"\n{'─'*50}")
        log(f"TOUR {round_num} — reste {time_remaining(deadline)}")
        log(f"{'─'*50}")

        candidates = collect_candidates(
            driver, debug=debug_scan, visual=show_ui
        )
        if not candidates:
            empty_rounds += 1
            if show_ui:
                _ui_delete_banner(
                    driver,
                    f"Aucun candidat (tour {empty_rounds}) — rescan…",
                    "#546e7a",
                )
            if deadline:
                log(f"   Aucun candidat (tour {empty_rounds}) — rescan…")
                refresh_end_of_round(driver, "attente")
                continue
            if empty_rounds >= max_empty_rounds:
                stats["stop_reason"] = "file_vide"
                break
            refresh_end_of_round(driver, "vide")
            continue

        empty_rounds = 0
        batch = candidates[:max_per_round]
        deleted_round = 0

        for n, cand in enumerate(batch, 1):
            if should_stop(deadline):
                stats["stop_reason"] = f"heure_limite ({format_deadline(deadline)})"
                break
            log(f"\n   [{n}/{len(batch)}] {cand['matricule']} — {cand['partner']}")
            try:
                res = sweep_delete_one(
                    driver, cand, dry_run=dry_run, show_ui=show_ui
                )
            except Exception as e:
                log(f"   Erreur: {e}", "ERROR")
                stats["failed"] += 1
                stats["processed"] += 1
                continue

            stats["processed"] += 1
            if res.get("deleted"):
                stats["deleted"] += 1
                deleted_round += 1
            elif res.get("skipped"):
                stats["skipped"] += 1
            elif res.get("success"):
                stats["skipped"] += 1
            else:
                stats["failed"] += 1

        if should_stop(deadline):
            break
        refresh_end_of_round(driver, f"tour {round_num}")

    if not stats["stop_reason"]:
        stats["stop_reason"] = "max_rounds" if round_num >= max_rounds else "arret"
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Supprime les véhicules dont la carte grise n'est pas téléchargée",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simule sans supprimer (recommandé pour le 1er test)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Navigateur visible + bannière et surlignage couleur des lignes",
    )
    parser.add_argument("--fast", action="store_true", help="Pauses réduites")
    parser.add_argument("--debug-scan", action="store_true", help="Log chaque ligne scannée")
    parser.add_argument(
        "--from",
        "--since",
        dest="from_time",
        metavar="HEURE",
        help="Attendre cette heure avant de démarrer (24h : 00:00, 6h)",
    )
    parser.add_argument(
        "--until",
        metavar="HEURE",
        help="Arrêt à cette heure (24h). Avec --from : fin de fenêtre (ex. 06:00)",
    )
    parser.add_argument(
        "--window",
        metavar="DEBUT-FIN",
        help="Fenêtre planifiée : ex. 00:00-06:00 (équivalent --from 00:00 --until 06:00)",
    )
    parser.add_argument("--scan-top", type=int, default=_DEFAULT_SCAN_TOP, metavar="N")
    parser.add_argument("--scan-all", action="store_true", help="Scanner tout le tableau")
    parser.add_argument("--max-per-round", type=int, default=10)
    parser.add_argument("--max-empty-rounds", type=int, default=3)
    parser.add_argument("--max-rounds", type=int, default=500)

    args = parser.parse_args()
    qa._FAST_MODE = bool(args.fast)
    global _SCAN_TOP
    _SCAN_TOP = 0 if args.scan_all else max(0, args.scan_top)

    from_time = (args.from_time or "").strip() or None
    until_time = (args.until or "").strip() or None
    if args.window:
        parts = re.split(r"[-–—]+", args.window.strip(), maxsplit=1)
        if len(parts) != 2:
            log(f"Fenêtre invalide « {args.window} » — ex. 00:00-06:00", "ERROR")
            sys.exit(2)
        from_time, until_time = parts[0].strip(), parts[1].strip()

    start_at: datetime | None = None
    deadline: datetime | None = None
    try:
        if from_time and until_time:
            start_at, deadline = resolve_run_window(from_time, until_time)
        elif from_time:
            start_at = parse_until_time(from_time)
            if datetime.now() >= start_at:
                start_at = None
        elif until_time:
            deadline = parse_until_time(until_time)
    except ValueError as e:
        log(str(e), "ERROR")
        sys.exit(2)

    log(f"\n{'='*60}")
    log("SWEEP DELETE — véhicules non téléchargés")
    if args.dry_run:
        log("   *** MODE DRY-RUN — aucune suppression réelle ***")
    else:
        log("   *** SUPPRESSION RÉELLE — irréversible ***", "WARNING")
    log(f"   Max {args.max_per_round}/tour | scan top {_SCAN_TOP if _SCAN_TOP > 0 else 'tout'}")
    if start_at:
        log(f"   Début planifié : {format_deadline(start_at)}")
    if deadline:
        log(f"   Fin planifiée : {format_deadline(deadline)}")
    if not start_at and not deadline:
        log("   Durée : illimitée (Ctrl+C) ou arrêt après tours vides sans --until")
    log(f"{'='*60}")

    if start_at and datetime.now() < start_at:
        wait_until(start_at)

    driver = None
    stats: dict = {}
    debug_port = None if args.headed else qa.BASE_DEBUG_PORT
    try:
        driver = qa.setup_driver(headed=args.headed, debug_port=debug_port, wid=0)
        if not qa.admin_login(driver, 0):
            sys.exit(1)
        stats = run_sweep(
            driver,
            max_per_round=max(1, args.max_per_round),
            max_empty_rounds=max(1, args.max_empty_rounds),
            max_rounds=max(1, args.max_rounds),
            deadline=deadline,
            dry_run=bool(args.dry_run),
            debug_scan=bool(args.debug_scan),
            show_ui=bool(args.headed),
        )
    except KeyboardInterrupt:
        log("Interruption Ctrl+C", "WARNING")
        stats["stop_reason"] = "ctrl_c"
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    log(f"\nRÉSUMÉ — tours {stats.get('rounds', 0)} | traités {stats.get('processed', 0)}")
    log(f"   Supprimés : {stats.get('deleted', 0)} | skip : {stats.get('skipped', 0)} | échecs : {stats.get('failed', 0)}")
    log(f"   Arrêt : {stats.get('stop_reason', '—')}")
    log(f"   Log : {qa.LOG_FILE}")
    sys.exit(0 if stats.get("failed", 0) == 0 else 1)


if __name__ == "__main__":
    main()
