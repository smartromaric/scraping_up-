#!/usr/bin/env python3
"""
sweep_approve_pending_documents_vps.py
======================================

Balayage manage-fleet : lignes flotte « EN ATTENTE » → ouvre la page document
→ vérifie le statut carte grise (comme quick_approve_all_vehicle_vps.py)
→ approuve si « en attente d'approbation » → puis menu ⋮ flotte. Pas d'upload.

Arrêt :
  - Ctrl+C (interruption manuelle)
  - --until HH ou HH:MM (format 24h)
  - --max-rounds N (optionnel : plafond de tours ; sans ce flag, boucle sans limite de tours)
  - Crash / perte de session Chrome : redémarrage automatique du navigateur (re-login) ; le script
    ne s'arrête pas pour ça tant que tu n'as pas Ctrl+C / --until / --max-rounds atteint

Usage:
  python sweep_approve_pending_documents_vps.py
  python sweep_approve_pending_documents_vps.py --fast --headed
  python sweep_approve_pending_documents_vps.py --until 08:30
  python sweep_approve_pending_documents_vps.py --until 14 --max-per-round 30
  python sweep_approve_pending_documents_vps.py --until 16h --fast

Mode par défaut : file dynamique — mini-scan après chaque cas ; nouveaux en tête,
SKIP « non téléchargé » en fin de file (retry). Succès = clos session. --batch-mode : tour figé.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timedelta

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    WebDriverException,
)

import quick_approve_all_vehicle_vps as qa

# ─── Journal dédié (avant tout log) ───────────────────────────────────────────

RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
qa.LOG_FILE = qa.LOG_DIR / f"sweep_approve_pending_{RUN_TIMESTAMP}.log"

# Indices plaque selon layout tableau (doc entre partenaire et plaque → souvent 5)
COL_PLAQUE_INDICES = (5, 4, 6)

_UNTIL_TIME_RE = re.compile(
    r"^(\d{1,2})(?:(?:h|H|:)(\d{2}))?\s*(?:h|H)?$",
    re.IGNORECASE,
)

# Pauses fin de tour (secondes)
END_ROUND_PAUSE_NORMAL = 6.0
END_ROUND_PAUSE_FAST = 3.0
FIRST_EMPTY_SCAN_PAUSE_NORMAL = 4.0
FIRST_EMPTY_SCAN_PAUSE_FAST = 2.0

# Après pagination / filtres : laisser charger texte + badges avant scan
SWEEP_TABLE_SETTLE_NORMAL = 6.0
SWEEP_TABLE_SETTLE_FAST = 10.0
_TABLE_SETTLE_OVERRIDE: float | None = None

# Après perte de session Chrome / WebDriver (crash renderer, mémoire, etc.)
BROWSER_RESTART_PAUSE_SEC = 5.0

# Nombre de lignes lues en haut du tableau (les plus récentes sont en premier)
_DEFAULT_SCAN_TOP = 30
_SCAN_TOP = _DEFAULT_SCAN_TOP

# Mini-scan après chaque cas (file dynamique — nouveaux en tête)
_DEFAULT_RESCAN_TOP = 10
_RESCAN_TOP = _DEFAULT_RESCAN_TOP


def log(msg: str, level: str = "INFO") -> None:
    qa.log(msg, level, worker_id=0)


def parse_until_time(value: str, now: datetime | None = None) -> datetime:
    """
    Heure limite en 24h : 08:30, 8:30, 15h15, 14, 16h.
    Si l'heure est déjà passée aujourd'hui → lendemain à cette heure.
    """
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
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def format_deadline(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def is_browser_session_dead(exc: BaseException) -> bool:
    """Chrome / WebDriver coupé alors que le script continue (souvent OOM ou crash renderer)."""
    if isinstance(exc, (InvalidSessionIdException, NoSuchWindowException)):
        return True
    if isinstance(exc, WebDriverException):
        msg = str(exc).lower()
        return any(
            k in msg
            for k in (
                "invalid session id",
                "session deleted",
                "chrome not reachable",
                "disconnected",
                "not connected to devtools",
                "target window already closed",
                "unable to receive message from renderer",
            )
        )
    return False


def merge_sweep_stats(dst: dict, src: dict) -> None:
    """Cumule les compteurs d'une session `run_sweep` dans les totaux globaux."""
    for k in ("rounds", "processed", "success", "skipped", "failed"):
        dst[k] = dst.get(k, 0) + int(src.get(k, 0) or 0)


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


def empty_scan_pause() -> float:
    return FIRST_EMPTY_SCAN_PAUSE_FAST if qa._FAST_MODE else FIRST_EMPTY_SCAN_PAUSE_NORMAL


def table_settle_pause() -> float:
    if _TABLE_SETTLE_OVERRIDE is not None:
        return max(0.0, _TABLE_SETTLE_OVERRIDE)
    return SWEEP_TABLE_SETTLE_FAST if qa._FAST_MODE else SWEEP_TABLE_SETTLE_NORMAL


def prepare_table_for_scan(driver, *, reason: str = "") -> None:
    """Attente après stabilisation du nombre de lignes (contenu cellules souvent en retard)."""
    secs = table_settle_pause()
    if secs <= 0:
        return
    suffix = f" ({reason})" if reason else ""
    log(f"   ⏳ Consolidation tableau avant scan{suffix} — {secs:.0f}s…")
    qa._pause(secs, 2.0)
    qa._wait_table(driver, 6)


def get_matricule_from_row(row) -> str:
    """Plaque : colonnes connues puis heuristique (évite confondre partenaire / statut)."""
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
            "blocked",
            "campagne",
            "business",
            "moto",
            "eco",
            "confort",
        )
    ):
        return False
    norm = qa.normalize_plate(text)
    return bool(norm and 2 <= len(norm) <= 20)


def row_has_document_access(row) -> bool:
    """Lien document ou icône cliquable sur la ligne."""
    if qa.get_row_doc_uuid(row) or qa.get_row_doc_href(row):
        return True
    try:
        for sel in (
            "a[href*='document']",
            "svg[data-icon='file-alt']",
            ".document-icon",
            "[class*='document']",
            "button[class*='doc']",
        ):
            if row.find_elements(qa.By.CSS_SELECTOR, sel):
                return True
    except Exception:
        pass
    return False


