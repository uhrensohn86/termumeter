# TermuMeter

**IEC 62056-21 & SML electricity meter reader for Android/Termux**

TermuMeter reads compatible electricity meters directly on an Android device using
Termux, USB OTG and an optical infrared read head. It supports automatic protocol
detection, local storage of readings, history and analysis functions, CSV export,
measurement series and privacy-conscious diagnostic reports.

> **Beta software:** TermuMeter has been tested with a growing selection of real
> meters and optical heads, but electricity meters differ considerably between
> manufacturers and firmware versions. New hardware should therefore be treated
> as unverified until tested.

## Features

- IEC 62056-21 meter reading
- SML meter reading
- automatic SML -> IEC 62056-21 detection
- FTDI-based optical USB read heads via Android USB OTG
- local SQLite storage
- multiple meters with stable meter identities
- reading history and compact analysis
- IEC historical/monthly register display where supported
- measurement series
- CSV export
- diagnostic/support report without raw telegrams or measurement values
- command-line operation in addition to the interactive menu
- locally compiled USB readers from included C source code
- safe Git-based updater for future installations

TermuMeter works locally. No cloud service or account is required for meter
reading or storage.

## Requirements

Hardware:

- Android device with USB OTG support
- compatible optical infrared read head
- compatible electricity meter

Software:

- Termux
- the matching Android **Termux:API app**
- official Termux packages:
  - `python`
  - `clang`
  - `libusb`
  - `pkg-config`
  - `termux-api`
  - `git`

The separate Android Termux:API app is required for `termux-usb`. Installing
only the `termux-api` package inside Termux is not sufficient.

Tested development environment includes Android 16 / SDK 36 on aarch64,
Python 3.14.6, clang 21.1.8, libusb 1.0.30 and Termux:API 0.59.1.
These are tested versions, **not declared minimum versions**.

## Installation

Install the required packages from the official Termux repositories:

```sh
pkg install python clang libusb pkg-config termux-api git
```

Clone the repository and run the installer:

```sh
git clone https://github.com/uhrensohn86/termumeter.git
cd termumeter
./install.sh
```

`install.sh` checks the local prerequisites, validates the Python sources and
builds the native USB tools from the included source code. It does not install
unknown third-party software and does not replace an existing TermuMeter
database or settings file.

If you want to use exports or diagnostic reports in Android's shared Download
directory, grant Termux shared-storage access once:

```sh
termux-setup-storage
```

Start TermuMeter with:

```sh
python meter.py
```

Android may ask for permission to access the connected USB read head.

## Updating

For a Git-based installation:

```sh
cd ~/termumeter
./update.sh
```

The updater is deliberately conservative. It aborts if the working tree contains
local changes or if the local and remote Git histories have diverged. It does
not use `git reset --hard` and does not automatically resolve merges.

After a successful fast-forward update, the installation checks and native build
are run again.

## User data and privacy

Program files and user data are deliberately separated.

Local readings and settings are stored below `data/`, including:

```text
data/meter-reader.db
data/settings.json
```

These files are excluded from Git and are **not part of the public repository or
a normal TermuMeter installation**. A new user starts with an empty local
database.

The database filename retains the historical name `meter-reader.db` for
compatibility.

Diagnostic reports are designed for public support requests. They do not contain:

- raw meter telegrams
- measurement values
- USB serial numbers
- smartphone manufacturer or model

Detected meter IDs are represented only by a shortened SHA-256 pseudonym.
OBIS identifiers, units, Android version, Python version, USB VID:PID and
non-serial USB product information may be included because they are useful for
compatibility diagnosis.

## Protocols

### IEC 62056-21

TermuMeter supports the optical IEC 62056-21 communication used by several
tested meters, including Mode C baud-rate switching. Telegram BCC is checked
before a reading is treated as valid.

A compatibility path for an observed EFR STX-less telegram variant is also
included.

### SML

TermuMeter can passively receive SML telegrams at 9600 baud / 8N1 and validates
the SML frame CRC. The SML parser preserves protocol metadata and applies SML
scalers using decimal arithmetic.

Meter identification supports the standard `96.1.0` path and the observed
`0.0.9` fallback used by some tested meters.

## Tested electricity meters

The following models/families have been tested with real hardware during
development.

