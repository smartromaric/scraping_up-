#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import create_missing_driver_vehicle_assoc_vps as base


BASE_DIR = Path(__file__).parent
SCRAPE_DIR = BASE_DIR / "output" / "partenaire_drivers_scrape"


def _key_display(key) -> str:
    if isinstance(key, tuple) and key:
        return key[0]
    return str(key)


def _chunk_list(items: list, n: int) -> list:
    if n <= 1:
        return [items]
    chunks = [[] for _ in range(n)]
    for i, item in enumerate(items):
        chunks[i % n].append(item)
    return [c for c in chunks if c]


def _build_json_counts(drivers: list):
    json_counts = Counter()
    display = {}
    ignored_invalid_mats = 0

    for d in drivers:
        vehicle = d.get("vehicle", {}) or {}
        vtype = base.resolve_type(vehicle.get("type", ""), d.get("transport_hint", ""))
        marque = vehicle.get("marque", "")
        modele = vehicle.get("modele", "")
        matricule = vehicle.get("matricule", "")

        if not base.is_valid_matricule(matricule):
            ignored_invalid_mats += 1
            continue

        key = base.make_vehicle_key(vtype, marque, modele, matricule)
        json_counts[key] += 1
        display[key] = matricule

    return json_counts, display, ignored_invalid_mats


def _build_site_counts(online_rows: list):
    site_counts = Counter()
    display = {}
    for r in online_rows:
        key = r.get("key")
        if not key:
            continue
        site_counts[key] += 1
        display[key] = r.get("plaque", _key_display(key))
    return site_counts, display


def _log_key_block(title: str, keys: list, display_map: dict, limit: int = 20):
    if not keys:
        base.log(f"   {title}: aucun")
        return
    base.log(f"   {title} ({len(keys)}):")
    for k in keys[:limit]:
        base.log(f"      - {display_map.get(k, _key_display(k))}")
    if len(keys) > limit:
        base.log(f"      ... +{len(keys) - limit} autre(s)")


def _show_zone_badge(driver, text: str, color: str = "#0ea5e9"):
    """Affiche un badge fixe à l'écran pour suivre la phase en cours."""
    try:
        driver.execute_script(
            """
            const id = 'audit-zone-badge';
            let el = document.getElementById(id);
            if (!el) {
              el = document.createElement('div');
              el.id = id;
              el.style.position = 'fixed';
              el.style.top = '10px';
              el.style.right = '10px';
              el.style.zIndex = '999999';
              el.style.padding = '10px 14px';
              el.style.borderRadius = '10px';
              el.style.fontFamily = 'Arial, sans-serif';
              el.style.fontWeight = '700';
              el.style.fontSize = '12px';
              el.style.color = '#ffffff';
              el.style.boxShadow = '0 8px 20px rgba(0,0,0,0.28)';
              document.body.appendChild(el);
            }
            el.style.background = arguments[1];
            el.textContent = arguments[0];
            """,
            text,
            color,
        )
    except Exception:
        pass


def _highlight_problem_rows(driver, plates_or_norm: set, color: str, label: str):
    """
    Surligne les lignes du tableau dont l'immatriculation correspond à la liste fournie.
    On compare la plaque brute et sa version normalisée.
    """
    if not plates_or_norm:
        return 0
    try:
        highlighted = driver.execute_script(
            """
            const wanted = new Set(arguments[0] || []);
            const color = arguments[1] || '#f59e0b';
            const label = arguments[2] || 'AUDIT';
            const rows = Array.from(document.querySelectorAll('table tbody tr'));
            let count = 0;
            const norm = (s) => String(s || '').toUpperCase().replace(/[\\s\\-_.\\/]/g, '');
            for (const row of rows) {
              const cells = row.querySelectorAll('td');
              if (!cells || cells.length < 5) continue;
              const plate = (cells[4].textContent || '').trim();
              if (!plate) continue;
              const plateNorm = norm(plate);
              if (wanted.has(plate) || wanted.has(plateNorm)) {
                row.style.outline = `3px solid ${color}`;
                row.style.outlineOffset = '-2px';
                row.style.background = 'rgba(255, 241, 118, 0.24)';
                row.setAttribute('data-audit-label', label);
                count++;
              }
            }
            return count;
            """,
            list(plates_or_norm),
            color,
            label,
        )
        return int(highlighted or 0)
    except Exception:
        return 0


