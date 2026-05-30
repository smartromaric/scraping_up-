"""Lancement des scripts et suivi de progression (logs + statut JSON)."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from partner_dashboard.config import (
    ACTIVATION_SCRIPT,
    DASHBOARD_DIR,
    DOWNLOAD_GODSEYE_SCRIPT,
    JOB_STATUS_FILE,
    NIGHTLY_SCRIPT,
    ORCHESTRATOR_SCRIPT,
    OUTPUT_DIR,
    PAYMENT_SYNC_SCRIPT,
    SCRIPT_DIR,
)
from partner_dashboard.metrics import find_best_export_json

# Export / orchestrateur : "── Campagne 1/20 (n°1) ──" ou "▶ Campagne 1/20 (n°1)"
_CAMPAIGN_RE = re.compile(r"Campagne\s+(\d+)\s*/\s*(\d+)", re.I)
_ORCH_INDEX_RE = re.compile(r"\(n°\s*(\d+)\)", re.I)
_PARTNER_START_RE = re.compile(r"PARTENAIRE\s+(\d+)", re.I)
_NIGHTLY_PHASE_RE = re.compile(
    r"(NIGHTLY|Export|Activation lot|TERMINÉ|ZIP lot|rapport_partenaires)",
    re.I,
)
_ZIP_LOT_RE = re.compile(r"ZIP lot\s+(\d+)-(\d+)", re.I)


@dataclass
class JobStatus:
    job_id: str
    kind: str
    status: str = "pending"
    phase: str = "init"
    phase_label: str = "Initialisation"
    progress_pct: int = 0
    campaign_current: int | None = None
    campaign_total: int | None = None
    started_at: str = ""
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    headed: bool = False
    campaign_start: int = 1
    campaign_end: int = 20

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Termine le processus et ses enfants (Selenium, sous-scripts Python)."""
    if proc.poll() is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
                creationflags=flags,
            )
        else:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _parse_campaign_range(extra: list[str]) -> tuple[int, int, int]:
    start, end = 1, 20
    try:
        if "--start" in extra:
            start = int(extra[extra.index("--start") + 1])
        if "--end" in extra:
            end = int(extra[extra.index("--end") + 1])
    except (ValueError, IndexError):
        pass
    total = max(1, end - start + 1)
    return start, end, total


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: JobStatus | None = None
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._listeners: list[Callable[[JobStatus], None]] = []
        DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    def subscribe(self, cb: Callable[[JobStatus], None]) -> None:
        self._listeners.append(cb)

    def _notify(self, job: JobStatus) -> None:
        for cb in self._listeners:
            try:
                cb(job)
            except Exception:
                pass
        self._persist(job)

    def _persist(self, job: JobStatus) -> None:
        with self._lock:
            try:
                JOB_STATUS_FILE.write_text(
                    json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return self._current.to_dict() if self._current else None

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._current and self._current.status == "running")

    def _append_log(self, job: JobStatus, line: str, max_lines: int = 400) -> None:
        job.logs.append(line)
        if len(job.logs) > max_lines:
            job.logs = job.logs[-max_lines:]
        self._update_from_line(job, line)

    def _set_campaign_progress(
        self,
        job: JobStatus,
        *,
        slot_pos: int,
        total: int,
        camp_num: int | None = None,
    ) -> None:
        total = max(1, total)
        slot_pos = max(1, min(slot_pos, total))
        job.campaign_current = slot_pos
        job.campaign_total = total
        num = camp_num if camp_num is not None else slot_pos
        if job.kind == "orchestrator":
            if job.phase in ("payment_sync", "orchestrator_done"):
                job.phase = "payment_sync"
                job.phase_label = f"Sync paiements admin — campagne {num} ({slot_pos}/{total})"
                pct = 82 + int(slot_pos / total * 16)
            else:
                job.phase = "orchestrator"
                job.phase_label = f"Orchestrateur — campagne {num} ({slot_pos}/{total})"
                pct = int(slot_pos / total * 80)
        elif job.kind == "nightly":
            job.phase = "export"
            job.phase_label = f"Export admin — campagne {slot_pos}/{total}"
            pct = int(slot_pos / total * 72)
        elif job.kind == "zip":
            job.phase = "zip"
            job.phase_label = f"ZIP — lot en cours ({slot_pos}/{total})"
            pct = int(slot_pos / total * 90)
        else:
            job.phase_label = f"Campagne {slot_pos}/{total}"
            pct = int(slot_pos / total * 50)
        job.progress_pct = max(job.progress_pct, min(95, pct))

    def _update_from_line(self, job: JobStatus, line: str) -> None:
        m = _CAMPAIGN_RE.search(line)
        if m:
            slot_pos = int(m.group(1))
            total = int(m.group(2))
            idx_m = _ORCH_INDEX_RE.search(line)
            camp_num = int(idx_m.group(1)) if idx_m else None
            self._set_campaign_progress(
                job,
                slot_pos=slot_pos,
                total=total,
                camp_num=camp_num,
            )

        if job.kind == "orchestrator":
            pm = _PARTNER_START_RE.search(line)
            if pm and not m:
                idx = int(pm.group(1))
                total = job.campaign_total or max(
                    1,
                    job.campaign_end - job.campaign_start + 1,
                )
                slot_pos = idx - job.campaign_start + 1
                if 1 <= slot_pos <= total:
                    self._set_campaign_progress(
                        job,
                        slot_pos=slot_pos,
                        total=total,
                        camp_num=idx,
                    )
            if "BILAN |" in line and job.phase != "payment_sync":
                job.phase = "orchestrator_done"
                job.phase_label = "Orchestrateur terminé — sync paiements…"
                job.progress_pct = max(job.progress_pct, 82)

        if job.kind in ("orchestrator", "payment_sync"):
            if "SYNC HISTORIQUE PAIEMENTS" in line.upper():
                job.phase = "payment_sync"
                job.phase_label = "Sync historique paiements → state.json"
                job.progress_pct = max(job.progress_pct, 84)
            if "PARTENAIRE " in line and job.phase == "payment_sync":
                pm = _PARTNER_START_RE.search(line)
                if pm:
                    idx = int(pm.group(1))
                    total = job.campaign_total or max(
                        1,
                        job.campaign_end - job.campaign_start + 1,
                    )
                    slot_pos = idx - job.campaign_start + 1
                    if 1 <= slot_pos <= total:
                        self._set_campaign_progress(
                            job,
                            slot_pos=slot_pos,
                            total=total,
                            camp_num=idx,
                        )
            if "CROISEMENT RAPPORT" in line.upper():
                job.phase_label = "Croisement paiements ↔ state.json"
                job.progress_pct = max(job.progress_pct, 98)

        if "NIGHTLY REPORTS" in line.upper():
            job.phase = "nightly"
            job.phase_label = "Rapports du soir — démarrage"
            job.progress_pct = max(job.progress_pct, 5)
        if "Export campagnes" in line or "export_partner" in line.lower():
            job.phase = "export"
            job.phase_label = "Export admin (Selenium)"
            job.progress_pct = max(job.progress_pct, 10)
        if "Activation lot" in line:
            job.phase = "activation"
            job.phase_label = "Génération rapports HTML"
            job.progress_pct = max(job.progress_pct, 75)
        zm = _ZIP_LOT_RE.search(line)
        if zm:
            job.phase = "zip"
            lot_end = int(zm.group(2))
            job.phase_label = f"ZIP lot {zm.group(1)}-{zm.group(2)} (global + campagnes)"
            if lot_end <= 10:
                job.progress_pct = max(job.progress_pct, 50)
            else:
                job.progress_pct = max(job.progress_pct, 95)
        elif "ZIP lot" in line:
            job.phase = "zip"
            job.phase_label = "Création des archives ZIP"
            job.progress_pct = max(job.progress_pct, 90)
        if job.kind != "orchestrator" and (
            "TERMINÉ" in line.upper() or "Email envoyé" in line
        ):
            job.phase = "done"
            job.phase_label = "Terminé"
            job.progress_pct = 100
        if job.kind == "orchestrator" and (
            re.search(r"Log:\s+.*orchestrator\.log", line, re.I)
        ):
            if job.phase != "payment_sync":
                job.phase = "orchestrator_done"
                job.phase_label = "Orchestrateur terminé — sync paiements…"
                job.progress_pct = max(job.progress_pct, 82)

        if job.kind == "godseye":
            if "Connexion admin" in line or "admin_login" in line.lower():
                job.phase = "login"
                job.phase_label = "Connexion admin UpJunoo"
                job.progress_pct = max(job.progress_pct, 15)
            if "Vue cartographique" in line or "gods_eye" in line.lower():
                job.phase = "map"
                job.phase_label = "Vue cartographique Godseye"
                job.progress_pct = max(job.progress_pct, 35)
            if "En ligne" in line and "sélectionn" in line.lower():
                job.phase = "filter"
                job.phase_label = "Filtre conducteurs en ligne"
                job.progress_pct = max(job.progress_pct, 55)
            if "Exporter Excel" in line or "Attente du fichier" in line:
                job.phase = "export"
                job.phase_label = "Export Excel Godseye"
                job.progress_pct = max(job.progress_pct, 75)
            if "Fichier téléchargé" in line or "drivers-godseye" in line.lower():
                job.phase = "done"
                job.phase_label = "Export Godseye terminé"
                job.progress_pct = max(job.progress_pct, 95)

    def _set_job(self, job: JobStatus) -> None:
        with self._lock:
            self._current = job
        self._notify(job)

    def stop(self) -> bool:
        with self._lock:
            proc = self._proc
            job = self._current
        if not job or job.status != "running":
            return False
        self._append_log(job, "[STOP] Arrêt demandé — fermeture de l'arbre de processus…")
        self._notify(job)
        if proc and proc.poll() is None:
            _kill_process_tree(proc)
        job.status = "cancelled"
        job.phase = "cancelled"
        job.phase_label = "Arrêté par l'utilisateur"
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        job.exit_code = -1
        self._append_log(job, "[STOP] Tâche annulée.")
        self._notify(job)
        return True

    def start_nightly(
        self,
        *,
        headed: bool = False,
        skip_email: bool = True,
        skip_zip: bool = False,
        lots: str = "1-10,11-20",
        start: int = 1,
        end: int = 20,
    ) -> dict[str, Any]:
        if self.is_running():
            return {"ok": False, "error": "Un job est déjà en cours."}
        extra = [
            "--start",
            str(start),
            "--end",
            str(end),
            "--lots",
            lots,
        ]
        if headed:
            extra.append("--headed")
        if skip_email:
            extra.append("--skip-email")
        if skip_zip:
            extra.append("--skip-zip")
        label = "Rapports du soir"
        if not skip_zip:
            label += " + ZIP"
        return self._start(
            kind="nightly",
            script=NIGHTLY_SCRIPT,
            extra=extra,
            phase_label=label,
            campaign_start=start,
            campaign_end=end,
        )

    def start_zip_only(self, *, lots: str = "1-10,11-20") -> dict[str, Any]:
        if self.is_running():
            return {"ok": False, "error": "Un job est déjà en cours."}
        extra = [
            "--skip-export",
            "--skip-activation",
            "--skip-email",
            "--lots",
            lots,
        ]
        return self._start(
            kind="zip",
            script=NIGHTLY_SCRIPT,
            extra=extra,
            phase_label="Archives ZIP par lot",
        )

    def start_orchestrator(
        self,
        *,
        start: int = 1,
        end: int = 20,
        headed: bool = False,
    ) -> dict[str, Any]:
        if self.is_running():
            return {"ok": False, "error": "Un job est déjà en cours."}
        extra = ["--start", str(start), "--end", str(end)]
        if headed:
            extra.append("--headed")
        return self._start(
            kind="orchestrator",
            script=ORCHESTRATOR_SCRIPT,
            extra=extra,
            phase_label="Orchestrateur + sync paiements admin",
            campaign_start=start,
            campaign_end=end,
            follow_scripts=[(PAYMENT_SYNC_SCRIPT, extra)],
        )

    def start_godseye_download(self, *, headed: bool = False) -> dict[str, Any]:
        if self.is_running():
            return {"ok": False, "error": "Un job est déjà en cours."}
        extra = ["--output-dir", str(OUTPUT_DIR.resolve())]
        if headed:
            extra.append("--headed")
        return self._start(
            kind="godseye",
            script=DOWNLOAD_GODSEYE_SCRIPT,
            extra=extra,
            phase_label="Téléchargement Godseye (en ligne)",
        )

    def start_activation_only(self, json_path: Path) -> dict[str, Any]:
        if self.is_running():
            return {"ok": False, "error": "Un job est déjà en cours."}
        extra = [
            "--input",
            str(json_path.resolve()),
            "--state",
            str((OUTPUT_DIR / "state.json").resolve()),
            "--out",
            str((OUTPUT_DIR / "rapports_activation").resolve()),
            "--start",
            "1",
            "--end",
            "20",
        ]
        return self._start(
            kind="activation",
            script=ACTIVATION_SCRIPT,
            extra=extra,
            phase_label="Génération HTML",
        )

    def _start(
        self,
        *,
        kind: str,
        script: Path,
        extra: list[str],
        phase_label: str,
        campaign_start: int | None = None,
        campaign_end: int | None = None,
        follow_scripts: list[tuple[Path, list[str]]] | None = None,
    ) -> dict[str, Any]:
        c_start, c_end, c_total = _parse_campaign_range(extra)
        if campaign_start is not None:
            c_start = campaign_start
        if campaign_end is not None:
            c_end = campaign_end
        c_total = max(1, c_end - c_start + 1)

        job_id = uuid.uuid4().hex[:12]
        job = JobStatus(
            job_id=job_id,
            kind=kind,
            status="running",
            phase="init",
            phase_label=phase_label,
            progress_pct=2,
            campaign_total=c_total,
            campaign_start=c_start,
            campaign_end=c_end,
            started_at=datetime.now().isoformat(timespec="seconds"),
            headed="--headed" in extra,
        )
        self._set_job(job)

        def runner() -> None:
            scripts_to_run: list[tuple[Path, list[str]]] = [(script, extra)]
            scripts_to_run.extend(follow_scripts or [])
            final_rc = 0

            for step_idx, (step_script, step_extra) in enumerate(scripts_to_run):
                if job.status == "cancelled":
                    break
                if step_idx > 0 and final_rc != 0:
                    self._append_log(
                        job,
                        f"[SKIP] Étape suivante ignorée (code {final_rc})",
                    )
                    break
                if step_idx > 0:
                    job.phase = "payment_sync"
                    job.phase_label = "Sync historique paiements → state.json"
                    job.progress_pct = max(job.progress_pct, 84)
                    self._notify(job)

                cmd = [sys.executable, str(step_script), *step_extra]
                self._append_log(job, f">>> {' '.join(cmd)}")
                try:
                    env = os.environ.copy()
                    env.setdefault("PYTHONIOENCODING", "utf-8")
                    env.setdefault("PYTHONUTF8", "1")
                    popen_kw: dict[str, Any] = {
                        "cwd": str(SCRIPT_DIR),
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.STDOUT,
                        "text": True,
                        "encoding": "utf-8",
                        "errors": "replace",
                        "bufsize": 1,
                        "env": env,
                    }
                    if sys.platform != "win32":
                        popen_kw["start_new_session"] = True
                    proc = subprocess.Popen(cmd, **popen_kw)
                    with self._lock:
                        self._proc = proc
                    if proc.stdout:
                        for line in proc.stdout:
                            line = line.rstrip()
                            if line:
                                self._append_log(job, line)
                                self._notify(job)
                    final_rc = proc.wait()
                except Exception as e:
                    final_rc = 1
                    job.status = "failed"
                    job.error = str(e)
                    job.finished_at = datetime.now().isoformat(timespec="seconds")
                    self._append_log(job, f"ERREUR: {e}")
                    self._notify(job)
                    break

            job.exit_code = final_rc
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            if job.status == "cancelled":
                pass
            elif final_rc == 0:
                job.status = "completed"
                job.phase = "done"
                suffix = " + sync paiements" if follow_scripts else ""
                job.phase_label = f"Terminé avec succès{suffix}"
                job.progress_pct = 100
            elif job.status != "failed":
                job.status = "failed"
                job.phase = "error"
                job.phase_label = f"Échec (code {final_rc})"
                job.error = f"Code de sortie {final_rc}"
            self._notify(job)
            with self._lock:
                self._proc = None

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        return {"ok": True, "job_id": job_id}

    def tail_log_files(self, job: JobStatus) -> None:
        """Optionnel : pourrait être étendu pour suivre nightly_runner.log en parallèle."""
        pass


run_manager = RunManager()
