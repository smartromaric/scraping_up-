"""API FastAPI — dashboard partenaires UPJUNOO."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from partner_dashboard.chauffeurs_actifs_service import (
    archive_all_timestamped,
    find_latest_chauffeurs_xlsx,
    generate_chauffeurs_xlsx,
    godseye_status,
    list_chauffeurs_archives,
)
from partner_dashboard.config import (
    ACTIVATION_DIR,
    CHAUFFEURS_ARCHIVE_DIR,
    HOST,
    OUTPUT_DIR,
    PORT,
    SCRIPT_DIR,
    STATE_FILE,
    STATIC_DIR,
    ZIP_DIR,
)
from partner_dashboard.metrics import (
    build_dashboard_payload,
    campaign_metrics,
    find_best_export_json,
    list_export_reports,
    load_report,
)
from partner_dashboard.recharge_service import (
    compare_states,
    export_recharge_csv_rows,
    export_state_timestamped,
    is_valid_state_upload_filename,
    list_drivers_filtered,
    list_partners_for_filter,
    list_partners_pending,
    list_to_recharge,
    load_state_dict,
    mark_drivers_recharged,
    merge_uploaded_into_current,
    parse_uploaded_state,
    recharge_summary,
    state_export_filename,
)
from partner_dashboard.run_manager import run_manager
from partner_dashboard.scheduler import auto_scheduler

from generate_activation_report import (
    PartnerRechargeData,
    build_recharge_index,
    load_state,
    partners_indexed,
)

app = FastAPI(title="UPJUNOO Partner Dashboard", version="1.0.0")

_sse_queues: list[asyncio.Queue[str]] = []
_main_loop: asyncio.AbstractEventLoop | None = None


def _broadcast_sse(payload: dict[str, Any]) -> None:
    line = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    dead: list[asyncio.Queue[str]] = []
    for q in _sse_queues:
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        if q in _sse_queues:
            _sse_queues.remove(q)


def _on_job_update(job) -> None:
    payload = {"type": "job", "job": job.to_dict()}
    if _main_loop and _main_loop.is_running():
        _main_loop.call_soon_threadsafe(_broadcast_sse, payload)


run_manager.subscribe(_on_job_update)


class NightlyRunBody(BaseModel):
    headed: bool = False
    skip_email: bool = True
    skip_zip: bool = False
    lots: str = "1-10,11-20"
    start: int = Field(1, ge=1, le=20)
    end: int = Field(20, ge=1, le=20)


class ZipOnlyRunBody(BaseModel):
    lots: str = "1-10,11-20"


class OrchestratorRunBody(BaseModel):
    headed: bool = False
    start: int = Field(1, ge=1, le=20)
    end: int = Field(20, ge=1, le=20)


class GodseyeRunBody(BaseModel):
    headed: bool = False


class MarkRechargedBody(BaseModel):
    match_keys: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    partner_index: int | None = Field(None, ge=1, le=20)
    all_pending_in_partner: bool = False


class SchedulerBody(BaseModel):
    enabled: bool | None = None
    time: str | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    auto_scheduler.start_background()


@app.on_event("shutdown")
def _shutdown() -> None:
    auto_scheduler.stop()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "partner-dashboard"}


@app.get("/api/dashboard")
def dashboard_data(
    report: str | None = Query(None, description="Chemin ou nom du fichier JSON export"),
) -> dict[str, Any]:
    path: Path | None = None
    if report:
        candidate = Path(report)
        if not candidate.is_file():
            candidate = OUTPUT_DIR / report
        if candidate.is_file():
            path = candidate
    return build_dashboard_payload(path)


@app.get("/api/reports/json")
def list_json_reports() -> list[dict[str, Any]]:
    return list_export_reports()


@app.get("/api/campaign/{index}")
def campaign_detail(
    index: int,
    report: str | None = Query(None),
) -> dict[str, Any]:
    path = Path(report) if report else None
    if path and not path.is_file():
        path = OUTPUT_DIR / (report or "")
    report_data = load_report(path if (path and path.is_file()) else None)
    if not report_data:
        raise HTTPException(404, "Aucun rapport JSON disponible.")
    state = load_state(STATE_FILE) if STATE_FILE.is_file() else {}
    recharge_index = build_recharge_index(state) if state else {}
    by_idx = partners_indexed(report_data)
    if index not in by_idx:
        raise HTTPException(404, f"Campagne {index} introuvable.")
    p = by_idx[index]
    rd = recharge_index.get(index) or PartnerRechargeData()
    return {
        "metrics": campaign_metrics(p, rd),
        "partner": {
            "index": p.get("index"),
            "name": p.get("name"),
            "email": p.get("email"),
            "vehicles_count": p.get("vehicles_count"),
            "drivers_count": p.get("drivers_count"),
            "vehicles_by_status": p.get("vehicles_by_status"),
            "drivers_by_status": p.get("drivers_by_status"),
            "errors": p.get("errors"),
        },
    }


@app.get("/api/reports/html")
def list_html_reports() -> dict[str, Any]:
    global_dir = ACTIVATION_DIR / "global"
    camp_dir = ACTIVATION_DIR / "campagnes"
    global_files = sorted(global_dir.glob("*.html"), reverse=True) if global_dir.is_dir() else []
    camp_files = sorted(camp_dir.glob("P*_rapport_activation.html")) if camp_dir.is_dir() else []
    return {
        "global": [{"name": p.name, "path": f"/api/reports/html/file/{p.relative_to(ACTIVATION_DIR).as_posix()}"} for p in global_files[:5]],
        "campaigns": [{"name": p.name, "path": f"/api/reports/html/file/{p.relative_to(ACTIVATION_DIR).as_posix()}"} for p in camp_files],
    }


@app.get("/api/reports/html/file/{file_path:path}")
def serve_html_report(file_path: str) -> FileResponse:
    target = (ACTIVATION_DIR / file_path).resolve()
    if not str(target).startswith(str(ACTIVATION_DIR.resolve())):
        raise HTTPException(403, "Chemin interdit.")
    if not target.is_file() or target.suffix.lower() != ".html":
        raise HTTPException(404, "Fichier introuvable.")
    return FileResponse(target, media_type="text/html; charset=utf-8")


@app.get("/api/reports/zip")
def list_zip_reports() -> dict[str, Any]:
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(ZIP_DIR.glob("rapport_soir_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for p in files[:20]:
        st = p.stat()
        items.append(
            {
                "name": p.name,
                "size_kb": round(st.st_size / 1024),
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "path": f"/api/reports/zip/file/{p.name}",
            }
        )
    return {"dir": str(ZIP_DIR), "files": items}


@app.get("/api/reports/zip/file/{filename}")
def serve_zip_report(filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(403, "Nom de fichier invalide.")
    target = (ZIP_DIR / filename).resolve()
    if not str(target).startswith(str(ZIP_DIR.resolve())):
        raise HTTPException(403, "Chemin interdit.")
    if not target.is_file() or target.suffix.lower() != ".zip":
        raise HTTPException(404, "Archive introuvable.")
    return FileResponse(target, media_type="application/zip", filename=target.name)


@app.post("/api/runs/nightly")
def run_nightly(body: NightlyRunBody) -> dict[str, Any]:
    return run_manager.start_nightly(
        headed=body.headed,
        skip_email=body.skip_email,
        skip_zip=body.skip_zip,
        lots=body.lots,
        start=body.start,
        end=body.end,
    )


@app.post("/api/runs/zip-only")
def run_zip_only(body: ZipOnlyRunBody) -> dict[str, Any]:
    return run_manager.start_zip_only(lots=body.lots)


@app.post("/api/runs/orchestrator")
def run_orchestrator(body: OrchestratorRunBody) -> dict[str, Any]:
    return run_manager.start_orchestrator(
        headed=body.headed,
        start=body.start,
        end=body.end,
    )


@app.get("/api/recharges")
def recharges_list(
    view: str = Query(
        "to_recharge",
        description="to_recharge | all | recharged | actifs | non_assignes",
    ),
    partner_index: int | None = Query(None, ge=1, le=20),
) -> dict[str, Any]:
    allowed_views = ("to_recharge", "all", "recharged", "actifs", "non_assignes")
    if view not in allowed_views:
        raise HTTPException(400, f"view invalide — valeurs: {', '.join(allowed_views)}")
    if not STATE_FILE.is_file():
        return {
            "ok": True,
            "state_exists": False,
            "state_path": str(STATE_FILE),
            "view": view,
            "partner_index": partner_index,
            "export_filename_example": state_export_filename(),
            "summary": {
                "drivers_total": 0,
                "actifs": 0,
                "non_assignes": 0,
                "recharges": 0,
                "a_recharger": 0,
                "invalid_phone": 0,
            },
            "partners_pending": [],
            "partners": [],
            "drivers": [],
            "drivers_count": 0,
        }
    state = load_state_dict(STATE_FILE)
    drivers = list_drivers_filtered(state, view=view, partner_index=partner_index)
    return {
        "ok": True,
        "state_exists": True,
        "state_path": str(STATE_FILE.resolve()),
        "view": view,
        "partner_index": partner_index,
        "export_filename_example": state_export_filename(),
        "summary": recharge_summary(state),
        "partners_pending": list_partners_pending(state),
        "partners": list_partners_for_filter(state),
        "drivers": drivers,
        "drivers_count": len(drivers),
    }


@app.get("/api/recharges/export-state")
def recharges_export_state() -> FileResponse:
    try:
        path, filename = export_state_timestamped()
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(
        path,
        media_type="application/json",
        filename=filename,
    )


@app.post("/api/recharges/mark")
def recharges_mark(body: MarkRechargedBody) -> dict[str, Any]:
    try:
        result = mark_drivers_recharged(
            match_keys=body.match_keys,
            phones=body.phones,
            partner_index=body.partner_index,
            all_pending_in_partner=body.all_pending_in_partner,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    result["ok"] = True
    return result


@app.get("/api/recharges/export-csv")
def recharges_export_csv() -> FileResponse:
    if not STATE_FILE.is_file():
        raise HTTPException(404, "state.json introuvable.")
    import csv
    import io

    state = load_state_dict(STATE_FILE)
    rows = export_recharge_csv_rows(state)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["numero", "montant", "name", "campagne", "statut"],
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    out_path = OUTPUT_DIR / "chauffeurs_a_recharger_dashboard.csv"
    out_path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return FileResponse(
        out_path,
        media_type="text/csv; charset=utf-8",
        filename="chauffeurs_a_recharger_appium.csv",
    )


@app.post("/api/recharges/compare")
async def recharges_compare(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "Aucun fichier sélectionné.")
    if not is_valid_state_upload_filename(file.filename):
        raise HTTPException(
            400,
            "Nom attendu : state.json, state (1).json, state_20260529_143052.json, etc.",
        )
    try:
        raw = await file.read()
        uploaded = parse_uploaded_state(raw, file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    current = load_state_dict(STATE_FILE) if STATE_FILE.is_file() else {"version": 1, "partners": {}}
    result = compare_states(current, uploaded)
    result["ok"] = True
    result["uploaded_filename"] = file.filename
    return result


@app.post("/api/recharges/apply")
async def recharges_apply(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "Aucun fichier sélectionné.")
    if not is_valid_state_upload_filename(file.filename):
        raise HTTPException(
            400,
            "Nom attendu : state.json, state (1).json, state_20260529_143052.json, etc.",
        )
    try:
        raw = await file.read()
        uploaded = parse_uploaded_state(raw, file.filename)
        result = merge_uploaded_into_current(
            uploaded,
            backup=True,
            uploaded_filename=file.filename,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"Fusion échouée: {e}") from e
    result["ok"] = True
    result["uploaded_filename"] = file.filename
    return result


@app.get("/api/chauffeurs-actifs/status")
def chauffeurs_actifs_status() -> dict[str, Any]:
    return godseye_status()


@app.post("/api/chauffeurs-actifs/generate")
def chauffeurs_actifs_generate() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        raise HTTPException(400, "state.json introuvable — lancez l'orchestrateur d'abord.")
    try:
        stats = generate_chauffeurs_xlsx()
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"Génération Excel échouée: {e}") from e
    return {"ok": True, "stats": stats}


@app.get("/api/chauffeurs-actifs/archives")
def chauffeurs_actifs_list_archives() -> dict[str, Any]:
    CHAUFFEURS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    return {"dir": str(CHAUFFEURS_ARCHIVE_DIR), "files": list_chauffeurs_archives()}


@app.post("/api/chauffeurs-actifs/archives")
def chauffeurs_actifs_create_bundle_archive() -> dict[str, Any]:
    """Archive tous les Excel horodatés dans un ZIP unique."""
    try:
        zip_path = archive_all_timestamped()
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"Archivage échoué: {e}") from e
    st = zip_path.stat()
    return {
        "ok": True,
        "archive_name": zip_path.name,
        "archive": str(zip_path.resolve()),
        "size_kb": round(st.st_size / 1024),
        "download_url": f"/api/chauffeurs-actifs/archives/file/{zip_path.name}",
    }


@app.get("/api/chauffeurs-actifs/archives/file/{filename}")
def chauffeurs_actifs_download_archive(filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or ".." in filename or not filename.endswith(".zip"):
        raise HTTPException(403, "Nom de fichier invalide.")
    if not (
        filename.startswith("chauffeurs_actifs_state_")
        or filename.startswith("chauffeurs_actifs_bundle_")
    ):
        raise HTTPException(403, "Archive non autorisée.")
    target = (CHAUFFEURS_ARCHIVE_DIR / filename).resolve()
    if not str(target).startswith(str(CHAUFFEURS_ARCHIVE_DIR.resolve())):
        raise HTTPException(403, "Chemin interdit.")
    if not target.is_file():
        raise HTTPException(404, "Archive introuvable.")
    return FileResponse(target, media_type="application/zip", filename=target.name)


@app.get("/api/chauffeurs-actifs/download")
def chauffeurs_actifs_download(
    file: str | None = Query(None, description="Nom du fichier horodaté à télécharger"),
) -> FileResponse:
    if file:
        if "/" in file or "\\" in file or ".." in file or not file.endswith(".xlsx"):
            raise HTTPException(403, "Nom de fichier invalide.")
        if not file.startswith("chauffeurs_actifs_state"):
            raise HTTPException(403, "Fichier non autorisé.")
        target = (OUTPUT_DIR / file).resolve()
        if not str(target).startswith(str(OUTPUT_DIR.resolve())):
            raise HTTPException(403, "Chemin interdit.")
    else:
        target = find_latest_chauffeurs_xlsx()
        if target is None:
            raise HTTPException(
                404,
                "Aucun Excel chauffeurs actifs — générez-le depuis le dashboard.",
            )
        target = target.resolve()
    if not target.is_file():
        raise HTTPException(404, "Fichier introuvable.")
    return FileResponse(
        target,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=target.name,
    )


@app.post("/api/runs/godseye-download")
def run_godseye_download(body: GodseyeRunBody) -> dict[str, Any]:
    return run_manager.start_godseye_download(headed=body.headed)


@app.post("/api/runs/html-only")
def run_html_only() -> dict[str, Any]:
    p = find_best_export_json()
    if not p:
        raise HTTPException(400, "Aucun JSON export — lancez d'abord un export.")
    return run_manager.start_activation_only(p)


@app.get("/api/runs/current")
def current_run() -> dict[str, Any]:
    job = run_manager.current()
    return {"running": job is not None and job.get("status") == "running", "job": job}


@app.post("/api/runs/stop")
def stop_run() -> dict[str, bool]:
    return {"stopped": run_manager.stop()}


@app.get("/api/scheduler")
def scheduler_status() -> dict[str, Any]:
    return auto_scheduler.status()


@app.put("/api/scheduler")
def scheduler_configure(body: SchedulerBody) -> dict[str, Any]:
    return auto_scheduler.configure(enabled=body.enabled, time_hm=body.time)


@app.get("/api/runs/stream")
async def runs_stream() -> StreamingResponse:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    _sse_queues.append(queue)

    async def gen():
        job = run_manager.current()
        if job:
            yield f"data: {json.dumps({'type': 'job', 'job': job}, ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield line
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in _sse_queues:
                _sse_queues.remove(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/")
async def index() -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(404, "index.html manquant")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    print(f"Dashboard UPJUNOO : http://{HOST}:{PORT}/")
    print(f"Racine projet : {SCRIPT_DIR}")
    uvicorn.run(
        "partner_dashboard.api:app",
        host=HOST,
        port=PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