def _read_site_total_fast(driver, retries: int = 3) -> int:
    """
    Lecture rapide du total site depuis le texte de pagination DataTables.
    Retourne -1 si introuvable.
    """
    driver.get(base.MANAGE_FLEET_URL)
    for i in range(1, retries + 1):
        try:
            time.sleep(1.5)
            data = driver.execute_script(
                """
                const sels = ['#DataTables_Table_0_info', '.dataTables_info', 'div.dataTables_info'];
                for (const s of sels) {
                  const el = document.querySelector(s);
                  if (el && el.textContent && el.textContent.trim()) {
                    return {source: s, text: el.textContent.trim()};
                  }
                }
                const body = (document.body && document.body.innerText) ? document.body.innerText : '';
                return {source: 'body', text: body.slice(0, 4000)};
                """
            ) or {}
            txt = (data.get("text") or "").strip()
            src = data.get("source", "?")
            compact = txt.replace("\xa0", " ")
            # EN: "Showing 1 to 101 of 101 entries"
            m = re.search(r"\bof\s+([\d\s.,]+)\s+entries\b", compact, re.IGNORECASE)
            # FR: "Affichage 1 à 101 de 101 entrées"
            if not m:
                m = re.search(r"\bde\s+([\d\s.,]+)\s+entr[ée]es\b", compact, re.IGNORECASE)
            if not m:
                m = re.search(r"\bsur\s+([\d\s.,]+)\b", compact, re.IGNORECASE)
            if m:
                raw_num = re.sub(r"[^\d]", "", m.group(1))
                total = int(raw_num) if raw_num else -1
                base.log(f"   ⚡ Total rapide détecté ({src}) : {total}")
                return total
            base.log(f"   ⚠️ Total rapide introuvable (tentative {i}/{retries})")
        except Exception as e:
            base.log(f"   ⚠️ Lecture total rapide échouée {i}/{retries}: {str(e).splitlines()[0]}")
        time.sleep(0.8)
    return -1


