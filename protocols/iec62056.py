from dataclasses import dataclass
from typing import Optional
import re


STX = 0x02
ETX = 0x03


@dataclass
class ObisValue:
    # Vollstaendige Kennung aus dem Telegramm, z. B.
    # "1-1:1.8.0", "1-0:1.8.0" oder "1.8.0".
    code: str

    # Normalisierte Kennung fuer die inhaltliche Suche.
    # Praefixe wie "1-1:" oder "1-0:" werden nur hier entfernt.
    # Historische Suffixe (*xx / &xx) bleiben erhalten.
    base_code: str

    # Inhalt der ersten Klammergruppe. Dieses Feld bleibt aus
    # Kompatibilitaetsgruenden zur bisherigen Anwendung erhalten.
    raw: str

    # Alle Klammergruppen der Zeile verlustfrei.
    # Beispiel: 1-1:1.6.1(3.051*kW)(2609040845)
    # -> ["3.051*kW", "2609040845"]
    groups: list[str]

    value: Optional[float]
    unit: Optional[str]


@dataclass
class IECMessage:
    raw: bytes
    bcc_received: int
    bcc_calculated: int
    bcc_valid: bool
    values: list[ObisValue]


def calculate_bcc(data: bytes) -> int:
    """
    IEC 62056-21 BCC:
    XOR ueber alle Bytes nach STX bis einschliesslich ETX.
    """
    bcc = 0
    for byte in data:
        bcc ^= byte
    return bcc


def normalize_obis_code(code: str) -> str:
    """
    Liefert eine herstellerunabhaengige Suchkennung.

    Beispiele:
        1.8.0          -> 1.8.0
        1-1:1.8.0      -> 1.8.0
        1-0:1.8.0      -> 1.8.0
        1-1:1.8.0*25   -> 1.8.0*25
        1-1:1.8.0&33   -> 1.8.0&33

    Der Originalcode wird NICHT veraendert und bleibt in ObisValue.code.
    Jede Zeile wird unabhaengig normalisiert.
    """
    code = code.strip()

    # IEC/OBIS-Praefix nur dann abtrennen, wenn vor dem Doppelpunkt
    # tatsaechlich die uebliche A-B-Form aus Ziffern steht.
    # Keine Hersteller- oder Zaehler-spezifische Liste.
    match = re.match(r"^\d+-\d+:(.+)$", code)
    if match:
        return match.group(1).strip()

    return code


def lookup_obis_code(code: str) -> str:
    """
    Semantische Suchkennung fuer aktuelle Standardwerte.

    Das A-B-Praefix wird wie bisher entfernt. Die Selektoren *255 bzw. *0
    duerfen fuer die Standardsuche entfallen. Andere Selektoren wie &01,
    *25 oder &33 bleiben erhalten und werden dadurch nicht mit dem
    aktuellen Wert verwechselt.

    Beispiele:
        0-1:0.0.0*255 -> 0.0.0
        1-0:1.8.0*255 -> 1.8.0
        1-0:1.8.0&01  -> 1.8.0&01
        1-1:1.8.0*25  -> 1.8.0*25
    """
    base = normalize_obis_code(code)

    # Standardselektoren der bisher unterstuetzten Zaehler.
    match = re.match(r"^(.+)\*(?:255|0)$", base)
    if match:
        return match.group(1)

    # EFR verwendet fuer einige aktuelle Metadaten einen zusaetzlichen
    # Punkt-Selektor statt *255. Nur die von uns live beobachteten
    # Kennungen werden semantisch reduziert; der Originalcode bleibt
    # unveraendert gespeichert.
    match = re.match(r"^(0\.0\.0|0\.9\.[12])\.255$", base)
    if match:
        return match.group(1)

    return base


def _parse_first_group(content: str):
    """
    Numerische Interpretation nur bei expliziter Einheit.
    Seriennummern, Zeitstempel usw. bleiben dadurch Strings.
    """
    unit = None
    numeric_value = None

    if "*" in content:
        possible_value, possible_unit = content.rsplit("*", 1)
        if possible_unit:
            unit = possible_unit
            try:
                numeric_value = float(possible_value)
            except ValueError:
                numeric_value = None

    return numeric_value, unit