def evaluate_row(row) -> dict:
    """
    Tableau : sélectionne uniquement la flotte « EN ATTENTE » (+ accès document).
    Le statut document « en attente d'approbation » est vérifié sur la page document
    (même logique que quick_approve_all_vehicle_vps.py).
    """
    if qa._is_row_approved(row):
        return {
            "eligible": False,
            "matricule": "—",
            "fleet": "Approuvé",
            "has_doc": False,
            "doc_uuid": "",
            "reasons": ["flotte_deja_approuvee"],
        }

    matricule = get_matricule_from_row(row)
    fleet = qa.get_fleet_status_from_row(row)
    has_doc = row_has_document_access(row)
    doc_uuid = qa.get_row_doc_uuid(row)
    reasons: list[str] = []
    try:
        low = (row.text or "").lower()
    except Exception:
        low = ""

    if any(k in low for k in ("non téléchargé", "non telecharge", "not downloaded")):
        reasons.append("non_telecharge_dans_ligne")

    if not has_doc:
        reasons.append("pas_acces_document")

    eligible = False
    if has_doc and not qa._is_row_approved(row):
        if qa._fleet_row_document_approved(row):
            eligible = True
            reasons.append("doc_ok_flotte_attente→menu_flotte")
        elif qa._is_row_en_attente(row):
            eligible = True
            reasons.append("flotte_en_attente→ouvrir_page_doc")

    return {
        "eligible": eligible,
        "matricule": matricule,
        "fleet": fleet,
        "has_doc": has_doc,
        "doc_uuid": doc_uuid,
        "reasons": reasons or ["aucun_critere"],
    }


def _ui_sweep_banner(driver, text: str, color: str = "#1565c0") -> None:
    """Bannière haute (réutilise le mécanisme quick_approve si UI active)."""
    if not qa._ui_enabled(driver):
        return
    qa._ui_set_banner(driver, f"🔄 SWEEP — {text}", color)


def _ui_mark_scan_results(driver, rows_info: list[dict]) -> None:
    """Marqueurs : rouge = candidat, orange = EN ATTENTE non éligible, vert = approuvé, bleu = autre."""
    if not qa._ui_enabled(driver):
        return
    qa._ui_clear_marks(driver)
    for item in rows_info:
        row = item.get("row")
        if row is None:
            continue
        if item.get("eligible"):
            qa._ui_highlight_row(driver, row, "pending")
        elif qa._is_row_approved(row):
            qa._ui_highlight_row(driver, row, "done")
        elif qa._is_row_en_attente(row):
            qa._ui_highlight_row(driver, row, "active")
        else:
            qa._ui_highlight_row(driver, row, "dim")


def _parse_table_rows(rows, max_rows: int | None) -> list[tuple[int, object]]:
    """Extrait les lignes valides du tbody (optionnellement limité au haut du tableau)."""
    out: list[tuple[int, object]] = []
    subset = rows if max_rows is None or max_rows <= 0 else rows[:max_rows]
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


def scan_fleet_rows(
    driver,
    max_rows: int | None = None,
    *,
    skip_settle: bool = False,
) -> list[tuple[int, object]]:
    """
    Lignes du tableau manage-fleet.
    max_rows=None ou <=0 : tout le tbody visible (lent).
    Sinon : seulement les N premières lignes (récentes en haut).
    """
    if not skip_settle:
        prepare_table_for_scan(driver)

    limit = _SCAN_TOP if max_rows is None else max_rows
    retry_pause = max(4.0, table_settle_pause() * 0.45)

    for attempt in range(1, 5):
        try:
            rows = driver.find_elements(qa.By.CSS_SELECTOR, "table tbody tr")
            parsed = _parse_table_rows(rows, limit)
            if parsed:
                return parsed
            if rows and attempt < 4:
                log(
                    f"   ⏳ {len(rows)} ligne(s) DOM mais contenu pas prêt "
                    f"— nouvelle attente ({attempt}/3)…",
                    "WARNING",
                )
                qa._pause(retry_pause, 1.0)
                continue
            return parsed
        except Exception:
            time.sleep(0.35 * attempt)
    return []


def scan_all_fleet_rows(driver) -> list[tuple[int, object]]:
    """Alias — scan complet (éviter en mode temps réel)."""
    return scan_fleet_rows(driver, max_rows=0)


def doc_link_from_candidate(candidate: dict) -> str:
    """URL document enregistrée au scan — ne pas relire une ligne périmée (stale)."""
    link = (candidate.get("doc_link") or "").strip()
    if link:
        return link if link.startswith("http") else f"{qa.BASE_URL}/{link.lstrip('/')}"
    doc_uuid = (candidate.get("doc_uuid") or "").strip()
    if doc_uuid:
        return f"{qa.BASE_URL}/manage-fleet/document/{doc_uuid}"
    return ""


def _pick_row_among_matches(
    matching: list,
    matricule: str,
    row_key: str,
    partner: str,
    neighbor_before: str = "",
) -> object | None:
    """Choisit la bonne ligne parmi doublons (même plaque, docs différents)."""
    if not matching:
        return None

    exact: list = []
    partner_hits: list = []
    for _, row in matching:
        try:
            if qa._row_identity_key(row, matricule) == row_key:
                exact.append(row)
        except Exception:
            continue

    if len(exact) == 1:
        log("   ✓ Ligne identifiée (UUID document)")
        return exact[0]
    if len(exact) > 1:
        log(f"   ⚠️ {len(exact)} lignes même clé doc — première retenue", "WARNING")
        return exact[0]

    partner_norm = partner.strip().lower()
    if partner_norm:
        for _, row in matching:
            try:
                p = (qa.get_partner_from_row(row) or "").strip().lower()
                if p == partner_norm or partner_norm in p or p in partner_norm:
                    partner_hits.append(row)
            except Exception:
                continue
        if len(partner_hits) == 1:
            log(f"   ✓ Ligne identifiée (partenaire {partner})")
            return partner_hits[0]

    if neighbor_before:
        nb = qa.normalize_plate(neighbor_before)
        for pos, (_, row) in enumerate(matching):
            if pos == 0:
                continue
            try:
                prev = matching[pos - 1][1]
                prev_plate = qa.normalize_plate(get_matricule_from_row(prev))
                if prev_plate == nb:
                    log(f"   ✓ Ligne identifiée (voisin au-dessus: {neighbor_before})")
                    return row
            except Exception:
                continue

    if len(matching) == 1:
        log("   ✓ Une seule ligne pour ce matricule")
        return matching[0][1]

    log(
        f"   ⚠️ {len(matching)} lignes pour {matricule} — impossible de départager",
        "WARNING",
    )
    return None