def audit_partner(driver, partner: dict, threshold: int = 100, deep_all: bool = False) -> dict:
    folder = partner.get("_folder", "?")
    email = partner.get("email", "")
    password = partner.get("password", "")
    partner_json_path = Path(partner.get("_path", ""))

    base.log("=" * 68)
    base.log(f"📂 {folder}  |  📧 {email}")
    base.log("=" * 68)

    result = {
        "folder": folder,
        "email": email,
        "status": "OK",
        "alerts": [],
        "site_total": 0,
        "json_total": 0,
        "audit_mode": "quick_only",
        "deep_reason": [],
        "timings_sec": {},
        "problem_counters": {},
    }

    if not email:
        result["status"] = "ERROR"
        result["alerts"].append("Email partenaire manquant")
        base.log("   ❌ Email partenaire manquant")
        return result

    if not base.login(driver, email, password=password):
        result["status"] = "ERROR"
        result["alerts"].append("Login échoué")
        return result

    json_counts, json_display, ignored_invalid = _build_json_counts(partner.get("drivers", []))
    json_total = sum(json_counts.values())
    result["json_total"] = json_total
    base.log(f"   📋 Total JSON valide : {json_total} véhicule(s)")
    if ignored_invalid:
        base.log(f"   ⚠️ JSON ignoré (immat invalide) : {ignored_invalid} entrée(s)")

    # Phase 1: check rapide total site (beaucoup plus rapide qu'un audit complet)
    t_quick = time.time()
    _show_zone_badge(driver, "ZONE 1/3 - CHECK RAPIDE", "#0284c7")
    base.log("   ⚡ PHASE 1/2 : check rapide du total côté site")
    quick_site_total = _read_site_total_fast(driver, retries=3)
    result["timings_sec"]["quick_phase"] = round(time.time() - t_quick, 2)
    result["site_total"] = quick_site_total if quick_site_total >= 0 else 0

    deep_reasons = []
    if quick_site_total < 0:
        deep_reasons.append("total_site_introuvable")
    else:
        if quick_site_total > threshold:
            deep_reasons.append(f"site_total>{threshold}")
        if quick_site_total != json_total:
            deep_reasons.append("site_total!=json_total")

    if deep_all and "forced_deep_all" not in deep_reasons:
        deep_reasons.append("forced_deep_all")

    if quick_site_total >= 0:
        base.log(f"   📋 Total site (rapide) : {quick_site_total} véhicule(s)")
        if quick_site_total > threshold:
            msg = f"Flotte site dépasse le seuil {threshold}: {quick_site_total}"
            result["alerts"].append(msg)
            base.log(f"   ⚠️ {msg}")
        if quick_site_total != json_total:
            msg = f"Total JSON/SITE différent: JSON={json_total}, SITE={quick_site_total}"
            result["alerts"].append(msg)
            base.log(f"   ⚠️ {msg}")

    if not deep_reasons:
        base.log("   ✅ PHASE 2 ignorée (aucune anomalie rapide détectée)")
        base.log("   ✅ VERDICT: OK (contrôle rapide)")
        result["problem_counters"] = {
            "missing_on_site": 0,
            "extra_on_site": 0,
            "dup_missing_on_site": 0,
            "dup_extra_on_site": 0,
            "count_mismatch": 0,
        }
        return result

    result["audit_mode"] = "deep"
    result["deep_reason"] = deep_reasons
    _show_zone_badge(driver, "ZONE 2/3 - AUDIT PROFOND", "#f59e0b")
    base.log(f"   🔬 PHASE 2/2 : audit profond déclenché ({', '.join(deep_reasons)})")

    t_deep = time.time()
    online_rows = base.get_consistent_online_fleet(driver, attempts=3, min_rows=1)
    result["timings_sec"]["deep_phase"] = round(time.time() - t_deep, 2)
    site_counts, site_display = _build_site_counts(online_rows)
    site_total = sum(site_counts.values())
    result["site_total"] = site_total
    base.log(f"   📋 Total site (profond) : {site_total} véhicule(s)")

    dup_site_keys = {k for k, c in site_counts.items() if c > 1}
    dup_json_keys = {k for k, c in json_counts.items() if c > 1}
    missing_on_site = sorted(set(json_counts.keys()) - set(site_counts.keys()))
    extra_on_site = sorted(set(site_counts.keys()) - set(json_counts.keys()))
    dup_missing = sorted(dup_json_keys - dup_site_keys)
    dup_extra = sorted(dup_site_keys - dup_json_keys)

    count_mismatch = []
    for k in sorted(set(json_counts.keys()) & set(site_counts.keys())):
        if json_counts[k] != site_counts[k]:
            count_mismatch.append({
                "matricule": json_display.get(k, site_display.get(k, _key_display(k))),
                "json_count": json_counts[k],
                "site_count": site_counts[k],
                "delta_site_json": site_counts[k] - json_counts[k],
            })

    base.log(f"   📊 Doublons JSON : {len(dup_json_keys)} | Doublons site : {len(dup_site_keys)}")
    if dup_missing or dup_extra:
        result["alerts"].append("Set des matricules doublées non identique entre JSON et site")
        base.log("   ⚠️ Les matricules doublées ne sont pas alignées JSON vs site")

    _log_key_block("Doublons manquants sur site", dup_missing, json_display)
    _log_key_block("Doublons en surplus sur site", dup_extra, site_display)
    _log_key_block("Matricules JSON absentes du site", missing_on_site, json_display)
    _log_key_block("Matricules site absentes du JSON", extra_on_site, site_display)

    if count_mismatch:
        result["alerts"].append(f"Écarts de volumes par matricule: {len(count_mismatch)}")
        base.log(f"   ⚠️ Écarts de nombre d'exemplaires pour {len(count_mismatch)} matricule(s)")
        for item in count_mismatch[:20]:
            base.log(
                f"      - {item['matricule']} | JSON={item['json_count']} | "
                f"SITE={item['site_count']} | Écart={item['delta_site_json']:+d}"
            )
        if len(count_mismatch) > 20:
            base.log(f"      ... +{len(count_mismatch) - 20} autre(s)")
    else:
        base.log("   ✅ Aucun écart de volume par matricule")

    result["details"] = {
        "dup_json_count": len(dup_json_keys),
        "dup_site_count": len(dup_site_keys),
        "dup_missing_on_site": [json_display.get(k, _key_display(k)) for k in dup_missing],
        "dup_extra_on_site": [site_display.get(k, _key_display(k)) for k in dup_extra],
        "missing_on_site": [json_display.get(k, _key_display(k)) for k in missing_on_site],
        "extra_on_site": [site_display.get(k, _key_display(k)) for k in extra_on_site],
        "count_mismatch": count_mismatch,
    }
    result["problem_counters"] = {
        "missing_on_site": len(missing_on_site),
        "extra_on_site": len(extra_on_site),
        "dup_missing_on_site": len(dup_missing),
        "dup_extra_on_site": len(dup_extra),
        "count_mismatch": len(count_mismatch),
    }

    # Zone visuelle de suivi (mode headed): surlignage des lignes problématiques côté site.
    # Légende:
    # - Rouge: doublons en surplus sur site
    # - Orange: écarts de volumes (JSON != SITE) sur la plaque
    # - Bleu: plaques présentes sur site mais absentes du JSON
    try:
        mismatch_plates = set()
        for item in count_mismatch:
            mismatch_plates.add(item.get("matricule", ""))
            mismatch_plates.add(base.norm_str(item.get("matricule", "")))
        dup_extra_plates = set(site_display.get(k, _key_display(k)) for k in dup_extra)
        dup_extra_plates |= {base.norm_str(p) for p in dup_extra_plates}
        extra_site_plates = set(site_display.get(k, _key_display(k)) for k in extra_on_site)
        extra_site_plates |= {base.norm_str(p) for p in extra_site_plates}

        # Recharger la grille pour appliquer des zones visuelles visibles.
        base.load_fleet_page(driver)
        _show_zone_badge(driver, "ZONE 3/3 - SURBRILLANCE VISUELLE", "#7c3aed")
        c1 = _highlight_problem_rows(driver, dup_extra_plates, "#ef4444", "DUP_EXTRA_SITE")
        c2 = _highlight_problem_rows(driver, mismatch_plates, "#f59e0b", "COUNT_MISMATCH")
        c3 = _highlight_problem_rows(driver, extra_site_plates, "#3b82f6", "SITE_ONLY")
        base.log(
            f"   🎯 Zones visuelles: DUP_EXTRA={c1} | COUNT_MISMATCH={c2} | SITE_ONLY={c3} "
            "(rouge/orange/bleu)"
        )
    except Exception as e:
        base.log(f"   ⚠️ Zones visuelles non appliquées: {str(e).splitlines()[0]}")

    base.log(
        "   🧮 Compteurs anomalies | "
        f"missing={len(missing_on_site)} | extra={len(extra_on_site)} | "
        f"dup_missing={len(dup_missing)} | dup_extra={len(dup_extra)} | "
        f"count_mismatch={len(count_mismatch)}"
    )

    if result["alerts"]:
        result["status"] = "ALERTE"
        base.log(f"   🚨 VERDICT: ALERTE ({len(result['alerts'])})")
    else:
        base.log("   ✅ VERDICT: OK (JSON et site alignés)")

    # Rapport partenaire
    report_dir = partner_json_path.parent if partner_json_path.exists() else SCRAPE_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"audit_manage_fleet_{folder}_{ts}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    base.log(f"   📄 Rapport audit : {report_path.name}")

    return result


