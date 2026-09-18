#!/usr/bin/env python3

from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import argparse
import csv
import shutil
import json
import re
import subprocess
import sys
import time
import platform
import hashlib

from protocols.iec62056 import (
    parse_message as parse_iec_message,
    lookup_obis_code,
)
from protocols.sml import parse_bytes as parse_sml_bytes, meter_id as sml_meter_id
from storage import (
    init_database,
    get_meter_by_uid,
    create_meter,
    set_meter_name,
    set_meter_identity,
    rekey_meter,
    set_ha_meter,
    save_reading,
    note_meter_protocol,
    list_meters,
    get_meter_by_id,
    get_last_reading,
    get_history,
    decode_values,
    get_reading,
    delete_reading,
    delete_all_readings_for_meter,
    delete_meter_with_readings,
    create_measurement_series,
    finish_measurement_series,
    list_measurement_series,
    get_measurement_series,
    get_series_readings,
)

BASE_DIR = Path(__file__).resolve().parent
IEC_READER = BASE_DIR / "usb" / "iec_reader"
SML_READER = BASE_DIR / "usb" / "sml_reader"
CONFIG_FILE = BASE_DIR / "data" / "settings.json"
VERSION_FILE = BASE_DIR / "VERSION"
DEFAULT_SETTINGS = {"protocol_mode": "auto"}


def _load_app_version():
    """Zentrale Versionsnummer aus VERSION lesen."""
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return version or "unknown"


APP_VERSION = _load_app_version()
DIAGNOSTIC_DIR = Path.home() / "storage" / "shared" / "Download"
DEFAULT_TEST_FILE = BASE_DIR / "test_telegram.bin"


def _format_raw_measurement(raw, unit):
    """
    Formatiert den vom Zaehler gelieferten Zahlenstring ohne Verlust
    seiner Nachkommastellen. Nur fuehrende Nullen im Ganzzahlteil
    werden entfernt.

    Beispiele:
        0000042.800 -> 42.800
        09670.091   -> 9670.091
        00000.000   -> 0.000
        240.6       -> 240.6
    """
    if raw is None or not unit:
        return None

    raw = str(raw).strip()

    # Je nach IEC-Zaehler/Parser liegt die Einheit im gespeicherten Rohwert
    # entweder als "*kWh" oder als " kWh" vor. Beide Darstellungen sind
    # semantisch gleich und werden hier akzeptiert.
    star_suffix = "*" + unit
    space_suffix = " " + unit
    if raw.endswith(star_suffix):
        number = raw[:-len(star_suffix)].strip()
    elif raw.endswith(space_suffix):
        number = raw[:-len(space_suffix)].strip()
    elif re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw):
        # Bei bereits geparsten/gespeicherten Messungen steht die Einheit
        # separat im Feld "unit"; "raw" enthaelt dann nur die Zahl.
        number = raw
    else:
        return None
    if not number:
        return None

    sign = ""
    if number[:1] in ("+", "-"):
        sign, number = number[0], number[1:]

    if "." in number:
        integer, fraction = number.split(".", 1)
        integer = integer.lstrip("0") or "0"
        number = integer + "." + fraction
    else:
        number = number.lstrip("0") or "0"

    return sign + number


def format_value(entry):
    if getattr(entry, "unit", None):
        number = _format_raw_measurement(entry.raw, entry.unit)
        if number is not None:
            return f"{number} {entry.unit}"
        value = getattr(entry, "value", None)
        if value is not None:
            return f"{value} {entry.unit}"
    return str(entry.raw)


def format_stored_value(entry):
    if not entry:
        return "-"
    unit = entry.get("unit")
    raw = entry.get("raw")
    if unit:
        number = _format_raw_measurement(raw, unit)
        if number is not None:
            return f"{number} {unit}"
        value = entry.get("value")
        if value is not None:
            return f"{value} {unit}"
    return str(raw) if raw is not None else "-"


def get_stored_entries(values, base_code):
    matches = []
    wanted = lookup_obis_code(base_code)
    for original_code, entry in values.items():
        stored_base = entry.get("base_code")
        if stored_base is None:
            # Rueckwaertskompatibilitaet mit bereits gespeicherten
            # IEC-Messungen aus der alten Datenbank.
            stored_base = original_code
        if lookup_obis_code(stored_base) == wanted:
            matches.append((original_code, entry))
    return matches


def get_stored_entry(values, base_code):
    matches = get_stored_entries(values, base_code)
    if len(matches) == 1:
        return matches[0][1]

    if len(matches) > 1:
        # Einige IEC-Zaehler (z. B. Iskraemeco MT173) speichern neben dem
        # aktuellen Register 1.8.0*255 auch historische Register
        # 1.8.0*01, *02, ... . lookup_obis_code() normalisiert diese auf
        # denselben OBIS-Code. Fuer normale Anzeige/Auswertung muss deshalb
        # das aktuelle Register bevorzugt werden.
        current = []
        for original_code, entry in matches:
            stored_base = str(entry.get("base_code") or original_code)
            if stored_base.endswith("*255") or "*" not in stored_base:
                current.append(entry)
        if len(current) == 1:
            return current[0]

    return None




def _historical_datetime(raw):
    """IEC-Stichtagsdatum YYMMDDhhmm[ss] sicher als datetime interpretieren."""
    if raw is None:
        return None
    text = str(raw).strip()

    # IEC-Zaehler liefern Stichtagszeiten in unterschiedlichen Laengen:
    # - 12 Stellen: YYMMDDhhmmss
    # - 10 Stellen: YYMMDDhhmm (z. B. Iskraemeco MT173)
    if re.fullmatch(r"\d{12}", text):
        fmt = "%y%m%d%H%M%S"
    elif re.fullmatch(r"\d{10}", text):
        fmt = "%y%m%d%H%M"
    else:
        return None

    try:
        dt = datetime.strptime(text, fmt)
    except ValueError:
        return None

    # Werte aus uninitialisiertem/alten Zaehler-Speicher nicht als echtes
    # Monatsdatum verwenden (z. B. 000101000000).
    if dt.year < 2005:
        return None
    return dt


def _historical_date(raw):
    dt = _historical_datetime(raw)
    return dt.strftime("%d.%m.%Y") if dt is not None else None


def _stored_historical_entry(values, base_code, suffix):
    """Eindeutigen historischen OBIS-Wert base_code*NN suchen."""
    wanted = lookup_obis_code(f"{base_code}*{suffix}")
    matches = []
    for original_code, entry in values.items():
        stored_base = entry.get("base_code") or original_code
        if lookup_obis_code(stored_base) == wanted:
            matches.append(entry)
    return matches[0] if len(matches) == 1 else None


def _stored_numeric_value(entry):
    """Messwert als Decimal fuer reine Anzeigeentscheidungen, falls moeglich."""
    if not entry:
        return None
    unit = entry.get("unit")
    number = _format_raw_measurement(entry.get("raw"), unit) if unit else None
    if number is None and entry.get("value") is not None:
        number = str(entry.get("value"))
    if number is None:
        return None
    try:
        return Decimal(number)
    except (InvalidOperation, ValueError):
        return None


def print_monthly_values(values):
    """Vorhandene IEC-Monats-/Stichtagswerte kompakt anzeigen."""
    rows = []
    for number in range(1, 100):
        suffix = f"{number:02d}"
        row = {
            "suffix": suffix,
            "total": _stored_historical_entry(values, "1.8.0", suffix),
            "t1": _stored_historical_entry(values, "1.8.1", suffix),
            "t2": _stored_historical_entry(values, "1.8.2", suffix),
            "t3": _stored_historical_entry(values, "1.8.3", suffix),
            "t4": _stored_historical_entry(values, "1.8.4", suffix),
        }
        if not any(row[key] for key in ("total", "t1", "t2", "t3", "t4")):
            continue
        date_entry = _stored_historical_entry(values, "0.1.2", suffix)
        row["date"] = _historical_date(date_entry.get("raw") if date_entry else None)
        rows.append(row)

    if not rows:
        return

    def column_has_nonzero(key):
        for row in rows:
            value = _stored_numeric_value(row[key])
            if value is None:
                # Nicht sicher numerisch interpretierbar: lieber anzeigen.
                if row[key] is not None:
                    return True
            elif value != 0:
                return True
        return False

    columns = [("total", "Gesamt"), ("t1", "Tarif 1"), ("t2", "Tarif 2")]
    if column_has_nonzero("t3"):
        columns.append(("t3", "Tarif 3"))
    if column_has_nonzero("t4"):
        columns.append(("t4", "Tarif 4"))

    print("\nMonatsvorwerte")
    print("==============\n")
    print(f"{'Datum':<12}", end="")
    for _, label in columns:
        print(f"{label:>12}", end="")
    print()

    for row in rows:
        label = row["date"] or f"Stichtag {row['suffix']}"
        print(f"{label:<12}", end="")
        units = set()
        for key, _ in columns:
            entry = row[key]
            if not entry:
                text = "-"
            else:
                unit = entry.get("unit")
                if unit:
                    units.add(unit)
                text = format_stored_value(entry)
                if unit and text.endswith(" " + unit):
                    text = text[:-(len(unit) + 1)]
            print(f"{text:>12}", end="")
        print(f" {next(iter(units))}" if len(units) == 1 else "")


def format_datetime(value):
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M:%S")
    except (ValueError, TypeError):
        return value or "-"


def values_to_dict(message):
    """
    Speicherung immer unter der vollstaendigen Originalkennung.
    Der normalisierte base_code ist zusaetzlich enthalten, aber niemals
    der Dictionary-Schluessel. Dadurch gehen 1-0:/1-1:-Unterschiede nicht
    verloren und kollidieren nicht.
    """
    values = {}
    for entry in message.values:
        item = {
            "raw": entry.raw,
            "value": entry.value,
            "unit": entry.unit,
            "base_code": getattr(entry, "base_code", entry.code),
        }
        groups = getattr(entry, "groups", None)
        if groups is not None:
            item["groups"] = groups
        for attr in (
            "sml_raw_value", "sml_scaler", "sml_unit_code",
            "sml_status", "sml_value_time", "sml_signature",
        ):
            if hasattr(entry, attr):
                item[attr] = _json_safe(getattr(entry, attr))
        item["value"] = _json_safe(item["value"])
        values[entry.code] = item
    return values


def get_entries(message, base_code):
    """
    Alle Werte mit passender semantischer OBIS-Suchkennung.

    Die vollstaendige Kennung und base_code bleiben unveraendert gespeichert.
    Nur fuer die Suche duerfen Standardselektoren wie *255 entfallen.
    Historische Selektoren wie &01 bleiben verschieden.
    """
    wanted = lookup_obis_code(base_code)
    return [
        entry for entry in message.values
        if lookup_obis_code(getattr(entry, "base_code", entry.code)) == wanted
    ]


def get_entry(message, base_code):
    """
    Liefert einen Wert nur dann eindeutig zurueck, wenn genau ein
    passender Basiscode existiert. So werden z. B. 1-0:1.8.0 und
    1-1:1.8.0 niemals stillschweigend miteinander verwechselt.
    """
    matches = get_entries(message, base_code)
    if len(matches) == 1:
        return matches[0]
    return None


def get_raw(message, base_code):
    entry = get_entry(message, base_code)
    return entry.raw if entry else None


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        if CONFIG_FILE.is_file():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                settings.update(data)
    except (OSError, json.JSONDecodeError):
        pass

    if settings.get("protocol_mode") not in ("auto", "sml", "iec"):
        settings["protocol_mode"] = "auto"
    return settings


def save_settings(settings):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(CONFIG_FILE)


def protocol_mode_label(mode):
    return {
        "auto": "Automatisch (SML -> IEC)",
        "sml": "Nur SML",
        "iec": "Nur IEC 62056-21",
    }.get(mode, "Automatisch (SML -> IEC)")


def meter_display_number(meter):
    """Benutzerseitige Zaehlernummer, technische ID bleibt interner Schluessel."""
    if meter is None:
        return "-"
    try:
        preferred = meter["preferred_number"]
    except (KeyError, IndexError):
        preferred = None

    def field(name):
        try:
            value = meter[name]
        except (KeyError, IndexError):
            return None
        return str(value).strip() if value is not None and str(value).strip() else None

    if preferred == "evu":
        return field("evu_number") or field("factory_number") or field("meter_uid") or "-"
    if preferred == "factory":
        return field("factory_number") or field("meter_uid") or "-"
    return field("meter_uid") or field("factory_number") or field("evu_number") or "-"


def clear_screen():
    """Terminalinhalt loeschen und Cursor links oben positionieren."""
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="", flush=True)


def screen_title(title):
    clear_screen()
    print(title)
    print("=" * len(title))
    print()


def pause():
    try:
        input("\nEnter zum Fortfahren ...")
    except EOFError:
        pass


def read_file(path):
    if not path.exists():
        raise RuntimeError(f"Datei nicht gefunden: {path}")
    return path.read_bytes()


