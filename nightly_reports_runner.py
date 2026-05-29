#!/usr/bin/env python3
"""
nightly_reports_runner.py
=========================

Chaîne « rapports du soir » (sans partner_fleet_orchestrator.py) :

  1. export_partner_fleet_drivers_report.py  → JSON + Excel
  2. generate_activation_report.py         → HTML par lot (global inclus dans chaque lot)
  3. ZIP par plage (ex. 1-10, 11-20) — uniquement les fichiers HTML des rapports
  4. Envoi email optionnel (variables d'environnement)

Tu mets à jour ``output/partner_automation/state.json`` (recharges manuelles)
**avant** l'heure de lancement (tâche Windows ou --wait-from).

Usage :
  python nightly_reports_runner.py
  python nightly_reports_runner.py --headed
  python nightly_reports_runner.py --until 19:00
  python nightly_reports_runner.py --lots 1-10,11-20 --skip-email
  python nightly_reports_runner.py --skip-export --input output/partner_automation/rapport_partenaires_xxx.json
"""

from __future__ import annotations

import argparse
import os
import re
import smtplib
import subprocess
import sys
import traceback
import zipfile
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output" / "partner_automation"
STATE_FILE = OUTPUT_DIR / "state.json"
ACTIVATION_DIR = OUTPUT_DIR / "rapports_activation"
LOG_FILE = OUTPUT_DIR / "nightly_runner.log"
ZIP_DIR = OUTPUT_DIR / "zip_soir"

EXPORT_SCRIPT = SCRIPT_DIR / "export_partner_fleet_drivers_report.py"
ACTIVATION_SCRIPT = SCRIPT_DIR / "generate_activation_report.py"