def _run_partner_batch(partners_batch: list, headed: bool, debug_port: int, threshold: int, deep_all: bool, worker_label: str):
    os.environ["WORKER_LABEL"] = worker_label
    base.WORKER_LABEL = worker_label
    out = []
    driver = base.setup_driver(headed=headed, debug_port=debug_port)
    try:
        for partner in partners_batch:
            t0 = time.time()
            st = audit_partner(driver, partner, threshold=threshold, deep_all=deep_all)
            st["duration_sec"] = round(time.time() - t0, 2)
            base.log(
                f"   ⏱️ Durée partenaire: {st['duration_sec']}s | mode={st.get('audit_mode')} | "
                f"alertes={len(st.get('alerts', []))}"
            )
            if st.get("alerts"):
                for idx, msg in enumerate(st.get("alerts", []), start=1):
                    base.log(f"      ⚠️ Alerte {idx}: {msg}")
            out.append(st)
            base.logout(driver)
            time.sleep(1)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return out


def parse_args():
    p = argparse.ArgumentParser(description="Audit de cohérence manage-fleet (site vs JSON)")
    p.add_argument("--only", help="Traiter un seul partenaire (ex: partenaire-6)")
    p.add_argument("--start", help="Reprendre depuis ce partenaire")
    p.add_argument("--end", help="S'arrêter après ce partenaire")
    p.add_argument("--workers", type=int, default=1, help="Nombre de workers (navigateurs)")
    p.add_argument("--headed", action="store_true", help="Chrome visible")
    p.add_argument("--no-headless", action="store_true", help="Alias de --headed")
    p.add_argument("--debug-port", type=int, default=9222, help="Port debug Chrome")
    p.add_argument("--threshold", type=int, default=100, help="Seuil d'alerte volume site")
    p.add_argument("--deep-all", action="store_true",
                   help="Forcer l'audit profond pour tous les partenaires (plus lent)")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="Audit sans écriture (par défaut déjà sans écriture)")
    return p.parse_args()