def find_row_for_candidate(
    driver,
    candidate: dict,
    *,
    wait_timeout: float = 14,
    use_matricule_filter: bool = True,
) -> object | None:
    """
    Retrouve la ligne après rechargement / nouveaux véhicules en tête du tableau.
    Priorité : clé doc UUID → filtre matricule → scan haut du tableau.
    """
    matricule = candidate["matricule"]
    row_key = candidate["row_key"]
    partner = (candidate.get("partner") or "").strip()
    neighbor = (candidate.get("neighbor_before") or "").strip()

    row = qa.find_vehicle_row_by_key(driver, matricule, row_key, 0, wait_timeout=wait_timeout)
    if row:
        log("   ✓ Ligne retrouvée dans le tableau (clé)")
        return row

    if use_matricule_filter:
        filter_applied = False
        picked_via_filter = None
        try:
            if qa.open_filters(driver, 0) and qa.set_filter_matricule(driver, matricule, 0):
                if qa.apply_filters(driver, 0, wait_matricule=matricule):
                    filter_applied = True
                    matching = qa.find_vehicle_rows(driver, matricule, 0, wait_timeout=8)
                    picked_via_filter = _pick_row_among_matches(
                        matching, matricule, row_key, partner, neighbor,
                    )
                    if picked_via_filter:
                        log("   ✓ Ligne retrouvée après filtre matricule")
        except Exception as e:
            log(f"   ⚠️ Recherche filtre matricule: {e}", "WARNING")
        else:
            if picked_via_filter:
                return picked_via_filter
        if filter_applied:
            log("   ↪ Fin filtre matricule — rechargement tableau complet…")
            release_matricule_filter(driver)

    end = time.time() + wait_timeout
    while time.time() < end:
        matching = [
            (i, r)
            for i, r in scan_fleet_rows(driver, max_rows=_SCAN_TOP, skip_settle=True)
        ]
        picked = _pick_row_among_matches(matching, matricule, row_key, partner, neighbor)
        if picked:
            return picked
        qa._pause(0.45, 0.1)

    return None


def _candidate_dict_from_row(
    row,
    *,
    index: int,
    pos: int,
    neighbor_before: str,
    info: dict,
) -> dict:
    matricule = info["matricule"]
    row_key = qa._row_identity_key(row, matricule)
    doc_uuid = info["doc_uuid"]
    return {
        "index": index,
        "matricule": matricule,
        "row_key": row_key,
        "partner": qa.get_partner_from_row(row) or "?",
        "fleet_status": info["fleet"],
        "doc_uuid": doc_uuid,
        "doc_link": doc_link_from_candidate({"doc_uuid": doc_uuid}),
        "scan_pos": pos,
        "neighbor_before": neighbor_before,
        "reasons": info["reasons"],
    }


def discover_candidates(
    driver,
    *,
    scan_top: int,
    processed_keys: set[str],
    queue_keys: set[str],
    skip_keys: set[str],
    visual: bool = False,
    debug: bool = False,
) -> tuple[list[dict], list[dict]]:
    """
    Lit le haut du tableau.
    - fresh : jamais traités (priorité tête de file)
    - retry : déjà SKIP non téléchargé (fin de file, réessai si doc uploadé)
    Succès (processed_keys) : ignorés.
    """
    limit_label = "tout le tableau" if scan_top <= 0 else f"top {scan_top}"
    fresh: list[dict] = []
    retry: list[dict] = []
    seen_in_scan: set[str] = set()
    rows_info: list[dict] = []
    scanned = scan_fleet_rows(driver, max_rows=scan_top)
    n_pending = 0
    prev_matricule = ""

    for pos, (idx, row) in enumerate(scanned, 1):
        if qa._is_row_en_attente(row):
            n_pending += 1

        info = evaluate_row(row)
        info["row"] = row
        info["index"] = idx
        info["pos"] = pos
        rows_info.append(info)

        if debug:
            mark = "⏳" if info["eligible"] else ("✅" if qa._is_row_approved(row) else "❔")
            log(
                f"      L{pos}: {mark} plaque={info['matricule']!r} | flotte={info['fleet']!r} | "
                f"doc={'oui' if info['has_doc'] else 'NON'} | "
                f"eligible={info['eligible']} | {', '.join(info['reasons'])}"
            )

        matricule = info["matricule"]
        neighbor_before = prev_matricule

        if not info["eligible"]:
            if matricule and matricule != "—":
                prev_matricule = matricule
            continue

        row_key = qa._row_identity_key(row, matricule)
        if (
            row_key in seen_in_scan
            or row_key in processed_keys
            or row_key in queue_keys
        ):
            if matricule and matricule != "—":
                prev_matricule = matricule
            continue

        seen_in_scan.add(row_key)
        cand = _candidate_dict_from_row(
            row,
            index=idx,
            pos=pos,
            neighbor_before=neighbor_before,
            info=info,
        )
        if row_key in skip_keys:
            cand["is_retry"] = True
            retry.append(cand)
        else:
            fresh.append(cand)
        if matricule and matricule != "—":
            prev_matricule = matricule

    if not debug:
        log(
            f"   Scan {limit_label} : {len(scanned)} l. | "
            f"EN ATTENTE: {n_pending} | nouveaux: {len(fresh)} | retry_skip: {len(retry)}"
        )
        for c in fresh:
            log(
                f"      → {c['matricule']} | {c['fleet_status']} | "
                f"{', '.join(c.get('reasons') or [])}"
            )
        for c in retry:
            log(
                f"      ↻ {c['matricule']} | retry SKIP | {c['fleet_status']}",
            )

    if visual:
        _ui_mark_scan_results(driver, rows_info)
        _ui_sweep_banner(
            driver,
            f"{len(fresh)} nouveau(x), {len(retry)} retry — {limit_label}",
            "#1565c0",
        )

    return fresh, retry


