#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
USB_DIR="$SCRIPT_DIR/usb"

die() {
    printf 'Fehler: %s\n' "$*" >&2
    exit 1
}

command -v clang >/dev/null 2>&1 || die \
    "clang fehlt. In Termux installieren mit: pkg install clang"

command -v pkg-config >/dev/null 2>&1 || die \
    "pkg-config fehlt. In Termux installieren mit: pkg install pkg-config"

pkg-config --exists libusb-1.0 || die \
    "libusb-1.0 wurde nicht gefunden. In Termux installieren mit: pkg install libusb"

for file in \
    "$USB_DIR/ftdi_transport.c" \
    "$USB_DIR/ftdi_transport.h" \
    "$USB_DIR/sml_reader.c" \
    "$USB_DIR/iec_reader.c" \
    "$USB_DIR/usb_info.c"
do
    [[ -f "$file" ]] || die "Quelldatei fehlt: $file"
done

CFLAGS=(-Wall -Wextra -O2)
read -r -a USB_CFLAGS <<< "$(pkg-config --cflags libusb-1.0)"
read -r -a USB_LIBS <<< "$(pkg-config --libs libusb-1.0)"

printf 'Baue SML-Reader ...\n'
clang "${CFLAGS[@]}" "${USB_CFLAGS[@]}" \
    "$USB_DIR/ftdi_transport.c" "$USB_DIR/sml_reader.c" \
    "${USB_LIBS[@]}" \
    -o "$USB_DIR/sml_reader"

printf 'Baue IEC-Reader ...\n'
clang "${CFLAGS[@]}" "${USB_CFLAGS[@]}" \
    "$USB_DIR/ftdi_transport.c" "$USB_DIR/iec_reader.c" \
    "${USB_LIBS[@]}" \
    -o "$USB_DIR/iec_reader"

printf 'Baue USB-Info ...\n'
clang "${CFLAGS[@]}" "${USB_CFLAGS[@]}" \
    "$USB_DIR/usb_info.c" \
    "${USB_LIBS[@]}" \
    -o "$USB_DIR/usb_info"

chmod 700 "$USB_DIR/sml_reader" "$USB_DIR/iec_reader" "$USB_DIR/usb_info"

printf '\nBuild erfolgreich:\n'
for binary in "$USB_DIR/sml_reader" "$USB_DIR/iec_reader" "$USB_DIR/usb_info"; do
    size="$(wc -c < "$binary" | tr -d ' ')"
    printf '  %s (%s Bytes)\n' "$(basename "$binary")" "$size"
done
