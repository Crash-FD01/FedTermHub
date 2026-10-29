#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copia il sito (site/) nella cartella di uscita sostituendo indirizzi e versione.
#
#   packaging/render-site.sh <uscita> <URL sito> <URL sorgenti> <versione>
#
# Se nella cartella di uscita c'e' gia' fedtermhub.asc (scritta da update-repo.sh), ne mostra l'impronta.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$1" SITE_URL="${2%/}" REPO_URL="${3%/}" VERSION="$4"
mkdir -p "$OUT"

FINGERPRINT="—"
if [[ -f "$OUT/fedtermhub.asc" ]]; then
    FINGERPRINT="$(gpg --show-keys --with-colons "$OUT/fedtermhub.asc" \
        | awk -F: '/^fpr/{print $10; exit}' | sed 's/.\{4\}/& /g; s/ $//')"
fi

for f in "$ROOT"/site/*; do
    sed -e "s|@SITE_URL@|$SITE_URL|g" -e "s|@REPO_URL@|$REPO_URL|g" \
        -e "s|@VERSION@|$VERSION|g" -e "s|@FINGERPRINT@|$FINGERPRINT|g" \
        "$f" > "$OUT/$(basename "$f")"
done
cp "$ROOT/share/icons/hicolor/128x128/apps/io.github.crash_fd01.fedtermhub.png" "$OUT/icon.png"
cp "$ROOT/docs/screenshots/principale-chiaro.png" "$OUT/screenshot-light.png"
cp "$ROOT/docs/screenshots/principale-scuro.png" "$OUT/screenshot-dark.png"
cp "$ROOT/docs/screenshots/file-remoti-chiaro.png" "$OUT/files-light.png"
cp "$ROOT/docs/screenshots/file-remoti-scuro.png" "$OUT/files-dark.png"
touch "$OUT/.nojekyll"

if grep -l '@[A-Z_]*@' "$OUT"/*.html "$OUT"/*.sh 2> /dev/null; then
    echo "Segnaposto non sostituiti" >&2
    exit 1
fi
echo "✓ Sito pronto in $OUT"