def collect_candidates(
    driver,
    *,
    visual: bool = False,
    debug: bool = False,
    scan_top: int | None = None,
) -> list[dict]:
    """Mode tour figé : tous les candidats du scan (sans file / processed)."""
    top = _SCAN_TOP if scan_top is None else scan_top
    fresh, retry = discover_candidates(
        driver,
        scan_top=top,
        processed_keys=set(),
        queue_keys=set(),
        skip_keys=set(),
        visual=visual,
        debug=debug,
    )
    return fresh + retry


def enqueue_front(
    queue: deque,
    queue_keys: set[str],
    new_items: list[dict],
) -> int:
    """Insère en tête de file (récent du mini-scan traité en premier)."""
    added = 0
    for cand in reversed(new_items):
        key = cand["row_key"]
        if key in queue_keys:
            continue
        queue.appendleft(cand)
        queue_keys.add(key)
        added += 1
    return added


def enqueue_tail(
    queue: deque,
    queue_keys: set[str],
    items: list[dict],
) -> int:
    """Fin de file — SKIP à réessayer (priorité basse vs nouveaux en tête)."""
    added = 0
    for cand in items:
        key = cand["row_key"]
        if key in queue_keys:
            continue
        cand["is_retry"] = True
        queue.append(cand)
        queue_keys.add(key)
        added += 1
    return added


def enqueue_discovered(
    queue: deque,
    queue_keys: set[str],
    fresh: list[dict],
    retry: list[dict],
) -> tuple[int, int]:
    """Nouveaux en tête, retry SKIP en fin."""
    n_front = enqueue_front(queue, queue_keys, fresh)
    n_tail = enqueue_tail(queue, queue_keys, retry)
    return n_front, n_tail


def release_matricule_filter(driver) -> bool:
    """Recharge manage-fleet après une recherche par matricule (pas de panneau Filtres vide)."""
    if not qa._safe_get(driver, qa.MANAGE_FLEET_URL):
        return False
    qa._wait_table(driver, 8)
    qa.set_pagination_max(driver, 0)
    return True


def navigate_manage_fleet_full(driver) -> bool:
    """Ouvre manage-fleet + pagination 500 (rechargement URL = pas de filtre matricule)."""
    if not qa._safe_get(driver, qa.MANAGE_FLEET_URL):
        log("Impossible d'ouvrir manage-fleet", "ERROR")
        return False
    qa._wait_table(driver, 10)
    qa.set_pagination_max(driver, 0)
    qa._wait_table(driver, 5)
    return True


def find_row_by_key_all(
    driver,
    matricule: str,
    row_key: str,
    wait_timeout: float = 12,
    scan_top: int | None = None,
):
    """Retrouve une ligne par clé doc UUID (cherche dans le haut du tableau)."""
    top = _SCAN_TOP if scan_top is None else scan_top
    end = time.time() + wait_timeout
    while time.time() < end:
        for _, row in scan_fleet_rows(driver, max_rows=top, skip_settle=True):
            try:
                if qa._row_identity_key(row, matricule) == row_key:
                    return row
            except Exception:
                continue
        qa._pause(0.45, 0.1)
    return None


def approve_document_on_page_no_upload(driver) -> tuple[bool, str]:
    """
    Même logique que quick_approve_all_vehicle_vps.approve_document_on_page,
    sans upload : lit le badge carte grise puis clic Approuver si éligible.
    Utilise qa._needs_document_approve_click (en attente d'approbation, etc.).
    """
    qa._wait_table(driver, 6, 0)
    row = qa._find_carte_grise_row(driver)
    if not row:
        log("   Carte grise non trouvée sur la page document", "ERROR")
        return False, "ligne_carte_grise_introuvable"

    log("   Ligne Carte grise trouvée")
    status = qa._doc_status_from_row(row)
    log(f"   Statut document (carte grise): {status!r}")

    sl = status.lower()
    if sl in ("approuvé", "approved") or "approuvé" in sl or "approved" in sl:
        log("   Document déjà approuvé sur la page")
        return True, "deja_approuve"

    if qa._needs_document_upload(status):
        log("   Carte grise non téléchargée — pas d'upload dans le sweep", "WARNING")
        return False, "non_telecharge_skip"

    row = qa._find_carte_grise_row(driver) or row
    status = qa._doc_status_from_row(row)

    if qa._needs_document_approve_click(status):
        log(f"   Statut « {status} » → clic Approuver (comme quick_approve)")
        if qa._click_approve_document_button(driver, row, 0):
            log("   Document approuvé sur la page")
            return True, "approuve"
        return False, "echec_clic_approuver"

    log(
        f"   Document non éligible — statut {status!r} "
        f"(attendu: en attente d'approbation sur la page document)",
        "WARNING",
    )
    return False, f"statut_non_eligible:{status}"


def approve_fleet_row(driver, row, matricule: str) -> bool:
    """Menu ⋮ → Approuver (sans filtre statut UI)."""
    if qa._is_row_approved(row):
        log("   Flotte déjà approuvée")
        return True
    if qa.click_menu_three_dots(driver, row, 0):
        if qa.click_approve_in_menu(driver, 0):
            log("   Flotte approuvée (menu ⋮)")
            return True
    log("   Échec approbation flotte (menu ⋮)", "WARNING")
    return False


def return_to_fleet_table(driver) -> bool:
    return navigate_manage_fleet_full(driver)