_UNTIL_TIME_RE = re.compile(
    r"^(\d{1,2})(?:(?:h|H|:)(\d{2}))?\s*(?:h|H)?$",
    re.IGNORECASE,
)
_LOT_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}][{level}] {msg}"
    print(line, flush=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_until_time(value: str, now: datetime | None = None) -> datetime:
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


def should_stop(deadline: datetime | None) -> bool:
    return deadline is not None and datetime.now() >= deadline


def parse_lots(spec: str) -> list[tuple[int, int]]:
    """Ex. « 1-10,11-20 » → [(1, 10), (11, 20)]."""
    out: list[tuple[int, int]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = _LOT_RE.match(part)
        if not m:
            raise ValueError(f"Plage lot invalide « {part} » — utiliser ex. 1-10")
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            raise ValueError(f"Plage lot invalide « {part} » (début > fin)")
        out.append((a, b))
    if not out:
        raise ValueError("Aucun lot défini — ex. --lots 1-10,11-20")
    return out


def run_python_script(script: Path, extra: list[str], *, deadline: datetime | None) -> int:
    if should_stop(deadline):
        log("Heure limite atteinte avant sous-script.", "WARNING")
        return 2
    cmd = [sys.executable, str(script), *extra]
    log(f"   > {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
    return int(proc.returncode)


def find_newest_export_json(out_dir: Path) -> Path | None:
    candidates = sorted(
        out_dir.glob("rapport_partenaires_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def matching_xlsx(json_path: Path) -> Path | None:
    xlsx = json_path.with_suffix(".xlsx")
    return xlsx if xlsx.is_file() else None


def global_html_for_lot(start: int, end: int) -> Path | None:
    """Fichier global du lot (suffixe _P01_P10 ou sans suffixe si 1-20)."""
    gdir = ACTIVATION_DIR / "global"
    if not gdir.is_dir():
        return None
    suffix = f"_P{start:02d}_P{end:02d}"
    hits = sorted(gdir.glob(f"rapport_activation_global_*{suffix}.html"), reverse=True)
    if hits:
        return hits[0]
    if (start, end) == (1, 20):
        hits = sorted(gdir.glob("rapport_activation_global_*.html"), reverse=True)
        for p in hits:
            if "_P" not in p.stem:
                return p
    return None


def campaign_htmls_for_lot(start: int, end: int) -> list[Path]:
    cdir = ACTIVATION_DIR / "campagnes"
    if not cdir.is_dir():
        return []
    out: list[Path] = []
    for i in range(start, end + 1):
        p = cdir / f"P{i:02d}_rapport_activation.html"
        if p.is_file():
            out.append(p)
    return out


def build_lot_zip(
    *,
    lot_start: int,
    lot_end: int,
    run_stamp: str,
) -> Path:
    """ZIP plat : uniquement les HTML de rapport (global du lot + campagnes)."""
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    zip_name = f"rapport_soir_P{lot_start:02d}_P{lot_end:02d}_{run_stamp}.zip"
    zip_path = ZIP_DIR / zip_name

    global_html = global_html_for_lot(lot_start, lot_end)
    campaigns = campaign_htmls_for_lot(lot_start, lot_end)
    report_files: list[Path] = []
    if global_html:
        report_files.append(global_html)
    report_files.extend(campaigns)

    if not report_files:
        log(f"   Aucun rapport HTML pour le lot {lot_start}-{lot_end}.", "WARNING")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in report_files:
            zf.write(path, path.name)

    log(
        f"   ZIP lot {lot_start}-{lot_end} : {zip_path} "
        f"({len(report_files)} fichier(s) HTML, sans sous-dossiers)"
    )
    return zip_path


def send_email_with_zips(
    zip_paths: list[Path],
    *,
    lot_labels: list[str],
) -> bool:
    host = os.getenv("NIGHTLY_SMTP_HOST", "").strip()
    port_s = os.getenv("NIGHTLY_SMTP_PORT", "587").strip()
    user = os.getenv("NIGHTLY_SMTP_USER", "").strip()
    password = os.getenv("NIGHTLY_SMTP_PASSWORD", "")
    mail_from = os.getenv("NIGHTLY_EMAIL_FROM", user).strip()
    mail_to = os.getenv("NIGHTLY_EMAIL_TO", "").strip()

    if not host or not mail_to:
        log(
            "Email ignoré — définir NIGHTLY_SMTP_HOST et NIGHTLY_EMAIL_TO "
            "(optionnel : NIGHTLY_SMTP_USER, NIGHTLY_SMTP_PASSWORD, NIGHTLY_EMAIL_FROM).",
            "WARNING",
        )
        return False

    try:
        port = int(port_s)
    except ValueError:
        port = 587

    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = os.getenv("NIGHTLY_EMAIL_SUBJECT", f"Rapports soir UPJUNOO — {today}")
    body_lines = [
        "Rapports du soir UPJUNOO (export + activation).",
        "",
        f"Généré le : {today}",
        f"Lots : {', '.join(lot_labels)}",
        "",
        "Pièces jointes : un ZIP par lot (rapports HTML uniquement).",
        "",
        f"Log runner : {LOG_FILE}",
    ]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = [a.strip() for a in mail_to.split(",") if a.strip()]
    msg.set_content("\n".join(body_lines))

    for zp in zip_paths:
        msg.add_attachment(
            zp.read_bytes(),
            maintype="application",
            subtype="zip",
            filename=zp.name,
        )

    log(f"Envoi email -> {msg['To']} via {host}:{port}")
    with smtplib.SMTP(host, port, timeout=120) as smtp:
        smtp.ehlo()
        if port != 25:
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)
    log("Email envoyé.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rapports du soir : export → activation par lot → ZIP → email.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--start", type=int, default=1, help="Export : première campagne")
    parser.add_argument("--end", type=int, default=20, help="Export : dernière campagne")
    parser.add_argument(
        "--lots",
        default="1-10,11-20",
        help="Plages pour rapports HTML + ZIP (ex. 1-10,11-20)",
    )
    parser.add_argument("--headed", action="store_true", help="Navigateur visible (export)")
    parser.add_argument(
        "--until",
        metavar="HEURE",
        help="Heure limite 24h (08:30, 19h) — arrêt si dépassée",
    )
    parser.add_argument("--skip-export", action="store_true")
    parser.add_argument("--skip-activation", action="store_true")
    parser.add_argument("--skip-zip", action="store_true")
    parser.add_argument("--skip-email", action="store_true")
    parser.add_argument(
        "--input",
        type=Path,
        help="JSON export existant (avec --skip-export)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=STATE_FILE,
        help="state.json pour les recharges dans les rapports HTML",
    )
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Affiche le plan sans exécuter")

    args = parser.parse_args()
    out_dir = Path(args.output_dir)
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    deadline: datetime | None = None
    if args.until:
        try:
            deadline = parse_until_time(args.until)
        except ValueError as e:
            log(str(e), "ERROR")
            sys.exit(2)

    try:
        lots = parse_lots(args.lots)
    except ValueError as e:
        log(str(e), "ERROR")
        sys.exit(2)

    log("=" * 60)
    log("NIGHTLY REPORTS RUNNER")
    log(f"   Export campagnes {args.start} -> {args.end}")
    log(f"   Lots activation/ZIP : {args.lots}")
    if args.state.is_file():
        log(f"   State : {args.state.resolve()}")
    else:
        log(f"   State absent ({args.state}) — recharges HTML à 0", "WARNING")
    if deadline:
        log(f"   Heure limite : {deadline.strftime('%Y-%m-%d %H:%M')}")
    log("   Orchestrateur : NON (state.json mis à jour manuellement avant le run)")
    log("=" * 60)

    if args.dry_run:
        log("[dry-run] Étapes prévues :")
        if not args.skip_export:
            log(f"  1. export --start {args.start} --end {args.end}")
        else:
            log(f"  1. (skip export) input={args.input or 'dernier JSON'}")
        for a, b in lots:
            log(f"  2. activation --input … --start {a} --end {b}")
            log(f"  3. zip lot {a}-{b}")
        if not args.skip_email:
            log("  4. email (si variables SMTP)")
        sys.exit(0)

    json_path: Path | None = Path(args.input) if args.input else None

    try:
        if not args.skip_export:
            extra = [
                "--start",
                str(args.start),
                "--end",
                str(args.end),
                "--output-dir",
                str(out_dir),
            ]
            if args.headed:
                extra.append("--headed")
            rc = run_python_script(EXPORT_SCRIPT, extra, deadline=deadline)
            if rc != 0:
                log(f"Export échoué (code {rc})", "ERROR")
                sys.exit(rc)
            json_path = find_newest_export_json(out_dir)
            if not json_path:
                log("Aucun rapport_partenaires_*.json après export.", "ERROR")
                sys.exit(1)
            log(f"   Export JSON : {json_path}")
        else:
            if json_path is None:
                json_path = find_newest_export_json(out_dir)
            if not json_path or not json_path.is_file():
                log("Pas de JSON — passe --input ou lance sans --skip-export.", "ERROR")
                sys.exit(1)
            log(f"   JSON existant : {json_path}")

        xlsx_path = matching_xlsx(json_path)

        if not args.skip_activation:
            state_arg = str(Path(args.state).resolve())
            for lot_start, lot_end in lots:
                if should_stop(deadline):
                    log("Heure limite — arrêt avant fin des lots activation.", "WARNING")
                    sys.exit(2)
                extra = [
                    "--input",
                    str(json_path.resolve()),
                    "--state",
                    state_arg,
                    "--out",
                    str(ACTIVATION_DIR),
                    "--start",
                    str(lot_start),
                    "--end",
                    str(lot_end),
                ]
                rc = run_python_script(ACTIVATION_SCRIPT, extra, deadline=deadline)
                if rc != 0:
                    log(
                        f"Activation lot {lot_start}-{lot_end} échouée (code {rc})",
                        "ERROR",
                    )
                    sys.exit(rc)

        zip_paths: list[Path] = []
        lot_labels: list[str] = []

        if not args.skip_zip:
            for lot_start, lot_end in lots:
                if should_stop(deadline):
                    log("Heure limite — arrêt avant fin des ZIP.", "WARNING")
                    sys.exit(2)
                zp = build_lot_zip(
                    lot_start=lot_start,
                    lot_end=lot_end,
                    run_stamp=run_stamp,
                )
                zip_paths.append(zp)
                lot_labels.append(f"P{lot_start:02d}-P{lot_end:02d}")

        if not args.skip_email and zip_paths:
            send_email_with_zips(zip_paths, lot_labels=lot_labels)

        log("=" * 60)
        log("TERMINÉ")
        log(f"   JSON : {json_path}")
        if xlsx_path:
            log(f"   Excel : {xlsx_path}")
        if zip_paths:
            log(f"   ZIP : {ZIP_DIR}")
            for z in zip_paths:
                log(f"      - {z.name}")
        log(f"   Log : {LOG_FILE}")
        log("=" * 60)

    except KeyboardInterrupt:
        log("Interruption Ctrl+C", "WARNING")
        sys.exit(130)
    except Exception as e:
        log(f"Erreur : {e}", "ERROR")
        log(traceback.format_exc(), "ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()
