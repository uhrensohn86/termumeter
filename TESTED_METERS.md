# TermuMeter hardware test matrix

This document tracks electricity meters and optical read heads that have been tested with real hardware during TermuMeter development.

The matrix is intentionally conservative: a successful test applies to the tested hardware, firmware, utility configuration and optical interface. It does not guarantee identical behavior for every unit of the same model or family.

## Summary

- 18 tested meter models/families
- 11 with successful IEC 62056-21 communication
- 10 with successful SML communication
- 3 tested successfully with both protocols
- FTDI-based USB infrared read head tested successfully

## Electricity meters

| Manufacturer | Meter / family | IEC 62056-21 | SML | Validation / notes |
|---|---|:---:|:---:|---|
| ZPA | ZE311 | Yes | — | Tested with real hardware |
| ABB | A1500 | Yes | — | Tested with real hardware |
| ZPA | GS303 | Yes | Yes | Both protocols tested; recent IEC fallback/BCC/save regression passed |
| EFR | SGM-DD | Yes | Yes | Both protocols tested; three-phase meter; observed IEC STX-less variant supported |
| EMH | eBZ D-W2E8 | — | Yes | Tested with real hardware |
| Sagemcom | SMARTY BZ-PLUS family* | Yes | Yes | Reduced and extended data observed |
| Iskraemeco | MT173 | Yes | — | Tested with real hardware |
| EasyMeter | Q3MA3170 | — | Yes | Tested with real hardware |
| Iskraemeco | MT175 | — | Yes | Tested with real hardware |
| EMH | DMTZ-XC | Yes | — | Tested with real hardware |
| Iskraemeco | MT371 | Yes | — | Factory-number identity handling validated for the tested unit |
| Iskraemeco | MT174 | Yes | — | Tested with real hardware |
| DZG | DWSB20.2H | — | Yes | Tested with real hardware |
| EMH | mMe4.0 | — | Yes | Tested with real hardware |
| ZPA | ZE111 | Yes | — | Tested with real hardware |
| Iskraemeco | ME172 | Yes | — | Tested with real hardware |
| DZG | WS7412.2.G2 | — | Yes | Tested with real hardware |
| EFR | SGM-C6 | — | Yes | Single-phase meter; recent SML regression passed |

\* The exact Sagemcom model designation of the tested unit has not been independently confirmed; the family assignment is intentionally marked as approximate.

## Current regression coverage

The compatibility list above records successful real-hardware tests over the course of development. It should not be read as meaning that every meter was re-tested for every TermuMeter release.

The most recently documented protocol regressions are:

| Meter | Regression coverage | Result |
|---|---|---|
| EFR SGM-C6 | Passive SML reception, multi-frame parsing and normal save path | Passed |
| ZPA GS303 | Automatic SML-to-IEC fallback, IEC Mode C path, BCC validation and save path | Passed |

For the other listed meters, compatibility was confirmed in earlier real-hardware testing, but a current-release regression date/version is not separately recorded yet.

## Feature-specific observations

These are only observations that have been explicitly validated or documented. Blank areas should not be interpreted as unsupported; they simply have not been separately tracked in the matrix yet.

| Meter / family | Identity handling | Historical/monthly values | Multi-tariff | Special protocol behavior |
|---|---|---|---|---|
| ZPA GS303 | Tested | Not separately tracked | Not separately tracked | IEC and SML both tested; IEC fallback regression passed |
| EFR SGM-DD | Tested | Not separately tracked | Not separately tracked | Observed IEC STX-less variant supported |
| Sagemcom SMARTY BZ-PLUS family* | Tested | Not separately tracked | Not separately tracked | Reduced and extended data observed |
| Iskraemeco MT371 | Factory number used for stable identity on tested unit | Not separately tracked | Not separately tracked | Observed `0.0.0` value is not unique on tested unit |
| EFR SGM-C6 | Tested | Not separately tracked | Not separately tracked | SML; single-phase meter; recent SML regression passed |

As feature-specific validation grows, this table should be expanded instead of inferring support from the general protocol result.

## Optical read heads

### Tested successfully

| Device | VID:PID | Status |
|---|---|---|
| FTDI-based `USB Infrarot-Adapter` | `0403:6001` | Successful meter communication |

### Recognized but not yet supported successfully

| Device | VID:PID | Status |
|---|---|---|
| Diehl Metering GmbH `USB-IrDA-Optokopf` | `0403:6001` | Recognized during testing; successful meter communication not yet supported |

USB serial numbers are intentionally not published.

TermuMeter currently expects exactly one relevant USB device to be connected. Selection between multiple USB devices is not yet implemented.

## How to add a new test result

For a new meter or a regression re-test, record only non-private information:

- manufacturer and exact model/family
- protocol used: IEC 62056-21 and/or SML
- TermuMeter version or commit
- whether automatic protocol detection/fallback was involved
- meter identity behavior if relevant
- historical/monthly and tariff registers if explicitly tested
- optical read-head model/type
- pass/fail result and a short compatibility note

Do not publish raw meter telegrams, meter serial numbers, measurement values, USB serial numbers or other user-specific data merely to document compatibility.