def sweep_approve_one_row(driver, candidate: dict) -> dict:
    """
    1) Ouvre document via lien figé au scan (pas de clic sur ligne périmée)
    2) Approuve doc si éligible
    3) Retour tableau → recherche par matricule + clé doc / partenaire / voisin
    4) Menu ⋮ → approuver la flotte
    """
    matricule = candidate["matricule"]
    row_key = candidate["row_key"]
    partner = candidate["partner"]

    result = {
        "matricule": matricule,
        "row_key": row_key,
        "partner": partner,
        "success": False,
        "document_approved": False,
        "fleet_approved": False,
        "skipped": False,
        "skip_reason": "",
        "errors": [],
    }

    log(
        f"   Cible: {matricule} | {partner} | flotte={candidate['fleet_status']} | "
        f"doc …{(candidate.get('doc_uuid') or '')[-8:]} | "
        f"critères: {', '.join(candidate.get('reasons') or [])}"
    )
    doc_link = doc_link_from_candidate(candidate)
    if not doc_link:
        result["errors"].append("doc_link_manquant_au_scan")
        log("   ❌ UUID/lien document absent au scan — ligne ignorée", "ERROR")
        return result

    qa._ui_clear_marks(driver)
    qa._ui_set_banner(driver, f"🎯 SWEEP — {matricule} — {partner}", "#ff9800")

    row_live = find_row_for_candidate(driver, candidate, wait_timeout=10, use_matricule_filter=False)
    if row_live:
        qa._ui_highlight_row(driver, row_live, "active")
        if qa._fleet_row_document_approved(row_live):
            log("   Badges tableau : doc déjà approuvé — approbation flotte uniquement")
            result["document_approved"] = True
            if approve_fleet_row(driver, row_live, matricule):
                result["fleet_approved"] = True
                result["success"] = True
            else:
                result["errors"].append("echec_flotte_doc_deja_ok")
            return result

    log("   Flotte EN ATTENTE → page document (lien direct enregistré au scan)…")
    opened = qa.open_document_page(driver, None, 0, doc_link=doc_link)
    if not opened:
        result["errors"].append("ouverture_document")
        return result

    doc_ok, reason = approve_document_on_page_no_upload(driver)
    if reason == "non_telecharge_skip":
        result["skipped"] = True
        result["skip_reason"] = reason
        log("   SKIP — document non téléchargé (pas d'upload dans ce script)", "WARNING")
        log("   Retour manage-fleet après SKIP…")
        return_to_fleet_table(driver)
        return result

    if doc_ok:
        result["document_approved"] = True
        if reason == "deja_approuve":
            log("   Document déjà approuvé sur la page")
        else:
            log("   Document approuvé sur la page")
    else:
        result["errors"].append(f"document:{reason}")
        return result

    log("   Retour manage-fleet → recherche matricule pour approuver la flotte…")
    if not return_to_fleet_table(driver):
        result["errors"].append("retour_tableau")
        return result

    row2 = find_row_for_candidate(driver, candidate, wait_timeout=16, use_matricule_filter=True)
    if not row2:
        result["errors"].append("ligne_introuvable_apres_doc")
        return result

    if approve_fleet_row(driver, row2, matricule):
        result["fleet_approved"] = True

    result["success"] = result["document_approved"] and result["fleet_approved"]
    if not result["success"] and not result["errors"]:
        result["errors"].append("flotte_non_approuvee")
    return result


def refresh_end_of_round(driver, label: str = "fin de tour") -> None:
    log(f"   Refresh ({label}) — pause {end_round_pause():.0f}s…")
    navigate_manage_fleet_full(driver)
    qa._pause(end_round_pause(), 1.0)


def run_sweep_dynamic(
    driver,
    *,
    max_rounds: int | None,
    deadline: datetime | None,
    show_ui: bool = False,
    debug_scan: bool = False,
    rescan_top: int | None = None,
) -> dict:
    """File en mémoire : mini-scan après chaque cas, nouveaux candidats en tête."""
    stats = {
        "rounds": 0,
        "processed": 0,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "stop_reason": "",
    }
    rescan = _RESCAN_TOP if rescan_top is None else max(1, rescan_top)
    processed_keys: set[str] = set()  # succès uniquement — plus jamais repris
    skip_keys: set[str] = set()  # SKIP non téléchargé — retry en fin de file
    queue_keys: set[str] = set()
    queue: deque = deque()
    empty_cycles = 0

    try:
        nav_ok = navigate_manage_fleet_full(driver)
    except Exception as e:
        if is_browser_session_dead(e):
            log(
                f"   Session navigateur perdue (avant démarrage file) — "
                f"browser_session_perdue | {type(e).__name__}: {e}",
                "WARNING",
            )
            stats["stop_reason"] = "browser_session_perdue"
            return stats
        raise

    if not nav_ok:
        stats["stop_reason"] = "navigation_initiale"
        return stats

    fill_top = _SCAN_TOP if _SCAN_TOP > 0 else 30
    scan_label = "tout le tableau" if fill_top <= 0 else f"top {fill_top}"
    log(
        f"   Mode file dynamique : remplissage {scan_label} | "
        f"mini-scan top {rescan} | nouveaux en tête, SKIP en fin (retry)"
    )
    if show_ui:
        qa._ui_enable(driver, True)
        _ui_sweep_banner(driver, "File dynamique — manage-fleet…", "#1565c0")

    init_fresh, init_retry = discover_candidates(
        driver,
        scan_top=fill_top,
        processed_keys=processed_keys,
        queue_keys=queue_keys,
        skip_keys=skip_keys,
        visual=show_ui,
        debug=debug_scan,
    )
    n_f, n_t = enqueue_discovered(queue, queue_keys, init_fresh, init_retry)
    log(
        f"   File initiale : {n_f} nouveau(x), {n_t} retry — "
        f"total en attente: {len(queue)}"
    )

    case_num = 0

    try:
        while max_rounds is None or stats["rounds"] < max_rounds:
            if should_stop(deadline):
                stats["stop_reason"] = f"heure_limite ({format_deadline(deadline)})"
                log(f"   Heure limite atteinte — arrêt (était prévu {format_deadline(deadline)})")
                break

            if not queue:
                stats["rounds"] += 1
                empty_cycles += 1
                log(f"\n{'─'*50}")
                log(
                    f"FILE VIDE — rescan {scan_label} "
                    f"(cycle {stats['rounds']}, vide x{empty_cycles}) — "
                    f"reste {time_remaining(deadline)}"
                    + (f" (jusqu'à {format_deadline(deadline)})" if deadline else " (Ctrl+C)")
                )
                log(f"{'─'*50}")
                try:
                    navigate_manage_fleet_full(driver)
                    found_fresh, found_retry = discover_candidates(
                        driver,
                        scan_top=fill_top,
                        processed_keys=processed_keys,
                        queue_keys=queue_keys,
                        skip_keys=skip_keys,
                        visual=show_ui,
                        debug=debug_scan,
                    )
                    nf, nt = enqueue_discovered(
                        queue, queue_keys, found_fresh, found_retry,
                    )
                    if nf or nt:
                        log(
                            f"   +{nf} nouveau(x) en tête, +{nt} retry en fin — "
                            f"file={len(queue)}"
                        )
                        empty_cycles = 0
                    else:
                        log(
                            f"   Rien à enfiler (nouveaux/retry) — pause "
                            f"{empty_scan_pause():.0f}s…"
                        )
                        qa._pause(empty_scan_pause(), 1.0)
                except Exception as e:
                    if is_browser_session_dead(e):
                        stats["stop_reason"] = "browser_session_perdue"
                        break
                    raise
                continue

            empty_cycles = 0
            cand = queue.popleft()
            queue_keys.discard(cand["row_key"])
            case_num += 1
            pending = len(queue)

            tag = " ↻RETRY" if cand.get("is_retry") else ""
            log(
                f"\n   [file {pending}] cas #{case_num}{tag} — "
                f"{cand['matricule']} — {cand['partner']}"
            )

            try:
                res = sweep_approve_one_row(driver, cand)
            except Exception as e:
                if is_browser_session_dead(e):
                    raise
                log(f"   Erreur ligne: {e}", "ERROR")
                log(traceback.format_exc(), "ERROR")
                stats["failed"] += 1
                stats["processed"] += 1
            else:
                stats["processed"] += 1
                if res.get("skipped"):
                    stats["skipped"] += 1
                    skip_keys.add(cand["row_key"])
                    if enqueue_tail(queue, queue_keys, [cand]):
                        log(
                            "   ↪ SKIP — fin de file (retry si doc uploadé, "
                            "après les nouveaux)"
                        )
                elif res.get("success"):
                    stats["success"] += 1
                    processed_keys.add(cand["row_key"])
                    skip_keys.discard(cand["row_key"])
                else:
                    stats["failed"] += 1
                    err = ", ".join(res.get("errors") or ["?"])
                    log(f"   Échec: {err}", "WARNING")
                    if cand["row_key"] not in processed_keys:
                        enqueue_tail(queue, queue_keys, [cand])
                        log(f"   ↪ Remis en fin de file (retry plus tard)")

            if should_stop(deadline):
                stats["stop_reason"] = f"heure_limite ({format_deadline(deadline)})"
                break

            try:
                navigate_manage_fleet_full(driver)
                fresh, retry = discover_candidates(
                    driver,
                    scan_top=rescan,
                    processed_keys=processed_keys,
                    queue_keys=queue_keys,
                    skip_keys=skip_keys,
                    visual=show_ui,
                    debug=debug_scan,
                )
                nf, nt = enqueue_discovered(queue, queue_keys, fresh, retry)
                if nf or nt:
                    log(
                        f"   +{nf} nouveau(x) en tête, +{nt} retry en fin "
                        f"(total en attente: {len(queue)})"
                    )
            except Exception as e:
                if is_browser_session_dead(e):
                    stats["stop_reason"] = "browser_session_perdue"
                    break
                raise

    except Exception as e:
        if is_browser_session_dead(e):
            log(
                f"   Session navigateur perdue — browser_session_perdue "
                f"(redémarrage navigateur par le programme principal)\n"
                f"   {type(e).__name__}: {e}",
                "WARNING",
            )
            stats["stop_reason"] = "browser_session_perdue"
        else:
            raise

    if not stats["stop_reason"]:
        if max_rounds is not None and stats["rounds"] >= max_rounds:
            stats["stop_reason"] = "max_rounds"
        else:
            stats["stop_reason"] = "arret"
    return stats