def parse_obis_line(line: str) -> Optional[ObisValue]:
    """
    Toleranter IEC-62056-21-Zeilenparser.

    Unterstuetzt u. a.:
        1.8.0(0000042.800*kWh)
        1-1:1.8.0(09670.091*kWh)
        1-0:1.8.0(09670.091*kWh)
        0.1.2*01(000101000000)
        1-1:0.1.2&33(2609031600)
        1-1:1.6.1(3.051*kW)(2609040845)

    Praefixe werden nicht verworfen: code enthaelt immer die
    Originalkennung. base_code dient nur zur normalisierten Suche.
    """
    line = line.strip()

    if not line or "(" not in line:
        return None

    first_paren = line.find("(")
    code = line[:first_paren].strip()

    if not code:
        return None

    # Alle vollstaendigen Klammergruppen erhalten.
    # Falls nach der letzten Gruppe ungueltiger Rest steht, wird die
    # Zeile nicht als normaler OBIS-Wert akzeptiert.
    rest = line[first_paren:]
    groups = re.findall(r"\(([^()]*)\)", rest)

    if not groups:
        return None

    reconstructed = "".join(f"({group})" for group in groups)
    if reconstructed != rest:
        return None

    raw = groups[0]
    numeric_value, unit = _parse_first_group(raw)

    return ObisValue(
        code=code,
        base_code=normalize_obis_code(code),
        raw=raw,
        groups=groups,
        value=numeric_value,
        unit=unit,
    )


def parse_message(raw: bytes) -> IECMessage:
    """
    Unterstuetzt zwei live verifizierte IEC-Rahmungen:

      1) Standard:
         STX ... ETX BCC

         BCC = XOR ueber alle Bytes nach STX bis einschliesslich ETX.

      2) EFR-Textblock:
         [CR/LF] ASCII-OBIS ... ! CR LF ETX BCC

         Der EFR sendet keinen fuehrenden STX. Fuer die BCC-Pruefung
         werden fuehrende CR/LF ignoriert. Der auf mehreren Live-
         Telegrammen beobachtete EFR-Startwert ist 0x0C:

         BCC = 0x0C XOR ASCII-Nutzdaten ... XOR ETX

    raw bleibt in beiden Faellen unveraendert.
    """
    if len(raw) < 3:
        raise ValueError("IEC-Datenblock ist zu kurz")

    if raw[0] == STX:
        try:
            etx_index = raw.index(bytes([ETX]), 1)
        except ValueError:
            raise ValueError("ETX fehlt") from None

        if etx_index + 1 >= len(raw):
            raise ValueError("BCC hinter ETX fehlt")

        payload_start = 1
        bcc_received = raw[etx_index + 1]
        bcc_calculated = calculate_bcc(raw[payload_start:etx_index + 1])
        payload = raw[payload_start:etx_index]
    else:
        # EFR: fuehrende CR/LF gehoeren zur Uebertragung, aber nicht zum
        # semantischen Textblock/BCC-Bereich.
        payload_start = 0
        while payload_start < len(raw) and raw[payload_start] in (0x0D, 0x0A):
            payload_start += 1

        if payload_start >= len(raw):
            raise ValueError("IEC-Textblock enthaelt keine Nutzdaten")

        first = raw[payload_start]
        if first < 0x20 or first > 0x7E:
            raise ValueError(
                f"STX fehlt und kein gueltiger IEC-Textblock erkannt: "
                f"erstes Nutzbyte ist 0x{first:02X}"
            )

        try:
            etx_index = raw.index(bytes([ETX]), payload_start)
        except ValueError:
            raise ValueError("ETX fehlt") from None

        if etx_index + 1 >= len(raw):
            raise ValueError("BCC hinter ETX fehlt")

        # Schutz gegen ein zu grosszuegiges Akzeptieren beliebiger
        # STX-loser Daten: der beobachtete EFR-Readout endet mit
        # "!\\r\\n" direkt vor ETX.
        if raw[max(payload_start, etx_index - 3):etx_index] != b"!\r\n":
            raise ValueError(
                "STX-loser IEC-Textblock hat keinen erwarteten "
                "EFR-Abschluss !<CR><LF>"
            )

        bcc_received = raw[etx_index + 1]
        bcc_calculated = 0x0C ^ calculate_bcc(
            raw[payload_start:etx_index + 1]
        )
        payload = raw[payload_start:etx_index]

    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Nutzdaten sind nicht gueltig ASCII: {exc}"
        ) from exc

    values = []
    for line in text.splitlines():
        parsed = parse_obis_line(line)
        if parsed is not None:
            values.append(parsed)

    return IECMessage(
        raw=raw[:etx_index + 2],
        bcc_received=bcc_received,
        bcc_calculated=bcc_calculated,
        bcc_valid=(bcc_received == bcc_calculated),
        values=values,
    )
