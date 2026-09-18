#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

die() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }

printf 'TermuMeter - Update\n===================\n\n'
VERSION_FILE="$SCRIPT_DIR/VERSION"
if [[ -f "$VERSION_FILE" ]]; then
    printf 'Installierte Version: %s\n\n' "$(tr -d '\r\n' < "$VERSION_FILE")"
fi

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
if [[ -f "$VERSION_FILE" ]]; then
    printf 'Version: %s\n' "$(tr -d '\r\n' < "$VERSION_FILE")"
fi
printf 'Aktueller Stand: %s\n' "$(git log -1 --format='%h %cs %s')"