def run_sweep_batch(
    driver,
    *,
    max_per_round: int,
    max_rounds: int | None,
    deadline: datetime | None,
    show_ui: bool = False,
    debug_scan: bool = False,
) -> dict:
    """Ancien mode : tour figé (collect → batch → refresh)."""
    stats = {
        "rounds": 0,
        "processed": 0,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "stop_reason": "",
    }

    empty_rounds = 0
    round_num = 0

    try:
        nav_ok = navigate_manage_fleet_full(driver)
    except Exception as e:
        if is_browser_session_dead(e):
            log(
                f"   Session navigateur perdue (avant le 1er tour) — "
                f"browser_session_perdue | {type(e).__name__}: {e}",
                "WARNING",
            )
            stats["stop_reason"] = "browser_session_perdue"
            return stats
        raise

    if not nav_ok:
        stats["stop_reason"] = "navigation_initiale"
        return stats

    scan_label = "tout le tableau" if _SCAN_TOP <= 0 else f"top {_SCAN_TOP} lignes"
    log(
        f"   Mode tour figé (--batch-mode) : scan {scan_label} → batch max {max_per_round}"
    )
    if show_ui:
        qa._ui_enable(driver, True)
        _ui_sweep_banner(driver, "Scan manage-fleet…", "#1565c0")

    while max_rounds is None or round_num < max_rounds:
        if should_stop(deadline):
            stats["stop_reason"] = f"heure_limite ({format_deadline(deadline)})"
            log(f"   Heure limite atteinte — arrêt (était prévu {format_deadline(deadline)})")
            break

        try:
            round_num += 1
            stats["rounds"] = round_num
            log(f"\n{'─'*50}")
            log(
                f"TOUR {round_num} — reste {time_remaining(deadline)}"
                + (f" (jusqu'à {format_deadline(deadline)})" if deadline else "")
            )
            log(f"{'─'*50}")

            candidates = collect_candidates(driver, visual=show_ui, debug=debug_scan)
            log(f"   {len(candidates)} candidat(s) à traiter ce tour")

            if not candidates:
                empty_rounds += 1
                if deadline:
                    log(
                        f"   Aucun candidat (tour {empty_rounds}) — "
                        f"rescan jusqu'à {format_deadline(deadline)} "
                        f"(reste {time_remaining(deadline)})"
                    )
                else:
                    log(
                        f"   Aucun candidat (tour {empty_rounds}) — rescan "
                        f"(Ctrl+C pour arrêter)"
                    )
                refresh_end_of_round(driver, "attente candidats")
                continue

            empty_rounds = 0
            batch = candidates[:max_per_round]
            if len(candidates) > max_per_round:
                log(f"   Limite tour : {max_per_round}/{len(candidates)} traité(s) ce tour")

            approved_this_round = 0

            for n, cand in enumerate(batch, 1):
                if should_stop(deadline):
                    stats["stop_reason"] = f"heure_limite ({format_deadline(deadline)})"
                    log("   Heure limite — arrêt avant fin du tour")
                    break

                log(f"\n   [{n}/{len(batch)}] {cand['matricule']} — {cand['partner']}")
                try:
                    res = sweep_approve_one_row(driver, cand)
                except Exception as e:
                    if is_browser_session_dead(e):
                        raise
                    log(f"   Erreur ligne: {e}", "ERROR")
                    log(traceback.format_exc(), "ERROR")
                    stats["failed"] += 1
                    stats["processed"] += 1
                    continue

                stats["processed"] += 1
                if res.get("skipped"):
                    stats["skipped"] += 1
                elif res.get("success"):
                    stats["success"] += 1
                    approved_this_round += 1
                else:
                    stats["failed"] += 1
                    err = ", ".join(res.get("errors") or ["?"])
                    log(f"   Échec: {err}", "WARNING")

                if n < len(batch) and not should_stop(deadline):
                    navigate_manage_fleet_full(driver)

            if should_stop(deadline):
                break

            if approved_this_round == 0 and batch:
                log(
                    f"   Tour sans succès — rescan au prochain tour"
                    + (
                        f" (jusqu'à {format_deadline(deadline)}, reste {time_remaining(deadline)})"
                        if deadline
                        else " (Ctrl+C pour arrêter)"
                    ),
                    "WARNING",
                )
            else:
                empty_rounds = 0

            refresh_end_of_round(driver, f"tour {round_num} terminé")

        except Exception as e:
            if is_browser_session_dead(e):
                log(
                    f"   Session navigateur perdue — browser_session_perdue "
                    f"(redémarrage navigateur par le programme principal)\n"
                    f"   {type(e).__name__}: {e}",
                    "WARNING",
                )
                stats["stop_reason"] = "browser_session_perdue"
                break
            raise

    if not stats["stop_reason"]:
        if max_rounds is not None and round_num >= max_rounds:
            stats["stop_reason"] = "max_rounds"
        else:
            stats["stop_reason"] = "arret"
    return stats