def list_usb_devices():
    try:
        result = subprocess.run(
            ["termux-usb", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("termux-usb wurde nicht gefunden")

    if result.returncode != 0:
        raise RuntimeError(
            f"termux-usb -l fehlgeschlagen: "
            f"{result.stderr.strip() or 'unbekannter Fehler'}"
        )

    try:
        devices = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Ungueltige Ausgabe von termux-usb -l") from exc

    if not isinstance(devices, list):
        raise RuntimeError("Unerwartete USB-Geraeteliste")

    return [
        d for d in devices
        if isinstance(d, str) and d.startswith("/dev/bus/usb/")
    ]



def _diag_mask(value):
    if value is None:
        return "-"
    return "id-" + hashlib.sha256(
        str(value).encode("utf-8", errors="replace")
    ).hexdigest()[:12]


def _diag_android_info():
    result = {}
    # Fuer oeffentliche Supportberichte bewusst keine Modell-/Herstellerkennung.
    for key in ("ro.build.version.release", "ro.build.version.sdk"):
        try:
            p = subprocess.run(
                ["getprop", key], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, check=False,
            )
            result[key] = p.stdout.strip() or "-"
        except (FileNotFoundError, OSError):
            result[key] = "-"
    return result


def _diag_redact_reader_log(value):
    safe = []
    for line in value.splitlines():
        low = line.lower()
        if "zähler:" in low or "zaehler:" in low:
            safe.append(line.split(":", 1)[0] + ": [ANONYMISIERT]")
        else:
            safe.append(line)
    return "\n".join(safe)


def _diag_run(reader, device):
    return subprocess.run(
        ["termux-usb", "-r", "-e", str(reader), device],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def _diag_reader_build(reader):
    try:
        st = reader.stat()
        digest = hashlib.sha256(reader.read_bytes()).hexdigest()[:12]
        return f"sha256:{digest} size={st.st_size}"
    except OSError:
        return "NICHT VERFUEGBAR"


def _diag_usb_info(device):
    """USB-Metadaten ueber das vorhandene read-only usb_info-Tool erfassen."""
    tool = BASE_DIR / "usb" / "usb_info"
    result = {
        "status": "NICHT VERFUEGBAR",
        "vid_pid": "-",
        "manufacturer": "-",
        "product": "-",
        "usb_version": "-",
        "device_version": "-",
        "interfaces": [],
    }
    if not tool.is_file():
        return result
    try:
        p = subprocess.run(
            ["termux-usb", "-r", "-e", str(tool), device],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except OSError as exc:
        result["status"] = f"FEHLER: {exc}"
        return result

    text = p.stdout.decode("utf-8", errors="replace")
    result["status"] = f"Exit-Code {p.returncode}"
    for line in text.splitlines():
        s = line.strip()
        low = s.lower()
        # Seriennummer absichtlich weder speichern noch in den Bericht uebernehmen.
        if low.startswith("serial:"):
            continue
        if low.startswith("vid:pid:"):
            result["vid_pid"] = s.split(":", 2)[2].strip()
        elif low.startswith("manufacturer:"):
            result["manufacturer"] = s.split(":", 1)[1].strip()
        elif low.startswith("product:"):
            result["product"] = s.split(":", 1)[1].strip()
        elif low.startswith("usb-version:"):
            result["usb_version"] = s.split(":", 1)[1].strip()
        elif low.startswith("device-version:"):
            result["device_version"] = s.split(":", 1)[1].strip()
        elif low.startswith("interface ") or low.startswith("endpoint "):
            result["interfaces"].append(s)
    return result


def _diag_append_error(lines, code, detail):
    lines.extend([f"Fehlercode:      {code}", f"Fehlerdetails:   {detail}"])


def create_diagnostic_report():
    screen_title("DIAGNOSE")
    print("Die Diagnose speichert keine Messung in der Datenbank.")
    print("Es werden keine Rohtelegramme gespeichert.")
    print("Zaehler-IDs werden pseudonymisiert; Messwerte werden nicht exportiert.")
    print("USB-Seriennummer und Smartphone-Modell werden nicht in den Bericht aufgenommen.\n")
    try:
        choice = input("1  Diagnose starten\n0  Abbrechen\n\nAuswahl: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if choice != "1":
        return

    started = datetime.now().astimezone()
    android = _diag_android_info()
    mode = load_settings()["protocol_mode"]
    lines = [
        "TERMUMETER DIAGNOSTIC REPORT",
        "==============================",
        "",
        f"App-Version: {APP_VERSION}",
        f"Zeit:        {started.isoformat(timespec='seconds')}",
        f"Python:      {platform.python_version()}",
        f"Android:     {android['ro.build.version.release']} "
        f"(SDK {android['ro.build.version.sdk']})",
        f"Modus:       {protocol_mode_label(mode)}",
        "",
        "READER-BUILDS",
        "-------------",
        f"SML: {_diag_reader_build(SML_READER)}",
        f"IEC: {_diag_reader_build(IEC_READER)}",
        "",
    ]

    final_code = "UNKNOWN_ERROR"
    success = False

    try:
        devices = list_usb_devices()
        lines.append(f"USB-Geraete: {len(devices)}")
        if not devices:
            final_code = "USB_NOT_FOUND"
            raise RuntimeError("Kein USB-Geraet gefunden")
        if len(devices) > 1:
            final_code = "MULTIPLE_USB_DEVICES"
            raise RuntimeError("Mehrere USB-Geraete vorhanden")

        device = devices[0]
        usb = _diag_usb_info(device)
        lines.extend([
            "",
            "USB-TASTKOPF",
            "------------",
            f"usb_info:       {usb['status']}",
            f"VID:PID:        {usb['vid_pid']}",
            f"Hersteller:     {usb['manufacturer']}",
            f"Produkt:        {usb['product']}",
            f"USB-Version:    {usb['usb_version']}",
            f"Device-Version: {usb['device_version']}",
        ])
        for item in usb["interfaces"]:
            lines.append(f"  {item}")
        lines.append("Seriennummer:   NICHT ERFASST")
        lines.append("")

        # Im Automatikmodus werden SML und IEC diagnostisch unabhaengig bewertet.
        # Ein kaputtes/ungewoehnliches SML-Telegramm verhindert daher nicht mehr
        # den anschliessenden IEC-Versuch.
        if mode in ("auto", "sml"):
            print("\nDiagnose: SML ...")
            lines.extend(["SML", "---"])
            sml_ok = False
            try:
                r = _diag_run(SML_READER, device)
                log = r.stderr.decode("utf-8", errors="replace").rstrip()
                lines.append(f"Exit-Code:       {r.returncode}")
                if log:
                    lines.append(_diag_redact_reader_log(log))

                if r.returncode == 10 or not r.stdout:
                    code = "NO_SML_FRAME"
                    lines.append("Ergebnis:        Kein SML-Telegramm im Zeitfenster")
                    _diag_append_error(lines, code, "Kein passiver SML-Frame empfangen")
                elif r.returncode != 0:
                    code = "SML_READER_FAILED"
                    _diag_append_error(lines, code, f"Reader Exit-Code {r.returncode}")
                else:
                    try:
                        binary = bytes.fromhex(r.stdout.decode("ascii").strip())
                        frames = parse_sml_bytes(binary)
                    except Exception as exc:
                        code = "SML_PARSE_FAILED"
                        _diag_append_error(lines, code, f"{type(exc).__name__}: {exc}")
                    else:
                        lines.append(f"Telegramm-Bytes: {len(binary)}")
                        lines.append(f"Frames:           {len(frames)}")
                        if len(frames) != 1:
                            code = "SML_FRAME_COUNT"
                            _diag_append_error(lines, code, f"Frames={len(frames)}")
                        else:
                            frame, values = frames[0]
                            uid = sml_meter_id(values)
                            lines.append(f"CRC:              {'OK' if frame.crc_valid else 'FEHLER'}")
                            lines.append(f"Datensaetze:      {len(values)}")
                            lines.append(
                                f"Zaehler-ID:       {_diag_mask(uid) if uid else 'NICHT GEFUNDEN'}"
                            )
                            lines.append("OBIS (ohne Messwerte):")
                            for value in values:
                                lines.append(f"  {value.code}  unit={value.unit or '-'}")
                            if not frame.crc_valid:
                                code = "CRC_FAILED"
                                _diag_append_error(lines, code, "SML CRC-Pruefung fehlgeschlagen")
                            elif not uid:
                                code = "METER_ID_NOT_FOUND"
                                _diag_append_error(lines, code, "Keine SML-Zaehlerkennung gefunden")
                            else:
                                code = "OK_SML"
                                sml_ok = True
                                success = True
                                final_code = code
                                lines.append("Ergebnis:        SML erfolgreich")
            except Exception as exc:
                code = "SML_DIAGNOSTIC_FAILED"
                _diag_append_error(lines, code, f"{type(exc).__name__}: {exc}")
            lines.append("")
            if mode == "sml":
                final_code = code

        if mode in ("auto", "iec"):
            # Bei Auto immer IEC versuchen, auch wenn SML Daten/Parser/ID-Probleme hatte.
            print("\nDiagnose: IEC 62056-21 ...")
            lines.extend(["IEC 62056-21", "-------------"])
            iec_ok = False
            try:
                r = _diag_run(IEC_READER, device)
                log = r.stderr.decode("utf-8", errors="replace").rstrip()
                lines.append(f"Exit-Code:       {r.returncode}")
                if log:
                    lines.append(_diag_redact_reader_log(log))

                if r.returncode != 0:
                    code = "NO_IEC_IDENTIFICATION" if r.returncode == 1 else "IEC_READER_FAILED"
                    _diag_append_error(lines, code, f"Reader Exit-Code {r.returncode}")
                elif not r.stdout:
                    code = "NO_IEC_DATA"
                    _diag_append_error(lines, code, "Keine IEC-Daten empfangen")
                else:
                    try:
                        msg = parse_iec_message(r.stdout)
                    except Exception as exc:
                        code = "IEC_PARSE_FAILED"
                        _diag_append_error(lines, code, f"{type(exc).__name__}: {exc}")
                    else:
                        try:
                            ident = meter_identity(msg)
                            uid = ident["technical_id"]
                            uid_source = ident["technical_source"]
                        except RuntimeError:
                            uid = None
                            uid_source = None
                        lines.append(f"Telegramm-Bytes: {len(r.stdout)}")
                        lines.append(f"BCC:              {'OK' if msg.bcc_valid else 'FEHLER'}")
                        lines.append(f"Datensaetze:      {len(msg.values)}")
                        lines.append(
                            f"Zaehler-ID:       {_diag_mask(uid) if uid else 'NICHT GEFUNDEN'}"
                        )
                        if uid_source:
                            lines.append(f"ID-Quelle:        {uid_source}")
                        lines.append("OBIS (ohne Messwerte):")
                        for entry in msg.values:
                            lines.append(f"  {entry.code}  unit={entry.unit or '-'}")
                        if not msg.bcc_valid:
                            code = "BCC_FAILED"
                            _diag_append_error(lines, code, "IEC BCC-Pruefung fehlgeschlagen")
                        elif not uid:
                            code = "METER_ID_NOT_FOUND"
                            _diag_append_error(lines, code, "Keine IEC-Zaehlerkennung gefunden")
                        else:
                            code = "OK_IEC"
                            iec_ok = True
                            success = True
                            final_code = code
                            lines.append("Ergebnis:        IEC erfolgreich")
            except Exception as exc:
                code = "IEC_DIAGNOSTIC_FAILED"
                _diag_append_error(lines, code, f"{type(exc).__name__}: {exc}")
            lines.append("")
            if mode == "iec":
                final_code = code
            elif not iec_ok and not success:
                final_code = code

        if mode == "auto" and success:
            # Erfolg eines der Protokolle ist fuer den Gesamtstatus ausreichend;
            # beide Teilresultate bleiben vollstaendig im Bericht sichtbar.
            final_code = "OK"

    except Exception as exc:
        lines.extend([
            "", "FEHLER", "------", f"Code:    {final_code}",
            f"Details: {type(exc).__name__}: {exc}",
        ])

    lines.extend([
        "ERGEBNIS", "--------", f"Code: {final_code}", "",
        "DATENSCHUTZ", "-----------",
        "Keine Rohtelegramme enthalten.",
        "Keine USB-Seriennummer enthalten.",
        "Kein Smartphone-Modell/Hersteller enthalten.",
        "Erkannte Zaehler-IDs werden nur als SHA-256-Pseudonym ausgegeben.",
        "OBIS-Kennungen und Einheiten koennen enthalten sein; Messwerte nicht.",
        "",
    ])

    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
    path = DIAGNOSTIC_DIR / (
        "termumeter-diagnostic-" + started.strftime("%Y%m%d-%H%M%S") + ".txt"
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    print("\nDiagnose abgeschlossen.")
    print(f"Ergebnis: {final_code}")
    print(f"Bericht:  {path}")


def run_iec_reader(device):
    if not IEC_READER.is_file():
        raise RuntimeError(f"IEC-Reader nicht gefunden: {IEC_READER}")

    print(f"\nUSB-Geraet: {device}")
    print("Starte IEC-Auslesung ...\n")

    result = subprocess.run(
        ["termux-usb", "-r", "-e", str(IEC_READER), device],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    if result.stderr:
        print(
            result.stderr.decode("utf-8", errors="replace").rstrip(),
            file=sys.stderr,
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"IEC-Reader fehlgeschlagen (Exit-Code {result.returncode})"
        )
    if not result.stdout:
        raise RuntimeError("Keine Daten vom Zaehler empfangen")
    return result.stdout


def choose_usb_device():
    devices = list_usb_devices()
    if not devices:
        raise RuntimeError("Kein USB-Geraet gefunden")
    if len(devices) > 1:
        raise RuntimeError(
            "Mehrere USB-Geraete vorhanden. "
            "FTDI-Auswahl wird spaeter ergaenzt."
        )
    return devices[0]



class LiveMessage:
    """Kleine protokollneutrale Sicht fuer Anzeige und Speicherung."""
    def __init__(self, protocol, values, valid, meter_uid,
                 meter_date=None, meter_time=None):
        self.protocol = protocol
        self.values = values
        self.bcc_valid = bool(valid)  # DB-Feld bleibt aus Kompatibilitaetsgruenden so benannt.
        self.meter_uid = meter_uid
        self.meter_date = meter_date
        self.meter_time = meter_time


class SmlEntryAdapter:
    def __init__(self, entry):
        self.code = entry.code
        self.base_code = entry.base_code
        self.unit = entry.unit
        self.groups = None

        if isinstance(entry.value, bytes):
            self.raw = entry.value.hex()
            self.value = self.raw
        else:
            scaled = entry.scaled_value
            self.value = scaled if scaled is not None else entry.value
            self.raw = str(self.value)

        # SML-Zusatzinformationen verlustfrei fuer SQLite.
        self.sml_raw_value = (
            entry.value.hex() if isinstance(entry.value, bytes) else entry.value
        )
        self.sml_scaler = entry.scaler
        self.sml_unit_code = entry.unit_code
        self.sml_status = entry.status
        self.sml_value_time = entry.value_time
        self.sml_signature = (
            entry.signature.hex()
            if isinstance(entry.signature, bytes)
            else entry.signature
        )


def _json_safe(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    # Decimal und aehnliche exakte Zahlen als Text speichern.
    if value.__class__.__name__ == "Decimal":
        return str(value)
    return value


def parse_sml_hex_output(raw):
    try:
        text = raw.decode("ascii").strip()
        binary = bytes.fromhex(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Ungueltige Hex-Ausgabe des SML-Readers") from exc

    frames = parse_sml_bytes(binary)
    if len(frames) != 1:
        raise RuntimeError(
            f"Erwartet wurde ein SML-Telegramm, empfangen: {len(frames)}"
        )

    frame, values = frames[0]
    uid = sml_meter_id(values)

    return LiveMessage(
        protocol="sml",
        values=[SmlEntryAdapter(v) for v in values],
        valid=frame.crc_valid,
        meter_uid=uid,
    )


def run_sml_reader(device, quiet_timeout=False):
    if not SML_READER.is_file():
        if quiet_timeout:
            return None
        raise RuntimeError("SML-Reader nicht gefunden")

    print(f"\nUSB-Geraet: {device}")
    print("Pruefe passiv auf SML ...\n")

    result = subprocess.run(
        ["termux-usb", "-r", "-e", str(SML_READER), device],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    # Exit 10 ist der definierte "kein SML im Zeitfenster"-Status.
    # Im Automatikmodus ist das ein normaler Protokoll-Fallback und wird
    # deshalb nicht als Fehler auf stderr ausgegeben.
    if result.returncode == 10:
        return None

    if result.stderr:
        print(
            result.stderr.decode("utf-8", errors="replace").rstrip(),
            file=sys.stderr,
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"SML-Reader fehlgeschlagen (Exit-Code {result.returncode})"
        )
    if not result.stdout:
        return None

    return parse_sml_hex_output(result.stdout)

def read_live_message():
    device = choose_usb_device()
    mode = load_settings()["protocol_mode"]

    if mode in ("auto", "sml"):
        sml = run_sml_reader(device, quiet_timeout=(mode == "auto"))
        if sml is not None:
            print("\nProtokoll erkannt: SML")
            return sml
        if mode == "sml":
            raise RuntimeError(
                "Im Modus 'Nur SML' wurde kein SML-Telegramm erkannt."
            )
        print("\nKein SML erkannt - versuche IEC 62056-21 ...")

    iec = parse_iec_message(run_iec_reader(device))
    iec.protocol = "iec62056"
    iec.meter_uid = None
    iec.meter_date = get_raw(iec, "0.9.2")
    iec.meter_time = get_raw(iec, "0.9.1")
    print("\nProtokoll erkannt: IEC 62056-21")
    return iec


def _clean_identity_value(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def meter_identity(message):
    """Technische ID und bekannte Identitaetsmetadaten bestimmen.

    meter_uid bleibt der stabile interne Wiedererkennungsschluessel.
    Beim live beobachteten MT371 ist 0.0.0 (1ISK1000/1ISK0000) nicht
    individuell; dort wird C.1.0 als Fabriknummer und technische ID benutzt.
    """
    protocol = getattr(message, "protocol", None)

    if protocol == "sml":
        uid = _clean_identity_value(getattr(message, "meter_uid", None))
        if not uid:
            raise RuntimeError("Keine eindeutige SML-Zaehlerkennung gefunden")
        return {
            "technical_id": uid,
            "technical_source": "sml",
            "factory_number": None,
        }

    zero_id = _clean_identity_value(get_raw(message, "0.0.0"))
    factory = _clean_identity_value(get_raw(message, "C.1.0"))

    # Iskraemeco MT371: diese beobachteten 0.0.0-Werte sind Serienpraefixe
    # und duerfen nicht mehrere physische Zaehler auf dieselbe UID abbilden.
    if zero_id in {"1ISK1000", "1ISK0000"}:
        if not factory:
            raise RuntimeError(
                "Iskraemeco-Praefix erkannt, aber keine Fabriknummer in C.1.0 gefunden"
            )
        return {
            "technical_id": factory,
            "technical_source": "obis:C.1.0",
            "factory_number": factory,
        }

    if zero_id:
        return {
            "technical_id": zero_id,
            "technical_source": "obis:0.0.0",
            "factory_number": None,
        }

    if factory:
        return {
            "technical_id": factory,
            "technical_source": "obis:C.1.0",
            "factory_number": factory,
        }

    raise RuntimeError("Keine eindeutige Zaehlerkennung gefunden")


def identify_meter(message):
    return meter_identity(message)["technical_id"]


def _ask_yes_no(prompt, default=True):
    suffix = " [J/n]: " if default else " [j/N]: "
    try:
        answer = input(prompt + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("j", "ja", "y", "yes")


def register_meter_if_needed(meter_uid, current_message):
    meter = get_meter_by_uid(meter_uid)
    if meter is not None:
        return meter

    identity = meter_identity(current_message)
    factory = identity["factory_number"]
    source = identity["technical_source"]

    # Upgrade-Pfad fuer bereits mit dem nicht eindeutigen MT371-Praefix
    # gespeicherte Daten. Nur nach ausdruecklicher Bestaetigung umschluesseln.
    if source == "obis:C.1.0" and factory:
        legacy = get_meter_by_uid("1ISK1000") or get_meter_by_uid("1ISK0000")
        if legacy is not None:
            print("\nBestehender MT371-Eintrag gefunden")
            print("================================\n")
            print(f"Alter technischer Schluessel: {legacy['meter_uid']}")
            print(f"Ausgelesene Fabriknummer:     {factory}")
            if _ask_yes_no("Bestehenden Eintrag auf die Fabriknummer umstellen?"):
                rekey_meter(legacy["id"], meter_uid)
                set_meter_identity(
                    legacy["id"],
                    factory_number=factory,
                    technical_source=source,
                    preferred_number="factory",
                )
                print("Bestehende Auslesungen bleiben erhalten.")
                return get_meter_by_uid(meter_uid)
            print("Alter Eintrag bleibt unveraendert.\n")

    print("\nNeuer Zaehler erkannt")
    print("=====================\n")
    print(f"Technische ID: {meter_uid}")
    print(f"Erkannt ueber: {source}")
    if factory:
        print(f"Fabriknummer:  {factory}")

    # Eine automatisch gelesene Fabriknummer kann einmalig mit dem
    # Typenschild abgeglichen werden. Die technische Wiedererkennung bleibt
    # davon unberuehrt.
    if factory:
        print()
        if not _ask_yes_no("Stimmt die Fabriknummer mit dem Typenschild ueberein?"):
            print(
                "\nHinweis: Die Auslesung wird nicht automatisch umgedeutet. "
                "Bitte Diagnosebericht erstellen und die Typenschildangabe pruefen."
            )

    evu_number = None
    preferred = "factory" if factory else "technical"

    if factory:
        print("\nWelche Nummer soll in der Anwendung bevorzugt angezeigt werden?")
        print(f"1  Fabriknummer: {factory}")
        print("2  EVU-/Eigentumsnummer vom Typenschild eingeben")
        try:
            choice = input("Auswahl [1]: ").strip()
        except EOFError:
            choice = ""
        if choice == "2":
            try:
                evu_number = input("EVU-/Eigentumsnummer: ").strip()
            except EOFError:
                evu_number = ""
            evu_number = _clean_identity_value(evu_number)
            if evu_number:
                preferred = "evu"

    try:
        name = input("\nName fuer diesen Zaehler (optional): ").strip()
    except EOFError:
        name = ""
    if not name:
        name = evu_number or factory or meter_uid

    create_meter(
        meter_uid=meter_uid,
        protocol=getattr(current_message, "protocol", "iec62056"),
        identification=None,
        name=name,
        technical_source=source,
        factory_number=factory,
        evu_number=evu_number,
        preferred_number=preferred,
    )
    print(f"\nGespeichert als: {name}")
    return get_meter_by_uid(meter_uid)


def _manual_identity_fallback(message):
    """Fallback nur wenn das Telegramm keine eindeutige technische ID liefert."""
    print("\nKeine eindeutige Zaehler-ID automatisch erkannt")
    print("==============================================\n")
    print("Das Telegramm wurde gelesen, kann aber keinem Zaehler eindeutig")
    print("zugeordnet werden. Rohdaten werden dabei nicht veraendert.\n")
    print("1  Bestehendem Zaehler manuell zuordnen")
    print("2  Neuen Zaehler mit Nummer vom Typenschild anlegen")
    print("0  Auslesung nicht speichern")

    try:
        choice = input("\nAuswahl: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise RuntimeError("Auslesung ohne Zaehlerzuordnung abgebrochen")

    if choice == "1":
        meters = list_meters()
        if not meters:
            raise RuntimeError("Noch kein bestehender Zaehler fuer eine manuelle Zuordnung vorhanden")
        print()
        show_meters()
        try:
            meter_id = int(input("Zaehler-ID: ").strip())
        except (ValueError, EOFError):
            raise RuntimeError("Ungueltige Zaehler-ID")
        meter = get_meter_by_id(meter_id)
        if meter is None:
            raise RuntimeError("Zaehler nicht gefunden")
        if not _ask_yes_no(
            f"Auslesung wirklich '{meter['name']}' ({meter_display_number(meter)}) zuordnen?",
            default=False,
        ):
            raise RuntimeError("Manuelle Zuordnung abgebrochen")
        return meter

    if choice == "2":
        print("\nDie Nummer muss eindeutig vom Typenschild des angeschlossenen")
        print("Zaehler stammen. Sie dient als manueller technischer Schluessel.")
        try:
            manual_id = input("Nummer vom Typenschild: ").strip()
        except EOFError:
            manual_id = ""
        manual_id = _clean_identity_value(manual_id)
        if not manual_id:
            raise RuntimeError("Keine Typenschildnummer eingegeben")

        existing = get_meter_by_uid(manual_id)
        if existing is not None:
            if _ask_yes_no(
                f"Nummer gehoert bereits zu '{existing['name']}'. Diesem Zaehler zuordnen?",
                default=False,
            ):
                return existing
            raise RuntimeError("Manuelle Zuordnung abgebrochen")

        print("\nWelche Art von Nummer wurde eingegeben?")
        print("1  Fabrik-/Seriennummer")
        print("2  EVU-/Eigentumsnummer")
        try:
            kind = input("Auswahl: ").strip()
        except EOFError:
            kind = ""
        if kind not in ("1", "2"):
            raise RuntimeError("Nummernart nicht ausgewaehlt")

        factory = manual_id if kind == "1" else None
        evu = manual_id if kind == "2" else None
        preferred = "factory" if factory else "evu"

        try:
            name = input("Name fuer diesen Zaehler (optional): ").strip()
        except EOFError:
            name = ""
        if not name:
            name = manual_id

        if not _ask_yes_no(
            f"Neuen Zaehler '{name}' mit Typenschildnummer {manual_id} anlegen?",
            default=False,
        ):
            raise RuntimeError("Manuelle Anlage abgebrochen")

        create_meter(
            meter_uid=manual_id,
            protocol=getattr(message, "protocol", "unknown"),
            identification=None,
            name=name,
            technical_source="manual:typeplate",
            factory_number=factory,
            evu_number=evu,
            preferred_number=preferred,
        )
        return get_meter_by_uid(manual_id)

    raise RuntimeError("Auslesung ohne Zaehlerzuordnung abgebrochen")


def resolve_meter(message):
    """Automatische Identitaet bevorzugen, manuelle Zuordnung nur als Fallback."""
    try:
        meter_uid = identify_meter(message)
    except RuntimeError:
        return _manual_identity_fallback(message)
    return register_meter_if_needed(meter_uid, message)


def store_message(message, series_id=None, resolved_meter=None):
    meter = resolved_meter if resolved_meter is not None else resolve_meter(message)

    # Verbindlicher Messzeitpunkt ist die Systemzeit des Handys.
    read_at = datetime.now().astimezone().isoformat(timespec="seconds")

    # Zaehlereigene RTC nur als Zusatzinformation.
    reading_id = save_reading(
        meter_id=meter["id"],
        read_at=read_at,
        meter_date=getattr(message, "meter_date", None) or get_raw(message, "0.9.2"),
        meter_time=getattr(message, "meter_time", None) or get_raw(message, "0.9.1"),
        bcc_valid=message.bcc_valid,
        values=values_to_dict(message),
        protocol=getattr(message, "protocol", None),
        series_id=series_id,
    )
    note_meter_protocol(meter["id"], getattr(message, "protocol", "iec62056"))
    return get_meter_by_uid(meter["meter_uid"]), reading_id, read_at


IMPORTANT_VALUES = [
    ("1.8.0", "Gesamtbezug"),
    ("1.8.1", "Tarif 1"),
    ("1.8.2", "Tarif 2"),
    ("1.8.3", "Tarif 3"),
    ("1.8.4", "Tarif 4"),
    ("2.8.0", "Einspeisung"),
    ("16.7.0", "Leistung"),
    ("32.7.0", "Spannung L1"),
    ("52.7.0", "Spannung L2"),
    ("72.7.0", "Spannung L3"),
]


def print_compact(message, meter, reading_id, read_at):
    print("\nAuslesung erfolgreich")
    print("=====================\n")
    print(f"Zaehler:       {meter['name']}")
    print(f"Zaehlernummer: {meter_display_number(meter)}")
    print(f"Messzeitpunkt: {format_datetime(read_at)}")
    check_name = "CRC" if getattr(message, "protocol", "") == "sml" else "BCC"
    print(f"Protokoll:     {getattr(message, 'protocol', '-')}")
    print(f"{check_name + ':':<14}{'OK' if message.bcc_valid else 'FEHLER'}\n")

    for code, label in IMPORTANT_VALUES:
        entry = get_entry(message, code)
        if entry:
            print(f"{label:<14} {format_value(entry)}")

    print(f"\n{len(message.values)} Datensaetze gespeichert")
    print(f"Messung #{reading_id}")


def print_all_live(message):
    print("\nDetaildaten")
    print("===========\n")
    for entry in message.values:
        print(f"{entry.code:<12} {format_value(entry)}")


def print_stored_reading(reading, show_all=False, show_monthly=False):
    values = decode_values(reading)
    meter = get_meter_by_id(reading["meter_id"])

    print("Gespeicherte Auslesung")
    print("======================\n")
    print(f"Messung:       #{reading['id']}")
    print(f"Zaehler:       {reading['name']}")
    print(f"Zaehlernummer: {meter_display_number(meter) if meter is not None else reading['meter_uid']}")
    print(f"Messzeitpunkt: {format_datetime(reading['read_at'])}")
    protocol = reading["protocol"] if "protocol" in reading.keys() else None
    check_name = "CRC" if protocol == "sml" else "BCC"
    print(f"{check_name + ':':<14}{'OK' if reading['bcc_valid'] else 'FEHLER'}\n")

    for code, label in IMPORTANT_VALUES:
        entry = get_stored_entry(values, code)
        if entry is not None:
            print(f"{label:<14} {format_stored_value(entry)}")

    if show_monthly:
        print_monthly_values(values)

    print(f"\n{len(values)} Datensaetze gespeichert")

    if show_all:
        print("\nDetaildaten")
        print("===========\n")
        for code, entry in values.items():
            print(f"{code:<12} {format_stored_value(entry)}")


def show_last(meter_id=None, show_all=False):
    if meter_id is not None and get_meter_by_id(meter_id) is None:
        raise RuntimeError(f"Zaehler {meter_id} nicht gefunden")
    reading = get_last_reading(meter_id)
    if reading is None:
        raise RuntimeError("Keine gespeicherte Auslesung gefunden")
    print_stored_reading(reading, show_all)


def show_history(meter_id=None):
    if meter_id is not None and get_meter_by_id(meter_id) is None:
        raise RuntimeError(f"Zaehler {meter_id} nicht gefunden")

    readings = get_history(meter_id=meter_id, limit=50)
    print("Auslesungsverlauf")
    print("=================\n")
    if not readings:
        print("Keine Auslesungen gespeichert.")
        return

    for reading in readings:
        values = decode_values(reading)
        meter = get_meter_by_id(reading["meter_id"])
        print(f"#{reading['id']}  {format_datetime(reading['read_at'])}")
        print(f"    Zaehler:      {reading['name']}")
        print(f"    Zaehl.-Nr.:   {meter_display_number(meter) if meter is not None else reading['meter_uid']}")
        print(
            f"    Gesamtbezug:  "
            f"{format_stored_value(get_stored_entry(values, '1.8.0'))}"
        )
        protocol = reading["protocol"] if "protocol" in reading.keys() else None
        check_name = "CRC" if protocol == "sml" else "BCC"
        print(
            f"    {check_name + ':':<14}"
            f"{'OK' if reading['bcc_valid'] else 'FEHLER'}"
        )
        print()


def preferred_meter_number(meter):
    keys = set(meter.keys())
    preferred = meter["preferred_number"] if "preferred_number" in keys else "technical"
    if preferred == "evu" and "evu_number" in keys and meter["evu_number"]:
        return meter["evu_number"]
    if preferred == "factory" and "factory_number" in keys and meter["factory_number"]:
        return meter["factory_number"]
    return meter["meter_uid"]


def show_meters():
    meters = list_meters()
    print("Bekannte Zaehler")
    print("================\n")
    if not meters:
        print("Noch keine Zaehler gespeichert.")
        return

    for meter in meters:
        marker = " [HA]" if meter["ha_enabled"] else ""
        print(f"{meter['id']}: {meter['name']}{marker}")
        print(f"   Zaehl.-Nr.: {preferred_meter_number(meter)}")
        if meter["factory_number"]:
            print(f"   Fabriknr.:  {meter['factory_number']}")
        if meter["evu_number"]:
            print(f"   EVU-Nr.:    {meter['evu_number']}")
        print(f"   Techn. ID:  {meter['meter_uid']} ({meter['technical_source'] or 'legacy'})")
        print(f"   Auslesungen: {meter['reading_count']}")
        if meter["last_reading"]:
            print(f"   Letzte: {format_datetime(meter['last_reading'])}")
        print()


def rename_meter():
    show_meters()
    try:
        meter_id = int(input("Zaehler-ID: ").strip())
    except (ValueError, EOFError):
        print("Ungueltige ID.")
        return

    meter = get_meter_by_id(meter_id)
    if meter is None:
        print("Zaehler nicht gefunden.")
        return

    print(f"\nAktueller Name: {meter['name']}")
    try:
        new_name = input("Neuer Name: ").strip()
    except EOFError:
        return

    if not new_name:
        print("Name nicht geaendert.")
        return

    set_meter_name(meter_id, new_name)
    print("\nName gespeichert.")


def edit_meter_identity():
    show_meters()
    try:
        meter_id = int(input("Zaehler-ID: ").strip())
    except (ValueError, EOFError):
        print("Ungueltige ID.")
        return
    meter = get_meter_by_id(meter_id)
    if meter is None:
        print("Zaehler nicht gefunden.")
        return

    print(f"\nTechnische ID: {meter['meter_uid']}")
    print(f"Fabriknummer:  {meter['factory_number'] or '-'}")
    print(f"EVU-Nr.:       {meter['evu_number'] or '-'}")
    print("\n1  Fabriknummer bevorzugen")
    print("2  EVU-/Eigentumsnummer eingeben und bevorzugen")
    print("3  Technische ID bevorzugen")
    print("0  Abbrechen")
    try:
        choice = input("\nAuswahl: ").strip()
    except EOFError:
        return

    if choice == "1":
        if not meter["factory_number"]:
            print("Keine Fabriknummer gespeichert.")
            return
        set_meter_identity(meter_id, preferred_number="factory")
    elif choice == "2":
        try:
            evu = input("EVU-/Eigentumsnummer: ").strip()
        except EOFError:
            return
        if not evu:
            print("Keine Nummer eingegeben.")
            return
        set_meter_identity(meter_id, evu_number=evu, preferred_number="evu")
    elif choice == "3":
        set_meter_identity(meter_id, preferred_number="technical")
    else:
        return
    print("\nIdentitaetsanzeige gespeichert.")


def choose_ha_meter():
    meters = list_meters()
    if not meters:
        print("Noch keine Zaehler gespeichert.")
        return

    show_meters()
    try:
        meter_id = int(input("ID des HA-Zaehler: ").strip())
    except (ValueError, EOFError):
        print("Ungueltige ID.")
        return

    if meter_id not in {m["id"] for m in meters}:
        print("Zaehler nicht gefunden.")
        return

    set_ha_meter(meter_id)
    print("\nHA-Zaehler gespeichert.")


def perform_live_read(show_all=False):
    print("Neue Zaehlerauslesung")
    print("=====================")
    message = read_live_message()

    if not message.bcc_valid:
        raise RuntimeError(
            f"{'CRC' if getattr(message, 'protocol', '') == 'sml' else 'BCC'}-Pruefung "
            "fehlgeschlagen. Messung wird nicht gespeichert."
        )

    meter, reading_id, read_at = store_message(message)
    print_compact(message, meter, reading_id, read_at)
    if show_all:
        print_all_live(message)


def _read_positive_int(prompt, minimum=1):
    try:
        value = int(input(prompt).strip())
    except (ValueError, EOFError):
        return None
    return value if value >= minimum else None


def _format_decimal_compact(value, min_decimals=0):
    """Decimal ohne unnoetige Nachkommastellen darstellen."""
    if value is None:
        return "-"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if min_decimals > 0:
        if "." not in text:
            text += "." + ("0" * min_decimals)
        else:
            decimals = len(text.split(".", 1)[1])
            if decimals < min_decimals:
                text += "0" * (min_decimals - decimals)
    return text


def _format_series_interval(seconds):
    if seconds % 3600 == 0:
        value = seconds // 3600
        return f"{value} Stunde" if value == 1 else f"{value} Stunden"
    if seconds % 60 == 0:
        value = seconds // 60
        return f"{value} Minute" if value == 1 else f"{value} Minuten"
    return f"{seconds} Sekunden"


def _format_series_duration(seconds):
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} s")
    return " ".join(parts)


def _series_countdown(target_monotonic, current, total, successful, failed, meter):
    while True:
        remaining = target_monotonic - time.monotonic()
        if remaining <= 0:
            return
        screen_title("MESSREIHE")
        if meter is not None:
            print(f"Zaehler:     {meter['name']}")
            print(f"Zaehl.-Nr.:  {meter_display_number(meter)}")
        else:
            print("Zaehler:     noch nicht erkannt")
        print(f"Messung:     {current} von {total}")
        print(f"Erfolgreich: {successful}")
        print(f"Fehler:      {failed}\n")
        print(f"Naechste Messung in {int(remaining + 0.999):02d} s")
        print("\nCtrl+C  Messreihe beenden")
        time.sleep(min(1.0, remaining))


def perform_measurement_series(count, interval_seconds):
    successful = 0
    failed = 0
    meter = None
    meter_uid = None
    series_id = None
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    start_monotonic = time.monotonic()
    status = "completed"

    try:
        for index in range(count):
            if index > 0:
                target = start_monotonic + index * interval_seconds
                _series_countdown(target, index + 1, count, successful, failed, meter)

            screen_title("MESSREIHE")
            print(f"Messung {index + 1} von {count}")
            print(f"Erfolgreich: {successful}")
            print(f"Fehler:      {failed}\n")
            print("Starte Zaehlerauslesung ...\n")

            try:
                message = read_live_message()
                if not message.bcc_valid:
                    check = "CRC" if getattr(message, "protocol", "") == "sml" else "BCC"
                    raise RuntimeError(
                        f"{check}-Pruefung fehlgeschlagen. Messung wird nicht gespeichert."
                    )

                try:
                    current_uid = identify_meter(message)
                except RuntimeError:
                    current_uid = None

                if meter_uid is None:
                    meter = resolve_meter(message)
                    meter_uid = meter["meter_uid"]
                    series_id = create_measurement_series(
                        meter_id=meter["id"],
                        started_at=started_at,
                        requested_count=count,
                        interval_seconds=interval_seconds,
                    )
                elif current_uid is not None and current_uid != meter_uid:
                    raise RuntimeError(
                        "Anderer Zaehler erkannt. Messung wird nicht gespeichert."
                    )

                meter, reading_id, read_at = store_message(
                    message, series_id=series_id, resolved_meter=meter
                )
                successful += 1
                print(f"\nGespeichert: Messung #{reading_id}")
                print(f"Zeitpunkt:   {format_datetime(read_at)}")
            except (RuntimeError, ValueError) as exc:
                failed += 1
                print(f"\nFehler bei Messung {index + 1}: {exc}", file=sys.stderr)

    except KeyboardInterrupt:
        status = "aborted"
        print("\n\nMessreihe durch Benutzer beendet.")
    finally:
        if series_id is not None:
            finish_measurement_series(
                series_id=series_id,
                completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                successful_count=successful,
                failed_count=failed,
                status=status,
            )

    screen_title("MESSREIHE BEENDET" if status == "completed" else "MESSREIHE ABGEBROCHEN")
    if meter is not None:
        print(f"Zaehler:     {meter['name']}")
        print(f"Zaehl.-Nr.:  {meter_display_number(meter)}")
    print(f"Geplant:     {count}")
    print(f"Erfolgreich: {successful}")
    print(f"Fehler:      {failed}")
    if status == "aborted":
        print(f"Nicht gestartet: {max(0, count - successful - failed)}")
    if series_id is not None:
        print(f"Messreihe:   #{series_id}")
    elif successful == 0:
        print("\nKeine gueltige Messung gespeichert; keine Messreihe in SQLite angelegt.")


def configure_measurement_series():
    screen_title("MESSREIHE")
    count = _read_positive_int("Anzahl Messungen (mind. 2): ", minimum=2)
    if count is None:
        print("\nUngueltige Anzahl.")
        pause()
        return

    print("\nIntervall")
    print("1  Sekunden")
    print("2  Minuten")
    print("3  Stunden")
    print("0  Abbrechen\n")
    try:
        unit_choice = input("Auswahl: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if unit_choice == "0":
        return
    factor = {"1": 1, "2": 60, "3": 3600}.get(unit_choice)
    if factor is None:
        print("\nUngueltige Auswahl.")
        pause()
        return

    value = _read_positive_int("Intervall: ", minimum=1)
    if value is None:
        print("\nUngueltiges Intervall.")
        pause()
        return
    interval_seconds = value * factor
    if interval_seconds < 10:
        print("\nDas Intervall muss mindestens 10 Sekunden betragen.")
        pause()
        return

    duration = (count - 1) * interval_seconds
    screen_title("MESSREIHE STARTEN")
    print(f"Messungen:              {count}")
    print(f"Intervall:              {_format_series_interval(interval_seconds)}")
    print(f"Voraussichtliche Dauer: {_format_series_duration(duration)}")
    print("\nDie Startzeitpunkte bleiben am gewaehlten Intervall ausgerichtet.")
    print("Einzelne Lesefehler brechen die Messreihe nicht ab.\n")
    print("1  Messreihe starten")
    print("0  Abbrechen\n")
    try:
        choice = input("Auswahl: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if choice != "1":
        return

    perform_measurement_series(count, interval_seconds)
    pause()


def menu_new_reading():
    while True:
        screen_title("NEUE ZAEHLERAUSLESUNG")
        print("1  Einzelmessung")
        print("2  Messreihe")
        print("0  Zurueck\n")
        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == "1":
            screen_title("EINZELMESSUNG")
            try:
                perform_live_read()
            except (RuntimeError, ValueError) as exc:
                print(f"\nFehler: {exc}", file=sys.stderr)
            pause()
        elif choice == "2":
            configure_measurement_series()
        elif choice == "0":
            return


def menu_last_reading():
    reading = get_last_reading()
    if reading is None:
        screen_title("LETZTE AUSLESUNG")
        print("Keine gespeicherte Auslesung gefunden.")
        pause()
        return

    view = "compact"
    while True:
        screen_title("LETZTE AUSLESUNG")
        print_stored_reading(
            reading,
            show_all=(view == "all"),
            show_monthly=(view == "monthly"),
        )

        if view == "compact":
            print("\n1  Alle Werte anzeigen")
            print("2  Monatsvorwerte")
        elif view == "all":
            print("\n1  Kompakte Ansicht")
            print("2  Monatsvorwerte")
        else:
            print("\n1  Alle Werte anzeigen")
            print("2  Kompakte Ansicht")
        print("0  Zurueck\n")

        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == "1":
            view = "all" if view != "all" else "compact"
        elif choice == "2":
            view = "monthly" if view != "monthly" else "compact"
        elif choice == "0":
            return






def choose_meter_for_history():
    meters = list_meters()
    if not meters:
        screen_title("AUSLESUNGSVERLAUF")
        print("Noch keine Zaehler gespeichert.")
        pause()
        return None

    while True:
        screen_title("AUSLESUNGSVERLAUF")
        print("Zaehler auswaehlen\n")
        for meter in meters:
            marker = " [HA]" if meter["ha_enabled"] else ""
            print(f"{meter['id']}  {meter['name']}{marker}")
            print(f"   Zaehl.-Nr.: {meter_display_number(meter)}")
            print(f"   Auslesungen: {meter['reading_count']}")
            if meter["last_reading"]:
                print(f"   Letzte: {format_datetime(meter['last_reading'])}")
            print()
        print("0  Zurueck\n")

        try:
            value = input("Zaehler-ID: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if value == "0":
            return None
        try:
            meter_id = int(value)
        except ValueError:
            continue

        meter = get_meter_by_id(meter_id)
        if meter is not None:
            return meter


def choose_reading_for_history(meter):
    while True:
        readings = get_history(meter_id=meter["id"], limit=50)

        screen_title("AUSLESUNGSVERLAUF")
        print(f"Zaehler: {meter['name']}")
        print(f"Zaehl.-Nr.: {meter_display_number(meter)}\n")

        if not readings:
            print("Keine Auslesungen gespeichert.")
            pause()
            return None

        for reading in readings:
            values = decode_values(reading)
            total = format_stored_value(get_stored_entry(values, "1.8.0"))
            protocol = (
                reading["protocol"]
                if "protocol" in reading.keys() and reading["protocol"]
                else "unbekannt"
            )
            print(
                f"#{reading['id']}  {format_datetime(reading['read_at'])}"
                f"  {protocol}"
            )
            print(f"    Gesamtbezug: {total}")

        print("\n0  Zurueck\n")
        try:
            value = input("Messungs-ID: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if value == "0":
            return None
        try:
            reading_id = int(value)
        except ValueError:
            continue

        reading = get_reading(reading_id)
        if reading is not None and reading["meter_id"] == meter["id"]:
            return reading


def menu_history_reading(reading):
    view = "compact"
    while True:
        screen_title("AUSLESUNG")
        print_stored_reading(
            reading,
            show_all=(view == "all"),
            show_monthly=(view == "monthly"),
        )

        if view == "compact":
            print("\n1  Alle Werte anzeigen")
            print("2  Monatsvorwerte")
        elif view == "all":
            print("\n1  Kompakte Ansicht")
            print("2  Monatsvorwerte")
        else:
            print("\n1  Alle Werte anzeigen")
            print("2  Kompakte Ansicht")
        print("0  Zurueck\n")

        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == "1":
            view = "all" if view != "all" else "compact"
        elif choice == "2":
            view = "monthly" if view != "monthly" else "compact"
        elif choice == "0":
            return


def _series_status_text(status):
    return {
        "completed": "beendet",
        "aborted": "abgebrochen",
        "running": "laufend",
    }.get(status or "", status or "-")


def choose_measurement_series(meter):
    while True:
        series = list_measurement_series(meter_id=meter["id"], limit=100)
        screen_title("MESSREIHEN")
        print(f"Zaehler: {meter['name']}")
        print(f"Zaehl.-Nr.: {meter_display_number(meter)}\n")
        if not series:
            print("Keine Messreihen gespeichert.")
            pause()
            return None
        for row in series:
            print(
                f"#{row['id']}  {format_datetime(row['started_at'])}  "
                f"{row['actual_count']}/{row['requested_count']} Messungen"
            )
            print(
                f"    Intervall: {_format_series_interval(row['interval_seconds'])}  "
                f"Status: {_series_status_text(row['status'])}"
            )
        print("\n0  Zurueck\n")
        try:
            value = input("Messreihen-ID: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if value == "0":
            return None
        try:
            series_id = int(value)
        except ValueError:
            continue
        row = get_measurement_series(series_id)
        if row is not None and row["meter_id"] == meter["id"]:
            return row


def parse_read_at(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def series_analysis_rows(series_id):
    """Verbrauch und mittlere Leistung zwischen Messpunkten einer Reihe."""
    readings = get_series_readings(series_id)
    points = []
    for reading in readings:
        if not reading["bcc_valid"]:
            continue
        value = stored_energy_kwh(reading, "1.8.0")
        when = parse_read_at(reading["read_at"])
        if value is not None and when is not None:
            points.append((when, value, reading))

    rows = []
    for previous, current in zip(points, points[1:]):
        t0, e0, r0 = previous
        t1, e1, r1 = current
        seconds = (t1 - t0).total_seconds()
        delta = e1 - e0
        if seconds <= 0 or delta < 0:
            continue
        watts = (delta * Decimal("3600000")) / Decimal(str(seconds))
        rows.append({
            "from": t0, "to": t1, "seconds": seconds,
            "delta_kwh": delta, "watts": watts,
            "from_id": r0["id"], "to_id": r1["id"],
        })
    return readings, rows


def menu_measurement_series_detail(series):
    while True:
        readings, rows = series_analysis_rows(series["id"])
        screen_title("MESSREIHE")
        print(f"Messreihe:   #{series['id']}")
        print(f"Zaehler:     {series['name']}")
        print(f"Zaehl.-Nr.:  {meter_display_number(get_meter_by_id(series['meter_id']))}")
        print(f"Start:       {format_datetime(series['started_at'])}")
        print(f"Intervall:   {_format_series_interval(series['interval_seconds'])}")
        print(f"Geplant:     {series['requested_count']}")
        print(f"Gespeichert: {len(readings)}")
        print(f"Fehler:      {series['failed_count']}")
        print(f"Status:      {_series_status_text(series['status'])}\n")
        print("1  Messpunkte anzeigen")
        print("2  Verbrauch / mittlere Leistung")
        print("3  CSV exportieren")
        print("0  Zurueck\n")
        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "1":
            screen_title(f"MESSREIHE #{series['id']} - MESSPUNKTE")
            for index, reading in enumerate(readings, start=1):
                total = stored_energy_kwh(reading, "1.8.0")
                value = format_kwh(total, 4) if total is not None else "-"
                print(f"{index:>3}  #{reading['id']}  {format_datetime(reading['read_at'])}")
                print(f"     Gesamtbezug: {value}")
            pause()
        elif choice == "2":
            screen_title(f"MESSREIHE #{series['id']} - AUSWERTUNG")
            if not rows:
                print("Mindestens zwei auswertbare Messpunkte mit veraendertem oder")
                print("gleichbleibendem Gesamtzaehlerstand werden benoetigt.")
            else:
                print(f"{'Nr.':>3}  {'Intervall':>9} {'Verbrauch':>13} {'Leistung':>11}")
                for index, row in enumerate(rows, start=1):
                    print(
                        f"{index:>3}  {row['seconds']:>7.1f} s "
                        f"{format_kwh(row['delta_kwh'], 4):>13} "
                        f"{float(row['watts']):>8.1f} W"
                    )
                print("\nLeistung = Verbrauchsdifferenz / tatsaechliche Zeitdifferenz.")
                print("Bei grob aufloesenden Zaehlerstaenden sind kurze Intervalle ungenau.")
            pause()
        elif choice == "3":
            try:
                path, count = export_measurement_series_csv(series)
                export_finished(path, count)
            except OSError as exc:
                print(f"\nExport fehlgeschlagen: {exc}")
                pause()
        elif choice == "0":
            return


def menu_history():
    while True:
        meter = choose_meter_for_history()
        if meter is None:
            return
        while True:
            screen_title("AUSLESUNGSVERLAUF")
            print(f"Zaehler: {meter['name']}")
            print(f"Zaehl.-Nr.: {meter_display_number(meter)}\n")
            print("1  Einzelne Auslesungen")
            print("2  Messreihen")
            print("0  Anderen Zaehler waehlen\n")
            try:
                choice = input("Auswahl: ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if choice == "1":
                while True:
                    reading = choose_reading_for_history(meter)
                    if reading is None:
                        break
                    menu_history_reading(reading)
            elif choice == "2":
                while True:
                    series = choose_measurement_series(meter)
                    if series is None:
                        break
                    menu_measurement_series_detail(series)
            elif choice == "0":
                break


ENERGY_UNIT_TO_KWH = {
    "Wh": Decimal("0.001"),
    "kWh": Decimal("1"),
    "MWh": Decimal("1000"),
}


def stored_energy_kwh(reading, code="1.8.0"):
    """Kumulativen Energiezaehlerstand verlustfrei nach kWh umrechnen."""
    values = decode_values(reading)
    entry = get_stored_entry(values, code)
    if not entry:
        return None

    raw = entry.get("raw")
    unit = entry.get("unit")

    # Zuerst den bereits separat gespeicherten Wert verwenden.
    if unit in ENERGY_UNIT_TO_KWH:
        factor = ENERGY_UNIT_TO_KWH[unit]
        number = _format_raw_measurement(raw, unit)
        if number is None and entry.get("value") is not None:
            number = str(entry.get("value"))
        if number is not None:
            try:
                return Decimal(number) * factor
            except (InvalidOperation, ValueError):
                pass

    # IEC-Fallback fuer Datensaetze, bei denen Parser/SQLite den kompletten
    # Messwert im raw-Feld abgelegt haben, z. B. "0000001.9 kWh".
    # Bewusst ohne Regex, damit unterschiedliche Leerzeichen/IEC-Schreibweisen
    # robust verarbeitet werden.
    if raw is not None:
        raw_text = str(raw).strip()
        for raw_unit in ("MWh", "kWh", "Wh"):
            if raw_text.endswith(raw_unit):
                number = raw_text[:-len(raw_unit)].strip()
                if number.endswith("*"):
                    number = number[:-1].strip()
                try:
                    return Decimal(number) * ENERGY_UNIT_TO_KWH[raw_unit]
                except (InvalidOperation, ValueError):
                    return None

    return None

def analysis_points(meter_id, code="1.8.0", start=None, end=None):
    """
    Gueltige Messpunkte chronologisch.
    read_at vom Smartphone ist die Zeitachse.
    """
    readings = get_history(meter_id=meter_id, limit=1000000)
    points = []

    for reading in readings:
        if not reading["bcc_valid"]:
            continue

        value = stored_energy_kwh(reading, code)
        if value is None:
            continue

        try:
            when = datetime.fromisoformat(reading["read_at"])
        except (ValueError, TypeError):
            continue

        if start is not None and when < start:
            continue
        if end is not None and when > end:
            continue

        points.append((when, value, reading))

    points.sort(key=lambda item: (item[0], item[2]["id"]))
    return points


def format_kwh(value, decimals=4):
    if value is None:
        return "-"
    return f"{value:.{decimals}f} kWh"


def format_percent(value):
    return f"{value:.1f} %"


def format_duration(seconds):
    if seconds < 0:
        return "-"
    days = seconds / 86400
    if days >= 1:
        return f"{days:.2f} Tage"
    hours = seconds / 3600
    return f"{hours:.2f} Stunden"


def parse_date_input(text, end_of_day=False):
    try:
        value = datetime.strptime(text.strip(), "%d.%m.%Y")
    except ValueError:
        return None
    if end_of_day:
        return value.replace(hour=23, minute=59, second=59, microsecond=999999)
    return value


def latest_analysis_time(meter_id):
    points = analysis_points(meter_id)
    if not points:
        return None
    return points[-1][0]


def choose_analysis_period(meter):
    """
    Presets beziehen sich auf die letzte gespeicherte 1.8.0-Auslesung,
    nicht auf die aktuelle Uhrzeit. Das ist fuer Offline-Nutzung robuster.
    """
    latest = latest_analysis_time(meter["id"])
    if latest is None:
        screen_title("ZEITRAUM")
        print("Keine auswertbare 1.8.0-Auslesung vorhanden.")
        pause()
        return None

    while True:
        screen_title("ZEITRAUM")
        print(f"Zaehler: {meter['name']}")
        print(f"Letzte Auslesung: {latest.strftime('%d.%m.%Y %H:%M:%S')}\n")
        print("1  Gesamter Verlauf")
        print("2  Letzte 24 Stunden")
        print("3  Letzte 7 Tage")
        print("4  Letzte 30 Tage")
        print("5  Benutzerdefiniert")
        print("0  Zurueck\n")

        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if choice == "1":
            return ("Gesamter Verlauf", None, latest)
        if choice == "2":
            return ("Letzte 24 Stunden", latest - timedelta(hours=24), latest)
        if choice == "3":
            return ("Letzte 7 Tage", latest - timedelta(days=7), latest)
        if choice == "4":
            return ("Letzte 30 Tage", latest - timedelta(days=30), latest)
        if choice == "5":
            try:
                start_text = input("Von (TT.MM.JJJJ): ").strip()
                end_text = input("Bis (TT.MM.JJJJ): ").strip()
            except (EOFError, KeyboardInterrupt):
                return None

            start = parse_date_input(start_text)
            end = parse_date_input(end_text, end_of_day=True)
            if start is None or end is None:
                print("\nUngueltiges Datum. Erwartet wird TT.MM.JJJJ.")
                pause()
                continue
            if end < start:
                print("\nDas Enddatum liegt vor dem Startdatum.")
                pause()
                continue
            return (f"{start_text} - {end_text}", start, end)
        if choice == "0":
            return None


def energy_result(meter_id, code, start, end):
    points = analysis_points(meter_id, code, start, end)
    if len(points) < 2:
        return {"points": points, "delta": None}

    first = points[0]
    last = points[-1]
    delta = last[1] - first[1]
    if delta < 0:
        delta = None

    return {
        "points": points,
        "first": first,
        "last": last,
        "delta": delta,
    }


def print_period_header(meter, period):
    label, start, end = period
    print(f"Zaehler: {meter['name']}")
    print(f"Zaehl.-Nr.: {meter_display_number(meter)}")
    print(f"Zeitraumwahl: {label}\n")


def print_analysis_summary(meter, period):
    label, start, end = period
    result = energy_result(meter["id"], "1.8.0", start, end)

    screen_title("VERBRAUCH")
    print_period_header(meter, period)

    points = result["points"]
    if len(points) < 2:
        print("Nicht genug auswertbare Werte im gewaehlten Zeitraum.")
        print(f"Auswertbare Werte: {len(points)}")
        return

    first_time, first_value, first_reading = result["first"]
    last_time, last_value, last_reading = result["last"]
    delta = result["delta"]
    seconds = (last_time - first_time).total_seconds()

    print(f"Tatsaechlich von:   {format_datetime(first_reading['read_at'])}")
    print(f"             bis:   {format_datetime(last_reading['read_at'])}")
    print(f"Auswertbare Werte:  {len(points)}")
    print(f"Zeitraumlaenge:     {format_duration(seconds)}\n")
    print(f"Zaehlerstand Start  {format_kwh(first_value)}")
    print(f"Zaehlerstand Ende   {format_kwh(last_value)}")

    if delta is None:
        print("\nVerbrauch kann nicht berechnet werden.")
        print("Moeglicher Zaehlerwechsel oder Zaehler-Reset.")
        return

    print(f"Verbrauch           {format_kwh(delta)}")
    if seconds > 0:
        days = Decimal(str(seconds)) / Decimal("86400")
        print(f"Durchschnitt/Tag    {format_kwh(delta / days)}")

    print("\nHinweis: Angezeigt wird der tatsaechlich durch Messwerte")
    print("abgedeckte Zeitraum innerhalb der Zeitraumwahl.")


def print_analysis_tariffs(meter, period):
    label, start, end = period
    t1 = energy_result(meter["id"], "1.8.1", start, end)
    t2 = energy_result(meter["id"], "1.8.2", start, end)

    screen_title("TARIFAUFTEILUNG")
    print_period_header(meter, period)

    d1 = t1.get("delta")
    d2 = t2.get("delta")

    print(f"Tarif 1 Verbrauch   {format_kwh(d1) if d1 is not None else '-'}")
    print(f"Tarif 2 Verbrauch   {format_kwh(d2) if d2 is not None else '-'}")

    if d1 is not None and d2 is not None:
        total = d1 + d2
        print(f"Summe Tarife        {format_kwh(total)}")
        if total > 0:
            print(f"Anteil Tarif 1      {format_percent((d1 / total) * 100)}")
            print(f"Anteil Tarif 2      {format_percent((d2 / total) * 100)}")
        else:
            print("Anteile             -  (kein Verbrauch im Zeitraum)")
    else:
        print("\nFuer die Tarifauswertung werden je Tarif mindestens")
        print("zwei gueltige Messwerte im Zeitraum benoetigt.")


def print_analysis_feedin(meter, period):
    label, start, end = period
    result = energy_result(meter["id"], "2.8.0", start, end)

    screen_title("EINSPEISUNG")
    print_period_header(meter, period)

    points = result["points"]
    if len(points) < 2:
        print("Nicht genug 2.8.0-Werte fuer eine Auswertung.")
        print(f"Auswertbare Werte: {len(points)}")
        return

    first_time, first_value, first_reading = result["first"]
    last_time, last_value, last_reading = result["last"]
    delta = result["delta"]

    print(f"Tatsaechlich von:   {format_datetime(first_reading['read_at'])}")
    print(f"             bis:   {format_datetime(last_reading['read_at'])}")
    print(f"Zaehlerstand Start  {format_kwh(first_value)}")
    print(f"Zaehlerstand Ende   {format_kwh(last_value)}")
    if delta is None:
        print("Einspeisung         -")
        print("\nZaehlerstand ist gesunken; keine Differenz berechnet.")
    else:
        print(f"Einspeisung         {format_kwh(delta)}")


def print_analysis_intervals(meter, period):
    label, start, end = period
    points = analysis_points(meter["id"], "1.8.0", start, end)

    screen_title("MESSINTERVALLE")
    print_period_header(meter, period)

    if len(points) < 2:
        print("Mindestens zwei gueltige Messpunkte erforderlich.")
        return

    for previous, current in zip(points, points[1:]):
        t1, v1, r1 = previous
        t2, v2, r2 = current
        delta = v2 - v1
        seconds = (t2 - t1).total_seconds()

        print(
            f"#{r1['id']} -> #{r2['id']}  "
            f"{t1.strftime('%d.%m.%Y %H:%M')} -> "
            f"{t2.strftime('%d.%m.%Y %H:%M')}"
        )

        if delta < 0:
            print("    Verbrauch: -  (Zaehlerstand gesunken)")
        else:
            print(f"    Verbrauch: {format_kwh(delta)}")
            if seconds > 0:
                days = Decimal(str(seconds)) / Decimal("86400")
                print(f"    Ø/Tag:     {format_kwh(delta / days)}")
        print(f"    Dauer:     {format_duration(seconds)}")
        print()


def _historical_energy_kwh(entry):
    """Historischen Energiezaehlerstand verlustfrei nach kWh umrechnen."""
    if not entry:
        return None

    raw = entry.get("raw")
    unit = entry.get("unit")

    # Standardfall: Einheit wurde separat gespeichert.
    if unit in ENERGY_UNIT_TO_KWH:
        factor = ENERGY_UNIT_TO_KWH[unit]
        number = _format_raw_measurement(raw, unit)
        if number is None and entry.get("value") is not None:
            number = str(entry.get("value"))
        if number is not None:
            try:
                return Decimal(number) * factor
            except (InvalidOperation, ValueError):
                pass

    # IEC-Fallback wie bei der erfolgreich getesteten Messreihen-Auswertung:
    # Einige Zaehler (z. B. Iskraemeco MT173) speichern historische Werte
    # komplett in raw, etwa "0000001.9 kWh", waehrend unit=None bleibt.
    if raw is not None:
        raw_text = str(raw).strip()
        for raw_unit in ("MWh", "kWh", "Wh"):
            if raw_text.endswith(raw_unit):
                number = raw_text[:-len(raw_unit)].strip()
                if number.endswith("*"):
                    number = number[:-1].strip()
                try:
                    return Decimal(number) * ENERGY_UNIT_TO_KWH[raw_unit]
                except (InvalidOperation, ValueError):
                    return None

    return None

def historical_month_points(values):
    """Gueltige Stichtage mit historischen Importzaehlerstaenden sammeln."""
    points = []
    for number in range(1, 100):
        suffix = f"{number:02d}"
        date_entry = _stored_historical_entry(values, "0.1.2", suffix)
        when = _historical_datetime(date_entry.get("raw") if date_entry else None)
        total = _historical_energy_kwh(
            _stored_historical_entry(values, "1.8.0", suffix)
        )
        if when is None or total is None:
            continue
        point = {
            "date": when,
            "total": total,
            "t1": _historical_energy_kwh(
                _stored_historical_entry(values, "1.8.1", suffix)
            ),
            "t2": _historical_energy_kwh(
                _stored_historical_entry(values, "1.8.2", suffix)
            ),
            "t3": _historical_energy_kwh(
                _stored_historical_entry(values, "1.8.3", suffix)
            ),
            "t4": _historical_energy_kwh(
                _stored_historical_entry(values, "1.8.4", suffix)
            ),
        }
        points.append(point)

    # Ein Datum darf nur einmal vorkommen. Bei Mehrdeutigkeit lieber nicht
    # auswerten, statt stillschweigend einen Wert zu bevorzugen.
    unique = {}
    duplicates = set()
    for point in points:
        key = point["date"]
        if key in unique:
            duplicates.add(key)
        else:
            unique[key] = point
    for key in duplicates:
        unique.pop(key, None)

    return sorted(unique.values(), key=lambda point: point["date"])


def latest_historical_month_source(meter_id):
    """Neueste gueltige Auslesung mit mindestens zwei Monatsstichtagen."""
    readings = get_history(meter_id=meter_id, limit=1000000)
    for reading in readings:
        if not reading["bcc_valid"]:
            continue
        values = decode_values(reading)
        points = historical_month_points(values)
        if len(points) >= 2:
            return reading, points
    return None, []


def historical_month_results(points):
    """Verbrauch zwischen aufeinanderfolgenden Stichtagen berechnen."""
    results = []
    for older, newer in zip(points, points[1:]):
        total = newer["total"] - older["total"]
        if total < 0:
            continue

        tariffs = {}
        for key in ("t1", "t2", "t3", "t4"):
            old_value = older.get(key)
            new_value = newer.get(key)
            if old_value is None or new_value is None:
                tariffs[key] = None
                continue
            delta = new_value - old_value
            tariffs[key] = delta if delta >= 0 else None

        results.append({
            "month": older["date"],
            "from": older["date"],
            "to": newer["date"],
            "total": total,
            **tariffs,
        })

    results.sort(key=lambda row: row["month"], reverse=True)
    return results


def menu_monthly_consumption(meter):
    reading, points = latest_historical_month_source(meter["id"])
    results = historical_month_results(points)

    while True:
        screen_title("MONATSVERBRAUCH")
        print(f"Zaehler: {meter['name']}")
        print(f"Zaehl.-Nr.: {meter_display_number(meter)}")

        if reading is None or not results:
            print("\nKeine auswertbaren Monatsvorwerte vorhanden.")
            print("Benoetigt werden mindestens zwei gueltige Stichtage mit 1.8.0.")
            pause()
            return

        print(
            f"Quelle:   Messung #{reading['id']} vom "
            f"{format_datetime(reading['read_at'])}\n"
        )
        print(f"{'Nr.':>3}  {'Monat':<10} {'Verbrauch':>14}")
        for index, row in enumerate(results, start=1):
            label = row["month"].strftime("%m/%Y")
            print(f"{index:>3}  {label:<10} {format_kwh(row['total'], 3):>14}")

        print("\nMonatsnummer fuer Details")
        print("0  Zurueck\n")
        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "0":
            return
        try:
            index = int(choice) - 1
        except ValueError:
            continue
        if not 0 <= index < len(results):
            continue

        row = results[index]
        screen_title("MONATSVERBRAUCH - DETAILS")
        print(f"Zaehler: {meter['name']}")
        print(f"Monat:   {row['month'].strftime('%m/%Y')}")
        print(
            f"Zeitraum: {row['from'].strftime('%d.%m.%Y')} - "
            f"{row['to'].strftime('%d.%m.%Y')}\n"
        )
        print(f"Gesamtverbrauch  {format_kwh(row['total'], 3)}")
        for key, label in (("t1", "Tarif 1"), ("t2", "Tarif 2"),
                           ("t3", "Tarif 3"), ("t4", "Tarif 4")):
            value = row.get(key)
            if value is not None and (key in ("t1", "t2") or value != 0):
                print(f"{label:<16} {format_kwh(value, 3)}")
        pause()


def menu_analysis():
    meter = choose_meter_for_history()
    if meter is None:
        return

    period = ("Gesamter Verlauf", None, latest_analysis_time(meter["id"]))

    while True:
        screen_title("AUSWERTUNG")
        print(f"Zaehler: {meter['name']}")
        print(f"Zaehl.-Nr.: {meter_display_number(meter)}")
        print(f"Zeitraum: {period[0]}\n")
        print("1  Verbrauch")
        print("2  Tarifaufteilung")
        print("3  Einspeisung")
        print("4  Messintervalle")
        print("5  Monatsverbrauch")
        print("6  Messreihen")
        print("7  Zeitraum waehlen")
        print("0  Zurueck\n")

        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == "1":
            print_analysis_summary(meter, period)
            pause()
        elif choice == "2":
            print_analysis_tariffs(meter, period)
            pause()
        elif choice == "3":
            print_analysis_feedin(meter, period)
            pause()
        elif choice == "4":
            print_analysis_intervals(meter, period)
            pause()
        elif choice == "5":
            menu_monthly_consumption(meter)
        elif choice == "6":
            while True:
                series = choose_measurement_series(meter)
                if series is None:
                    break
                menu_measurement_series_detail(series)
        elif choice == "7":
            selected = choose_analysis_period(meter)
            if selected is not None:
                period = selected
        elif choice == "0":
            return


def export_directory():
    """
    Export in den gemeinsam sichtbaren Android-Speicher.
    Nach 'termux-setup-storage' zeigt ~/storage/shared auf
    /storage/emulated/0.
    """
    shared = Path.home() / "storage" / "shared"
    if not shared.exists():
        raise OSError(
            "Android-Speicher ist in Termux noch nicht eingerichtet.\n"
            "Bitte einmal 'termux-setup-storage' ausfuehren und den\n"
            "Speicherzugriff bestaetigen."
        )

    path = shared / "MeterReader" / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def share_export(path):
    """
    Optional: Android Share-Sheet via termux-share.
    Der eigentliche Export ist davon unabhaengig.
    """
    command = shutil.which("termux-share")
    if command is None:
        print("\nTeilen ist auf diesem Termux-System nicht verfuegbar.")
        print("Die Datei bleibt im Android-Dateispeicher erhalten.")
        return False

    try:
        result = subprocess.run(
            [command, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        print(f"\nTeilen konnte nicht gestartet werden: {exc}")
        return False

    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        print("\nAndroid-Teilen konnte nicht gestartet werden.")
        if detail:
            print(detail)
        return False
    return True


def export_finished(path, reading_count, value_count=None):
    print()
    if value_count is None:
        print(f"Exportiert: {reading_count} Auslesungen")
    else:
        print(f"Exportiert: {reading_count} Auslesungen / {value_count} Messwerte")
    print(f"Datei: {path}")

    while True:
        print("\n1  Datei teilen")
        print("0  Zurueck\n")
        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == "1":
            share_export(path)
            return
        if choice == "0":
            return


def safe_filename(text):
    cleaned = "".join(
        c if c.isalnum() or c in ("-", "_") else "_"
        for c in str(text).strip()
    )
    return cleaned.strip("_") or "meter"


def export_readings(meter_id, start=None, end=None):
    readings = get_history(meter_id=meter_id, limit=1000000)
    result = []
    for reading in readings:
        try:
            when = datetime.fromisoformat(reading["read_at"])
        except (ValueError, TypeError):
            continue
        if start is not None and when < start:
            continue
        if end is not None and when > end:
            continue
        result.append(reading)
    result.sort(key=lambda r: (r["read_at"], r["id"]))
    return result


def stored_measurement(reading, code):
    values = decode_values(reading)
    entry = get_stored_entry(values, code)
    if not entry:
        return None, None

    unit = entry.get("unit")
    number = _format_raw_measurement(entry.get("raw"), unit)
    if number is None and entry.get("value") is not None:
        number = str(entry.get("value"))
    return number, unit


def stored_energy_export_kwh(reading, code):
    value = stored_energy_kwh(reading, code)
    if value is None:
        return ""
    return f"{value:.4f}"


def stored_power_w(reading):
    # Fuer die kompakte Exportansicht typische Wirkleistungs-OBIS pruefen.
    for code in ("16.7.0", "1.7.0"):
        number, unit = stored_measurement(reading, code)
        if number is None:
            continue
        try:
            value = Decimal(number)
        except (InvalidOperation, ValueError):
            continue
        if unit == "kW":
            value *= Decimal("1000")
        elif unit not in ("W", None, ""):
            continue
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return ""


def integrity_text(reading):
    return "OK" if reading["bcc_valid"] else "FEHLER"


def export_overview_csv(meter, period):
    label, start, end = period
    readings = export_readings(meter["id"], start, end)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_filename(meter['name'])}_{stamp}_overview.csv"
    path = export_directory() / filename

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\n")
        writer.writerow([
            "timestamp",
            "meter_id",
            "meter_name",
            "protocol",
            "total_import_kwh",
            "tariff_1_kwh",
            "tariff_2_kwh",
            "total_export_kwh",
            "power_w",
            "integrity",
        ])
        for reading in readings:
            writer.writerow([
                reading["read_at"],
                meter["meter_uid"],
                meter["name"],
                reading["protocol"] or "",
                stored_energy_export_kwh(reading, "1.8.0"),
                stored_energy_export_kwh(reading, "1.8.1"),
                stored_energy_export_kwh(reading, "1.8.2"),
                stored_energy_export_kwh(reading, "2.8.0"),
                stored_power_w(reading),
                integrity_text(reading),
            ])
    return path, len(readings)


def export_overview_txt(meter, period):
    label, start, end = period
    readings = export_readings(meter["id"], start, end)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_filename(meter['name'])}_{stamp}_overview.txt"
    path = export_directory() / filename

    lines = [
        "TERMUMETER EXPORT",
        "===================",
        "",
        f"Zaehler: {meter['name']}",
        f"ID:      {meter['meter_uid']}",
        f"Zeitraumwahl: {label}",
        "",
    ]

    for reading in readings:
        protocol = reading["protocol"] or "-"
        check = "CRC" if protocol == "sml" else "BCC"
        lines.extend([
            format_datetime(reading["read_at"]),
            f"Protokoll:     {protocol}",
            f"{check}:          {integrity_text(reading)}",
            f"Gesamtbezug:   {stored_energy_export_kwh(reading, '1.8.0') or '-'} kWh",
            f"Tarif 1:       {stored_energy_export_kwh(reading, '1.8.1') or '-'} kWh",
            f"Tarif 2:       {stored_energy_export_kwh(reading, '1.8.2') or '-'} kWh",
            f"Einspeisung:   {stored_energy_export_kwh(reading, '2.8.0') or '-'} kWh",
            f"Leistung:      {stored_power_w(reading) or '-'} W",
            "",
        ])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path, len(readings)


def flatten_all_values(reading):
    """
    Vollstaendiger technischer Export der gespeicherten values_json-Struktur.
    Nichts wird fuer den Detail-Export auf die Kompaktwerte reduziert.
    """
    values = decode_values(reading)
    rows = []

    if isinstance(values, dict):
        iterator = values.items()
    elif isinstance(values, list):
        iterator = enumerate(values)
    else:
        return rows

    for key, entry in iterator:
        if isinstance(entry, dict):
            code = entry.get("code") or str(key)
            raw = entry.get("raw")
            value = entry.get("value")
            unit = entry.get("unit")
            scaler = entry.get("scaler")
            value_type = entry.get("value_type")
            rows.append((code, raw, value, unit, scaler, value_type))
        else:
            rows.append((str(key), None, entry, None, None, None))
    return rows


def export_all_csv(meter, period):
    label, start, end = period
    readings = export_readings(meter["id"], start, end)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_filename(meter['name'])}_{stamp}_all.csv"
    path = export_directory() / filename
    row_count = 0

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\n")
        writer.writerow([
            "timestamp", "reading_id", "meter_id", "meter_name",
            "protocol", "integrity", "obis", "raw", "value",
            "unit", "scaler", "value_type",
        ])
        for reading in readings:
            for code, raw, value, unit, scaler, value_type in flatten_all_values(reading):
                writer.writerow([
                    reading["read_at"],
                    reading["id"],
                    meter["meter_uid"],
                    meter["name"],
                    reading["protocol"] or "",
                    integrity_text(reading),
                    code,
                    "" if raw is None else raw,
                    "" if value is None else value,
                    "" if unit is None else unit,
                    "" if scaler is None else scaler,
                    "" if value_type is None else value_type,
                ])
                row_count += 1
    return path, len(readings), row_count


def export_all_txt(meter, period):
    label, start, end = period
    readings = export_readings(meter["id"], start, end)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_filename(meter['name'])}_{stamp}_all.txt"
    path = export_directory() / filename
    row_count = 0

    lines = [
        "TERMUMETER TECHNICAL EXPORT",
        "=============================",
        "",
        f"Zaehler: {meter['name']}",
        f"ID:      {meter['meter_uid']}",
        f"Zeitraumwahl: {label}",
        "",
    ]

    for reading in readings:
        protocol = reading["protocol"] or "-"
        check = "CRC" if protocol == "sml" else "BCC"
        lines.extend([
            "-" * 60,
            f"Messung #{reading['id']}  {format_datetime(reading['read_at'])}",
            f"Protokoll: {protocol}  {check}: {integrity_text(reading)}",
            "",
        ])
        for code, raw, value, unit, scaler, value_type in flatten_all_values(reading):
            details = []
            if raw is not None:
                details.append(f"raw={raw}")
            if value is not None:
                details.append(f"value={value}")
            if unit is not None:
                details.append(f"unit={unit}")
            if scaler is not None:
                details.append(f"scaler={scaler}")
            if value_type is not None:
                details.append(f"type={value_type}")
            lines.append(f"{code}: " + ", ".join(details))
            row_count += 1
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path, len(readings), row_count


def export_measurement_series_csv(series):
    readings, rows = series_analysis_rows(series["id"])
    path = export_directory() / (
        f"messreihe_{series['id']}_{series['meter_uid']}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    analysis_by_to = {row["to_id"]: row for row in rows}
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow([
            "series_id", "reading_id", "read_at", "protocol", "integrity",
            "total_kwh", "delta_kwh", "interval_seconds", "average_power_w",
        ])
        for reading in readings:
            total = stored_energy_kwh(reading, "1.8.0")
            row = analysis_by_to.get(reading["id"])
            writer.writerow([
                series["id"], reading["id"], reading["read_at"],
                reading["protocol"] or "", "OK" if reading["bcc_valid"] else "FEHLER",
                str(total) if total is not None else "",
                str(row["delta_kwh"]) if row else "",
                f"{row['seconds']:.3f}" if row else "",
                f"{row['watts']:.3f}" if row else "",
            ])
    return path, len(readings)


def choose_export_period(meter):
    # Gleiche Zeitraumlogik wie die lokale Auswertung.
    return choose_analysis_period(meter)


def menu_export():
    meter = choose_meter_for_history()
    if meter is None:
        return

    period = ("Gesamter Verlauf", None, latest_analysis_time(meter["id"]))

    while True:
        screen_title("DATEN EXPORTIEREN")
        print(f"Zaehler: {meter['name']}")
        print(f"Zaehl.-Nr.: {meter_display_number(meter)}")
        print(f"Zeitraum: {period[0]}\n")
        print("1  CSV - Uebersicht")
        print("2  TXT - Uebersicht")
        print("3  CSV - Alle Messwerte")
        print("4  TXT - Alle Messwerte")
        print("5  Zeitraum waehlen")
        print("0  Zurueck\n")

        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        try:
            if choice == "1":
                path, count = export_overview_csv(meter, period)
                export_finished(path, count)
            elif choice == "2":
                path, count = export_overview_txt(meter, period)
                export_finished(path, count)
            elif choice == "3":
                path, count, rows = export_all_csv(meter, period)
                export_finished(path, count, rows)
            elif choice == "4":
                path, count, rows = export_all_txt(meter, period)
                export_finished(path, count, rows)
            elif choice == "5":
                selected = choose_export_period(meter)
                if selected is not None:
                    period = selected
            elif choice == "0":
                return
        except OSError as exc:
            print(f"\nExport fehlgeschlagen: {exc}")
            pause()

def confirm_delete(prompt):
    """Destruktive Aktionen sind standardmaessig Nein."""
    try:
        return input(f"{prompt} [j/N]: ").strip().lower() in ("j", "ja")
    except EOFError:
        return False


def choose_meter_for_management():
    meters = list_meters()
    if not meters:
        print("Noch keine Zaehler gespeichert.")
        return None

    show_meters()
    try:
        meter_id = int(input("Zaehler-ID: ").strip())
    except (ValueError, EOFError):
        print("Ungueltige ID.")
        return None

    meter = get_meter_by_id(meter_id)
    if meter is None:
        print("Zaehler nicht gefunden.")
        return None
    return meter


def delete_single_reading_for_meter(meter):
    readings = get_history(meter_id=meter["id"], limit=50)
    if not readings:
        print("\nKeine Auslesungen fuer diesen Zaehler gespeichert.")
        return

    print(f"\nAuslesungen von: {meter['name']}")
    print("=" * (18 + len(meter["name"] or "")))
    print()
    for reading in readings:
        values = decode_values(reading)
        total = format_stored_value(get_stored_entry(values, "1.8.0"))
        protocol = (
            reading["protocol"]
            if "protocol" in reading.keys() and reading["protocol"]
            else "unbekannt"
        )
        print(
            f"#{reading['id']}  {format_datetime(reading['read_at'])}"
            f"  {protocol}  Gesamtbezug: {total}"
        )

    try:
        reading_id = int(input("\nMessungs-ID zum Loeschen: ").strip())
    except (ValueError, EOFError):
        print("Ungueltige ID.")
        return

    reading = get_reading(reading_id)
    if reading is None or reading["meter_id"] != meter["id"]:
        print("Diese Messung gehoert nicht zum ausgewaehlten Zaehler.")
        return

    if not confirm_delete(
        f"Messung #{reading_id} vom "
        f"{format_datetime(reading['read_at'])} wirklich loeschen?"
    ):
        print("Nicht geloescht.")
        return

    if delete_reading(reading_id):
        print("Auslesung geloescht.")
    else:
        print("Auslesung wurde nicht gefunden.")


def delete_all_readings_interactive(meter):
    count = len(get_history(meter_id=meter["id"], limit=1000000))
    if count == 0:
        print("\nKeine Auslesungen fuer diesen Zaehler gespeichert.")
        return

    print(f"\nZaehler: {meter['name']}")
    print(f"Zaehl.-Nr.: {meter_display_number(meter)}")
    print(f"Auslesungen: {count}")

    if not confirm_delete(
        f"Wirklich ALLE {count} Auslesungen dieses Zaehlers loeschen?"
    ):
        print("Nicht geloescht.")
        return

    deleted = delete_all_readings_for_meter(meter["id"])
    print(f"{deleted} Auslesung(en) geloescht. Der Zaehler bleibt gespeichert.")


def menu_readings_delete():
    meter = choose_meter_for_management()
    if meter is None:
        return

    while True:
        screen_title("AUSLESUNGEN VERWALTEN")
        print(f"Zaehler: {meter['name']}\n")
        print("1  Einzelne Auslesung loeschen")
        print("2  Alle Auslesungen dieses Zaehlers loeschen")
        print("0  Zurueck\n")

        try:
            choice = input("Auswahl: ").strip()
        except EOFError:
            return

        if choice == "1":
            print()
            delete_single_reading_for_meter(meter)
            pause()
        elif choice == "2":
            print()
            delete_all_readings_interactive(meter)
            pause()
        elif choice == "0":
            return
        else:
            print("Ungueltige Auswahl.")


def delete_meter_interactive():
    meter = choose_meter_for_management()
    if meter is None:
        return

    readings = get_history(meter_id=meter["id"], limit=1000000)
    count = len(readings)

    print("\nACHTUNG")
    print("=======")
    print(f"Zaehler:      {meter['name']}")
    print(f"Zaehlernummer: {meter_display_number(meter)}")
    print(f"Auslesungen:  {count}")
    if meter["ha_enabled"]:
        print("Home Assistant: Dieser Zaehler ist aktuell als HA-Quelle markiert.")

    if not confirm_delete(
        f'Zaehler "{meter["name"]}" UND alle {count} Auslesung(en) wirklich loeschen?'
    ):
        print("Nicht geloescht.")
        return

    deleted, deleted_readings = delete_meter_with_readings(meter["id"])
    if deleted:
        print(
            f'Zaehler "{meter["name"]}" und '
            f"{deleted_readings} Auslesung(en) geloescht."
        )
    else:
        print("Zaehler wurde nicht gefunden.")

def menu_meters():
    while True:
        screen_title("ZAEHLER VERWALTEN")
        print("1  Bekannte Zaehler anzeigen")
        print("2  Zaehler umbenennen")
        print("3  Zaehlernummer / Identitaet")
        print("4  Auslesungen verwalten")
        print("5  Zaehler loeschen")
        print("6  HA-Zaehler auswaehlen")
        print("0  Zurueck\n")
        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == "1":
            screen_title("BEKANNTE ZAEHLER")
            show_meters()
            pause()
        elif choice == "2":
            screen_title("ZAEHLER UMBENENNEN")
            rename_meter()
            pause()
        elif choice == "3":
            screen_title("ZAEHLERNUMMER / IDENTITAET")
            edit_meter_identity()
            pause()
        elif choice == "4":
            clear_screen()
            menu_readings_delete()
        elif choice == "5":
            screen_title("ZAEHLER LOESCHEN")
            delete_meter_interactive()
            pause()
        elif choice == "6":
            screen_title("HA-ZAEHLER AUSWAEHLEN")
            choose_ha_meter()
            pause()
        elif choice == "0":
            return

def menu_settings():
    while True:
        settings = load_settings()
        mode = settings["protocol_mode"]

        screen_title("EINSTELLUNGEN")
        print("Protokollerkennung")
        print("-------------------")
        print(f"Aktuell: {protocol_mode_label(mode)}\n")
        print("1  Automatisch (SML -> IEC)")
        print("2  Nur SML")
        print("3  Nur IEC 62056-21")
        print("4  Diagnose / Supportbericht")
        print("0  Zurueck\n")

        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        selected = {"1": "auto", "2": "sml", "3": "iec"}.get(choice)
        if selected is not None:
            settings["protocol_mode"] = selected
            save_settings(settings)
        elif choice == "4":
            create_diagnostic_report()
            pause()
        elif choice == "0":
            return


def main_menu():
    while True:
        clear_screen()
        print("======================================")
        print("              TERMUMETER")
        print(f"           {APP_VERSION}")
        print("======================================\n")
        print("1  Neue Zaehlerauslesung")
        print("2  Letzte Auslesung")
        print("3  Auslesungsverlauf")
        print("4  Auswertung")
        print("5  Daten exportieren")
        print("6  Zaehler verwalten")
        print("7  Home Assistant")
        print("8  Einstellungen")
        print("\n0  Beenden\n")

        try:
            choice = input("Auswahl: ").strip()
        except (EOFError, KeyboardInterrupt):
            clear_screen()
            return

        try:
            if choice == "1":
                menu_new_reading()
            elif choice == "2":
                menu_last_reading()
            elif choice == "3":
                menu_history()
            elif choice == "4":
                menu_analysis()
            elif choice == "5":
                menu_export()
            elif choice == "6":
                menu_meters()
            elif choice == "7":
                screen_title("HOME ASSISTANT")
                print("Synchronisation wird spaeter eingerichtet.")
                print("Den HA-Zaehler kannst du bereits unter")
                print('"Zaehler verwalten" auswaehlen.')
                pause()
            elif choice == "8":
                menu_settings()
            elif choice == "0":
                clear_screen()
                print("TermuMeter beendet.")
                return
        except (RuntimeError, ValueError) as exc:
            print(f"\nFehler: {exc}", file=sys.stderr)
            pause()

def print_file_test(message, filename, show_all=False):
    """
    Regressionstest fuer gespeicherte IEC-Telegramme.
    Schreibt bewusst nichts in SQLite.
    """
    meter_uid = identify_meter(message)

    print("\nIEC-Dateitest")
    print("=============\n")
    print(f"Datei:         {filename}")
    print(f"ID:            {meter_uid}")
    print(f"BCC:           {'OK' if message.bcc_valid else 'FEHLER'}")
    print(f"Datensaetze:   {len(message.values)}\n")

    for code, label in IMPORTANT_VALUES:
        entry = get_entry(message, code)
        if entry:
            print(f"{label:<14} {format_value(entry)}")

    print("\nKeine Speicherung in SQLite.")

    if show_all:
        print_all_live(message)


def build_parser():
    parser = argparse.ArgumentParser(description="TermuMeter")
    parser.add_argument("--read", action="store_true",
                        help="Neue Live-Auslesung starten")
    parser.add_argument("--file", nargs="?", const=str(DEFAULT_TEST_FILE),
                        help="Gespeichertes Telegramm auswerten")
    parser.add_argument("--all", action="store_true",
                        help="Alle OBIS-Datensaetze anzeigen")
    parser.add_argument("--last", action="store_true",
                        help="Letzte gespeicherte Auslesung anzeigen")
    parser.add_argument("--history", action="store_true",
                        help="Auslesungsverlauf anzeigen")
    parser.add_argument("--meter", type=int,
                        help="Gespeicherte Zaehler-ID auswaehlen")
    parser.add_argument("--meters", action="store_true",
                        help="Bekannte Zaehler anzeigen")
    parser.add_argument("--select-ha", action="store_true",
                        help="Zaehler fuer Home Assistant waehlen")
    return parser


def main():
    args = build_parser().parse_args()
    init_database()

    try:
        if args.meters:
            show_meters()
        elif args.select_ha:
            choose_ha_meter()
        elif args.last:
            show_last(args.meter, args.all)
        elif args.history:
            show_history(args.meter)
        elif args.read:
            perform_live_read(args.all)
        elif args.file:
            file_path = Path(args.file).expanduser()
            message = parse_iec_message(read_file(file_path))
            if not message.bcc_valid:
                raise RuntimeError(
                    "BCC-Pruefung fehlgeschlagen. "
                    "Dateitest abgebrochen; SQLite bleibt unveraendert."
                )
            print_file_test(message, file_path, args.all)
        else:
            main_menu()
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"\nFehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
