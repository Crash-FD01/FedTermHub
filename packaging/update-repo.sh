#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Firma i pacchetti di una versione e prepara i repository da pubblicare su GitHub Pages.
#
#   packaging/update-repo.sh <cartella pacchetti> <cartella uscita> <URL pacchetti> <URL sito>
#
# Variabile obbligatoria: GPG_KEY_ID (chiave già importata in gpg, senza passphrase).
#
# Uscita in <uscita>/site/:
#   rpm/repodata/            repository dnf per Fedora (RPM costruiti da fedtermhub.spec, nome *.fcNN.*).
#                            I pacchetti NON sono qui: restano tra gli allegati della release e i metadati
#                            li indicano con xml:base.
#   rpm-universal/           repository per RHEL/Alma/Rocky e openSUSE (RPM di build-packages.sh), con i
#                            pacchetti accanto ai metadati: zypper (libzypp con il download in parallelo
#                            di Tumbleweed e Leap 16) ignora xml:base e li cercava qui, ricevendo 404.
#   apt/                     repository APT «piatto» per Debian, Ubuntu, Mint: indici firmati e i .deb
#                            (apt vuole i pacchetti sotto lo stesso indirizzo degli indici).
#   fedtermhub.repo          file per /etc/yum.repos.d su Fedora
#   fedtermhub-universal.repo  file per /etc/yum.repos.d (RHEL) e zypper (openSUSE)
#   fedtermhub.asc           chiave pubblica con cui dnf, zypper e apt verificano pacchetti e metadati
#
# Nei repository c'è solo l'ultima versione: dnf, zypper e apt aggiornano sempre a quella.
set -euo pipefail

PKGS="$(cd "$1" && pwd)"
OUT="$2"
RPM_BASEURL="${3%/}/"
SITE_URL="${4%/}"
: "${GPG_KEY_ID:?serve GPG_KEY_ID}"
# ALLOW_MISSING=1: un repository senza pacchetti si salta invece di fermarsi (prove della CI, che
# costruiscono solo i pacchetti universali).
ALLOW_MISSING="${ALLOW_MISSING:-0}"

rm -rf "$OUT/site/rpm" "$OUT/site/rpm-universal" "$OUT/site/apt"
mkdir -p "$OUT/site/rpm" "$OUT/site/rpm-universal" "$OUT/site/apt"

shopt -s nullglob
# Firmati tutti, compresi debuginfo e sorgente: vanno anche tra gli allegati della release.
all=("$PKGS"/*.rpm)
(( ${#all[@]} )) || { echo "Nessun RPM in $PKGS" >&2; exit 1; }
rpmsign --addsign --define "_gpg_name $GPG_KEY_ID" "${all[@]}" > /dev/null

# Nei repository solo i pacchetti installabili: niente sorgente, debuginfo e debugsource.
for rpm in "${all[@]}"; do
    case "$(basename "$rpm")" in
        *.src.rpm | *-debuginfo-* | *-debugsource-*) ;;
        *.fc[0-9]*.rpm) cp "$rpm" "$OUT/site/rpm/" ;;
        *) cp "$rpm" "$OUT/site/rpm-universal/" ;;
    esac
done
for repo in rpm rpm-universal; do
    dir="$OUT/site/$repo"
    if ! compgen -G "$dir/*.rpm" > /dev/null; then
        echo "Nessun RPM per $repo" >&2
        [[ "$ALLOW_MISSING" == 1 ]] && continue
        exit 1
    fi
    if [[ "$repo" == rpm ]]; then
        createrepo_c --quiet --general-compress-type gz --baseurl "$RPM_BASEURL" "$dir"
        rm -f "$dir"/*.rpm
    else
        createrepo_c --quiet --general-compress-type gz "$dir"
    fi
    gpg --batch --yes --local-user "$GPG_KEY_ID" --detach-sign --armor "$dir/repodata/repomd.xml"
done

repo_file() {  # repo_file <id> <nome> <cartella>
    cat <<EOF
[$1]
name=$2
baseurl=$SITE_URL/$3/
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=$SITE_URL/fedtermhub.asc
metadata_expire=6h
EOF
}
repo_file fedtermhub FedTermHub rpm > "$OUT/site/fedtermhub.repo"
# Stesso id «fedtermhub» anche qui: per dnf e zypper è il repository di FedTermHub, qualunque sia il file.
repo_file fedtermhub FedTermHub rpm-universal > "$OUT/site/fedtermhub-universal.repo"
gpg --batch --yes --armor --export "$GPG_KEY_ID" > "$OUT/site/fedtermhub.asc"

# ------------------------------------------------------------------ APT: repository piatto firmato
debs=("$PKGS"/*.deb)
(( ${#debs[@]} )) || { echo "Nessun .deb in $PKGS" >&2; exit 1; }
cp "${debs[@]}" "$OUT/site/apt/"
(
    cd "$OUT/site/apt"
    apt-ftparchive packages . > Packages
    gzip -9 -k -f Packages
    archs="$(for d in *.deb; do dpkg-deb -f "$d" Architecture; done | grep -vx all | sort -u | paste -sd ' ' -)"
    apt-ftparchive \
        -o APT::FTPArchive::Release::Origin=FedTermHub \
        -o APT::FTPArchive::Release::Label=FedTermHub \
        -o APT::FTPArchive::Release::Suite=stable \
        -o APT::FTPArchive::Release::Codename=stable \
        -o "APT::FTPArchive::Release::Architectures=$archs all" \
        -o "APT::FTPArchive::Release::Description=FedTermHub: terminali, SSH, RDP e file in un'unica finestra" \
        release . > ../Release.apt.tmp
    mv ../Release.apt.tmp Release   # scritto fuori, se no elencherebbe sé stesso
    gpg --batch --yes --local-user "$GPG_KEY_ID" --digest-algo SHA512 --clearsign -o InRelease Release
    gpg --batch --yes --local-user "$GPG_KEY_ID" --digest-algo SHA512 -abs -o Release.gpg Release
)
echo "✓ Repository pronti in $OUT/site (rpm, rpm-universal, apt)"
