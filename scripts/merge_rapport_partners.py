#!/usr/bin/env python3
"""Fusionne des campagnes dans un rapport global et régénère HTML + ZIP."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from export_partner_fleet_drivers_report import build_report, export_excel
from nightly_reports_runner import build_lot_zip

OUTPUT = SCRIPT_DIR / "output" / "partner_automation"


def load_partner(path: Path, index: int) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    for p in data.get("partners") or []:
        if int(p.get("index") or 0) == index:
            return p
    raise KeyError(f"Campagne {index} absente de {path.name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--patch", action="append", nargs=2, metavar=("INDEX", "JSON"), required=True)
    args = parser.parse_args()

    base = json.loads(args.base.read_text(encoding="utf-8"))
    patches: dict[int, dict] = {}
    patch_names: dict[int, str] = {}
    for idx_str, path_str in args.patch:
        idx = int(idx_str)
        path = Path(path_str)
        patches[idx] = load_partner(path, idx)
        patch_names[idx] = path.name

    merged = []
    for p in base.get("partners") or []:
        idx = int(p.get("index") or 0)
        merged.append(patches.get(idx, p))

    report = build_report(merged)
    report["generated_at"] = datetime.now().isoformat(timespec="seconds")
    report["merged_from"] = {"base": args.base.name, **{f"p{k}": v for k, v in patch_names.items()}}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = OUTPUT / f"rapport_partenaires_{ts}.json"
    out_xlsx = OUTPUT / f"rapport_partenaires_{ts}.xlsx"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    export_excel(report, out_xlsx)

    exports = OUTPUT / "dashboard" / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_json, exports / out_json.name)
    shutil.copy2(out_xlsx, exports / out_xlsx.name)

    for idx in sorted(patches):
        p = next(x for x in report["partners"] if int(x["index"]) == idx)
        print(f"P{idx:02d}: {p['vehicles_count']} véh. / {p['drivers_count']} chauf.")
    print(f"Totaux: {report['totals']}")
    print(f"JSON: {out_json}")

    gen = SCRIPT_DIR / "generate_activation_report.py"
    for extra in ([], ["--start", "11", "--end", "20"], ["--start", "1", "--end", "10"]):
        subprocess.run([sys.executable, str(gen), "--input", str(out_json), *extra], check=True)

    for lot in ((1, 10), (11, 20)):
        z = build_lot_zip(lot_start=lot[0], lot_end=lot[1], run_stamp=ts)
        print(f"ZIP: {z.name}")


if __name__ == "__main__":
    main()
