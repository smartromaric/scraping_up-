#!/usr/bin/env python3
"""
schedule_reports_task.py
========================

Deux usages possibles :

1) **Installation Windows (schtasks)** — comme une tâche planifiée OS :
   tu passes ``--at 17:00`` ; le Planificateur lance le script chaque jour.

2) **Attente puis exécution (comme sweep_delete_non_uploaded_fleet_vps.py)** —
   tu passes ``--wait-from 17:00`` ; ce processus Python **attend** jusqu’à
   cette heure (prochain créneau calendaire), puis **lance** le script cible
   (``--script`` / ``--extra``). Fonctionne aussi sur Linux / VPS (pas de schtasks).

Tu mets à jour ``output/partner_automation/state.json`` avant l’heure d’exécution.

Exemples :

  # Windows — enregistrer la tâche quotidienne à 17:00
  python schedule_reports_task.py --at 17:00

  # N’importe quel OS — lancer le terminal à 15h, le script attend jusqu’à 17h puis exécute
  python schedule_reports_task.py --wait-from 17:00

  # Même logique d’heure flexible que le sweep : 17h, 17:00, 9h30
  python schedule_reports_task.py --wait-from 17h --script export_partner_fleet_drivers_report.py ^
      --extra "--start 1 --end 20"

  # Fin de planification schtasks (Windows uniquement, avec --at)
  python schedule_reports_task.py --at 17:00 --until-date 2026-12-31

Prérequis schtasks : souvent « Exécuter en tant qu’administrateur ».
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TASK_NAME = "Rapports Soir UPJUNOO"
DEFAULT_SCRIPT = SCRIPT_DIR / "nightly_reports_runner.py"
_TIME_AT_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_WAIT_FROM_RE = re.compile(
    r"^(\d{1,2})(?:(?:h|H|:)(\d{2}))?\s*(?:h|H)?$",
    re.IGNORECASE,
)


def parse_at(value: str) -> str:
    """Valide HH:MM (24h) pour schtasks /ST."""
    raw = (value or "").strip()
    m = _TIME_AT_RE.match(raw)
    if not m:
        raise argparse.ArgumentTypeError(
            f"Heure invalide « {value} » — pour --at utiliser HH:MM (ex. 17:00, 09:30)."
        )
    h, mn = int(m.group(1)), int(m.group(2))
    if h > 23 or mn > 59:
        raise argparse.ArgumentTypeError(f"Heure hors plage : {value}")
    return f"{h:02d}:{mn:02d}"


def parse_clock_parts(value: str) -> tuple[int, int]:
    """Heure 24h pour --wait-from : 08:30, 15h15, 14, 16h (comme le sweep)."""
    raw = (value or "").strip().replace(" ", "")
    m = _WAIT_FROM_RE.match(raw)
    if not m:
        raise argparse.ArgumentTypeError(
            f"Heure invalide « {value} » — formats : 08:30, 15h15, 14, 16h (24h)"
        )
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise argparse.ArgumentTypeError(f"Heure hors plage 24h : {hour}:{minute:02d}")
    return hour, minute


def next_wait_from_datetime(value: str, *, now: datetime | None = None) -> datetime:
    """Prochain instant calendaire à cette heure:minute (strictement > maintenant si égal, lendemain)."""
    now = now or datetime.now()
    hour, minute = parse_clock_parts(value)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def format_deadline(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def time_remaining(deadline: datetime) -> str:
    sec = max(0, int((deadline - datetime.now()).total_seconds()))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}min"
    if m:
        return f"{m}min {s:02d}s"
    return f"{s}s"


def wait_until(target: datetime) -> None:
    """Attente active par petits sommeils (même principe que sweep_delete_non_uploaded_fleet_vps)."""
    print(
        f"⏳ Attente jusqu'à {format_deadline(target)} "
        f"({time_remaining(target)})…",
        flush=True,
    )
    while datetime.now() < target:
        remaining = int((target - datetime.now()).total_seconds())
        time.sleep(min(30, max(1, remaining)))
    print("▶ Heure atteinte — lancement de la commande.", flush=True)


def parse_until_date(value: str) -> str:
    """YYYY-MM-MM → chaîne /ED pour schtasks."""
    raw = (value or "").strip()
    try:
        d = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"Date invalide « {value} » — utiliser YYYY-MM-DD (ex. 2026-12-31)."
        ) from e
    return f"{d.month}/{d.day}/{d.year}"


def task_exists(task_name: str) -> bool:
    proc = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def quote_tr_part(path: Path | str) -> str:
    s = str(path)
    if " " in s and not (s.startswith('"') and s.endswith('"')):
        return f'"{s}"'
    return s


def build_default_tr(*, python_exe: Path, script_path: Path, extra: str) -> str:
    py = quote_tr_part(python_exe)
    sc = quote_tr_part(script_path)
    tail = (extra or "").strip()
    if tail:
        return f"{py} {sc} {tail}"
    return f"{py} {sc}"


def run_schtasks(args: list[str], *, dry_run: bool) -> int:
    if dry_run:
        print("[dry-run] " + subprocess.list2cmdline(args))
        return 0
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    if proc.returncode != 0:
        print(
            f"Échec schtasks (code {proc.returncode}). "
            "Réessaie en invite « Exécuter en tant qu’administrateur » si besoin.",
            file=sys.stderr,
        )
    return proc.returncode


def build_run_argv(*, python_exe: Path, script_path: Path, extra: str) -> list[str]:
    argv = [str(python_exe), str(script_path.resolve())]
    tail = (extra or "").strip()
    if tail:
        argv.extend(shlex.split(tail, posix=sys.platform != "win32"))
    return argv


def run_wait_and_execute(
    *,
    python_exe: Path,
    script_path: Path,
    extra: str,
    wait_from: str,
    dry_run: bool,
) -> int:
    target = next_wait_from_datetime(wait_from)
    if dry_run:
        print(f"[dry-run] Attendreait jusqu'à {format_deadline(target)} puis :")
        print("[dry-run] " + subprocess.list2cmdline(build_run_argv(
            python_exe=python_exe, script_path=script_path, extra=extra,
        )))
        return 0
    wait_until(target)
    argv = build_run_argv(python_exe=python_exe, script_path=script_path, extra=extra)
    print("Commande : " + subprocess.list2cmdline(argv), flush=True)
    proc = subprocess.run(argv, cwd=str(SCRIPT_DIR))
    return int(proc.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Planifie les rapports : soit tâche Windows (--at + schtasks), "
            "soit attente jusqu'à une heure puis exécution (--wait-from), "
            "sur le modèle de sweep_delete_non_uploaded_fleet_vps.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--at",
        dest="at_time",
        type=parse_at,
        default=None,
        metavar="HH:MM",
        help="Heure quotidienne pour schtasks (Windows), ex. 17:00",
    )
    parser.add_argument(
        "--wait-from",
        default=None,
        metavar="HEURE",
        help=(
            "Attendre jusqu'à cette heure (24h, formats 17, 17h, 17:00) puis lancer "
            "--script (comme le sweep : un processus qui attend puis agit)."
        ),
    )
    parser.add_argument(
        "--task-name",
        default=DEFAULT_TASK_NAME,
        help=f"Nom de la tâche schtasks (défaut: {DEFAULT_TASK_NAME!r})",
    )
    parser.add_argument(
        "--until-date",
        type=parse_until_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Avec --at uniquement : dernier jour de la tâche schtasks (inclus)",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Interpréteur Python (défaut: celui qui exécute ce script)",
    )
    parser.add_argument(
        "--script",
        type=Path,
        default=DEFAULT_SCRIPT,
        help=f"Script .py à lancer (défaut: {DEFAULT_SCRIPT.name})",
    )
    parser.add_argument(
        "--extra",
        default="",
        metavar="ARGS",
        help="Arguments supplémentaires pour le script (ex. --until 19:00 --headed)",
    )
    parser.add_argument(
        "--tr",
        default=None,
        metavar="LIGNE",
        help="Ligne /TR complète pour schtasks uniquement (remplace python/script/extra)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les actions sans schtasks ni attente réelle (--wait-from simule la cible)",
    )

    args = parser.parse_args()

    if args.wait_from and args.at_time:
        parser.error("Choisis soit --wait-from (attente puis run), soit --at (schtasks), pas les deux.")

    if not args.wait_from and not args.at_time:
        parser.error("Indique --at (installation Windows) ou --wait-from (attente puis exécution).")

    if args.wait_from:
        if not args.script.is_file():
            print(
                f"Avertissement : « {args.script} » introuvable. "
                "Crée le fichier ou passe --script.",
                file=sys.stderr,
            )
        rc = run_wait_and_execute(
            python_exe=args.python,
            script_path=args.script,
            extra=args.extra,
            wait_from=args.wait_from.strip(),
            dry_run=args.dry_run,
        )
        sys.exit(rc)

    # Mode schtasks (--at)
    if sys.platform != "win32":
        print(
            "Le mode --at (schtasks) est réservé à Windows. "
            "Sur Linux / VPS utilise :  python schedule_reports_task.py --wait-from 17:00 ...\n"
            "Ou une ligne cron : 0 17 * * * cd ... && python3 nightly_reports_runner.py",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.tr:
        tr = args.tr.strip()
    else:
        if not args.script.is_file():
            print(
                f"Avertissement : le script « {args.script} » est introuvable. "
                "Crée-le ou passe --script / --tr.",
                file=sys.stderr,
            )
        tr = build_default_tr(
            python_exe=args.python,
            script_path=args.script.resolve(),
            extra=args.extra,
        )

    tn = args.task_name.strip()
    st = args.at_time
    assert st is not None

    exists = task_exists(tn)

    if exists:
        cmd = ["schtasks", "/Change", "/TN", tn, "/ST", st, "/TR", tr]
        if args.until_date:
            cmd.extend(["/ED", args.until_date])
        rc = run_schtasks(cmd, dry_run=args.dry_run)
        if rc == 0 and not args.dry_run:
            print(f"Tâche « {tn} » mise à jour — quotidien à {st}.")
            print(f"Action : {tr}")
    else:
        cmd = [
            "schtasks",
            "/Create",
            "/SC",
            "DAILY",
            "/TN",
            tn,
            "/TR",
            tr,
            "/ST",
            st,
            "/F",
        ]
        if args.until_date:
            cmd.extend(["/ED", args.until_date])
        rc = run_schtasks(cmd, dry_run=args.dry_run)
        if rc == 0 and not args.dry_run:
            print(f"Tâche « {tn} » créée — quotidien à {st}.")
            print(f"Action : {tr}")

    if rc != 0:
        sys.exit(rc)


if __name__ == "__main__":
    main()
