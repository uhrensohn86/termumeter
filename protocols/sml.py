from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from decimal import Decimal


SML_START = bytes.fromhex("1b1b1b1b01010101")
SML_END = bytes.fromhex("1b1b1b1b1a")

# DLMS/COSEM unit codes used by SML. Unknown codes remain numeric.
UNIT_NAMES = {
    27: "W",
    30: "Wh",
    33: "A",
    35: "V",
    44: "Hz",
}


class SmlError(ValueError):
    pass


@dataclass(frozen=True)
class SmlFrame:
    raw: bytes
    payload: bytes
    fill: int
    crc_transmitted: int
    crc_calculated: int

    @property
    def crc_valid(self) -> bool:
        return self.crc_transmitted == self.crc_calculated


@dataclass(frozen=True)
class SmlValue:
    code: str
    raw_code: bytes
    value: Any
    scaler: int | None
    unit_code: int | None
    unit: str | None
    scaled_value: int | Decimal | None
    status: Any = None
    value_time: Any = None
    signature: Any = None

    @property
    def base_code(self) -> str:
        # Same lookup idea as the IEC parser: transport prefix and F-field
        # are preserved in code, while common measurement lookup can use C.D.E.
        if len(self.raw_code) == 6:
            return f"{self.raw_code[2]}.{self.raw_code[3]}.{self.raw_code[4]}"
        return self.code


def crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def iter_frames(data: bytes) -> Iterator[SmlFrame]:
    pos = 0

    while True:
        start = data.find(SML_START, pos)
        if start < 0:
            return

        end = data.find(SML_END, start + len(SML_START))
        if end < 0:
            return

        if end + 8 > len(data):
            return

        fill = data[end + 5]
        frame_end = end + 8
        raw = data[start:frame_end]

        transmitted = int.from_bytes(data[end + 6:end + 8], "little")
        calculated = crc16_x25(data[start:end + 6])

        # Payload is between start escape and end escape. The fill bytes are
        # padding after the SML file content and are not TLV objects.
        payload_end = end - fill
        if payload_end < start + len(SML_START):
            raise SmlError("Ungueltige SML-Fuellbyte-Angabe")

        payload = data[start + len(SML_START):payload_end]

        yield SmlFrame(
            raw=raw,
            payload=payload,
            fill=fill,
            crc_transmitted=transmitted,
            crc_calculated=calculated,
        )

        pos = frame_end


def _decode_tl(data: bytes, pos: int) -> tuple[int, int, int, int]:
    if pos >= len(data):
        raise SmlError("Unerwartetes Ende im TL-Feld")

    first = data[pos]
    if first == 0:
        # Optional/absent value. Caller handles this specially.
        return -1, 0, pos + 1, 1

    start = pos
    typ = (first >> 4) & 0x07
    length = first & 0x0F
    continued = bool(first & 0x80)
    pos += 1

    while continued:
        if pos >= len(data):
            raise SmlError("Unvollstaendiges mehrbyteiges TL-Feld")
        b = data[pos]
        pos += 1
        continued = bool(b & 0x80)
        length = (length << 4) | (b & 0x0F)

    return typ, length, pos, pos - start


def _parse_value(data: bytes, pos: int = 0, depth: int = 0) -> tuple[Any, int]:
    if depth > 64:
        raise SmlError("SML-Struktur zu tief verschachtelt")

    typ, length, payload_pos, tl_len = _decode_tl(data, pos)

    if typ == -1:
        return None, payload_pos

    if typ == 7:  # list; length is number of elements
        values = []
        p = payload_pos
        for _ in range(length):
            value, p = _parse_value(data, p, depth + 1)
            values.append(value)
        return values, p

    payload_len = length - tl_len
    if payload_len < 0:
        raise SmlError("Ungueltige TL-Laenge")

    end = payload_pos + payload_len
    if end > len(data):
        raise SmlError("TL-Wert reicht ueber das Datenende hinaus")

    raw = data[payload_pos:end]

    if typ == 0:       # octet string
        value: Any = raw
    elif typ == 4:     # boolean
        value = bool(raw[-1]) if raw else None
    elif typ == 5:     # signed integer
        value = int.from_bytes(raw, "big", signed=True) if raw else 0
    elif typ == 6:     # unsigned integer
        value = int.from_bytes(raw, "big", signed=False) if raw else 0
    else:
        # Preserve unsupported scalar types rather than discarding data.
        value = raw

    return value, end


def parse_payload(payload: bytes) -> list[Any]:
    values = []
    pos = 0
    while pos < len(payload):
        value, new_pos = _parse_value(payload, pos)
        if new_pos <= pos:
            raise SmlError("Parser hat keinen Fortschritt gemacht")
        values.append(value)
        pos = new_pos
    return values


def obis_code(raw: bytes) -> str:
    if len(raw) != 6:
        return raw.hex()
    a, b, c, d, e, f = raw
    return f"{a}-{b}:{c}.{d}.{e}*{f}"


