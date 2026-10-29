#!/bin/sh
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Aggiunge il repository di FedTermHub al sistema e installa FedTermHub. Da li' gli aggiornamenti
# arrivano con «sudo dnf upgrade» insieme a quelli del sistema.
#
#   curl -fsSL @SITE_URL@/setup.sh | sudo sh
#   curl -fsSL @SITE_URL@/setup.sh | sudo sh -s -- --remove     (toglie FedTermHub e il repository)
#
# Leggilo pure prima di eseguirlo: fa solo quello che c'e' scritto qui sotto.
set -eu

SITE="@SITE_URL@"
SOURCE="@REPO_URL@"
MIN_FEDORA=43

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'Errore: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "servono i permessi di amministratore: curl -fsSL $SITE/setup.sh | sudo sh"

fetch() {  # fetch URL FILE
    if command -v curl > /dev/null 2>&1; then curl -fsSL "$1" -o "$2"
    elif command -v wget > /dev/null 2>&1; then wget -q "$1" -O "$2"
    else die "manca curl (o wget) per scaricare $1"; fi
}

if [ "${1:-}" = "--remove" ]; then
    say "Rimuovo FedTermHub e il suo repository"
    if [ -e /run/ostree-booted ] && command -v rpm-ostree > /dev/null 2>&1; then
        rpm-ostree uninstall fedtermhub || true
    elif command -v dnf > /dev/null 2>&1; then
        dnf remove -y fedtermhub || true
    fi
    rm -f /etc/yum.repos.d/fedtermhub.repo
    say "Fatto. Le tue sessioni restano in ~/.config/fedtermhub."
    exit 0
fi

# ------------------------------------------------------------------ il pacchetto funziona qui?
[ "$(uname -m)" = x86_64 ] || die "i pacchetti ci sono per x86_64. Su $(uname -m) installa dai sorgenti: $SOURCE/blob/main/docs/GUIDA.md#dai-sorgenti"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
fi
case "${ID:-}" in
    fedora) ;;
    *) die "il pacchetto e' costruito per Fedora (qui: ${PRETTY_NAME:-sistema sconosciuto}). Installa dai sorgenti: $SOURCE/blob/main/docs/GUIDA.md#dai-sorgenti" ;;
esac
if [ "${VERSION_ID:-0}" -lt "$MIN_FEDORA" ] 2> /dev/null; then
    die "serve Fedora $MIN_FEDORA o piu' recente (qui: Fedora ${VERSION_ID:-?}). Aggiorna il sistema o installa dai sorgenti."
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fetch "$SITE/fedtermhub.asc" "$tmp/fedtermhub.asc"
grep -q "BEGIN PGP PUBLIC KEY BLOCK" "$tmp/fedtermhub.asc" || die "la chiave scaricata da $SITE/fedtermhub.asc non e' valida"

say "Aggiungo il repository di FedTermHub"
fetch "$SITE/fedtermhub.repo" /etc/yum.repos.d/fedtermhub.repo
rpm --import "$tmp/fedtermhub.asc"

if [ -e /run/ostree-booted ] && command -v rpm-ostree > /dev/null 2>&1; then
    say "Installo FedTermHub (sistema immutabile: sara' attivo dopo il riavvio)"
    rpm-ostree install --idempotent fedtermhub
    upgrade="rpm-ostree upgrade (o gli aggiornamenti automatici del sistema)"
else
    say "Installo FedTermHub"
    dnf install -y fedtermhub
    upgrade="sudo dnf upgrade"
fi

say "✓ FedTermHub installato. Lo trovi nel menu delle applicazioni, o avvialo con: fedtermhub"
echo "  Gli aggiornamenti arrivano con quelli del sistema: $upgrade"
echo "  Per toglierlo: curl -fsSL $SITE/setup.sh | sudo sh -s -- --remove"
