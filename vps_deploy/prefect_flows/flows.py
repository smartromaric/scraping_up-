"""
Flows Prefect : lancent les scripts Python de vps_deploy (cwd + chemins relatifs inchangés).

Paramètres visibles dans l’UI Prefect pour déclencher / planifier des runs.

Voir ``serve_prefect.py`` (docstring) pour démarrer le serveur / runner et utiliser l’UI.
"""

from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from prefect import flow, task
from prefect.logging import get_run_logger

VPS_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = VPS_DIR.parent


def build_script_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    entries = [str(PROJECT_ROOT)]
    if pythonpath:
        entries.append(pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def normalize_cli_args(cli_args: list[str] | str | None) -> list[str]:
    """
    Transforme ce que renvoie l’UI Prefect en argv pour ``subprocess``.

    Gère notamment un seul champ texte du type ``["--only", "Partenaire1"]``,
    ``[--only "Partenaire1"]`` (pseudo-liste), ou une vraie liste déjà correcte.
    """
    if cli_args is None:
        return []
    if isinstance(cli_args, str):
        s = cli_args.strip()
        return normalize_cli_args([s]) if s else []
    if not isinstance(cli_args, (list, tuple)):
        raise TypeError(f"cli_args inattendu: {type(cli_args)!r}")
    raw = [str(x) for x in cli_args if str(x).strip() != ""]
    if not raw:
        return []
    if len(raw) != 1:
        out: list[str] = []
        for a in raw:
            t = a.strip()
            if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
                t = t[1:-1]
            out.append(t)
        return out
    sole = raw[0].strip()
    if sole.startswith("[") and sole.endswith("]"):
        try:
            parsed = json.loads(sole)
            if isinstance(parsed, list) and parsed and all(isinstance(x, str) for x in parsed):
                return list(parsed)
        except json.JSONDecodeError:
            pass
        try:
            parsed = ast.literal_eval(sole)
            if isinstance(parsed, (list, tuple)) and parsed:
                return [str(x) for x in parsed]
        except (ValueError, SyntaxError):
            pass
        inner = sole[1:-1].strip()
        if inner:
            try:
                parts = shlex.split(inner)
                if parts:
                    return parts
            except ValueError:
                pass
    return [sole]


def _discover_allowed_scripts() -> frozenset[str]:
    allowed: set[str] = set()
    for p in VPS_DIR.glob("*.py"):
        if p.name.startswith("test_"):
            continue
        if p.name in ("serve_prefect.py",):
            continue
        allowed.add(p.name)
    return frozenset(allowed)


ALLOWED_SCRIPTS = _discover_allowed_scripts()

# Scripts qui ont déjà un flow avec champs dédiés dans l’UI (pas de second déploiement « générique »).
SCRIPTS_WITH_TYPED_UI = frozenset(
    {"sync_fleet_vps.py", "count_fleet_vps.py", "match_and_organize_vps.py"}
)


def _make_auto_script_flow(script_basename: str):
    stem = Path(script_basename).stem
    stem_safe = stem.replace("-", "_").replace(".", "_")

    def _impl(cli_args: list[str] | str | None = None):
        # Fermeture sur script_basename : un flow par fichier, seul cli_args est exposé dans l’UI.
        run_script_task(script_basename, cli_args)

    _impl.__name__ = f"flow_{stem_safe}"
    _impl.__qualname__ = f"flow_{stem_safe}"
    return flow(name=stem, log_prints=True)(_impl)


AUTO_SCRIPT_FLOWS: dict[str, object] = {
    script: _make_auto_script_flow(script)
    for script in sorted(ALLOWED_SCRIPTS)
    if script not in SCRIPTS_WITH_TYPED_UI
}


def _expose_auto_flows_as_module_attributes() -> None:
    """
    Prefect 3 résout les runs via un entrypoint ``module:attribut`` (ex. flow_kpi_partner_assignments).
    Les flows créés dynamiquement doivent exister sur le module, pas seulement dans un dict.
    """
    mod = sys.modules[__name__]
    for script, flow_obj in AUTO_SCRIPT_FLOWS.items():
        stem_safe = Path(script).stem.replace("-", "_").replace(".", "_")
        attr = f"flow_{stem_safe}"
        setattr(mod, attr, flow_obj)


@task(name="run-script-subprocess", retries=0)
def run_script_task(script: str, cli_args: list[str] | str | None = None) -> None:
    """Exécute ``python <script>`` depuis ``vps_deploy``."""
    logger = get_run_logger()
    args = normalize_cli_args(cli_args)
    if script not in ALLOWED_SCRIPTS:
        raise ValueError(
            f"Script non autorisé: {script!r}. "
            f"Utilise un .py présent dans vps_deploy (hors test_*, serve_prefect.py)."
        )
    script_path = VPS_DIR / script
    if not script_path.is_file():
        raise FileNotFoundError(str(script_path))
    cmd = [sys.executable, "-u", str(script_path), *args]
    logger.info("Exécution: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=str(VPS_DIR),
        env=build_script_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        logger.info(line.rstrip())
    returncode = proc.wait()
    if returncode != 0:
        raise RuntimeError(
            f"Le script s'est terminé avec le code {returncode}: {' '.join(cmd)}"
        )


@flow(name="run-vps-script", log_prints=True)
def run_vps_script(
    script: str = "count_fleet_vps.py",
    cli_args: list[str] | str | None = None,
) -> None:
    """
    Lance un script du dossier vps_deploy.

    Exemples d'arguments (liste ``cli_args``) : ``["--dry-run"]``, ``["--only", "Partenaire1"]``.
    """
    run_script_task(script, cli_args)


@flow(name="sync-fleet-vps", log_prints=True)
def sync_fleet_vps_flow(
    partner_only: str | None = None,
    dry_run: bool = False,
    skip_create: bool = False,
    start_from: str | None = None,
) -> None:
    """Enveloppe autour de ``sync_fleet_vps.py`` avec paramètres typés pour l’UI."""
    args: list[str] = []
    if partner_only:
        args.extend(["--only", partner_only])
    if dry_run:
        args.append("--dry-run")
    if skip_create:
        args.append("--skip-create")
    if start_from:
        args.extend(["--start", start_from])
    run_script_task("sync_fleet_vps.py", args)


@flow(name="count-fleet-vps", log_prints=True)
def count_fleet_vps_flow(start: int | None = None, end: int | None = None) -> None:
    """Enveloppe autour de ``count_fleet_vps.py``."""
    args: list[str] = []
    if start is not None:
        args.extend(["--start", str(start)])
    if end is not None:
        args.extend(["--end", str(end)])
    run_script_task("count_fleet_vps.py", args)


@flow(name="match-and-organize-vps", log_prints=True)
def match_and_organize_vps_flow() -> None:
    """Pipeline matching / organisation (sans args CLI)."""
    run_script_task("match_and_organize_vps.py", [])


def build_all_deployments():
    """Tous les déploiements servis par ``serve_prefect.py`` (un par script + outils)."""
    deps = [
        run_vps_script.to_deployment(
            name="run-vps-script",
            tags=["vps", "meta"],
            description="Choix libre du .py + arguments CLI (liste cli_args).",
        ),
        sync_fleet_vps_flow.to_deployment(
            name="sync-fleet-vps",
            tags=["vps", "selenium"],
            description="Champs dédiés : partenaire, dry-run, skip-create, reprise --start.",
        ),
        count_fleet_vps_flow.to_deployment(
            name="count-fleet-vps",
            tags=["vps", "selenium"],
            description="Plage partenaires : start / end.",
        ),
        match_and_organize_vps_flow.to_deployment(
            name="match-and-organize-vps",
            tags=["vps"],
            description="Matching + dossiers par partenaire (sans args).",
        ),
    ]
    for script, flow_obj in sorted(AUTO_SCRIPT_FLOWS.items()):
        stem = Path(script).stem
        deps.append(
            flow_obj.to_deployment(
                name=stem,
                tags=["vps", "script"],
                description=f'Équivalent à : python {script} … — passe les options du script dans cli_args.',
            )
        )
    return deps


_expose_auto_flows_as_module_attributes()
