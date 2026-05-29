"""Planification automatique des rapports du soir (17h par défaut)."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta

from partner_dashboard.config import (
    AUTO_RUN_ENABLED,
    AUTO_RUN_TIME,
    DASHBOARD_DIR,
    SCHEDULER_STATE_FILE,
)
from partner_dashboard.run_manager import run_manager

_TIME_RE = re.compile(
    r"^(\d{1,2})(?::|h)?(\d{2})?\s*(am|pm)?$",
    re.I,
)


def parse_time_hm(value: str) -> tuple[int, int]:
    raw = (value or "17:00").strip().lower().replace(" ", "")
    m = _TIME_RE.match(raw)
    if not m:
        return 17, 0
    h = int(m.group(1))
    mn = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    return min(h, 23), min(mn, 59)


class AutoScheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = AUTO_RUN_ENABLED
        self._hour, self._minute = parse_time_hm(AUTO_RUN_TIME)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
        self._load_persisted_settings()

    def _load_persisted_settings(self) -> None:
        state = self._read_state()
        if "enabled" in state:
            self._enabled = bool(state["enabled"])
        if state.get("time"):
            self._hour, self._minute = parse_time_hm(str(state["time"]))

    def _persist_settings(self) -> None:
        data = self._read_state()
        data["enabled"] = self._enabled
        data["time"] = f"{self._hour:02d}:{self._minute:02d}"
        self._write_state(data)

    def _next_run_datetime(self) -> datetime:
        now = datetime.now()
        target = now.replace(
            hour=self._hour,
            minute=self._minute,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        if (
            now.hour == self._hour
            and now.minute == self._minute
            and self._already_ran_today()
        ):
            target += timedelta(days=1)
        return target

    def status(self) -> dict:
        with self._lock:
            last = self._read_state()
            nxt = self._next_run_datetime()
            return {
                "enabled": self._enabled,
                "time": f"{self._hour:02d}:{self._minute:02d}",
                "last_auto_run_date": last.get("last_auto_run_date"),
                "last_result": last.get("last_result"),
                "last_at": last.get("last_at"),
                "next_run_at": nxt.isoformat(timespec="seconds"),
                "next_run_label": nxt.strftime("%d/%m/%Y %H:%M"),
            }

    def configure(self, *, enabled: bool | None = None, time_hm: str | None = None) -> dict:
        with self._lock:
            if enabled is not None:
                self._enabled = enabled
            if time_hm:
                self._hour, self._minute = parse_time_hm(time_hm)
            self._persist_settings()
        self._wakeup.set()
        return self.status()

    def _read_state(self) -> dict:
        if not SCHEDULER_STATE_FILE.is_file():
            return {}
        try:
            return json.loads(SCHEDULER_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, data: dict) -> None:
        SCHEDULER_STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _mark_run(self, result: str) -> None:
        data = self._read_state()
        data["last_auto_run_date"] = datetime.now().strftime("%Y-%m-%d")
        data["last_result"] = result
        data["last_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_state(data)

    def _already_ran_today(self) -> bool:
        last = self._read_state().get("last_auto_run_date")
        return last == datetime.now().strftime("%Y-%m-%d")

    def _in_trigger_minute(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        return now.hour == self._hour and now.minute == self._minute

    def _try_trigger(self) -> bool:
        if not self._enabled:
            return False
        if not self._in_trigger_minute():
            return False
        if self._already_ran_today():
            return False
        if run_manager.is_running():
            return False
        res = run_manager.start_nightly(skip_email=True, skip_zip=False)
        self._mark_run("started" if res.get("ok") else res.get("error", "failed"))
        return True

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wakeup.clear()
            if not self._enabled:
                if self._wakeup.wait(timeout=30):
                    continue
                continue

            if self._try_trigger():
                self._wakeup.wait(timeout=61)
                continue

            if self._wakeup.wait(timeout=15):
                continue


auto_scheduler = AutoScheduler()