def main():
    args = parse_args()
    headed_mode = args.headed or args.no_headless

    partners = base.load_partners_scrape(SCRAPE_DIR)

    if args.only:
        norm = (args.only or "").strip().lower()
        partners = [p for p in partners if (p.get("_folder", "").strip().lower() == norm)]

    if args.start:
        s = base.partner_num(args.start)
        partners = [p for p in partners if base.partner_num(p.get("_folder", "")) >= s]

    if args.end:
        e = base.partner_num(args.end)
        partners = [p for p in partners if base.partner_num(p.get("_folder", "")) <= e]

    base.log(f"📋 Audit: {len(partners)} partenaire(s) à traiter")
    if not partners:
        base.log("✅ Rien à faire.")
        return

    workers = max(1, int(args.workers or 1))
    workers = min(workers, len(partners))
    all_stats = []

    t0 = time.time()
    if workers == 1:
        all_stats = _run_partner_batch(partners, headed_mode, args.debug_port, args.threshold, args.deep_all, "W1")
    else:
        batches = _chunk_list(partners, workers)
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = []
            for i, batch in enumerate(batches):
                futs.append(ex.submit(
                    _run_partner_batch,
                    batch,
                    headed_mode,
                    args.debug_port + i,
                    args.threshold,
                    args.deep_all,
                    f"W{i+1}",
                ))
            for fut in as_completed(futs):
                try:
                    all_stats.extend(fut.result())
                except Exception as e:
                    base.log(f"❌ Worker crash: {e}")

    ok = sum(1 for s in all_stats if s.get("status") == "OK")
    alert = sum(1 for s in all_stats if s.get("status") == "ALERTE")
    err = sum(1 for s in all_stats if s.get("status") == "ERROR")
    deep_count = sum(1 for s in all_stats if s.get("audit_mode") == "deep")
    quick_only_count = sum(1 for s in all_stats if s.get("audit_mode") == "quick_only")
    dt = round((time.time() - t0) / 60.0, 1)

    summary = {
        "generated_at": datetime.now().isoformat(),
        "workers": workers,
        "threshold": args.threshold,
        "partners_total": len(all_stats),
        "ok": ok,
        "alert": alert,
        "error": err,
        "deep_audit_count": deep_count,
        "quick_only_count": quick_only_count,
        "results": all_stats,
    }
    summary_path = SCRAPE_DIR / f"audit_manage_fleet_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    base.log("=" * 70)
    base.log(f"✅ AUDIT TERMINÉ en {dt} min")
    base.log(f"   Partenaires audités : {len(all_stats)}")
    base.log(f"   ✅ OK      : {ok}")
    base.log(f"   🚨 Alertes : {alert}")
    base.log(f"   ❌ Erreurs : {err}")
    base.log(f"   ⚡ Quick-only : {quick_only_count}")
    base.log(f"   🔬 Audit profond : {deep_count}")
    problematic = [s for s in all_stats if s.get("alerts")]
    if problematic:
        base.log(f"   📌 Partenaires problématiques ({len(problematic)}):")
        for p in problematic[:20]:
            base.log(
                f"      - {p.get('folder')} | mode={p.get('audit_mode')} | "
                f"site={p.get('site_total')} | json={p.get('json_total')} | "
                f"alertes={len(p.get('alerts', []))}"
            )
        if len(problematic) > 20:
            base.log(f"      ... +{len(problematic) - 20} autre(s)")
    base.log(f"   📄 Summary : {summary_path.name}")
    base.log("=" * 70)


if __name__ == "__main__":
    main()

