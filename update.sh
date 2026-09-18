#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SETTINGS_FILE="$SCRIPT_DIR/data/settings.json"
VERSION_FILE="$SCRIPT_DIR/VERSION"

# English is the default. Read the same language setting used by TermuMeter.
LANGUAGE="en"
if [[ -f "$SETTINGS_FILE" ]] && command -v python >/dev/null 2>&1; then
    LANGUAGE="$(
        python - "$SETTINGS_FILE" <<'PY' 2>/dev/null || printf 'en'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        value = json.load(f).get("language", "en")
    print(value if value in ("en", "de") else "en")
except (OSError, json.JSONDecodeError, AttributeError):
    print("en")
PY
    )"
fi
[[ "$LANGUAGE" == "de" ]] || LANGUAGE="en"

if [[ "$LANGUAGE" == "de" ]]; then
    die() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }
    printf 'TermuMeter - Update\n===================\n\n'
    [[ -f "$VERSION_FILE" ]] && printf 'Installierte Version: %s\n\n' "$(tr -d '\r\n' < "$VERSION_FILE")"

    command -v git >/dev/null 2>&1 || die "git fehlt. Installiere es mit: pkg install git"
    [[ -d .git ]] || die "Dieses Verzeichnis ist kein Git-Repository."
    [[ -f install.sh ]] || die "install.sh fehlt."
    [[ -f build.sh ]] || die "build.sh fehlt."

    if [[ -n "$(git status --porcelain)" ]]; then
        printf 'Update abgebrochen: Im Projekt gibt es lokale Aenderungen.\n\n'
        git status --short
        printf '\nBitte sichere oder committe diese Aenderungen zuerst. Es wurde nichts veraendert.\n'
        exit 1
    fi

    branch="$(git branch --show-current)"
    [[ -n "$branch" ]] || die "Kein aktiver Git-Branch erkannt (detached HEAD)."

    printf 'Branch: %s\nPruefe auf neue Version ...\n' "$branch"
    git fetch --prune

    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    [[ -n "$upstream" ]] || die "Fuer Branch '$branch' ist kein Upstream konfiguriert."

    local_rev="$(git rev-parse HEAD)"
    remote_rev="$(git rev-parse "$upstream")"
    base_rev="$(git merge-base HEAD "$upstream")"

    if [[ "$local_rev" == "$remote_rev" ]]; then
        printf '\nTermuMeter ist bereits aktuell.\n'
        exit 0
    fi

    if [[ "$remote_rev" == "$base_rev" ]]; then
        printf '\nUpdate abgebrochen: Der lokale Branch enthaelt noch nicht veroeffentlichte Commits.\n'
        printf 'Es wird absichtlich nichts zurueckgesetzt. Bitte Git-Stand manuell pruefen.\n'
        exit 1
    fi

    if [[ "$local_rev" != "$base_rev" ]]; then
        printf '\nUpdate abgebrochen: Lokaler und entfernter Stand sind auseinander gelaufen.\n'
        printf 'Es wird absichtlich weder gemergt noch zurueckgesetzt. Bitte Git-Stand manuell pruefen.\n'
        exit 1
    fi

    printf 'Neue Version gefunden. Aktualisiere Projektdateien ...\n'
    git merge --ff-only "$upstream"

    printf '\nPruefe und baue aktualisierte Version ...\n'
    bash "$SCRIPT_DIR/install.sh"

    printf '\nTermuMeter wurde erfolgreich aktualisiert.\n'
    [[ -f "$VERSION_FILE" ]] && printf 'Version: %s\n' "$(tr -d '\r\n' < "$VERSION_FILE")"
    printf 'Aktueller Stand: %s\n' "$(git log -1 --format='%h %cs %s')"
else
    die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
    printf 'TermuMeter - Update\n===================\n\n'
    [[ -f "$VERSION_FILE" ]] && printf 'Installed version: %s\n\n' "$(tr -d '\r\n' < "$VERSION_FILE")"

    command -v git >/dev/null 2>&1 || die "git is missing. Install it with: pkg install git"
    [[ -d .git ]] || die "This directory is not a Git repository."
    [[ -f install.sh ]] || die "install.sh is missing."
    [[ -f build.sh ]] || die "build.sh is missing."

    if [[ -n "$(git status --porcelain)" ]]; then
        printf 'Update aborted: There are local changes in the project.\n\n'
        git status --short
        printf '\nPlease save or commit these changes first. Nothing was changed.\n'
        exit 1
    fi

    branch="$(git branch --show-current)"
    [[ -n "$branch" ]] || die "No active Git branch detected (detached HEAD)."

    printf 'Branch: %s\nChecking for updates ...\n' "$branch"
    git fetch --prune

    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    [[ -n "$upstream" ]] || die "No upstream is configured for branch '$branch'."

    local_rev="$(git rev-parse HEAD)"
    remote_rev="$(git rev-parse "$upstream")"
    base_rev="$(git merge-base HEAD "$upstream")"

    if [[ "$local_rev" == "$remote_rev" ]]; then
        printf '\nTermuMeter is already up to date.\n'
        exit 0
    fi

    if [[ "$remote_rev" == "$base_rev" ]]; then
        printf '\nUpdate aborted: The local branch contains unpublished commits.\n'
        printf 'Nothing will be reset intentionally. Please check the Git state manually.\n'
        exit 1
    fi

    if [[ "$local_rev" != "$base_rev" ]]; then
        printf '\nUpdate aborted: The local and remote branches have diverged.\n'
        printf 'Nothing will be merged or reset automatically. Please check the Git state manually.\n'
        exit 1
    fi

    printf 'New version found. Updating project files ...\n'
    git merge --ff-only "$upstream"

    printf '\nChecking and building the updated version ...\n'
    bash "$SCRIPT_DIR/install.sh"

    printf '\nTermuMeter was updated successfully.\n'
    [[ -f "$VERSION_FILE" ]] && printf 'Version: %s\n' "$(tr -d '\r\n' < "$VERSION_FILE")"
    printf 'Current revision: %s\n' "$(git log -1 --format='%h %cs %s')"
fi