def run_sweep(
    driver,
    *,
    max_per_round: int,
    max_rounds: int | None,
    deadline: datetime | None,
    show_ui: bool = False,
    debug_scan: bool = False,
    batch_mode: bool = False,
    rescan_top: int | None = None,
) -> dict:
    if batch_mode:
        return run_sweep_batch(
            driver,
            max_per_round=max_per_round,
            max_rounds=max_rounds,
            deadline=deadline,
            show_ui=show_ui,
            debug_scan=debug_scan,
        )
    return run_sweep_dynamic(
        driver,
        max_rounds=max_rounds,
        deadline=deadline,
        show_ui=show_ui,
        debug_scan=debug_scan,
        rescan_top=rescan_top,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Balayage manage-fleet : approuve les docs en attente d'approbation (sans upload)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  # Boucle jusqu'à Ctrl+C
  python sweep_approve_pending_documents_vps.py

  # Jusqu'à 08h30 ou 15h15 (rescan en boucle, pas d'arrêt file vide avant l'heure)
  python sweep_approve_pending_documents_vps.py --until 08:30
  python sweep_approve_pending_documents_vps.py --until 15h15

  # Jusqu'à 14h00 (14, 14h, 14:00 acceptés)
  python sweep_approve_pending_documents_vps.py --until 14 --fast

  # Jusqu'à 16h, max 20 lignes par tour
  python sweep_approve_pending_documents_vps.py --until 16h --max-per-round 20

  # Plafond optionnel de tours (sinon boucle sans limite de tours jusqu'à Ctrl+C / --until)
  python sweep_approve_pending_documents_vps.py --max-rounds 500
        """,
    )
    parser.add_argument("--headed", action="store_true", help="Navigateur visible + marqueurs couleur")
    parser.add_argument(
        "--debug-scan",
        action="store_true",
        help="Log détaillé de chaque ligne (plaque, statut, critères)",
    )
    parser.add_argument("--fast", action="store_true", help="Pauses réduites")
    parser.add_argument(
        "--until",
        metavar="HEURE",
        help="Arrêt automatique à cette heure (24h) : 08:30, 14, 16h",
    )
    parser.add_argument(
        "--max-per-round",
        type=int,
        default=30,
        help="Max lignes traitées par tour avant refresh (défaut: 30)",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        metavar="N",
        help="Plafond de tours de balayage (optionnel). Sans ce flag : pas de limite de tours.",
    )
    parser.add_argument(
        "--scan-top",
        type=int,
        default=_DEFAULT_SCAN_TOP,
        metavar="N",
        help=(
            "Nombre de lignes en haut du tableau à scanner (récentes en premier). "
            f"Défaut: {_DEFAULT_SCAN_TOP}. 0 = tout le tableau (lent)"
        ),
    )
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help="Scanner tout le tableau (équivalent --scan-top 0, lent)",
    )
    parser.add_argument(
        "--rescan-top",
        type=int,
        default=_DEFAULT_RESCAN_TOP,
        metavar="N",
        help=(
            "Mini-scan en haut du tableau après chaque cas (file dynamique). "
            f"Défaut: {_DEFAULT_RESCAN_TOP}"
        ),
    )
    parser.add_argument(
        "--batch-mode",
        action="store_true",
        help="Ancien mode tour figé (--max-per-round) au lieu de la file dynamique",
    )
    parser.add_argument(
        "--table-settle",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "Pause (s) après chargement du tableau avant chaque scan "
            f"(défaut: {SWEEP_TABLE_SETTLE_NORMAL:.0f}s, --fast: {SWEEP_TABLE_SETTLE_FAST:.0f}s)"
        ),
    )

    args = parser.parse_args()
    qa._FAST_MODE = bool(args.fast)
    global _SCAN_TOP, _RESCAN_TOP, _TABLE_SETTLE_OVERRIDE
    _SCAN_TOP = 0 if args.scan_all else max(0, args.scan_top)
    _RESCAN_TOP = max(1, args.rescan_top)
    _TABLE_SETTLE_OVERRIDE = args.table_settle

    deadline: datetime | None = None
    if args.until:
        try:
            deadline = parse_until_time(args.until)
        except ValueError as e:
            log(str(e), "ERROR")
            sys.exit(2)

    log(f"\n{'='*60}")
    log("SWEEP APPROVE PENDING DOCUMENTS")
    log(f"   Pas d'upload | pas de filtre UI statut | 1 navigateur")
    rounds_desc = (
        f"max {args.max_rounds} cycle(s) file vide"
        if args.max_rounds is not None
        else "cycles file vide illimités (--max-rounds pour plafonner)"
    )
    if args.batch_mode:
        log(f"   Mode tour figé | max {args.max_per_round} ligne(s)/tour | {rounds_desc}")
        log(
            f"   Scan : {'tout le tableau' if _SCAN_TOP <= 0 else f'top {_SCAN_TOP} lignes'} "
            f"(--debug-scan)"
        )
    else:
        fill = _SCAN_TOP if _SCAN_TOP > 0 else 30
        log(
            f"   Mode file dynamique | mini-scan top {_RESCAN_TOP} | "
            f"nouveaux en tête, SKIP retry en fin | {rounds_desc}"
        )
        log(f"   Remplissage initial : top {fill} (--debug-scan)")
    log(f"   Consolidation tableau avant scan : {table_settle_pause():.0f}s (--table-settle pour ajuster)")
    if qa._FAST_MODE:
        log("   Mode --fast (pauses réduites)")
    if deadline:
        log(f"   Arrêt programmé : {format_deadline(deadline)} (dans {time_remaining(deadline)})")
    else:
        log("   Durée : jusqu'à Ctrl+C (pas d'arrêt automatique si file vide)")
    log(
        "   Si Chrome / WebDriver perd la session : redémarrage auto du navigateur "
        f"(pause {BROWSER_RESTART_PAUSE_SEC:.0f}s puis re-login)"
    )
    log(f"{'='*60}")

    slack_rounds = (
        f"• Max {args.max_rounds} cycles file vide\n"
        if args.max_rounds is not None
        else "• Cycles file vide illimités\n"
    )
    mode_slack = (
        f"• Tour figé, max {args.max_per_round}/tour\n"
        if args.batch_mode
        else f"• File dynamique, rescan top {_RESCAN_TOP}\n"
    )
    qa.send_slack_message(
        "🔄 *Sweep approbation documents démarré*\n"
        + mode_slack
        + slack_rounds
        + "• Redémarrage auto du navigateur si session Chrome perdue\n"
        + (f"• Jusqu'à {format_deadline(deadline)}" if deadline else "• Durée illimitée (Ctrl+C)")
    )

    driver = None
    start = time.time()
    stats: dict = {
        "rounds": 0,
        "processed": 0,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "stop_reason": "",
        "browser_restarts": 0,
    }

    try:
        while True:
            if should_stop(deadline):
                stats["stop_reason"] = f"heure_limite ({format_deadline(deadline)})"
                log(f"   Heure limite — arrêt avant nouveau navigateur", "WARNING")
                break

            if args.max_rounds is not None:
                rem = args.max_rounds - stats["rounds"]
                if rem <= 0:
                    stats["stop_reason"] = "max_rounds"
                    break
                max_rounds_this_run = rem
            else:
                max_rounds_this_run = None

            debug_port = None if args.headed else qa.BASE_DEBUG_PORT
            driver = qa.setup_driver(headed=args.headed, debug_port=debug_port, wid=0)
            if not qa.admin_login(driver, 0):
                log("Connexion admin échouée", "ERROR")
                sys.exit(1)

            try:
                run_stats = run_sweep(
                    driver,
                    max_per_round=max(1, args.max_per_round),
                    max_rounds=max_rounds_this_run,
                    deadline=deadline,
                    show_ui=bool(args.headed),
                    debug_scan=bool(args.debug_scan),
                    batch_mode=bool(args.batch_mode),
                    rescan_top=_RESCAN_TOP,
                )
            except Exception as e:
                if not is_browser_session_dead(e):
                    raise
                log(
                    f"   Session navigateur perdue (hors run_sweep) — "
                    f"browser_session_perdue | {type(e).__name__}: {e}",
                    "WARNING",
                )
                run_stats = {"stop_reason": "browser_session_perdue"}
            merge_sweep_stats(stats, run_stats)
            reason = run_stats.get("stop_reason", "")

            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None

            if reason == "browser_session_perdue":
                stats["browser_restarts"] = stats.get("browser_restarts", 0) + 1
                n = stats["browser_restarts"]
                log(
                    f"   Redémarrage navigateur dans {BROWSER_RESTART_PAUSE_SEC:.0f}s "
                    f"(reprise du balayage, n°{n})…",
                    "WARNING",
                )
                if should_stop(deadline):
                    stats["stop_reason"] = f"heure_limite ({format_deadline(deadline)})"
                    log("   Heure limite atteinte pendant la pause — arrêt", "WARNING")
                    break
                time.sleep(BROWSER_RESTART_PAUSE_SEC)
                continue

            stats["stop_reason"] = reason
            break

    except KeyboardInterrupt:
        log("\n   Interruption clavier (Ctrl+C) — arrêt propre", "WARNING")
        if not stats.get("stop_reason"):
            stats["stop_reason"] = "ctrl_c"

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    elapsed = time.time() - start
    log(f"\n{'='*60}")
    log(f"RÉSUMÉ SWEEP — {elapsed/60:.1f} min")
    log(f"{'='*60}")
    log(f"   Tours : {stats.get('rounds', 0)}")
    log(f"   Lignes traitées : {stats.get('processed', 0)}")
    log(f"   Succès : {stats.get('success', 0)}")
    log(f"   Ignorées (non téléchargé) : {stats.get('skipped', 0)}")
    log(f"   Échecs : {stats.get('failed', 0)}")
    br = int(stats.get("browser_restarts", 0) or 0)
    if br:
        log(f"   Redémarrages navigateur (session perdue) : {br}")
    log(f"   Arrêt : {stats.get('stop_reason', '—')}")
    log(f"\n   Log : {qa.LOG_FILE}")

    slack_tail = (
        f"\n• Redémarrages navigateur : {br}"
        if (br := int(stats.get("browser_restarts", 0) or 0))
        else ""
    )
    qa.send_slack_message(
        f"🏁 *Sweep terminé* ({elapsed/60:.1f} min)\n"
        f"• OK: {stats.get('success', 0)} | skip: {stats.get('skipped', 0)} | "
        f"échecs: {stats.get('failed', 0)}\n"
        f"• Raison: {stats.get('stop_reason', '—')}"
        + slack_tail
    )

    sys.exit(0 if stats.get("failed", 0) == 0 else 1)


if __name__ == "__main__":
    main()