def _scaled(value: Any, scaler: Any) -> int | Decimal | None:
    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
        return None
    if not isinstance(scaler, int):
        return value
    if scaler == 0:
        return value
    return Decimal(value) * (Decimal(10) ** scaler)


def _looks_like_list_entry(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 7
        and isinstance(value[0], bytes)
        and len(value[0]) == 6
    )


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)


def extract_values(parsed: list[Any]) -> list[SmlValue]:
    result: list[SmlValue] = []
    seen: set[tuple[bytes, str]] = set()

    for node in _walk(parsed):
        if not _looks_like_list_entry(node):
            continue

        raw_code, status, value_time, unit_code, scaler, value, signature = node

        # Empty octet strings are used by some meters where an optional field
        # would otherwise be absent.
        def empty_to_none(v: Any) -> Any:
            return None if v == b"" else v

        status = empty_to_none(status)
        value_time = empty_to_none(value_time)
        unit_code = empty_to_none(unit_code)
        scaler = empty_to_none(scaler)
        signature = empty_to_none(signature)

        code = obis_code(raw_code)
        key = (raw_code, repr(value))
        if key in seen:
            continue
        seen.add(key)

        unit = UNIT_NAMES.get(unit_code) if isinstance(unit_code, int) else None

        result.append(
            SmlValue(
                code=code,
                raw_code=raw_code,
                value=value,
                scaler=scaler if isinstance(scaler, int) else None,
                unit_code=unit_code if isinstance(unit_code, int) else None,
                unit=unit,
                scaled_value=_scaled(value, scaler),
                status=status,
                value_time=value_time,
                signature=signature,
            )
        )

    return result


def parse_frame(frame: SmlFrame) -> list[SmlValue]:
    if not frame.crc_valid:
        raise SmlError(
            f"CRC ungueltig: gesendet={frame.crc_transmitted:04x}, "
            f"berechnet={frame.crc_calculated:04x}"
        )
    return extract_values(parse_payload(frame.payload))


def parse_bytes(data: bytes) -> list[tuple[SmlFrame, list[SmlValue]]]:
    return [(frame, parse_frame(frame)) for frame in iter_frames(data)]


def decode_meter_number(server_id: bytes) -> str | None:
    """Decode the common 10-byte DIN 43863-5 server ID.

    Layout seen on German electricity meters:
      0A | 01 | MFG(3 ASCII) | generation(1 byte) | serial(4 bytes)

    Human-readable meter number:
      medium/channel digit + manufacturer + generation as two hex digits
      + 8-digit decimal serial.

    Synthetic example:
      0a 01 41 42 43 00 00 00 00 01 -> 1ABC0000000001
    """
    if len(server_id) != 10:
        return None
    try:
        manufacturer = server_id[2:5].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not (manufacturer.isalnum() and manufacturer.isupper()):
        return None

    prefix = str(server_id[1])
    generation = f"{server_id[5]:02X}"
    serial = int.from_bytes(server_id[6:10], "big")
    return f"{prefix}{manufacturer}{generation}{serial:08d}"


def _meter_id_candidates(values: list[SmlValue]) -> Iterator[SmlValue]:
    """Yield meter-ID candidates in priority order.

    96.1.0 is the established primary identifier used by the meters already
    supported by this parser. Some EasyMeter devices (for example Q3MA3170)
    expose their stable device identifier as 0.0.9 instead.
    """
    for base_code in ("96.1.0", "0.0.9"):
        for item in values:
            if item.base_code == base_code:
                yield item


def raw_meter_id(values: list[SmlValue]) -> str | None:
    for item in _meter_id_candidates(values):
        if isinstance(item.value, bytes):
            return item.value.hex()
        if item.value is not None:
            return str(item.value)
    return None


def meter_id(values: list[SmlValue]) -> str | None:
    for item in _meter_id_candidates(values):
        if isinstance(item.value, bytes):
            return decode_meter_number(item.value) or item.value.hex()
        if item.value is not None:
            return str(item.value)
    return None


def get_value(values: list[SmlValue], base_code: str) -> SmlValue | None:
    matches = [v for v in values if v.base_code == base_code]
    return matches[0] if len(matches) == 1 else None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="SML Regressionstest")
    ap.add_argument("file", type=Path)
    args = ap.parse_args()

    data = args.file.read_bytes()
    parsed = parse_bytes(data)

    print(f"Datei: {args.file}")
    print(f"Vollstaendige SML-Telegramme: {len(parsed)}")

    for i, (frame, values) in enumerate(parsed, 1):
        print(
            f"\nTelegramm {i}: {len(frame.raw)} Bytes, "
            f"CRC={'OK' if frame.crc_valid else 'FEHLER'}, "
            f"Werte={len(values)}, Zaehlernummer={meter_id(values) or '-'}"
        )
        for item in values:
            if item.base_code in {
                "1.8.0", "1.8.1", "1.8.2",
                "2.8.0", "2.8.1", "2.8.2",
                "16.7.0", "32.7.0", "52.7.0", "72.7.0",
            }:
                shown = item.scaled_value
                print(f"  {item.code:<18} {shown!s:<12} {item.unit or ''}")
