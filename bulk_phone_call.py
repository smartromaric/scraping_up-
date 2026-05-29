#!/usr/bin/env python3
"""
Appels telephoniques en masse via Appium (Methode 3 : Bluetooth PC).

Ce script :
1) Lit un CSV de contacts (colonnes : numero + optionnellement nom, statut).
2) Pour chaque contact, compose le numero via le dialer Android.
3) Detecte si l'appel est decroche.
4) Joue un fichier audio (.mp3/.wav) sur le PC (qui route via Bluetooth HFP).
5) Attend la fin du message puis raccroche.
6) Log le statut (DECROCHE, PAS_DE_REPONSE, ERREUR) dans le CSV.

Pre-requis :
- Appium lance (port 4723)
- Telephone connecte USB + Bluetooth appaire au PC en mode mains-libres
- VB-Audio Virtual Cable installe (ou sortie audio PC dirigee vers Bluetooth HFP)
- Fichier audio du message a jouer
"""

import csv
import glob
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from typing import List, Dict, Tuple, Iterable

from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

APPIUM_URL = os.getenv("APPIUM_URL", "http://127.0.0.1:4723")

CONTACT_FILE = os.getenv("CONTACT_FILE", "call_list.csv")
AUDIO_FILE = os.getenv("AUDIO_FILE", "message.mp3")
COUNTRY_PREFIX = os.getenv("COUNTRY_PREFIX", "+225")

RING_TIMEOUT = int(os.getenv("RING_TIMEOUT", "35"))
POST_AUDIO_WAIT = int(os.getenv("POST_AUDIO_WAIT", "2"))
DELAY_BETWEEN_CALLS = int(os.getenv("DELAY_BETWEEN_CALLS", "5"))

# "phone"    = joue l'audio sur le telephone via adb (methode haut-parleur)
# "pc"       = joue l'audio sur le PC (methode Bluetooth HFP)
# "manual"   = le script compose et tu parles toi-meme (pas besoin d'audio)
AUDIO_MODE = os.getenv("AUDIO_MODE", "manual")

# Chemin sur le telephone ou pousser le fichier audio (utilise si AUDIO_MODE=phone)
PHONE_AUDIO_PATH = "/sdcard/Download/message_auto.mp3"

Locator = Tuple[str, str]

# ---------------------------------------------------------------------------
# Audio playback
# ---------------------------------------------------------------------------

def _get_audio_duration(path: str) -> float:
    """Retourne la duree approximative du fichier audio en secondes."""
    lower = path.lower()
    if lower.endswith(".wav"):
        try:
            with wave.open(path, "r") as wf:
                return wf.getnframes() / wf.getframerate()
        except Exception:
            pass
    try:
        import mutagen
        m = mutagen.File(path)
        if m and m.info:
            return m.info.length
    except ImportError:
        pass
    except Exception:
        pass
    return 30.0


def push_audio_to_phone(local_path: str):
    """Copie le fichier audio du PC vers le telephone via adb push."""
    adb = _find_adb_executable()
    if not adb:
        raise RuntimeError("adb introuvable, impossible de pousser l'audio sur le telephone.")
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    r = subprocess.run(
        [adb, "push", local_path, PHONE_AUDIO_PATH],
        capture_output=True, text=True, timeout=30,
        creationflags=creationflags,
    )
    if r.returncode != 0:
        raise RuntimeError(f"adb push echoue : {r.stderr}")
    print(f"  [OK] Audio pousse sur le telephone : {PHONE_AUDIO_PATH}")


def play_audio_on_phone():
    """Joue le fichier audio directement sur le telephone via adb shell am start."""
    adb_shell("am force-stop com.google.android.music")
    time.sleep(0.3)

    adb_shell(
        f"am start -a android.intent.action.VIEW "
        f"-d file://{PHONE_AUDIO_PATH} -t audio/mpeg "
        f"--activity-clear-task"
    )
    print("  [AUDIO] Lecture sur le telephone...")


def stop_audio_on_phone():
    """Arrete la lecture audio sur le telephone."""
    adb_shell("am force-stop com.google.android.music")
    adb_shell("input keyevent 86")


