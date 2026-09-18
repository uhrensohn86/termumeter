#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
BUILD_SCRIPT="$SCRIPT_DIR/build.sh"
VERSION_FILE="$SCRIPT_DIR/VERSION"

die() {
    printf 'Fehler: %s\n' "$*" >&2
    exit 1
}

printf 'TermuMeter - Installationspruefung\n'
printf '=================================\n\n'

missing=()
command -v python >/dev/null 2>&1 || missing+=("python")
command -v clang >/dev/null 2>&1 || missing+=("clang")
command -v pkg-config >/dev/null 2>&1 || missing+=("pkg-config")
command -v termux-usb >/dev/null 2>&1 || missing+=("termux-api")

if command -v pkg-config >/dev/null 2>&1; then
    pkg-config --exists libusb-1.0 || missing+=("libusb")
fi

if ((${#missing[@]})); then
    printf 'Es fehlen Voraussetzungen:\n'
    printf '  %s\n' "${missing[@]}"
    printf '\nBitte mit den offiziellen Termux-Paketen installieren:\n'
    printf '  pkg install python clang libusb pkg-config termux-api\n'
    exit 1
fi

[[ -f "$VERSION_FILE" ]] || die "VERSION fehlt im Projektverzeichnis"
VERSION="$(tr -d '\r\n' < "$VERSION_FILE")"
[[ -n "$VERSION" ]] || die "VERSION ist leer"

[[ -f "$BUILD_SCRIPT" ]] || die "build.sh fehlt im Projektverzeichnis"
[[ -f "$SCRIPT_DIR/meter.py" ]] || die "meter.py fehlt"
[[ -f "$SCRIPT_DIR/storage.py" ]] || die "storage.py fehlt"
[[ -f "$SCRIPT_DIR/protocols/iec62056.py" ]] || die "protocols/iec62056.py fehlt"
[[ -f "$SCRIPT_DIR/protocols/sml.py" ]] || die "protocols/sml.py fehlt"

printf 'Version: %s\n\n' "$VERSION"
printf 'Voraussetzungen gefunden:\n'
printf '  Python:  %s\n' "$(python --version 2>&1)"
printf '  Clang:   %s\n' "$(clang --version | head -n 1)"
printf '  libusb:  %s\n' "$(pkg-config --modversion libusb-1.0)"
printf '  USB-API: termux-usb vorhanden\n\n'

mkdir -p "$DATA_DIR"

printf 'Pruefe Python-Quellen ...\n'
python -m py_compile \
    "$SCRIPT_DIR/meter.py" \
    "$SCRIPT_DIR/storage.py" \
    "$SCRIPT_DIR/protocols/iec62056.py" \
    "$SCRIPT_DIR/protocols/sml.py"
printf 'Python-Quellen: OK\n\n'

printf 'Kompiliere USB-Reader ...\n'
bash "$BUILD_SCRIPT"

printf '\nTermuMeter %s ist vorbereitet.\n' "$VERSION"
printf 'Vorhandene Datenbank und Einstellungen wurden nicht geloescht oder ueberschrieben.\n'
printf '\nWichtig fuer USB-Zugriff:\n'
printf '  Neben dem Termux-Paket termux-api muss auf Android die passende\n'
printf '  Termux:API-App installiert sein. Android fragt beim Zugriff auf den\n'
printf '  USB-Tastkopf nach der USB-Berechtigung.\n'
printf '\nStart:\n'
printf '  cd %q\n' "$SCRIPT_DIR"
printf '  python meter.py\n'
