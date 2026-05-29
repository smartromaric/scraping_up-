"""Génération Excel chauffeurs actifs + détection export Godseye + archives ZIP."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from partner_dashboard.config import CHAUFFEURS_ARCHIVE_DIR, OUTPUT_DIR, STATE_FILE

_TS_XLSX_RE = re.compile(r"^chauffeurs_actifs_state_\d{8}_\d{6}\.xlsx$")


def _generator_script_path() -> Path:
    candidates = [
        Path(__file__).resolve().parent / "generate_chauffeurs_actifs_state.py",
        OUTPUT_DIR / "generate_chauffeurs_actifs_state.py",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return candidates[0]


def _generator_module():
    script = _generator_script_path()
    if not script.is_file():
        raise FileNotFoundError(f"Script introuvable: {script}")
    name = "generate_chauffeurs_actifs_state"
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise ImportError(f"Impossible de charger {script}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def find_latest_godseye() -> Path | None:
    mod = _generator_module()
    return mod.find_latest_godseye(OUTPUT_DIR)


def find_latest_chauffeurs_xlsx() -> Path | None:
    mod = _generator_module()
    return mod.find_latest_chauffeurs_xlsx(OUTPUT_DIR)


def _timestamped_xlsx_files() -> list[Path]:
    return sorted(
        [
            p
            for p in OUTPUT_DIR.glob("chauffeurs_actifs_state*.xlsx")
            if p.is_file()
            and not p.name.startswith("~$")
            and p.name != "chauffeurs_actifs_state.xlsx"
            and _TS_XLSX_RE.match(p.name)
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _archive_zip_name(stem: str) -> str:
    return f"{stem}.zip"


def create_archive_for_xlsx(xlsx_path: Path, stats: dict[str, Any] | None = None) -> Path:
    """Crée une archive ZIP pour un Excel (fichier + manifest JSON)."""
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.is_file():
        raise FileNotFoundError(f"Excel introuvable: {xlsx_path}")
    CHAUFFEURS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = CHAUFFEURS_ARCHIVE_DIR / _archive_zip_name(xlsx_path.stem)
    manifest = dict(stats or {})
    manifest.setdefault("archived_at", datetime.now().isoformat(timespec="seconds"))
    manifest["xlsx_name"] = xlsx_path.name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(xlsx_path, arcname=xlsx_path.name)
        zf.writestr(
            f"{xlsx_path.stem}_manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return zip_path


def archive_all_timestamped() -> Path:
    """Archive tous les Excel horodatés dans un ZIP unique."""
    files = list(reversed(_timestamped_xlsx_files()))  # plus ancien → plus récent
    if not files:
        raise FileNotFoundError("Aucun Excel horodaté à archiver.")
    CHAUFFEURS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = CHAUFFEURS_ARCHIVE_DIR / f"chauffeurs_actifs_bundle_{ts}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)
        zf.writestr(
            "bundle_manifest.json",
            json.dumps(
                {
                    "archived_at": datetime.now().isoformat(timespec="seconds"),
                    "files": [p.name for p in files],
                    "count": len(files),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return zip_path


def list_chauffeurs_archives(limit: int = 20) -> list[dict[str, Any]]:
    CHAUFFEURS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        CHAUFFEURS_ARCHIVE_DIR.glob("*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    items: list[dict[str, Any]] = []
    for p in files:
        st = p.stat()
        items.append(
            {
                "name": p.name,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "modified_display": datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M:%S"),
                "size_kb": round(st.st_size / 1024),
                "download_url": f"/api/chauffeurs-actifs/archives/file/{p.name}",
            }
        )
    return items


def godseye_status() -> dict[str, Any]:
    latest_g = find_latest_godseye()
    latest_x = find_latest_chauffeurs_xlsx()
    out: dict[str, Any] = {
        "state_exists": STATE_FILE.is_file(),
        "state_path": str(STATE_FILE),
        "godseye": None,
        "xlsx": None,
        "xlsx_history": [],
        "archives": list_chauffeurs_archives(),
        "archive_dir": str(CHAUFFEURS_ARCHIVE_DIR),
    }
    if latest_g:
        st = latest_g.stat()
        out["godseye"] = {
            "name": latest_g.name,
            "path": str(latest_g),
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "modified_display": datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M:%S"),
            "size_kb": round(st.st_size / 1024),
        }
    if latest_x:
        st = latest_x.stat()
        mod_display = datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M:%S")
        out["xlsx"] = {
            "name": latest_x.name,
            "path": str(latest_x),
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "modified_display": mod_display,
            "size_kb": round(st.st_size / 1024),
            "download_url": f"/api/chauffeurs-actifs/download?file={latest_x.name}",
        }
    for p in _timestamped_xlsx_files()[:8]:
        st = p.stat()
        out["xlsx_history"].append(
            {
                "name": p.name,
                "modified_display": datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M:%S"),
                "size_kb": round(st.st_size / 1024),
                "download_url": f"/api/chauffeurs-actifs/download?file={p.name}",
            }
        )
    return out


def generate_chauffeurs_xlsx(
    *,
    godseye_path: Path | None = None,
    output_path: Path | None = None,
    create_archive: bool = True,
) -> dict[str, Any]:
    mod = _generator_module()
    stats = mod.generate_report(
        state_path=STATE_FILE,
        godseye_path=godseye_path,
        output_path=output_path,
        timestamped=output_path is None,
    )
    if create_archive:
        zip_path = create_archive_for_xlsx(Path(stats["output"]), stats)
        stats["archive"] = str(zip_path.resolve())
        stats["archive_name"] = zip_path.name
        stats["archive_download_url"] = f"/api/chauffeurs-actifs/archives/file/{zip_path.name}"
    return stats