| Manufacturer | Meter / family | IEC 62056-21 | SML | Status / notes |
|---|---|:---:|:---:|---|
| ZPA | ZE311 | Yes | — | Tested |
| ABB | A1500 | Yes | — | Tested |
| ZPA | GS303 | Yes | Yes | Both protocols tested |
| EFR | SGM-DD | Yes | Yes | Both protocols tested; observed IEC variant supported |
| EMH | eBZ D-W2E8 | — | Yes | Tested |
| Sagemcom | SMARTY BZ-PLUS family* | Yes | Yes | Reduced and extended data observed |
| Iskraemeco | MT173 | Yes | — | Tested |
| EasyMeter | Q3MA3170 | — | Yes | Tested |
| Iskraemeco | MT175 | — | Yes | Tested |
| EMH | DMTZ-XC | Yes | — | Tested |
| Iskraemeco | MT371 | Yes | — | Tested; factory-number identity handling |
| Iskraemeco | MT174 | Yes | — | Tested |
| DZG | DWSB20.2H | — | Yes | Tested |

\* The exact Sagemcom model designation of the tested unit has not been
independently confirmed; the family assignment is therefore intentionally
marked as approximate.

A model appearing in this table does not imply that every firmware version,
utility configuration or optical interface variant will behave identically.

## Optical read heads

TermuMeter currently targets FTDI-style USB optical heads accessible through
Termux:API and libusb.

Tested successfully:

- FTDI-based `USB Infrarot-Adapter` (VID:PID `0403:6001`)

Recognized during testing but currently not supported for successful meter
communication:

- Diehl Metering GmbH `USB-IrDA-Optokopf` (VID:PID `0403:6001`)

USB serial numbers are intentionally not published.

At present TermuMeter expects exactly one relevant USB device to be connected.
Selection between multiple USB devices is not yet implemented.

## Meter identity

TermuMeter separates the stable internal meter identity from the number shown to
the user.

Where available, a protocol-provided technical identifier is preferred. Some
meters require model-specific handling; for example, the tested MT371 uses its
factory number because an observed `0.0.0` value is not unique.

If no unique technical ID can be determined, TermuMeter provides a manual
typeplate fallback. Raw telegram contents are not rewritten to manufacture an
identity.

The manual fallback is implemented but has not yet been validated against real
hardware that genuinely lacks a usable automatic identifier.

## Historical values

IEC meters can expose historical or billing-period registers using OBIS
selectors such as `*01`, `*02`, etc. TermuMeter preserves the complete original
OBIS identifiers when storing readings.

Observed `&NN` historical entries are intentionally kept distinct from `*NN`
entries. They are preserved in storage but are not currently merged into the
normal monthly-value display because their reset semantics differ.

## Command line

In addition to the interactive menu, TermuMeter supports command-line actions
including:

```text
--read
--read --all
--last
--last --all
--history
--history --meter ID
--meters
--select-ha
--file FILE
```

`--file` is intended for parsing IEC test data and does not store the parsed
telegram in SQLite.

Run:

```sh
python meter.py --help
```

for the options provided by the installed version.

## Known limitations

This first beta intentionally favors predictable behavior over broad automatic
guessing.

- only one connected USB device is currently supported
- compatibility is verified only for the hardware listed above
- the exact tested Sagemcom model designation is not confirmed
- the manual meter-ID fallback still needs real unsupported-ID hardware testing
- `&NN` historical/reset entries are stored but not shown as normal monthly values
- `--file` currently targets IEC parsing
- Home Assistant integration is planned but not yet implemented
- USB read heads using transports other than the currently supported FTDI path
  may require additional implementation

## Home Assistant

The database already supports selecting a meter for future Home Assistant use,
but Home Assistant synchronization is **not part of this beta release yet**.

## Support and diagnostics

From TermuMeter's settings menu, create a diagnostic/support report and attach
that report to a bug report when possible.

Before sharing any diagnostic file, you should still review its contents
yourself. The diagnostic generator is intentionally designed to omit raw
telegrams, readings, USB serial numbers and phone model information.

For a new meter or read head, useful non-private information includes:

- exact manufacturer and model
- protocol if known
- optical read-head manufacturer/model
- TermuMeter version
- diagnostic result/error code

## License

TermuMeter is released under the MIT License.

Copyright (c) 2026 uhrensohn86

See [LICENSE](LICENSE) for the full license text.