def play_audio_pc(path: str) -> threading.Thread:
    """
    Joue le fichier audio sur la sortie par defaut du PC (methode Bluetooth HFP).
    Retourne le thread pour pouvoir attendre la fin.
    """
    abs_path = os.path.abspath(path)

    def _play():
        if sys.platform == "win32":
            try:
                import winsound
                if abs_path.lower().endswith(".wav"):
                    winsound.PlaySound(abs_path, winsound.SND_FILENAME)
                    return
            except Exception:
                pass

        try:
            from pygame import mixer
            mixer.init()
            mixer.music.load(abs_path)
            mixer.music.play()
            while mixer.music.get_busy():
                time.sleep(0.2)
            mixer.quit()
            return
        except ImportError:
            pass

        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
            try:
                subprocess.run(
                    ["powershell", "-Command",
                     f'(New-Object Media.SoundPlayer "{abs_path}").PlaySync()'],
                    timeout=120, creationflags=creationflags,
                )
                return
            except Exception:
                pass
            try:
                os.startfile(abs_path)
                return
            except Exception:
                pass
        else:
            for player in ["ffplay -nodisp -autoexit", "aplay", "afplay"]:
                try:
                    subprocess.run(player.split() + [abs_path], timeout=120)
                    return
                except Exception:
                    continue

        print("  [!] Impossible de jouer l'audio automatiquement.")

    t = threading.Thread(target=_play, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# ADB helpers (reutilises de open_upjunoo_bulk_assisted.py)
# ---------------------------------------------------------------------------

def _find_adb_executable() -> str | None:
    p = shutil.which("adb")
    if p:
        return p
    for base in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")):
        if base:
            cand = os.path.join(base, "platform-tools", "adb.exe")
            if os.path.isfile(cand):
                return cand
    winget_pkgs = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    if os.path.isdir(winget_pkgs):
        matches = glob.glob(os.path.join(winget_pkgs, "Google.PlatformTools*", "platform-tools", "adb.exe"))
        if matches:
            return matches[0]
    return None


def list_adb_device_serials() -> List[str]:
    adb = _find_adb_executable()
    if not adb:
        return []
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        r = subprocess.run(
            [adb, "devices"], capture_output=True, text=True,
            timeout=20, creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    serials: List[str] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == "device":
            serials.append(parts[0])
    return serials


def resolve_android_udid(requested: str) -> str:
    serials = list_adb_device_serials()
    if not serials:
        if _find_adb_executable() is None:
            print("[!] adb introuvable (PATH).")
        if requested:
            print(f"  -> Utilisation de ANDROID_UDID={requested!r} sans verification adb.")
            return requested
        raise SystemExit(
            "Aucun appareil dans `adb devices`. Branche le telephone, active le debogage USB, autorise le PC."
        )
    if requested and requested in serials:
        return requested
    if requested and requested not in serials:
        if len(serials) == 1:
            print(f"  [!] ANDROID_UDID={requested!r} ne correspond pas. Utilisation de {serials[0]}.")
            return serials[0]
        raise SystemExit(f"ANDROID_UDID={requested!r} absent. Appareils : {serials}")
    if len(serials) == 1:
        print(f"  [OK] Appareil detecte : {serials[0]}")
        return serials[0]
    raise SystemExit(f"Plusieurs appareils USB : {serials}. Definis ANDROID_UDID.")


def adb_shell(cmd: str) -> str:
    adb = _find_adb_executable()
    if not adb:
        return ""
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        r = subprocess.run(
            [adb, "shell"] + cmd.split(),
            capture_output=True, text=True, timeout=10,
            creationflags=creationflags,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# CSV loading & status tracking
# ---------------------------------------------------------------------------

def load_contacts(path: str) -> List[Dict[str, str]]:
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise FileNotFoundError(f"Fichier introuvable: {p}")

    rows: List[Dict[str, str]] = []
    phone_keys = {"numero", "numéro", "telephone", "téléphone", "phone", "mobile"}
    name_keys = {"nom", "name", "prenom", "conducteur", "driver"}

    def _norm(s: str) -> str:
        return (s or "").strip().lower()

    with open(p, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV sans en-tetes.")
        fields = [_norm(c) for c in reader.fieldnames]
        phone_col = next((reader.fieldnames[i] for i, c in enumerate(fields) if c in phone_keys), None)
        name_col = next((reader.fieldnames[i] for i, c in enumerate(fields) if c in name_keys), None)
        status_col = next((reader.fieldnames[i] for i, c in enumerate(fields) if c in {"statut", "status"}), None)

        if not phone_col:
            raise ValueError("Colonne numero/telephone introuvable dans le CSV.")

        for idx, r in enumerate(reader, start=2):
            phone = str(r.get(phone_col, "")).strip()
            name = str(r.get(name_col, "")).strip() if name_col else ""
            status = str(r.get(status_col, "")).strip() if status_col else ""
            if phone:
                rows.append({"row_index": str(idx), "numero": phone, "nom": name, "statut": status})

    if not rows:
        raise ValueError("Aucun contact valide dans le fichier.")
    return rows


def ensure_csv_status_column(path: str):
    if not path.lower().endswith(".csv"):
        return
    p = os.path.abspath(path)
    with open(p, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        data = list(reader)
    if "statut" in fieldnames:
        return
    fieldnames = list(fieldnames) + ["statut"]
    for r in data:
        r["statut"] = r.get("statut", "")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(data)


def mark_status(path: str, row_index_1based: int, status: str):
    p = os.path.abspath(path)
    with open(p, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    if "statut" not in fieldnames:
        fieldnames = list(fieldnames) + ["statut"]
        for r in rows:
            r["statut"] = r.get("statut", "")
    target = row_index_1based - 2
    if 0 <= target < len(rows):
        rows[target]["statut"] = status
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Phone call automation via Appium
# ---------------------------------------------------------------------------

def normalize_phone(raw: str) -> str:
    """Normalise le numero : ajoute le prefixe pays si absent."""
    digits = "".join(c for c in raw if c.isdigit() or c == "+")
    if digits.startswith("+"):
        return digits
    if digits.startswith("00225"):
        return "+" + digits[2:]
    if digits.startswith("225") and len(digits) >= 13:
        return "+" + digits
    return COUNTRY_PREFIX + digits


def dial_number(driver, phone: str):
    """Ouvre le dialer avec le numero et lance l'appel via adb shell."""
    formatted = normalize_phone(phone)
    adb_shell(f"am start -a android.intent.action.CALL -d tel:{formatted}")
    print(f"  [APPEL] Composition : {formatted}")


def is_call_active(driver) -> bool:
    """Detecte si un appel est en cours via dumpsys telecom."""
    try:
        output = adb_shell("dumpsys telecom")
        return "ACTIVE" in output or "mState=ACTIVE" in output
    except Exception:
        return False


def is_call_ringing(driver) -> bool:
    """Detecte si l'appel sonne (DIALING/CONNECTING)."""
    try:
        output = adb_shell("dumpsys telecom")
        return any(s in output for s in ["DIALING", "CONNECTING", "RINGING"])
    except Exception:
        return False


def wait_for_pickup_or_timeout(driver, timeout: int) -> str:
    """
    Attend que l'interlocuteur decroche ou que le timeout expire.
    Retourne : 'DECROCHE', 'PAS_DE_REPONSE', 'OCCUPE', 'ERREUR'
    """
    start = time.time()
    call_started = False

    while time.time() - start < timeout:
        telecom = adb_shell("dumpsys telecom")

        if "ACTIVE" in telecom or "mState=ACTIVE" in telecom:
            return "DECROCHE"

        if "DIALING" in telecom or "CONNECTING" in telecom or "RINGING" in telecom:
            call_started = True
            time.sleep(1)
            continue

        if call_started and "DISCONNECTED" in telecom:
            if "BUSY" in telecom:
                return "OCCUPE"
            return "PAS_DE_REPONSE"

        if not call_started:
            time.sleep(0.5)
            continue

        time.sleep(1)

    return "PAS_DE_REPONSE"


def hang_up(driver):
    """Raccroche l'appel en cours via adb."""
    try:
        adb_shell("input keyevent KEYCODE_ENDCALL")
        print("  [RACCROCHE]")
    except Exception:
        print("  [!] Impossible de raccrocher automatiquement.")


def activate_speaker(driver):
    """Active le haut-parleur via keyevent."""
    adb_shell("input keyevent 79")
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    audio_path = None
    audio_duration = 30.0

    if AUDIO_MODE != "manual":
        audio_path = os.path.abspath(AUDIO_FILE)
        if not os.path.isfile(audio_path):
            raise SystemExit(
                f"Fichier audio introuvable : {audio_path}\n"
                f"Place ton message vocal dans le dossier et definis AUDIO_FILE."
            )
        audio_duration = _get_audio_duration(audio_path)

    mode_labels = {
        "phone": "TELEPHONE (haut-parleur auto)",
        "pc": "PC (Bluetooth HFP)",
        "manual": "MANUEL (tu parles toi-meme, le script compose)"
    }
    print(f"[CONFIG] Mode audio       : {mode_labels.get(AUDIO_MODE, AUDIO_MODE)}")
    if audio_path:
        print(f"[CONFIG] Fichier audio    : {audio_path} (~{audio_duration:.0f}s)")
    print(f"[CONFIG] Fichier contacts : {CONTACT_FILE}")
    print(f"[CONFIG] Prefixe pays     : {COUNTRY_PREFIX}")
    print(f"[CONFIG] Timeout sonnerie : {RING_TIMEOUT}s")
    print(f"[CONFIG] Delai entre appels: {DELAY_BETWEEN_CALLS}s")
    print()

    if AUDIO_MODE == "phone":
        print("[...] Copie de l'audio vers le telephone...")
        push_audio_to_phone(audio_path)

    ensure_csv_status_column(CONTACT_FILE)
    contacts = load_contacts(CONTACT_FILE)
    pending = [c for c in contacts if c.get("statut", "").strip().upper() not in {"OK", "DECROCHE", "SKIP"}]
    print(f"[INFO] {len(contacts)} contact(s) charges | a traiter : {len(pending)}")

    if not pending:
        print("[OK] Tous les contacts ont deja ete traites.")
        return

    udid = resolve_android_udid(os.getenv("ANDROID_UDID", "").strip())

    opts = UiAutomator2Options()
    opts.platform_name = "Android"
    opts.automation_name = "UiAutomator2"
    if udid:
        opts.udid = udid
    opts.no_reset = True
    opts.auto_launch = False
    opts.new_command_timeout = 300
    opts.ignore_hidden_api_policy_error = True
    opts.skip_device_initialization = True

    print("[...] Connexion Appium...")
    driver = webdriver.Remote(APPIUM_URL, options=opts)
    print("[OK] Session Appium ouverte.")

    stats = {"DECROCHE": 0, "PAS_DE_REPONSE": 0, "OCCUPE": 0, "ERREUR": 0}

    try:
        for i, contact in enumerate(pending, 1):
            phone = contact["numero"]
            nom = contact.get("nom", "")
            label = f"{nom} ({phone})" if nom else phone
            row_idx = int(contact["row_index"])

            print(f"\n{'='*60}")
            print(f"[{i}/{len(pending)}] Appel -> {label}")
            print(f"{'='*60}")

            try:
                dial_number(driver, phone)

                time.sleep(1)
                result = wait_for_pickup_or_timeout(driver, RING_TIMEOUT)

                if result == "DECROCHE":
                    print(f"  [OK] Decroche !")

                    if AUDIO_MODE == "manual":
                        print(f"  --> PARLE MAINTENANT ! Dis ton message.")
                        print(f"  --> Quand tu as fini, appuie Entree pour raccrocher.")
                        input()
                    elif AUDIO_MODE == "phone":
                        print(f"  [AUDIO] Lecture sur le telephone...")
                        activate_speaker(driver)
                        time.sleep(0.5)
                        play_audio_on_phone()
                        time.sleep(audio_duration + 1)
                        stop_audio_on_phone()
                    else:
                        print(f"  [AUDIO] Lecture sur le PC...")
                        activate_speaker(driver)
                        time.sleep(0.5)
                        audio_thread = play_audio_pc(audio_path)
                        audio_thread.join(timeout=audio_duration + 10)

                    time.sleep(POST_AUDIO_WAIT)
                    hang_up(driver)
                    mark_status(CONTACT_FILE, row_idx, "DECROCHE")
                    stats["DECROCHE"] += 1
                    print(f"  [OK] Appel termine pour {label}")

                elif result == "OCCUPE":
                    print(f"  [--] Ligne occupee pour {label}")
                    hang_up(driver)
                    mark_status(CONTACT_FILE, row_idx, "OCCUPE")
                    stats["OCCUPE"] += 1

                else:
                    print(f"  [--] Pas de reponse pour {label} (timeout {RING_TIMEOUT}s)")
                    hang_up(driver)
                    mark_status(CONTACT_FILE, row_idx, "PAS_DE_REPONSE")
                    stats["PAS_DE_REPONSE"] += 1

            except Exception as e:
                print(f"  [ERREUR] {label} : {e}")
                hang_up(driver)
                mark_status(CONTACT_FILE, row_idx, "ERREUR")
                stats["ERREUR"] += 1

            if i < len(pending):
                print(f"  [PAUSE] {DELAY_BETWEEN_CALLS}s avant le prochain appel...")
                time.sleep(DELAY_BETWEEN_CALLS)

    except KeyboardInterrupt:
        print("\n[STOP] Interrompu par l'utilisateur (Ctrl+C).")
        hang_up(driver)
    finally:
        driver.quit()
        print("\n[FIN] Session Appium fermee.")
        print(f"\n{'='*60}")
        print("RAPPORT FINAL")
        print(f"{'='*60}")
        print(f"  Decroches       : {stats['DECROCHE']}")
        print(f"  Pas de reponse  : {stats['PAS_DE_REPONSE']}")
        print(f"  Occupes         : {stats['OCCUPE']}")
        print(f"  Erreurs         : {stats['ERREUR']}")
        print(f"  TOTAL traites   : {sum(stats.values())}")


if __name__ == "__main__":
    main()
