#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Firma gli RPM di una versione e prepara il repository dnf da pubblicare su GitHub Pages.
#
#   packaging/update-repo.sh <cartella pacchetti> <cartella uscita> <URL pacchetti> <URL sito>
#
# Variabile obbligatoria: GPG_KEY_ID (chiave gia' importata in gpg, senza passphrase).
#
# Uscita in <uscita>/site/:
#   rpm/repodata/        metadati del repository, firmati (repomd.xml.asc). I pacchetti NON sono qui:
#                        restano tra gli allegati della release e i metadati li indicano con xml:base.
#   fedtermhub.repo      file per /etc/yum.repos.d
#   fedtermhub.asc       chiave pubblica con cui dnf verifica pacchetti e metadati
#
# Nel repository c'e' solo l'ultima versione: dnf aggiorna sempre a quella.
set -euo pipefail

PKGS="$(cd "$1" && pwd)"
OUT="$2"
RPM_BASEURL="${3%/}/"
SITE_URL="${4%/}"
: "${GPG_KEY_ID:?serve GPG_KEY_ID}"

rm -rf "$OUT/site/rpm"
mkdir -p "$OUT/site/rpm"

shopt -s nullglob
# Firmati tutti, compresi debuginfo e sorgente: vanno anche tra gli allegati della release.
all=("$PKGS"/*.rpm)
(( ${#all[@]} )) || { echo "Nessun RPM in $PKGS" >&2; exit 1; }
rpmsign --addsign --define "_gpg_name $GPG_KEY_ID" "${all[@]}" > /dev/null

# Nel repository solo i pacchetti installabili: niente sorgente, debuginfo e debugsource.
for rpm in "${all[@]}"; do
    case "$(basename "$rpm")" in
        *.src.rpm | *-debuginfo-* | *-debugsource-*) ;;
        *) cp "$rpm" "$OUT/site/rpm/" ;;
    esac
done
createrepo_c --quiet --general-compress-type gz --baseurl "$RPM_BASEURL" "$OUT/site/rpm"
rm -f "$OUT"/site/rpm/*.rpm
gpg --batch --yes --local-user "$GPG_KEY_ID" --detach-sign --armor "$OUT/site/rpm/repodata/repomd.xml"

cat > "$OUT/site/fedtermhub.repo" <<EOF
[fedtermhub]
name=FedTermHub
baseurl=$SITE_URL/rpm/
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=$SITE_URL/fedtermhub.asc
metadata_expire=6h
EOF
gpg --batch --yes --armor --export "$GPG_KEY_ID" > "$OUT/site/fedtermhub.asc"
echo "✓ Repository pronto in $OUT/site"
